"""The live recorder — one Cloud Run Job execution per live event.

    python -m media_server.live_capture      # JOB_ID, HLS_URL, EVENT_START,
                                              # EVENT_END, CHUNK_SEC in the env

Follows a live HLS playlist from the moment it is started until the event's
end time (or the stream's ``EXT-X-ENDLIST``), downloading every media segment
as it appears and joining them into fixed-length **chunks** — five minutes by
default — in the media bucket. Each closed chunk is recorded in Firestore under
``jobs/{job}/chunks/{index}`` with what the recorder knows about it: which
segments it holds, when they were broadcast, and whether they followed on from
the previous chunk without a gap. The agent's live tick picks those records up
and analyses each chunk as one segment.

Why this is a job and not a tick. A live playlist is a sliding window of a
handful of segments — typically 20 to 30 seconds — so anything that looks at it
once a minute has already missed most of what went by. The recorder is the one
process that must not stop, so it is the one process that runs to completion;
everything that can be done later, is.

Why chunks are joined here. A chunk is what Gemini reads, and Gemini reads one
object. GCS composes objects server-side, so joining fifty segments costs two
API calls and no bytes through this container. For MPEG-TS a concatenation is
a valid stream; for CMAF the init segment goes first and the result is a valid
fragmented MP4.

The continuity record is deliberately two-sided. The recorder writes what it
saw — the first and last media-sequence numbers, the wall-clock span, and any
``EXT-X-DISCONTINUITY`` inside — and the agent checks the next chunk against
the previous one *as stored*, so a gap the recorder missed (a restart between
two executions, say) is still caught by the numbers.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from media_server import hls

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("live-capture")

MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
# The playlist is re-read at half its target duration, which is how often a
# new segment can possibly appear; sooner is load on the origin for nothing.
_MIN_POLL_SEC = 1.0
_MAX_POLL_SEC = 6.0
# How long to keep asking for a playlist that does not answer before giving
# up on the event. Streams commonly come up late; ten minutes of 404 covers a
# late producer without spending the whole event on a dead URL.
_PLAYLIST_GRACE_SEC = 600
# A downloaded segment is retried this many times before it is written off
# as missed and recorded as a gap. A live segment that will not download in
# three tries is one the playlist will have slid past by the fourth.
_SEGMENT_ATTEMPTS = 3
# GCS composes at most this many sources per call.
_COMPOSE_LIMIT = 32


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- Chunk assembly (pure) ------------------------------------------------------


@dataclass
class Chunk:
    index: int
    parts: list[str] = field(default_factory=list)
    duration: float = 0.0
    first_seq: int | None = None
    last_seq: int | None = None
    first_pdt: datetime | None = None
    last_pdt_end: datetime | None = None
    discontinuities: int = 0
    # Gap between this chunk's first segment and the previous chunk's last,
    # noted by the assembler when the numbers do not follow on.
    gap_before: dict[str, Any] | None = None
    # Gaps inside the chunk: segments the playlist slid past mid-chunk.
    gaps_inside: list[dict[str, Any]] = field(default_factory=list)

    @property
    def segments(self) -> int:
        return len(self.parts)


class ChunkAssembler:
    """Groups arriving segments into chunks of about ``chunk_sec`` seconds.

    A chunk closes on the first segment that would take it past its length,
    so chunks are ``chunk_sec`` long give or take one segment, and no segment
    is ever split. Segment boundaries are keyframes, which is what makes each
    chunk playable and analysable on its own.

    Pure: it never touches storage. The recorder feeds it segments and asks
    it whether the open chunk is due to close.
    """

    def __init__(self, chunk_sec: float):
        self.chunk_sec = float(chunk_sec)
        self.next_index = 0
        self.open: Chunk | None = None
        self.last_seq: int | None = None
        self.last_pdt_end: datetime | None = None
        self.pending_gap: dict[str, Any] | None = None

    def note_gap(self, missed: int, seconds: float) -> None:
        """Segments the playlist slid past before they could be fetched."""
        gap = {"missedSegments": int(missed), "seconds": round(float(seconds), 3)}
        if self.open is not None and self.open.parts:
            self.open.gaps_inside.append(gap)
        else:
            self.pending_gap = gap

    def add(self, segment: hls.Segment, part_uri: str) -> Chunk:
        """Attach a downloaded segment to the open chunk, opening one if needed."""
        if self.open is None:
            self.open = Chunk(index=self.next_index)
            self.next_index += 1
            self.open.gap_before = self._gap_before(segment)
            self.pending_gap = None
        chunk = self.open
        if chunk.first_seq is None:
            chunk.first_seq = segment.seq
            chunk.first_pdt = segment.pdt
        elif chunk.last_seq is not None and segment.seq != chunk.last_seq + 1:
            chunk.gaps_inside.append({
                "missedSegments": segment.seq - chunk.last_seq - 1,
                "seconds": round(self._pdt_gap(segment) or 0.0, 3),
            })
        if segment.discontinuity:
            chunk.discontinuities += 1
        chunk.parts.append(part_uri)
        chunk.duration += segment.duration
        chunk.last_seq = segment.seq
        if segment.pdt is not None:
            chunk.last_pdt_end = segment.pdt + timedelta(seconds=segment.duration)
        self.last_seq = segment.seq
        self.last_pdt_end = chunk.last_pdt_end
        return chunk

    def should_close(self, next_duration: float | None = None) -> bool:
        """Whether the open chunk is full.

        With ``next_duration`` given, the question is whether adding that
        segment would overshoot; without it, whether the chunk has reached
        its length.
        """
        if self.open is None or not self.open.parts:
            return False
        if next_duration is None:
            return self.open.duration >= self.chunk_sec
        return self.open.duration + next_duration > self.chunk_sec

    def close(self) -> Chunk | None:
        chunk, self.open = self.open, None
        return chunk if chunk and chunk.parts else None

    def _gap_before(self, segment: hls.Segment) -> dict[str, Any] | None:
        if self.pending_gap:
            return self.pending_gap
        if self.last_seq is not None and segment.seq != self.last_seq + 1:
            return {
                "missedSegments": segment.seq - self.last_seq - 1,
                "seconds": round(self._pdt_gap(segment) or 0.0, 3),
            }
        return None

    def _pdt_gap(self, segment: hls.Segment) -> float | None:
        if segment.pdt is None or self.last_pdt_end is None:
            return None
        return (segment.pdt - self.last_pdt_end).total_seconds()


class AudioParts:
    """Audio segments fetched so far, by sequence number.

    A separate audio rendition is its own playlist, but on the origins that
    publish one (Unified Streaming, JW Live) it is cut on the same clock and
    numbered in step with the video, so the audio for a video chunk is the
    parts whose numbers fall inside the chunk's range. Pure: the recorder
    stores the bytes and tells this what it has.
    """

    def __init__(self) -> None:
        self.parts: dict[int, str] = {}
        self.next_seq: int | None = None

    def add(self, seq: int, name: str) -> None:
        self.parts[seq] = name
        self.next_seq = max(self.next_seq or 0, seq + 1)

    def take(self, first_seq: int, last_seq: int) -> tuple[list[str], int, list[str]]:
        """``(names in order, how many are missing, stale names to delete)``.

        Stale parts are ones numbered before the range: audio the video chunk
        that would have carried them never had, because it was missed.
        """
        names = [self.parts[seq] for seq in range(first_seq, last_seq + 1) if seq in self.parts]
        missing = (last_seq - first_seq + 1) - len(names)
        stale = [name for seq, name in self.parts.items() if seq < first_seq]
        for seq in [q for q in self.parts if q <= last_seq]:
            del self.parts[seq]
        return names, missing, stale


# --- The event as a playable stream ---------------------------------------------

# Where the stream lives in the HLS bucket, beside the package rather than
# inside it: an encode clears `jobs/{job}/hls/` before it writes, and the stream
# is what an editor is watching while that encode runs.
STREAM_DIR = "live"
STREAM_PLAYLIST = "index.m3u8"
# The playlist changes every few seconds, so the CDN must never answer from a
# copy. It caches purely by origin headers, so this header is the whole
# arrangement. Segments never change once written.
STREAM_PLAYLIST_CACHE = "no-cache, no-store, max-age=0"
STREAM_SEGMENT_CACHE = "public, max-age=86400"


class LiveStream:
    """The event so far as an HLS playlist, grown one segment at a time.

    A live event had nothing to play until someone packaged it, and packaging
    is an encode of the whole recording — so every moment the analysis found
    while the event was on opened on "not packaged for playback yet". The
    recorder already holds each segment as it arrives; writing them where the
    CDN serves from, with a playlist beside them, is a stream of the event
    that can be watched from its first analysed chunk, with no encode at all.

    **Time on this playlist is time on the event.** The moments carry offsets
    from the first segment the recorder took, so the playlist starts at that
    segment and a segment that could not be fetched keeps its place as an
    `EXT-X-GAP` rather than being left out — leaving it out would pull every
    later segment earlier by its length, and every moment after it would open
    on the wrong few seconds without anything saying so. A gap before the first
    segment is not written, because the event's clock starts at the first
    segment that was recorded.

    `EVENT` rather than a sliding window, so the whole day stays seekable;
    `EXT-X-ENDLIST` once the recording has finished, which makes it a plain VOD.
    Pure: the recorder uploads what `render` returns.
    """

    def __init__(self, container: str = "ts", target_duration: float = 6.0):
        self.container = container
        self.target_duration = float(target_duration)
        self.entries: list[tuple[int, float, bool]] = []   # (seq, duration, is_gap)
        self.init_name = ""
        self.ended = False

    def segment_name(self, seq: int) -> str:
        return f"{int(seq):09d}.{'m4s' if self.container == 'mp4' else 'ts'}"

    @property
    def last_seq(self) -> int | None:
        return self.entries[-1][0] if self.entries else None

    def add(self, seq: int, duration: float) -> bool:
        """List a segment. Returns False for one already listed."""
        return self._append(seq, duration, gap=False)

    def add_gap(self, seq: int, duration: float) -> bool:
        """Hold a missing segment's place, so the time after it stays true."""
        if not self.entries:
            return False
        return self._append(seq, duration, gap=True)

    def _append(self, seq: int, duration: float, *, gap: bool) -> bool:
        last = self.last_seq
        # A restarted recorder takes the whole window it first sees, which can
        # overlap what the last one listed; the playlist only moves forward.
        if last is not None and seq <= last:
            return False
        if last is not None:
            # Segments that slid past before anyone fetched them: their exact
            # length is unknown, the target duration is what they almost were.
            for missing in range(last + 1, seq):
                self.entries.append((missing, self.target_duration, True))
        self.entries.append((int(seq), float(duration), gap))
        return True

    def duration(self) -> float:
        return sum(d for _, d, _ in self.entries)

    def render(self) -> str:
        if not self.entries:
            return ""
        longest = max(d for _, d, _ in self.entries)
        lines = [
            "#EXTM3U",
            # Fractional EXTINF needs 3; an initialisation map needs 6.
            f"#EXT-X-VERSION:{6 if self.init_name else 3}",
            # RFC 8216: every EXTINF rounded to the nearest integer must not
            # exceed it. Half-up, not ceiling — a ceiling turns 6.006 into 7 and
            # the player then reloads the playlist a second later than it could.
            f"#EXT-X-TARGETDURATION:{max(1, int(longest + 0.5))}",
            f"#EXT-X-MEDIA-SEQUENCE:{self.entries[0][0]}",
            "#EXT-X-PLAYLIST-TYPE:EVENT",
        ]
        if self.init_name:
            lines.append(f'#EXT-X-MAP:URI="{self.init_name}"')
        for seq, duration, gap in self.entries:
            if gap:
                lines.append("#EXT-X-GAP")
            lines.append(f"#EXTINF:{duration:.3f},")
            lines.append(self.segment_name(seq))
        if self.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    @classmethod
    def parse(cls, text: str, container: str = "ts") -> LiveStream:
        """Read back a playlist this class wrote, for a restarted recorder.

        Never ended: a recording being resumed is still going, and an
        EXT-X-ENDLIST left from the execution that died would tell every
        player the event was over.
        """
        stream = cls(container)
        duration: float | None = None
        gap = False
        for raw in (text or "").splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-MAP:") and 'URI="' in line:
                stream.init_name = line.split('URI="', 1)[1].split('"', 1)[0]
            elif line == "#EXT-X-GAP":
                gap = True
            elif line.startswith("#EXTINF:"):
                try:
                    duration = float(line[len("#EXTINF:"):].split(",", 1)[0])
                except ValueError:
                    duration = None
            elif line and not line.startswith("#"):
                stem, _, ext = line.rpartition(".")
                if duration is not None and stem.isdigit():
                    stream.container = "mp4" if ext == "m4s" else "ts"
                    stream.entries.append((int(stem), duration, gap))
                duration, gap = None, False
        return stream


