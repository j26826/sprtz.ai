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

    def __init__(self, job_id: str, bucket: str):
        from google.cloud import firestore, storage

        self.job_id = job_id
        self.bucket = storage.Client().bucket(bucket)
        self.bucket_name = bucket
        self.db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
        self.prefix = f"jobs/{job_id}/live"

    def put(self, name: str, data: bytes, content_type: str) -> str:
        blob = self.bucket.blob(name)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{name}"

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
                 chunk_sec: float, store: Store, execution: str = ""):
        self.job_id = job_id
        self.hls_url = hls_url
        self.event_end = event_end
        self.store = store
        self.execution = execution
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
        }

    def report(self) -> None:
        try:
            self.store.patch_live(self._capture(), chunksCaptured=self.chunks_captured)
        except Exception:  # noqa: BLE001
            logger.warning("could not report capture state", exc_info=True)

    # -- playlist --

    def resolve(self) -> hls.MediaPlaylist | None:
        """Find the media playlist, waiting for a stream that is not up yet."""
        deadline = time.monotonic() + _PLAYLIST_GRACE_SEC
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

    # -- segments and chunks --

    def _ensure_init(self, playlist: hls.MediaPlaylist) -> None:
        if not playlist.init_url or self.init_name:
            return
        data = _fetch(playlist.init_url)
        self.init_name = f"{self.store.prefix}/init.mp4"
        self.store.put(self.init_name, data, "video/mp4")

    def take(self, playlist: hls.MediaPlaylist) -> int:
        """Download every segment not yet seen; returns how many."""
        fresh = [s for s in playlist.segments if self.next_seq is None or s.seq >= self.next_seq]
        if self.next_seq is not None and playlist.segments and playlist.segments[0].seq > self.next_seq:
            missed = playlist.segments[0].seq - self.next_seq
            self.assembler.note_gap(missed, missed * playlist.target_duration)
            logger.warning("playlist slid past %d segment(s) before they were fetched", missed)
        self._ensure_init(playlist)
        taken = 0
        ext = "m4s" if self.container == "mp4" else "ts"
        mime = "video/iso.segment" if ext == "m4s" else "video/mp2t"
        for segment in fresh:
            if self.assembler.should_close(segment.duration):
                self.close_chunk()
            data = self._download(segment)
            self.next_seq = segment.seq + 1
            if data is None:
                self.assembler.note_gap(1, segment.duration)
                continue
            if self.capture_start is None:
                self.capture_start = segment.pdt or now()
            name = f"{self.store.prefix}/parts/{segment.seq:09d}.{ext}"
            self.store.put(name, data, mime)
            self.assembler.add(segment, name)
            taken += 1
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
            self.state = "failed"
            self.report()
            logger.error("%s", self.error)
            return 1
        self.state = "recording"
        self.report()
        last_report = time.monotonic()
        while True:
            if now() >= self.event_end:
                logger.info("event end reached")
                break
            try:
                taken = self.take(playlist)
            except Exception:  # noqa: BLE001
                logger.warning("poll failed; will retry", exc_info=True)
                taken = 0
            if playlist.endlist:
                logger.info("stream ended (EXT-X-ENDLIST)")
                break
            if time.monotonic() - last_report > 60:
                self.report()
                last_report = time.monotonic()
            wait = _MIN_POLL_SEC if taken else min(_MAX_POLL_SEC, max(_MIN_POLL_SEC, playlist.target_duration / 2))
            time.sleep(wait)
            try:
                playlist = self.poll()
            except Exception:  # noqa: BLE001
                logger.warning("playlist fetch failed; keeping the last one", exc_info=True)
                playlist.segments = []
            if self.audio_url:
                # Audio after video, so the chunk a poll closes always has
                # its video parts first and its audio parts by the next.
                try:
                    self.take_audio(self.poll_audio())
                except Exception:  # noqa: BLE001
                    logger.warning("audio poll failed; will retry", exc_info=True)
        self.close_chunk()
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
    store = Store(job_id, MEDIA_BUCKET)
    recorder = Recorder(job_id, hls_url, event_end, chunk_sec, store, execution)
    try:
        point = store.resume_point()
        if point["next_index"]:
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
