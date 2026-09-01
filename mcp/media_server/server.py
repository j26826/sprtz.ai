"""mcp-media — the ffmpeg tool server.

Runs as a private Cloud Run service. Every tool takes and returns GCS URIs;
nothing is streamed through the MCP transport, because a match is gigabytes and
a tool result is not.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from media_server import ffmpeg_ops, gcs

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mcp-media")

UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")
MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
HLS_BUCKET = os.environ.get("HLS_BUCKET", "")
CDN_BASE_URL = os.environ.get("CDN_BASE_URL", "").rstrip("/")
# Cloud Run's writable filesystem is memory-backed, so scratch is only ever
# used for small artefacts: playlists in flight, thumbnails, rendered clips.
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


@mcp.tool
def probe_media(gcs_uri: str) -> dict:
    """Read a video's duration, resolution, frame rate and codecs.

    Tries the file header alone first, which avoids pulling gigabytes across the
    wire for a faststart MP4, and falls back to the whole file if that is not
    enough to determine duration.

    Args:
        gcs_uri: gs:// URI of the video.
    """
    work = _scratch()
    try:
        head = work / "head.mp4"
        try:
            gcs.download_range(gcs_uri, head, _HEADER_BYTES)
            info = ffmpeg_ops.probe(head)
            if info["duration_sec"] > 0:
                return {"status": "success", "gcs_uri": gcs_uri, "partial_read": True, **info}
        except Exception as exc:  # noqa: BLE001
            logger.info("header probe failed (%s); probing over HTTPS", exc)

        # Non-faststart file: probe it in place over HTTPS. ffprobe range-reads
        # the moov atom from the tail; the object never lands on local disk.
        info = ffmpeg_ops.probe(gcs.https_url(gcs_uri), bearer_token=gcs.bearer_token())
        return {"status": "success", "gcs_uri": gcs_uri, "partial_read": True, **info}
    except Exception as exc:  # noqa: BLE001
        logger.exception("probe_media failed for %s", gcs_uri)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "gcs_uri": gcs_uri}
    finally:
        _cleanup(work)


@mcp.tool
def transcode_hls(gcs_uri: str, job_id: str) -> dict:
    """Transcode a source video into an HLS package for CDN playback.

    Produces a multi-rendition ladder with a master playlist and uploads it to
    the HLS bucket under the job's prefix. The returned `playback_url` is the
    Cloud CDN URL the editor plays; seeking to a moment is a seek within this one
    stream, so no per-clip render is needed to review a suggestion.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the package belongs to; becomes the object prefix.
    """
    if not HLS_BUCKET:
        return {"status": "error", "error": "HLS_BUCKET is not configured."}

    work = _scratch()
    try:
        token = gcs.bearer_token()
        source_url = gcs.https_url(gcs_uri)
        info = ffmpeg_ops.probe(source_url, bearer_token=token)

        # Copy-remux streamed in over HTTPS and drained out segment by segment,
        # so neither the source nor the package is ever held locally — local
        # disk here is instance memory, and a full match is bigger than any
        # instance this service runs on.
        out_dir = work / "hls"
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"jobs/{job_id}/hls"
        with gcs.SegmentUploader(out_dir, HLS_BUCKET, prefix) as uploader:
            package = ffmpeg_ops.remux_hls(source_url, out_dir, bearer_token=token)
            uploaded = uploader.finish()

        poster_rel = f"jobs/{job_id}/poster.jpg"
        poster = work / "poster.jpg"
        ffmpeg_ops.thumbnail(source_url, poster,
                             at_sec=min(30.0, info["duration_sec"] / 2),
                             bearer_token=token)
        gcs.upload(
            poster,
            f"gs://{HLS_BUCKET}/{poster_rel}",
            cache_control="public, max-age=86400",
        )

        master_path = f"{prefix}/{package['master_playlist']}"
        return {
            "status": "success",
            "job_id": job_id,
            "playback_url": f"{CDN_BASE_URL}/{master_path}" if CDN_BASE_URL else "",
            "poster_url": f"{CDN_BASE_URL}/{poster_rel}" if CDN_BASE_URL else "",
            "master_playlist_uri": f"gs://{HLS_BUCKET}/{master_path}",
            "renditions": package["renditions"],
            "segment_seconds": package["segment_seconds"],
            "duration_sec": info["duration_sec"],
            **uploaded,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("transcode_hls failed for %s", gcs_uri)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job_id": job_id}
    finally:
        _cleanup(work)


@mcp.tool
def cut_clip(gcs_uri: str, job_id: str, clip_id: str, start_sec: float, end_sec: float) -> dict:
    """Render one clip out of the source video as a standalone MP4.

    Only needed for export — reviewing a suggestion in the editor seeks the HLS
    stream instead.

    Args:
        gcs_uri: gs:// URI of the source video.
        job_id: Job the clip belongs to.
        clip_id: Identifier for the clip.
        start_sec: In point in seconds.
        end_sec: Out point in seconds.
    """
    if end_sec <= start_sec:
        return {"status": "error", "error": "end_sec must be greater than start_sec."}

    work = _scratch()
    try:
        out = work / f"{clip_id}.mp4"
        # -ss over HTTPS is a range seek: a 30-second clip out of a three-hour
        # match reads megabytes, not the whole object.
        ffmpeg_ops.cut(gcs.https_url(gcs_uri), out, start_sec, end_sec,
                       bearer_token=gcs.bearer_token())

        dest = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/clips/{clip_id}.mp4"
        gcs.upload(out, dest)

        thumb = work / f"{clip_id}.jpg"
        ffmpeg_ops.thumbnail(out, thumb, at_sec=min(1.0, (end_sec - start_sec) / 2))
        thumb_uri = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/clips/{clip_id}.jpg"
        gcs.upload(thumb, thumb_uri)

        return {
            "status": "success",
            "clip_id": clip_id,
            "output_uri": dest,
            "thumbnail_uri": thumb_uri,
            "duration_sec": round(end_sec - start_sec, 2),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("cut_clip failed for %s", clip_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "clip_id": clip_id}
    finally:
        _cleanup(work)


@mcp.tool
def reframe_vertical(clip_uri: str, job_id: str, clip_id: str, aspect: str) -> dict:
    """Reframe a rendered clip to a vertical aspect for short-form publishing.

    Args:
        clip_uri: gs:// URI of the rendered clip.
        job_id: Job the clip belongs to.
        clip_id: Identifier for the clip.
        aspect: Target aspect, "9:16" or "1:1".
    """
    if aspect not in {"9:16", "1:1"}:
        return {"status": "error", "error": "aspect must be '9:16' or '1:1'."}

    work = _scratch()
    try:
        source = work / "clip.mp4"
        gcs.download(clip_uri, source)
        out = work / f"{clip_id}_vertical.mp4"
        ffmpeg_ops.reframe(source, out, aspect=aspect)
        dest = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/clips/{clip_id}_{aspect.replace(':', 'x')}.mp4"
        gcs.upload(out, dest)
        return {"status": "success", "clip_id": clip_id, "output_uri": dest, "aspect": aspect}
    except Exception as exc:  # noqa: BLE001
        logger.exception("reframe_vertical failed for %s", clip_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "clip_id": clip_id}
    finally:
        _cleanup(work)


@mcp.tool
def burn_captions(clip_uri: str, job_id: str, clip_id: str, hook_text: str) -> dict:
    """Burn the on-screen hook text over the opening of a clip.

    Args:
        clip_uri: gs:// URI of the rendered clip.
        job_id: Job the clip belongs to.
        clip_id: Identifier for the clip.
        hook_text: Text to burn in.
    """
    work = _scratch()
    try:
        source = work / "clip.mp4"
        gcs.download(clip_uri, source)
        out = work / f"{clip_id}_captioned.mp4"
        ffmpeg_ops.burn_text(source, out, hook_text)
        dest = f"gs://{MEDIA_BUCKET}/jobs/{job_id}/clips/{clip_id}_captioned.mp4"
        gcs.upload(out, dest)
        return {"status": "success", "clip_id": clip_id, "output_uri": dest}
    except Exception as exc:  # noqa: BLE001
        logger.exception("burn_captions failed for %s", clip_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "clip_id": clip_id}
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