def chunk_record(chunk: Chunk, gcs_uri: str, container: str,
                 capture_start: datetime | None, cumulative_sec: float,
                 audio: dict[str, Any] | None = None) -> dict[str, Any]:
    """The Firestore document for a closed chunk.

    ``startSec`` is the chunk's offset into the recording — from wall-clock
    when the playlist carries programme date-time, from the sum of the chunks
    before it when it does not. Moments found in the chunk are stamped with
    ``startSec`` plus their offset inside it, so the whole event reads as one
    timeline the way an uploaded match does.
    """
    if chunk.first_pdt is not None and capture_start is not None:
        start_sec = max(0.0, (chunk.first_pdt - capture_start).total_seconds())
    else:
        start_sec = cumulative_sec
    return {
        "index": chunk.index,
        "status": "captured",
        "gcsUri": gcs_uri,
        "container": container,
        "startSec": round(start_sec, 3),
        "durationSec": round(chunk.duration, 3),
        "segments": chunk.segments,
        "firstSeq": chunk.first_seq,
        "lastSeq": chunk.last_seq,
        "firstPdt": chunk.first_pdt.isoformat() if chunk.first_pdt else None,
        "lastPdtEnd": chunk.last_pdt_end.isoformat() if chunk.last_pdt_end else None,
        "discontinuities": chunk.discontinuities,
        "gapBefore": chunk.gap_before,
        "gapsInside": chunk.gaps_inside,
        "capturedAt": now().isoformat(),
        # The separate audio rendition's chunk, when the stream keeps its
        # audio apart from the video. The tick muxes the two before the
        # analysis; a chunk without one is analysed silent.
        "audioUri": (audio or {}).get("uri") or None,
        "audioContainer": (audio or {}).get("container") or None,
        "audioSegments": int((audio or {}).get("segments") or 0),
        "audioMissing": int((audio or {}).get("missing") or 0),
    }


