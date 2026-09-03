"""mcp-media — the media tool server.

Runs as a private Cloud Run service. Every tool takes and returns GCS URIs;
nothing is streamed through the MCP transport, because a match is gigabytes and
a tool result is not.

Two kinds of work live here and they run in different places. Packaging a match
for playback is a Transcoder API job: it reads the source from GCS and writes
the HLS package to GCS without a byte passing through this container, which is
what makes a real 480p encode possible at all. Everything else — probing an
upload to decide whether it is a video, one poster frame, the short per-clip
cuts and reframes an editor drives — is still ffmpeg here, because each reads a
few megabytes over a range request and finishes in seconds.
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

from media_server import ffmpeg_ops, gcs, transcoder

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
        started = transcoder.create_preview_job(gcs_uri, HLS_BUCKET, job_id)
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
