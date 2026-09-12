"""mcp-media — the media tool server.

Runs as a private Cloud Run service. Every tool takes and returns GCS URIs;
nothing is streamed through the MCP transport, because a match is gigabytes and
a tool result is not.

Two kinds of work live here and they run in different places. Packaging a match
for playback is a Transcoder API job: it reads the source from GCS and writes
the HLS package to GCS without a byte passing through this container, which is
what makes a real 480p encode possible at all. Everything else — probing an
upload to decide whether it is a video, one poster frame, the short cuts an
editor downloads or publishes — is still ffmpeg here, because each reads a few
megabytes over a range request and finishes in seconds.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

import requests

from media_server import ffmpeg_ops, gcs, hls, runjobs, transcoder, youtube

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mcp-media")

UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")
MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
HLS_BUCKET = os.environ.get("HLS_BUCKET", "")
CDN_BASE_URL = os.environ.get("CDN_BASE_URL", "").rstrip("/")
# The two Cloud Run Jobs this service starts, as full resource names. Empty
# means the deployment has none, and the tools that need them say so.
HLS2MP4_JOB = os.environ.get("HLS2MP4_JOB", "")
REMUX_JOB = os.environ.get("REMUX_JOB", "")
LIVE_CAPTURE_JOB = os.environ.get("LIVE_CAPTURE_JOB", "")
LIVE_CHUNK_SECONDS = int(os.environ.get("LIVE_CHUNK_SECONDS", "300") or 300)
# Cloud Run's writable filesystem is memory-backed, so scratch is only ever
# used for small artefacts: playlists in flight, thumbnails, rendered cuts.
# Multi-gigabyte sources are read over HTTPS and never land here.
SCRATCH = Path(os.environ.get("SCRATCH_DIR", "/tmp/scratch"))

# Enough of an MP4 to contain a faststart moov atom on a long recording.
_HEADER_BYTES = 32 * 1024 * 1024

mcp = FastMCP("sprtz-media")


def _scratch() -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(SCRATCH)))


def _cleanup(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# Containers whose header states the duration. A prefix of one of these is
# the whole answer; a prefix of anything else is a shorter file.
_HEADER_DURATION_CONTAINERS = ("mp4", "mov", "3gp")
# MPEG-TS packets are 188 bytes from the start of the file, so a tail slice
# that begins on a packet boundary probes clean.
_TS_PACKET = 188


def _slice_probe(gcs_uri: str, start_byte: int, end_byte: int) -> dict:
    """ffprobe one byte range of an object, downloaded to scratch."""
    work = _scratch()
    try:
        part = work / "part.bin"
        gcs.download_range(gcs_uri, part, end_byte, start_byte=start_byte)
        return ffmpeg_ops.probe(part)
    finally:
        _cleanup(work)


def _head_probe(gcs_uri: str) -> dict:
    """ffprobe the first ``_HEADER_BYTES`` of an object."""
    return _slice_probe(gcs_uri, 0, _HEADER_BYTES)


def _header_states_duration(info: dict) -> bool:
    container = (info.get("container") or "").lower()
    return any(name in container for name in _HEADER_DURATION_CONTAINERS)


def _ends_probe(gcs_uri: str, head: dict, size: int) -> dict:
    """The duration of a stream with no header, from its first and last bytes.

    A transport stream's length is the last timestamp minus the first, and
    those live in the first and last packets. Two range reads, the same size
    as the header read, and no dependence on how a given ffmpeg seeks over
    HTTPS — the service's ffmpeg 7.1 read a 6.9 GB recording to the end for
    a question ffmpeg 9 answered with one seek in a second, and the probe
    timed out at ten minutes.
    """
    start = max(0, size - _HEADER_BYTES)
    if "mpegts" in (head.get("container") or "").lower():
        start -= start % _TS_PACKET
    tail = _slice_probe(gcs_uri, start, size)
    end_sec = float(tail.get("start_sec") or 0.0) + float(tail.get("duration_sec") or 0.0)
    duration = end_sec - float(head.get("start_sec") or 0.0)
    if duration <= 0:
        raise RuntimeError(f"could not place the ends of {gcs_uri}: head {head.get('start_sec')}, "
                           f"tail {tail.get('start_sec')}+{tail.get('duration_sec')}")
    return {**head, "duration_sec": round(duration, 3), "bytes": size}


@mcp.tool
def probe_media(gcs_uri: str) -> dict:
    """Read a video's duration, resolution, frame rate and codecs.

    Tries the file header alone first, which avoids pulling gigabytes across the
    wire for a faststart MP4 — the moov atom states the duration, so a prefix
    is the whole answer. **A prefix of an MPEG-TS is not.** A transport stream
    has no header to state its length, ffprobe reports the duration of what it
    was given, and the first 32 MiB of a 6.9 GB recording probed as a
    149-second video: the analysis then ran on one window and found nothing.
    A container with no header is measured from its two ends instead — the
    last packet's time minus the first's, two range reads — and a
    non-faststart MP4 is probed in place over HTTPS, where ffprobe range-reads
    the moov from the tail. The size always comes from the object.

    Args:
        gcs_uri: gs:// URI of the video.
    """
    try:
        try:
            size = gcs.object_size(gcs_uri)
        except Exception:  # noqa: BLE001
            size = 0
        info: dict | None = None
        head: dict | None = None
        try:
            head = _head_probe(gcs_uri)
            if head["duration_sec"] > 0 and _header_states_duration(head):
                info = head
        except Exception as exc:  # noqa: BLE001
            logger.info("header probe failed (%s)", exc)

        if info is None and head is not None and size and not _header_states_duration(head):
            try:
                info = _ends_probe(gcs_uri, head, size)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ends probe failed (%s); probing over HTTPS", exc)

        if info is None:
            # Non-faststart MP4, or a stream whose ends could not be read:
            # probe it in place over HTTPS. The object never lands on disk.
            info = ffmpeg_ops.probe(gcs.https_url(gcs_uri), bearer_token=gcs.bearer_token())

        if size:
            info["bytes"] = size
        return {"status": "success", "gcs_uri": gcs_uri, "partial_read": True, **info}
    except Exception as exc:  # noqa: BLE001
        logger.exception("probe_media failed for %s", gcs_uri)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "gcs_uri": gcs_uri}


def _source_has_audio(gcs_uri: str) -> bool:
    """Whether the source carries an audio track, from its first bytes.

    Transcoder is told which elementary streams to make, and asked for an AAC
    track from a file that has none it fails minutes in with "does not have
    any inputs with an audio track" — which is what an HLS recording whose
    audio was a separate rendition looks like. Stream presence is in the
    first packets, so the head is enough. Unreadable means "assume audio":
    that is the encode failing as it did, rather than a silent proxy made of
    a source that had sound.
    """
    try:
        return bool(_head_probe(gcs_uri).get("has_audio"))
    except Exception:  # noqa: BLE001
        logger.warning("could not tell whether %s has audio; assuming it does", gcs_uri,
                       exc_info=True)
        return True


@mcp.tool
def delete_job_media(job_id: str, gcs_uri: str = "") -> dict:
    """Delete a job's source upload and its HLS package.

    Two buckets, and the source is addressed by URI because only the caller
    knows it: the upload path carries the owner's uid, which this service never
    sees.

    Args:
        job_id: Job whose media to remove.
        gcs_uri: gs:// URI of the source upload. Skipped when empty.
    """
    removed = {"hls_objects": 0, "already_gone": 0, "failed": 0, "source_deleted": False}
    try:
        if MEDIA_BUCKET:
            # A run that died mid-analysis leaves these; nothing else clears them.
            gcs.delete_prefix(MEDIA_BUCKET, f"jobs/{job_id}/")
        if HLS_BUCKET:
            counts = gcs.delete_prefix(HLS_BUCKET, f"jobs/{job_id}/")
            removed["hls_objects"] = counts["deleted"]
            removed["already_gone"] = counts["already_gone"]
            removed["failed"] = counts["failed"]
        if gcs_uri:
            removed["source_deleted"] = gcs.delete_object(gcs_uri)
        if UPLOADS_BUCKET:
            # An HLS source was downloaded here by the hls2mp4 job rather than
            # uploaded under the owner's prefix; the caller's URI names only
            # the file, not the run's other artefacts.
            gcs.delete_prefix(UPLOADS_BUCKET, f"hls/{job_id}/")

        # Objects that were already gone are not a problem — the caller wanted
        # them gone. Only ones that refused to delete leave the prefix dirty,
        # and the job should not be dropped from Firestore while they remain,
        # or nothing points at them any more.
        if removed["failed"]:
            return {
                "status": "error", "job_id": job_id,
                "error": f"{removed['failed']} object(s) could not be deleted", **removed,
            }
        return {"status": "success", "job_id": job_id, **removed}
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not delete media for %s", job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}


@mcp.tool
def validate_media(gcs_uri: str, declared_content_type: str = "") -> dict:
    """Check that an upload is really a video this service can process.

    Probes the actual bytes rather than trusting the filename or the
    Content-Type the browser sent, both of which the uploader controls. Returns
    every reason for rejection at once so the caller can report them together.

    Args:
        gcs_uri: gs:// URI of the uploaded file.
        declared_content_type: Content type the client claimed, if known.
    """
    probe = probe_media(gcs_uri)
    if probe.get("status") != "success":
        return {
            "status": "rejected",
            "gcs_uri": gcs_uri,
            "reasons": ["the file could not be read as media"],
            "detail": probe.get("error", ""),
        }

    reasons = ffmpeg_ops.validate(probe, declared_content_type=declared_content_type)
    if reasons:
        logger.warning("rejected upload %s: %s", gcs_uri, "; ".join(reasons))
        return {"status": "rejected", "gcs_uri": gcs_uri, "reasons": reasons, "media": probe}

    return {"status": "accepted", "gcs_uri": gcs_uri, "media": probe}


@mcp.tool
def transcode_hls(gcs_uri: str, job_id: str) -> dict:
    """Start a 480p HLS encode for review playback, and return immediately.

    Runs on Google Cloud Transcoder API rather than in this container: it reads
    the source from GCS and writes the package to GCS itself, so a match-length
    video never passes through here. The encode is asynchronous — poll it with
    `transcode_status` — because waiting for a three-hour match to finish would
    hold a request open for the whole encode.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the package belongs to; becomes the object prefix.
    """
    if not HLS_BUCKET:
        return {"status": "error", "error": "HLS_BUCKET is not configured."}

    try:
        # Anything already under this prefix is from an attempt that did not
        # finish. Transcoder names its segments differently from the ffmpeg
        # packager that came before it, so nothing here would ever be
        # overwritten — and a stale playlist would be served as if it were this
        # encode's.
        gcs.delete_prefix(HLS_BUCKET, f"jobs/{job_id}/hls/")
        started = transcoder.create_preview_job(gcs_uri, HLS_BUCKET, job_id,
                                                audio=_source_has_audio(gcs_uri))
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not start a transcoder job for %s", gcs_uri)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}

    master_path = f"jobs/{job_id}/hls/{transcoder.MASTER_PLAYLIST}"
    return {
        "status": "started",
        "job_id": job_id,
        # Known up front: Transcoder writes to a path this service chose, so the
        # playback URL does not have to wait for the encode to finish.
        "playback_url": f"{CDN_BASE_URL}/{master_path}" if CDN_BASE_URL else "",
        "renditions": [f"{transcoder.PREVIEW_HEIGHT}p"],
        "segment_seconds": transcoder.SEGMENT_SECONDS,
        **started,
    }


@mcp.tool
def split_for_analysis(gcs_uri: str, job_id: str, windows: list[dict]) -> dict:
    """Cut a source into physical segment files for analysis.

    Gemini fetches the *whole* object to serve a request, whatever time offsets
    are asked for: a 3.22 GiB match fails every segment with "File content
    exceeded the size limit. max_bytes_fetched: 2146971648". Slicing by time
    alone therefore does not help — the bytes have to be smaller, so the source
    is cut into real files, one per window.

    Stream copy, so this is a remux rather than an encode: `-ss` on an HTTPS
    source is a range read, so each cut pulls roughly its own share of the file
    and nothing decodes. Segments are written, uploaded and deleted one at a
    time, because the writable filesystem here is memory and holding thirteen
    of them at once is how this container died before.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the segments belong to.
        windows: [{"index": 0, "start_sec": 0.0, "end_sec": 900.0}, ...].
    """
    if not MEDIA_BUCKET:
        return {"status": "error", "error": "MEDIA_BUCKET is not configured."}

    work = _scratch()
    segments: list[dict] = []
    try:
        token = gcs.bearer_token()
        source_url = gcs.https_url(gcs_uri)

        for window in windows:
            index = int(window["index"])
            start = float(window["start_sec"])
            end = float(window["end_sec"])
            local = work / f"segment_{index:03d}.mp4"

            # Copy, not re-encode. The in-point can drift to the nearest
            # keyframe, which is exactly what the windows overlap to absorb —
            # and re-encoding thirteen segments of a three-hour match is hours
            # of CPU for a picture the model samples at 1 fps.
            ffmpeg_ops.cut(source_url, local, start, end,
                           reencode=False, bearer_token=token)

            uri = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/segments/{local.name}"
            size = local.stat().st_size
            gcs.upload(local, uri, content_type="video/mp4")
            local.unlink()

            segments.append({
                "index": index, "gcs_uri": uri,
                "start_sec": start, "end_sec": end, "bytes": size,
            })
            logger.info("segment %d of %d written (%d bytes)",
                        index + 1, len(windows), size)

        return {"status": "success", "job_id": job_id, "segments": segments,
                "count": len(segments)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("split_for_analysis failed for %s", gcs_uri)
        return {
            "status": "error", "error": f"{type(exc).__name__}: {exc}",
            "job_id": job_id,
            # Whatever was written before the failure is still usable, and
            # saying so stops a caller re-cutting the whole match to retry.
            "segments": segments,
        }
    finally:
        _cleanup(work)


@mcp.tool
def delete_analysis_segments(job_id: str) -> dict:
    """Remove the per-window files cut for analysis.

    They are a derived copy of the whole match and nothing needs them once it
    has been read. Deleting them is separate from deleting the job because the
    job keeps its source and its playback long after the analysis is done.

    Args:
        job_id: Job whose analysis segments to remove.
    """
    if not MEDIA_BUCKET:
        return {"status": "error", "error": "MEDIA_BUCKET is not configured."}
    try:
        counts = gcs.delete_prefix(MEDIA_BUCKET, f"jobs/{job_id}/segments/")
        return {"status": "success", "job_id": job_id, **counts}
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not remove analysis segments for %s", job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}


@mcp.tool
def playback_ready(job_id: str) -> dict:
    """Whether a job's HLS package is actually in the bucket.

    A job document can record a playback URL for a package that no longer
    exists — a delete that removed the media and then failed, or a bucket
    cleared by hand. The record is a claim; this is the check.

    Args:
        job_id: Job whose package to look for.
    """
    if not HLS_BUCKET:
        return {"status": "error", "error": "HLS_BUCKET is not configured."}
    try:
        path = f"jobs/{job_id}/hls/{transcoder.MASTER_PLAYLIST}"
        return {"status": "success", "job_id": job_id,
                "ready": gcs.object_exists(HLS_BUCKET, path), "path": path}
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not check playback for %s", job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}


@mcp.tool
def transcode_status(transcoder_job: str) -> dict:
    """Ask whether an HLS encode has finished.

    Args:
        transcoder_job: Full resource name returned by `transcode_hls`.
    """
    try:
        return {"status": "success", **transcoder.job_state(transcoder_job)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not read transcoder job %s", transcoder_job)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool
def generate_poster(gcs_uri: str, job_id: str) -> dict:
    """Write a poster frame for a job to the HLS bucket.

    Still ffmpeg: one frame read over a range request costs a few megabytes and
    finishes in seconds, which is not the workload that made packaging
    untenable here.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the poster belongs to.
    """
    if not HLS_BUCKET:
        return {"status": "error", "error": "HLS_BUCKET is not configured."}

    work = _scratch()
    try:
        token = gcs.bearer_token()
        source_url = gcs.https_url(gcs_uri)
        info = ffmpeg_ops.probe(source_url, bearer_token=token)

        poster_rel = f"jobs/{job_id}/poster.jpg"
        poster = work / "poster.jpg"
        ffmpeg_ops.thumbnail(
            source_url, poster,
            at_sec=min(30.0, info["duration_sec"] / 2),
            bearer_token=token,
        )
        gcs.upload(
            poster,
            f"gs://{HLS_BUCKET}/{poster_rel}",
            cache_control="public, max-age=86400",
        )
        return {
            "status": "success",
            "job_id": job_id,
            "poster_url": f"{CDN_BASE_URL}/{poster_rel}" if CDN_BASE_URL else "",
            "duration_sec": info["duration_sec"],
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_poster failed for %s", gcs_uri)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}
    finally:
        _cleanup(work)


# Moment ids are minted upstream from a model's output, and this one becomes a
# path segment. Anything outside the set below is replaced rather than rejected,
# because a thumbnail is not worth failing a run over — but a "moment id" of
# "../../poster" must not be able to name an object outside this job's prefix.
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")

# Wide enough for the editor's 72px thumbnail column at three times the density,
# which is all this picture is ever shown at. A frame at source resolution would
# be a megabyte of PNG per moment for no visible difference.
THUMBNAIL_WIDTH = 320


@mcp.tool
def generate_moment_thumbnails(gcs_uri: str, job_id: str, moments: list[dict]) -> dict:
    """Write one PNG per moment, taken at the moment's peak.

    The frame is the first I-frame at or after the peak. That is a whole picture
    the encoder already chose as a reference, and it costs one decode instead of
    a GOP of them — which is the difference that matters when this runs a couple
    of hundred times for one match.

    Called with a handful of moments at a time rather than all of them: a match
    has hundreds, each is its own range read, and one request holding all of
    them would run for minutes, report nothing while it did, and name no
    particular moment when it failed.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the moments belong to.
        moments: [{"moment_id": "...", "at_sec": 2835.0}, ...].
    """
    if not MEDIA_BUCKET:
        return {"status": "error", "error": "MEDIA_BUCKET is not configured."}

    work = _scratch()
    written: list[dict] = []
    failures: list[dict] = []
    try:
        token = gcs.bearer_token()
        source_url = gcs.https_url(gcs_uri)

        for moment in moments:
            moment_id = str(moment.get("moment_id") or "")
            safe_id = _SAFE_ID.sub("_", moment_id)[:120]
            at_sec = max(0.0, float(moment.get("at_sec") or 0.0))
            if not safe_id:
                failures.append({"moment_id": moment_id, "error": "empty moment_id"})
                continue

            local = work / f"{safe_id}.png"
            try:
                on_keyframe = ffmpeg_ops.keyframe_thumbnail(
                    source_url, local, at_sec,
                    width=THUMBNAIL_WIDTH, bearer_token=token,
                )
                if not on_keyframe:
                    # A peak in the file's last GOP has no keyframe after it.
                    # An exact frame is a worse still and a much better answer
                    # than a blank square.
                    ffmpeg_ops.still_frame(
                        source_url, local, at_sec,
                        width=THUMBNAIL_WIDTH, bearer_token=token,
                    )

                uri = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/moments/{safe_id}.png"
                gcs.upload(local, uri, content_type="image/png",
                           cache_control="public, max-age=86400")
                local.unlink(missing_ok=True)
                written.append({"moment_id": moment_id, "gcs_uri": uri,
                                "at_sec": at_sec, "on_keyframe": on_keyframe})
            except Exception as exc:  # noqa: BLE001
                # One unreadable frame is one missing thumbnail, not a failed
                # batch: the other moments in this request are still worth
                # having, and the moment itself is still a moment.
                logger.warning("thumbnail failed for %s at %.3fs: %s", moment_id, at_sec, exc)
                failures.append({"moment_id": moment_id,
                                 "error": f"{type(exc).__name__}: {exc}"})

        return {"status": "success", "job_id": job_id, "thumbnails": written,
                "count": len(written), "failures": failures}
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_moment_thumbnails failed for %s", gcs_uri)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                "job_id": job_id, "thumbnails": written, "failures": failures}
    finally:
        _cleanup(work)


@mcp.tool
def delete_moment_thumbnails(job_id: str) -> dict:
    """Remove a job's moment thumbnails.

    Re-analysing mints new moment ids, so last run's PNGs are not overwritten by
    this one's — they simply stay, named after moments that no longer exist.
    Clearing the prefix first is the same reasoning as the encode clearing the
    HLS prefix before packaging.

    Args:
        job_id: Job whose thumbnails to remove.
    """
    if not MEDIA_BUCKET:
        return {"status": "error", "error": "MEDIA_BUCKET is not configured."}
    try:
        counts = gcs.delete_prefix(MEDIA_BUCKET, f"jobs/{job_id}/moments/")
        return {"status": "success", "job_id": job_id, **counts}
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not delete moment thumbnails for %s", job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}


@mcp.tool
def cut_moment(gcs_uri: str, job_id: str, moment_id: str, start_sec: float,
               end_sec: float) -> dict:
    """Render one moment out of the source video as a standalone MP4.

    This is what a download is. Watching a moment in the editor seeks the HLS
    stream instead, so nothing renders until someone asks for the file itself.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the moment belongs to.
        moment_id: Identifier for the moment.
        start_sec: In point in seconds.
        end_sec: Out point in seconds.
    """
    if end_sec <= start_sec:
        return {"status": "error", "error": "end_sec must be greater than start_sec."}

    work = _scratch()
    try:
        out = work / f"{moment_id}.mp4"
        # -ss over HTTPS is a range seek: a 30-second cut out of a three-hour
        # match reads megabytes, not the whole object.
        ffmpeg_ops.cut(gcs.https_url(gcs_uri), out, start_sec, end_sec,
                       bearer_token=gcs.bearer_token())

        dest = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/downloads/{moment_id}.mp4"
        gcs.upload(out, dest)

        return {
            "status": "success",
            "moment_id": moment_id,
            "output_uri": dest,
            "bytes": out.stat().st_size,
            "duration_sec": round(end_sec - start_sec, 2),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("cut_moment failed for %s", moment_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                "moment_id": moment_id}
    finally:
        _cleanup(work)


@mcp.tool
def publish_youtube(clip_uri: str, title: str, description: str = "",
                    privacy: str = "private", tags: list[str] | None = None) -> dict:
    """Upload a rendered cut to the configured YouTube channel.

    The credentials are the channel's, read from the deployment's own config
    rather than passed in: a client secret that travels through a tool call is
    a client secret in somebody's log.

    Args:
        clip_uri: gs:// URI of the MP4 to publish.
        title: Video title. YouTube cuts anything past 100 characters.
        description: Video description.
        privacy: "private", "unlisted" or "public".
        tags: Tags, without the leading hash.
    """
    work = _scratch()
    try:
        creds = youtube.credentials()
        local = work / "upload.mp4"
        gcs.download(clip_uri, local)
        token = youtube.access_token(creds)
        result = youtube.upload(
            local, token=token, title=title, description=description,
            privacy=privacy, tags=tags or [],
        )
        return {"status": "success", **result}
    except youtube.YouTubeError as exc:
        # Expected and the editor's to act on: not configured, a revoked token,
        # a channel over its quota. Logged as a warning rather than an
        # exception, because a stack trace here says nothing a reader needs.
        logger.warning("youtube publish refused: %s", exc)
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish_youtube failed for %s", clip_uri)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _cleanup(work)


@mcp.tool
def render_preview(gcs_uri: str, job_id: str, start_sec: float, end_sec: float) -> dict:
    """Render a quick low-bitrate preview of a range, for checking a proposed cut.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the preview belongs to.
        start_sec: In point in seconds.
        end_sec: Out point in seconds.
    """
    work = _scratch()
    try:
        source_uri = gcs_uri
        preview_id = uuid.uuid4().hex[:12]
        out = work / f"{preview_id}.mp4"
        ffmpeg_ops.cut(gcs.https_url(gcs_uri), out, start_sec, end_sec,
                       bearer_token=gcs.bearer_token())
        dest = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/previews/{preview_id}.mp4"
        gcs.upload(out, dest)
        return {
            "status": "success",
            "preview_uri": dest,
            "source_uri": source_uri,
            "duration_sec": round(end_sec - start_sec, 2),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("render_preview failed")
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _cleanup(work)


# --- HLS sources and live events (Cloud Run Jobs) ------------------------------


def _hls_source_prefix(job_id: str) -> str:
    return f"hls/{job_id}/"


def _proxy_prefix(job_id: str) -> str:
    return f"jobs/{job_id}/proxy/"


PLAYLIST_CHECK_TIMEOUT = 15
PLAYLIST_MAX_BYTES = 4 * 1024 * 1024


def fetch_playlist(hls_url: str) -> tuple[str, str]:
    """``(problem, text)``: why a playlist URL cannot be used, or its contents.

    A signed CDN link expires, and the download job then costs a three-minute
    cold start to report "the job did not succeed" with the 403 in its own
    log. One request here says which HTTP status the URL answers, in
    seconds, before anything is started — and hands back the playlist, which
    is also how a separate audio rendition is found.
    """
    try:
        with requests.get(hls_url, stream=True, timeout=PLAYLIST_CHECK_TIMEOUT) as resp:
            if resp.status_code >= 400:
                reason = f"The playlist URL answered HTTP {resp.status_code} {resp.reason}"
                if resp.status_code in (401, 403):
                    reason += " — a signed link that has expired, or one that needs a token"
                return reason + ".", ""
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_content(64 * 1024):
                chunks.append(chunk or b"")
                size += len(chunk or b"")
                if size > PLAYLIST_MAX_BYTES:
                    break
            body = b"".join(chunks)
    except requests.RequestException as exc:
        return f"The playlist URL could not be fetched: {type(exc).__name__}: {exc}.", ""
    if b"#EXTM3U" not in body[:1024]:
        return "The URL answered, but not with an HLS playlist (no #EXTM3U at the top).", ""
    return "", body.decode("utf-8", "replace")


def check_playlist(hls_url: str) -> str:
    """Why a playlist URL cannot be downloaded, or "" when it answers."""
    return fetch_playlist(hls_url)[0]


def separate_audio_for(master_text: str, hls_url: str) -> str:
    """The audio playlist a video-only MPEG-TS variant needs, or "".

    The download tool muxes a separate audio rendition into a CMAF recording
    itself and leaves a transport stream silent, so only the TS case needs
    the second fetch — and which case it is takes one read of the variant's
    media playlist. Anything unreadable here is "", which is the recording
    as the tool leaves it.
    """
    if not hls.is_master(master_text):
        return ""
    try:
        variant = hls.pick_variant(hls.parse_master(master_text, hls_url))
        audio_url = hls.separate_audio_url(master_text, hls_url, variant)
        if not audio_url or variant is None:
            return ""
        problem, media_text = fetch_playlist(variant.url)
        if problem:
            return ""
        if hls.container_of(hls.parse_media(media_text, variant.url)) != "ts":
            return ""
        return audio_url
    except Exception:  # noqa: BLE001
        logger.warning("could not tell whether %s has a separate audio rendition", hls_url,
                       exc_info=True)
        return ""


@mcp.tool
def download_hls(job_id: str, hls_url: str) -> dict:
    """Start downloading an HLS (.m3u8) source into the uploads bucket.

    Runs `jobs/hls2mp4` as a Cloud Run Job execution and returns at once: a
    long recording is minutes of download, which does not belong inside a
    request. Poll it with `hls_download_status`.

    The tool writes a progressive MP4 for a CMAF stream and a .ts for an
    MPEG-TS one; which is only known once it has read the playlist, so the
    status call resolves the object rather than this one naming it.

    Only the download. The 1 fps analysis proxy is a Transcoder job started
    with `make_analysis_proxy` once the object is in the bucket — the tool
    can make one itself, but that is a single core decoding the whole
    recording after the download, an hour on a long one.

    Args:
        job_id: Job the source belongs to.
        hls_url: https:// URL of the multivariant or media playlist.
    """
    if not HLS2MP4_JOB:
        return {"status": "error", "error": "HLS2MP4_JOB is not configured."}
    if not UPLOADS_BUCKET:
        return {"status": "error", "error": "UPLOADS_BUCKET is not configured."}
    problem, master_text = fetch_playlist(hls_url)
    if problem:
        return {"status": "error", "error": problem, "job_id": job_id}
    audio_playlist_url = separate_audio_for(master_text, hls_url)
    try:
        # Anything already here is from an attempt that did not finish.
        gcs.delete_prefix(UPLOADS_BUCKET, _hls_source_prefix(job_id))
        env = {
            "EXT_SOURCE_URI": hls_url,
            "AIS_SOURCE_URI": f"gs://{UPLOADS_BUCKET}/{_hls_source_prefix(job_id)}source.mp4",
            "EVENT_ID": "source",
        }
        execution = runjobs.run(HLS2MP4_JOB, env)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not start the HLS download for %s", job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}
    return {"status": "started", "job_id": job_id, "execution": execution,
            # Non-empty when the recording will land silent and needs
            # `mux_audio` once it is in the bucket.
            "audio_playlist_url": audio_playlist_url}


@mcp.tool
def hls_download_status(execution: str, job_id: str) -> dict:
    """Where an HLS download is, and the object it produced once it is done.

    While it runs, carries ``segments_done``/``segments_total``/``fraction``
    read from the execution's own log when that has caught up.

    Args:
        execution: The execution name `download_hls` returned.
        job_id: Job the download belongs to.
    """
    try:
        state = runjobs.execution_state(execution)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "execution": execution}
    if state["state"] != "succeeded":
        result = {"status": state["state"], **state}
        if state["state"] == "running":
            try:
                result.update(runjobs.execution_progress(execution) or {})
            except Exception:  # noqa: BLE001
                # The poll is asking whether to keep waiting; a progress
                # figure it could not read is not a reason to stop.
                logger.warning("could not read progress for %s", execution, exc_info=True)
        return result

    source = None
    for blob in gcs.client().list_blobs(UPLOADS_BUCKET, prefix=f"{_hls_source_prefix(job_id)}source/"):
        if blob.name.endswith((".mp4", ".ts")) and "_proxy_" not in blob.name:
            if source is None or (blob.size or 0) > (source.size or 0):
                source = blob
    if source is None:
        return {"status": "failed", "execution": execution,
                "error": "the download finished but wrote no source object"}

    return {
        "status": "succeeded", "execution": execution, "job_id": job_id,
        "gcs_uri": f"gs://{UPLOADS_BUCKET}/{source.name}",
        "bytes": int(source.size or 0),
        "content_type": source.content_type or ("video/mp2t" if source.name.endswith(".ts") else "video/mp4"),
        "original_name": source.name.rsplit("/", 1)[-1],
    }


def _muxed_uri(job_id: str) -> str:
    return f"gs://{UPLOADS_BUCKET}/{_hls_source_prefix(job_id)}muxed/source.ts"


@mcp.tool
def mux_audio(job_id: str, gcs_uri: str, audio_playlist_url: str) -> dict:
    """Start muxing a separate audio rendition into a silent HLS recording.

    A Cloud Run Job on this image (`media_server.remux`): it fetches the
    audio playlist's segments, then one ffmpeg stream-copies the video from
    the bucket and the audio into a new transport stream written straight
    back to the bucket. Returns at once; poll with `mux_status`. The output
    path is known up front.

    Args:
        job_id: Job the recording belongs to.
        gcs_uri: gs:// URI of the video-only recording.
        audio_playlist_url: The audio rendition's playlist, as `download_hls`
            reported it.
    """
    if not REMUX_JOB:
        return {"status": "error", "error": "REMUX_JOB is not configured."}
    output_uri = _muxed_uri(job_id)
    try:
        gcs.delete_prefix(UPLOADS_BUCKET, f"{_hls_source_prefix(job_id)}muxed/")
        execution = runjobs.run(REMUX_JOB, {
            "JOB_ID": job_id, "VIDEO_URI": gcs_uri,
            "AUDIO_PLAYLIST_URL": audio_playlist_url, "OUTPUT_URI": output_uri,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not start the audio mux for %s", job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}
    return {"status": "started", "job_id": job_id, "execution": execution,
            "output_uri": output_uri}


@mcp.tool
def mux_status(execution: str, output_uri: str, original_uri: str = "") -> dict:
    """Where an audio mux is; once done, the muxed object and its size.

    On success the silent original is deleted, since the muxed recording is
    the source from then on and the two together are twice the bytes.

    Args:
        execution: The execution name `mux_audio` returned.
        output_uri: The output it named.
        original_uri: The video-only recording to remove once replaced.
    """
    try:
        state = runjobs.execution_state(execution)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "execution": execution}
    if state["state"] == "failed":
        return {"status": "failed", **state,
                "error": "the remux execution failed; its log names the segment or the ffmpeg error"}
    if state["state"] != "succeeded":
        return {"status": state["state"], **state}
    size = gcs.object_size(output_uri)
    if not size:
        return {"status": "failed", "execution": execution,
                "error": "the mux finished but wrote no object"}
    if original_uri:
        try:
            gcs.delete_object(original_uri)
        except Exception:  # noqa: BLE001
            logger.warning("could not remove the silent original %s", original_uri, exc_info=True)
    return {"status": "succeeded", "execution": execution, "gcs_uri": output_uri,
            "bytes": size, "content_type": "video/mp2t", "original_name": "source.ts"}


@mcp.tool
def compose_live_source(job_id: str, chunk_uris: list[str]) -> dict:
    """Join a live event's chunks into one object the rest of the pipeline can use.

    A live event has no source video: it has a row of five-minute chunks, and
    every stage after the analysis — packaging for playback, cutting a moment,
    a still from the source rather than from a proxy — wants one file. GCS
    composes them server-side in the bucket they already live in, so no bytes
    pass through here and a twelve-hour event costs a few API calls.

    Safe to call again as the event grows: the destination is rewritten from
    whatever chunks are named now, which is how playback can be prepared
    mid-event and again at the end.

    Args:
        job_id: The live event's job.
        chunk_uris: The chunk objects, in order. Each must be in the media
            bucket, which is where the recorder writes them.
    """
    if not MEDIA_BUCKET:
        return {"status": "error", "error": "MEDIA_BUCKET is not configured."}
    if not chunk_uris:
        return {"status": "error", "error": "no chunks to compose", "job_id": job_id}
    container = "mp4" if str(chunk_uris[0]).endswith(".mp4") else "ts"
    dest = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/live/source.{container}"
    try:
        gcs.compose(list(chunk_uris), dest, "video/mp4" if container == "mp4" else "video/mp2t")
        size = gcs.object_size(dest)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not compose the live source for %s", job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}
    return {"status": "success", "job_id": job_id, "gcs_uri": dest, "bytes": size,
            "chunks": len(chunk_uris),
            "content_type": "video/mp4" if container == "mp4" else "video/mp2t",
            "original_name": f"source.{container}"}


@mcp.tool
def mux_chunk(job_id: str, index: int, video_uri: str, audio_uri: str) -> dict:
    """Mux a live chunk's separate audio into its video, in this request.

    A chunk is five minutes — a hundred and fifty megabytes of video and a
    few of audio — so unlike a whole recording this is seconds of ffmpeg,
    reading both from the bucket and streaming the result back. The tick
    calls it before analysing a chunk that has an `audioUri`.

    Args:
        job_id: The live event's job.
        index: The chunk's index.
        video_uri: gs:// URI of the video chunk.
        audio_uri: gs:// URI of the audio chunk.
    """
    if not MEDIA_BUCKET:
        return {"status": "error", "error": "MEDIA_BUCKET is not configured."}
    from media_server import remux

    output_uri = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/live/chunks/chunk_{int(index):04d}_muxed.ts"
    try:
        written = remux.remux_to_gcs(video_uri, audio_uri, output_uri)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not mux chunk %s of %s", index, job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}
    return {"status": "success", "job_id": job_id, "index": int(index),
            "gcs_uri": output_uri, "bytes": written}


@mcp.tool
def make_analysis_proxy(gcs_uri: str, job_id: str) -> dict:
    """Start the 1 fps 480p proxy the analysis reads, and return immediately.

    A Transcoder job, like the playback package: it reads the source from
    the bucket and writes the proxy to the media bucket itself, so no video
    byte passes through here and a long recording is split across the
    service's own encoders rather than decoded by one core. Poll it with
    `transcode_status`. The proxy's URI is known up front — Transcoder names
    the file from the mux stream key — so the caller can record it as soon as
    the encode succeeds without listing the prefix.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the proxy belongs to; becomes the object prefix.
    """
    if not MEDIA_BUCKET:
        return {"status": "error", "error": "MEDIA_BUCKET is not configured."}
    try:
        # A proxy left by an attempt that did not finish, or by the download
        # job's own encoder in an earlier release.
        gcs.delete_prefix(MEDIA_BUCKET, _proxy_prefix(job_id))
        started = transcoder.create_proxy_job(gcs_uri, MEDIA_BUCKET, job_id,
                                              audio=_source_has_audio(gcs_uri))
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not start a proxy encode for %s", gcs_uri)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}
    return {"status": "started", "job_id": job_id, **started}


@mcp.tool
def start_live_capture(job_id: str, hls_url: str, event_end: str, chunk_sec: int = 0,
                       stall_minutes: float = 0, resume: bool = False) -> dict:
    """Start recording a live HLS stream into fixed-length chunks.

    One Cloud Run Job execution follows the playlist until `event_end` (or the
    stream's own end, or a stall) and records each closed chunk under the job
    in Firestore for the live tick to analyse. Returns at once; poll with
    `live_capture_status`.

    Args:
        job_id: The live event's job.
        hls_url: https:// URL of the live playlist.
        event_end: ISO 8601 time the recording stops.
        chunk_sec: Chunk length in seconds; the deployment default when 0.
        stall_minutes: End the event once a stream that was flowing has
            produced nothing for this long. 0 waits for `event_end`.
        resume: A restart mid-event. Keeps what was recorded: without it the
            first start clears the job's live prefix, which on a restart would
            delete every chunk recorded so far — the analysis already has their
            moments, but the recording the event is played back from is
            composed out of those files.
    """
    if not LIVE_CAPTURE_JOB:
        return {"status": "error", "error": "LIVE_CAPTURE_JOB is not configured."}
    try:
        if not resume:
            gcs.delete_prefix(MEDIA_BUCKET, f"jobs/{job_id}/live/")
            # And the playable stream a previous booking of this job left in
            # the HLS bucket, or a player would open on the last event's video.
            if HLS_BUCKET:
                gcs.delete_prefix(HLS_BUCKET, f"jobs/{job_id}/live/")
        execution = runjobs.run(LIVE_CAPTURE_JOB, {
            "JOB_ID": job_id,
            "HLS_URL": hls_url,
            "EVENT_END": event_end,
            "CHUNK_SEC": str(int(chunk_sec) or LIVE_CHUNK_SECONDS),
            "STALL_MINUTES": str(max(0.0, float(stall_minutes or 0))),
            # Where the recorder writes the event as a stream the CDN can serve,
            # so a moment can be watched while the event is still on.
            "HLS_BUCKET": HLS_BUCKET,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not start the live capture for %s", job_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}
    return {"status": "started", "job_id": job_id, "execution": execution,
            "chunk_sec": int(chunk_sec) or LIVE_CHUNK_SECONDS}


@mcp.tool
def live_capture_status(execution: str) -> dict:
    """Whether a live capture execution is still running.

    Args:
        execution: The execution name `start_live_capture` returned.
    """
    try:
        state = runjobs.execution_state(runjobs.qualify(execution, LIVE_CAPTURE_JOB))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "execution": execution}
    return {"status": state["state"], **state}


@mcp.tool
def cancel_live_capture(execution: str) -> dict:
    """Stop a live capture early. Chunks already closed stay where they are.

    Args:
        execution: The execution name `start_live_capture` returned.
    """
    try:
        runjobs.cancel(runjobs.qualify(execution, LIVE_CAPTURE_JOB))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "execution": execution}
    return {"status": "cancelled", "execution": execution}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp-media"})


def main() -> None:
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        path="/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