# --- Storage and Firestore (thin) ----------------------------------------------


class Store:
    """What the recorder writes: parts and chunks to GCS, records to Firestore."""

    def __init__(self, job_id: str, bucket: str, stream_bucket: str = ""):
        from google.cloud import firestore, storage

        self.job_id = job_id
        client = storage.Client()
        self.bucket = client.bucket(bucket)
        self.bucket_name = bucket
        # The HLS bucket, which the CDN serves. Empty means no stream is written
        # — an execution started from a template that does not name it.
        self.stream_bucket = client.bucket(stream_bucket) if stream_bucket else None
        self.db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
        self.prefix = f"jobs/{job_id}/live"
        self.stream_prefix = f"jobs/{job_id}/{STREAM_DIR}"

    def put(self, name: str, data: bytes, content_type: str) -> str:
        blob = self.bucket.blob(name)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{name}"

    def put_stream(self, name: str, data: bytes, content_type: str, cache_control: str) -> None:
        """Write one object of the playable stream, under the stream prefix."""
        if self.stream_bucket is None:
            return
        blob = self.stream_bucket.blob(f"{self.stream_prefix}/{name}")
        blob.cache_control = cache_control
        blob.upload_from_string(data, content_type=content_type)

    def read_stream(self) -> str:
        """The playlist a previous execution left, or empty."""
        if self.stream_bucket is None:
            return ""
        blob = self.stream_bucket.blob(f"{self.stream_prefix}/{STREAM_PLAYLIST}")
        try:
            return blob.download_as_bytes().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001  — NotFound on a first start
            return ""

    def compose(self, names: list[str], dest: str, content_type: str) -> str:
        """Join objects server-side, in order, however many there are.

        Compose takes at most 32 sources, so more than that is composed in
        rounds into intermediates that are then composed again. No bytes pass
        through here at any size.
        """
        level = list(names)
        round_no = 0
        scratch: list[str] = []
        while len(level) > _COMPOSE_LIMIT:
            next_level: list[str] = []
            for i in range(0, len(level), _COMPOSE_LIMIT):
                group = level[i:i + _COMPOSE_LIMIT]
                inter = f"{dest}.part{round_no}-{i // _COMPOSE_LIMIT:03d}"
                target = self.bucket.blob(inter)
                target.content_type = content_type
                target.compose([self.bucket.blob(n) for n in group])
                next_level.append(inter)
                scratch.append(inter)
            level = next_level
            round_no += 1
        target = self.bucket.blob(dest)
        target.content_type = content_type
        target.compose([self.bucket.blob(n) for n in level])
        for name in scratch:
            self._delete_quiet(name)
        return f"gs://{self.bucket_name}/{dest}"

    def delete_all(self, names: list[str]) -> None:
        for name in names:
            self._delete_quiet(name)

    def _delete_quiet(self, name: str) -> None:
        try:
            self.bucket.blob(name).delete()
        except Exception:  # noqa: BLE001
            logger.warning("could not delete %s", name, exc_info=True)

    def resume_point(self) -> dict[str, Any]:
        """What a restarted recorder continues from.

        Chunk numbering carries on after the last chunk on record and the
        offset after its end, so the second execution's chunks sit after the
        first's on one timeline rather than starting again at chunk 0 — which
        would make the merged event two overlapping copies of its own start.
        """
        job = self.db.collection("jobs").document(self.job_id)
        chunks = [d.to_dict() or {} for d in job.collection("chunks").stream()]
        snapshot = job.get()
        capture = ((snapshot.to_dict() or {}).get("live") or {}).get("capture") or {}
        if not chunks:
            return {"next_index": 0, "cumulative_sec": 0.0,
                    "capture_start": hls.parse_pdt(capture.get("captureStart") or "")}
        last = max(chunks, key=lambda c: int(c.get("index") or 0))
        return {
            "next_index": int(last.get("index") or 0) + 1,
            "cumulative_sec": float(last.get("startSec") or 0.0) + float(last.get("durationSec") or 0.0),
            "capture_start": hls.parse_pdt(capture.get("captureStart") or ""),
        }

    def write_chunk(self, record: dict[str, Any]) -> None:
        job = self.db.collection("jobs").document(self.job_id)
        job.collection("chunks").document(f"{record['index']:04d}").set(record)

    def patch_live(self, capture: dict[str, Any], **counts: Any) -> None:
        patch: dict[str, Any] = {"live.capture": capture, "updatedAt": now()}
        for key, value in counts.items():
            patch[f"live.{key}"] = value
        self.db.collection("jobs").document(self.job_id).update(patch)


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    import requests

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


# --- The recorder ---------------------------------------------------------------


class Recorder:
    def __init__(self, job_id: str, hls_url: str, event_end: datetime,
                 chunk_sec: float, store: Store, execution: str = "",
                 stall_sec: float = 0.0):
        self.job_id = job_id
        self.hls_url = hls_url
        self.event_end = event_end
        self.store = store
        self.execution = execution
        # How long a stream that was flowing may produce nothing before the
        # event is over. 0 means never — wait for the end time, as before.
        self.stall_sec = max(0.0, float(stall_sec))
        # Why the recording stopped, for the tick to say: "end", "endlist" or
        # "stalled". Empty while it is still going.
        self.ended_by = ""
        # A restart mid-event. Set by main() from the resume point: this
        # recording has already seen the stream, so the stall clock is running
        # from the moment it starts rather than waiting for a first segment.
        self.resumed = False
        # The event as a playable stream, written beside the recording. A
        # restarted execution is handed the one its predecessor left.
        self.stream = LiveStream()
        self.stream_failures = 0
        self.assembler = ChunkAssembler(chunk_sec)
        self.variant: hls.Variant | None = None
        self.media_url = hls_url
        self.next_seq: int | None = None
        self.init_name = ""
        self.container = "ts"
        # The separate audio rendition, when the master names one for the
        # chosen variant. Followed beside the video and paired by number.
        self.audio_url = ""
        self.audio_container = "ts"
        self.audio_init_name = ""
        self.audio = AudioParts()
        self.capture_start: datetime | None = None
        self.cumulative_sec = 0.0
        self.chunks_captured = 0
        self.started_at = now()
        self.state = "starting"
        self.error = ""

    # -- reporting --

    def _capture(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "execution": self.execution,
            "startedAt": self.started_at.isoformat(),
            "captureStart": self.capture_start.isoformat() if self.capture_start else None,
            "variant": self.media_url,
            "container": self.container,
            "audio": bool(self.audio_url),
            "lastSeq": self.assembler.last_seq,
            "lastPollAt": now().isoformat(),
            "error": self.error,
            "endedBy": self.ended_by,
            "stallMinutes": round(self.stall_sec / 60, 2),
            # What /playback serves for this event. Only once something is in
            # it — a playlist with no segments is a player that spins forever.
            "stream": (f"{self.store.stream_prefix}/{STREAM_PLAYLIST}"
                       if self.store.stream_bucket is not None and self.stream.entries else ""),
        }

    def report(self) -> None:
        try:
            self.store.patch_live(self._capture(), chunksCaptured=self.chunks_captured)
        except Exception:  # noqa: BLE001
            logger.warning("could not report capture state", exc_info=True)

    # -- playlist --

    def resolve(self) -> hls.MediaPlaylist | None:
        """Find the media playlist, waiting for a stream that is not up yet."""
        # A stream that is not up yet gets the grace period: producers are late.
        # A resumed recording's stream is not late, it was flowing and has gone,
        # so it gets the stall limit instead — and running out of that is the
        # event ending, not the recorder failing. Without this a recorder
        # restarted onto a dead stream gave up after ten minutes as "failed",
        # was restarted three times, and the tick then failed the whole event
        # over hours of good chunks.
        grace = _PLAYLIST_GRACE_SEC
        if self.resumed and self.stall_sec:
            grace = min(grace, self.stall_sec)
        deadline = time.monotonic() + grace
        while True:
            try:
                text = _fetch(self.hls_url).decode("utf-8", "replace")
                if hls.is_master(text):
                    chosen = hls.pick_variant(hls.parse_master(text, self.hls_url))
                    if chosen is None:
                        raise ValueError("multivariant playlist lists no variants")
                    self.variant = chosen
                    self.media_url = chosen.url
                    self.audio_url = hls.separate_audio_url(text, self.hls_url, chosen)
                    text = _fetch(self.media_url).decode("utf-8", "replace")
                playlist = hls.parse_media(text, self.media_url)
                self.container = hls.container_of(playlist)
                logger.info("following %s (%s, target %.1fs)",
                            self.media_url, self.container, playlist.target_duration)
                if self.audio_url:
                    self._resolve_audio()
                return playlist
            except Exception as exc:  # noqa: BLE001
                if time.monotonic() > deadline or now() >= self.event_end:
                    if self.resumed and self.stall_sec:
                        logger.info("the stream is gone (%s) and has been for the stall limit; "
                                    "ending the capture", exc)
                        self.ended_by = "stalled"
                        return None
                    self.error = f"the stream never answered: {type(exc).__name__}: {exc}"
                    return None
                logger.info("stream not ready (%s); retrying", exc)
                time.sleep(10)

    def poll(self) -> hls.MediaPlaylist:
        text = _fetch(self.media_url).decode("utf-8", "replace")
        return hls.parse_media(text, self.media_url)

    def _resolve_audio(self) -> None:
        """Read the audio rendition's playlist once, to learn its container.

        Not fatal: an audio playlist that does not answer leaves the event
        video-only, which is what it was before audio was captured at all.
        """
        try:
            text = _fetch(self.audio_url).decode("utf-8", "replace")
            audio = hls.parse_media(text, self.audio_url)
            self.audio_container = hls.container_of(audio)
            logger.info("following audio %s (%s)", self.audio_url, self.audio_container)
        except Exception as exc:  # noqa: BLE001
            logger.warning("audio rendition %s not readable (%s); recording video only",
                           self.audio_url, exc)
            self.audio_url = ""

    def poll_audio(self) -> hls.MediaPlaylist:
        text = _fetch(self.audio_url).decode("utf-8", "replace")
        return hls.parse_media(text, self.audio_url)

    def take_audio(self, playlist: hls.MediaPlaylist) -> int:
        """Fetch every audio segment not yet held, from where the video starts."""
        floor = self.audio.next_seq
        if floor is None:
            floor = self.assembler.open.first_seq if self.assembler.open else self.next_seq
        fresh = [s for s in playlist.segments if floor is None or s.seq >= floor]
        if playlist.init_url and not self.audio_init_name:
            self.audio_init_name = f"{self.store.prefix}/audio_init.mp4"
            self.store.put(self.audio_init_name, _fetch(playlist.init_url), "audio/mp4")
        ext = "m4s" if self.audio_container == "mp4" else "ts"
        mime = "audio/mp4" if ext == "m4s" else "video/mp2t"
        taken = 0
        for segment in fresh:
            data = self._download(segment)
            if data is None:
                self.audio.next_seq = segment.seq + 1
                continue
            name = f"{self.store.prefix}/audio_parts/{segment.seq:09d}.{ext}"
            self.store.put(name, data, mime)
            self.audio.add(segment.seq, name)
            taken += 1
        return taken

    # -- the playable stream --

    def _stream_write(self, what: str, action) -> None:
        """Run one write of the stream, never letting it stop the recording.

        The stream is how an editor watches; the recording is what the analysis
        reads and what the event is. A playback copy that fails is a warning —
        a recorder that dropped segments because it could not write one would
        be the worst possible trade. One traceback, then a line every twenty.
        """
        try:
            action()
            self.stream_failures = 0
        except Exception as exc:  # noqa: BLE001
            self.stream_failures += 1
            if self.stream_failures == 1:
                logger.warning("could not write the playable stream (%s)", what, exc_info=True)
            elif self.stream_failures % 20 == 0:
                logger.warning("the playable stream is still failing (%d): %s",
                               self.stream_failures, exc)

    def _stream_segment(self, segment: hls.Segment, data: bytes, mime: str) -> None:
        name = self.stream.segment_name(segment.seq)
        self._stream_write(name, lambda: self.store.put_stream(
            name, data, mime, STREAM_SEGMENT_CACHE))

    def _write_stream(self) -> None:
        text = self.stream.render()
        if text:
            self._stream_write(STREAM_PLAYLIST, lambda: self.store.put_stream(
                STREAM_PLAYLIST, text.encode("utf-8"), "application/vnd.apple.mpegurl",
                STREAM_PLAYLIST_CACHE))

    # -- segments and chunks --

    def _ensure_init(self, playlist: hls.MediaPlaylist) -> None:
        if not playlist.init_url or self.init_name:
            return
        data = _fetch(playlist.init_url)
        self.init_name = f"{self.store.prefix}/init.mp4"
        self.store.put(self.init_name, data, "video/mp4")
        self.stream.init_name = "init.mp4"
        self._stream_write("init.mp4", lambda: self.store.put_stream(
            "init.mp4", data, "video/mp4", STREAM_SEGMENT_CACHE))

    def take(self, playlist: hls.MediaPlaylist) -> int:
        """Download every segment not yet seen; returns how many."""
        fresh = [s for s in playlist.segments if self.next_seq is None or s.seq >= self.next_seq]
        if self.next_seq is not None and playlist.segments and playlist.segments[0].seq > self.next_seq:
            missed = playlist.segments[0].seq - self.next_seq
            self.assembler.note_gap(missed, missed * playlist.target_duration)
            logger.warning("playlist slid past %d segment(s) before they were fetched", missed)
        self._ensure_init(playlist)
        self.stream.container = self.container
        self.stream.target_duration = playlist.target_duration or self.stream.target_duration
        taken = 0
        listed = False
        ext = "m4s" if self.container == "mp4" else "ts"
        mime = "video/iso.segment" if ext == "m4s" else "video/mp2t"
        for segment in fresh:
            if self.assembler.should_close(segment.duration):
                self.close_chunk()
            data = self._download(segment)
            self.next_seq = segment.seq + 1
            if data is None:
                self.assembler.note_gap(1, segment.duration)
                listed = self.stream.add_gap(segment.seq, segment.duration) or listed
                continue
            if self.capture_start is None:
                self.capture_start = segment.pdt or now()
            name = f"{self.store.prefix}/parts/{segment.seq:09d}.{ext}"
            self.store.put(name, data, mime)
            self.assembler.add(segment, name)
            # The segment first and the playlist after, so a player is never
            # told about an object that is not there yet.
            if self.store.stream_bucket is not None:
                self._stream_segment(segment, data, mime)
                listed = self.stream.add(segment.seq, segment.duration) or listed
            taken += 1
        if listed:
            self._write_stream()
        return taken

    def _download(self, segment: hls.Segment) -> bytes | None:
        for attempt in range(1, _SEGMENT_ATTEMPTS + 1):
            try:
                return _fetch(segment.url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("segment %d attempt %d failed: %s", segment.seq, attempt, exc)
                time.sleep(min(2.0 * attempt, 5.0))
        return None

    def close_chunk(self) -> dict[str, Any] | None:
        chunk = self.assembler.close()
        if chunk is None:
            return None
        names = list(chunk.parts)
        if self.container == "mp4" and self.init_name:
            names = [self.init_name, *names]
        dest = f"{self.store.prefix}/chunks/chunk_{chunk.index:04d}.{self.container}"
        mime = "video/mp4" if self.container == "mp4" else "video/mp2t"
        uri = self.store.compose(names, dest, mime)
        audio = self._close_audio(chunk)
        record = chunk_record(chunk, uri, self.container, self.capture_start,
                              self.cumulative_sec, audio=audio)
        self.cumulative_sec += chunk.duration
        self.store.write_chunk(record)
        self.store.delete_all(chunk.parts)
        self.chunks_captured += 1
        logger.info("chunk %d closed: %d segments, %.1fs, seq %s-%s%s",
                    chunk.index, chunk.segments, chunk.duration, chunk.first_seq,
                    chunk.last_seq, " (gap before)" if chunk.gap_before else "")
        self.report()
        return record

    def _close_audio(self, chunk: Chunk) -> dict[str, Any] | None:
        """Compose the audio parts that pair with a closed video chunk."""
        if not self.audio_url or chunk.first_seq is None or chunk.last_seq is None:
            return None
        names, missing, stale = self.audio.take(chunk.first_seq, chunk.last_seq)
        self.store.delete_all(stale)
        if not names:
            return None
        sources = list(names)
        if self.audio_container == "mp4" and self.audio_init_name:
            sources = [self.audio_init_name, *sources]
        dest = f"{self.store.prefix}/chunks/chunk_{chunk.index:04d}_audio.{self.audio_container}"
        mime = "audio/mp4" if self.audio_container == "mp4" else "video/mp2t"
        try:
            uri = self.store.compose(sources, dest, mime)
        except Exception:  # noqa: BLE001
            logger.warning("audio for chunk %d could not be composed", chunk.index, exc_info=True)
            return None
        finally:
            self.store.delete_all(names)
        if missing:
            logger.warning("chunk %d audio is missing %d segment(s)", chunk.index, missing)
        return {"uri": uri, "container": self.audio_container,
                "segments": len(names), "missing": missing}

    # -- the loop --

    def run(self) -> int:
        playlist = self.resolve()
        if playlist is None:
            if self.ended_by == "stalled":
                # Nothing new to close — the predecessor closed its chunks and
                # the stream has nothing more. The event is over, cleanly.
                if self.store.stream_bucket is not None and self.stream.entries:
                    self.stream.ended = True
                    self._write_stream()
                self.state = "finished"
                self.report()
                return 0
            self.state = "failed"
            self.report()
            logger.error("%s", self.error)
            return 1
        self.state = "recording"
        self.report()
        last_report = time.monotonic()
        # The stall clock. It starts at the first segment rather than at the
        # start of the recording: the recorder begins five minutes before the
        # event, and a broadcaster who goes live at the scheduled minute has
        # produced nothing for exactly that long. Waiting on a stream that has
        # not begun is the lead-in; a stream that was flowing and stopped is
        # the thing being measured.
        # A resumed recording has seen the stream already, hours of it, so its
        # clock runs from this restart rather than waiting for a first segment
        # that a stream which has gone will never send.
        last_new: float | None = time.monotonic() if self.resumed else None
        fetch_failures = 0
        while True:
            if now() >= self.event_end:
                logger.info("event end reached")
                self.ended_by = "end"
                break
            try:
                taken = self.take(playlist)
            except Exception:  # noqa: BLE001
                logger.warning("poll failed; will retry", exc_info=True)
                taken = 0
            if taken:
                last_new = time.monotonic()
            if playlist.endlist:
                logger.info("stream ended (EXT-X-ENDLIST)")
                self.ended_by = "endlist"
                break
            # A live stream that stops is expected, not a failure: a class ends,
            # the broadcaster stops the encoder, the origin starts answering 404.
            # Castr did exactly that at 17:01 on the LeMieux day and the recorder
            # polled a 404 every three seconds for four and a half hours, holding
            # the event open and its bar at 44%, until the scheduled end. After
            # this long with nothing new, the event is over and it is finished
            # like any other — the partial chunk is closed and analysed.
            if (self.stall_sec and last_new is not None
                    and time.monotonic() - last_new >= self.stall_sec):
                logger.info("no new segment for %.0f min; the stream has stopped, ending the capture",
                            self.stall_sec / 60)
                self.ended_by = "stalled"
                break
            if time.monotonic() - last_report > 60:
                self.report()
                last_report = time.monotonic()
            wait = _MIN_POLL_SEC if taken else min(_MAX_POLL_SEC, max(_MIN_POLL_SEC, playlist.target_duration / 2))
            time.sleep(wait)
            try:
                playlist = self.poll()
                if fetch_failures:
                    logger.info("playlist answering again after %d failed fetches", fetch_failures)
                fetch_failures = 0
            except Exception as exc:  # noqa: BLE001
                # The traceback once, then a line a minute: a stream that has
                # gone is the same failure every poll, and the full stack every
                # three seconds was 2,700 of them in one afternoon.
                fetch_failures += 1
                if fetch_failures == 1:
                    logger.warning("playlist fetch failed; keeping the last one", exc_info=True)
                elif fetch_failures % 20 == 0:
                    logger.warning("playlist still failing (%d fetches): %s", fetch_failures, exc)
                playlist.segments = []
            if self.audio_url:
                # Audio after video, so the chunk a poll closes always has
                # its video parts first and its audio parts by the next.
                try:
                    self.take_audio(self.poll_audio())
                except Exception:  # noqa: BLE001
                    logger.warning("audio poll failed; will retry", exc_info=True)
        self.close_chunk()
        # Finished cleanly, so the stream is complete: ENDLIST makes it an
        # ordinary VOD, and a player stops asking for a newer playlist.
        # Not on the failure path — a recorder that died is restarted, and
        # its successor carries the same stream on.
        if self.store.stream_bucket is not None and self.stream.entries:
            self.stream.ended = True
            self._write_stream()
        self.state = "finished"
        self.report()
        return 0


def main() -> int:
    job_id = os.environ.get("JOB_ID", "")
    hls_url = os.environ.get("HLS_URL", "")
    end_raw = os.environ.get("EVENT_END", "")
    if not (job_id and hls_url and end_raw and MEDIA_BUCKET):
        logger.error("JOB_ID, HLS_URL, EVENT_END and MEDIA_BUCKET are required")
        return 2
    event_end = hls.parse_pdt(end_raw)
    if event_end is None:
        logger.error("EVENT_END %r is not an ISO 8601 time", end_raw)
        return 2
    chunk_sec = float(os.environ.get("CHUNK_SEC") or os.environ.get("LIVE_CHUNK_SECONDS") or 300)
    # The full resource name, not the bare id the platform hands the
    # container: the tick polls and cancels this execution by name, and the
    # API reads a bare id as a project. LIVE_CAPTURE_JOB is the job's own
    # resource name, set on its template.
    execution = os.environ.get("CLOUD_RUN_EXECUTION", "")
    job_resource = os.environ.get("LIVE_CAPTURE_JOB", "")
    if execution and "/" not in execution and job_resource:
        execution = f"{job_resource.rstrip('/')}/executions/{execution}"
    # Minutes a stream that was flowing may produce nothing before the event is
    # finished. Set per event from the editor's settings; absent — an execution
    # started before this existed — means wait for the end time, as before.
    try:
        stall_min = float(os.environ.get("STALL_MINUTES") or 0)
    except ValueError:
        stall_min = 0.0
    # The HLS bucket, which the CDN serves: where the playable stream goes.
    # Handed over per execution by start_live_capture.
    store = Store(job_id, MEDIA_BUCKET, os.environ.get("HLS_BUCKET", ""))
    recorder = Recorder(job_id, hls_url, event_end, chunk_sec, store, execution,
                        stall_sec=stall_min * 60)
    # A restart carries on the stream its predecessor was writing. A first
    # start finds nothing — start_live_capture clears the prefix — and that is
    # the same code path.
    try:
        recorder.stream = LiveStream.parse(store.read_stream())
        if recorder.stream.entries:
            logger.info("continuing the playable stream after segment %d", recorder.stream.last_seq)
    except Exception:  # noqa: BLE001
        logger.warning("could not read the previous stream; starting a new one", exc_info=True)
    try:
        point = store.resume_point()
        if point["next_index"]:
            recorder.resumed = True
            recorder.assembler.next_index = point["next_index"]
            recorder.cumulative_sec = point["cumulative_sec"]
            recorder.chunks_captured = point["next_index"]
            logger.info("resuming after chunk %d", point["next_index"] - 1)
        if point["capture_start"] is not None:
            recorder.capture_start = point["capture_start"]
    except Exception:  # noqa: BLE001
        logger.warning("could not read a resume point; starting from chunk 0", exc_info=True)
    try:
        return recorder.run()
    except Exception as exc:  # noqa: BLE001
        recorder.state = "failed"
        recorder.error = f"{type(exc).__name__}: {exc}"
        recorder.report()
        logger.exception("capture died")
        return 1


if __name__ == "__main__":
    sys.exit(main())
