"""Coarse-grained tools the agents call.

Each one wraps a whole stage of the analysis so the model orchestrates the run
without the match's data passing through its context. A 90-minute handball match
yields a few hundred moments carrying 768-dimension embeddings; that belongs in
Firestore, not in a prompt.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any

from google.adk.tools import ToolContext

from sprtz_agents.config import get_settings
from sprtz_agents.schemas import GameDetails, Moment, format_timecode
from sprtz_agents.sports import get_profile, list_sports
from sprtz_agents.tools import equipe, game_summary, grounding, mcp_client
from sprtz_agents.tools import rides as rides_tool
from sprtz_agents.tools.analysis import (
    analyse_segments,
    apply_team_names,
    plan_segments,
    resolve_team_names,
)

logger = logging.getLogger(__name__)


async def _emit(job_id: str, stage: str, message: str, level: str = "info", **data: Any) -> None:
    """Append to the job's event feed. The editor UI streams this live.

    Never allowed to fail the stage it is reporting on.
    """
    try:
        await mcp_client.call_tool(
            "catalog",
            "emit_event",
            {
                "job_id": job_id,
                "stage": stage,
                "level": level,
                "message": message,
                "data": data or {},
            },
        )
    except Exception:
        logger.warning("could not emit event for job %s: %s", job_id, message, exc_info=True)


# What fraction of a run each stage accounts for, so one bar can mean something
# across stages of wildly different length. These are wall-clock shares on a
# full match, not equal slices: analysis is an hour of Gemini calls and
# everything else is minutes, so a bar that gave each stage a fifth would sit at
# 40% for an hour and then jump.
STAGE_SPANS: dict[str, tuple[int, int]] = {
    # Ingest is a probe for an upload, and a download plus a proxy encode for
    # an HLS source — a quarter of an hour that the bar has to be seen moving
    # through, or it reads as a dead run. Transcode's band is mostly notional:
    # it runs beside the analysis and finishes inside its own slice.
    "ingest": (0, 10),
    "transcode": (10, 20),
    "analysis": (20, 80),
    # Finishing a run is a read and a status write, so its band is wide only
    # because the bar has to arrive at 100 somewhere. It used to be the clip
    # and caption stages' 80-100; clip generation is gone and the band stayed
    # rather than letting the analysis claim a share of the bar it does not
    # spend.
    "finalize": (80, 100),
}
STAGE_ORDER = tuple(STAGE_SPANS)


def stage_progress(stage_name: str, fraction: float = 1.0) -> int:
    """Overall percent when a stage is ``fraction`` of the way through itself."""
    start, end = STAGE_SPANS.get(stage_name, (0, 0))
    return round(start + (end - start) * max(0.0, min(1.0, fraction)))


async def _progress(job_id: str, stage_name: str, fraction: float = 1.0,
                    status: str = "") -> None:
    """Move the job's progress bar. Never fails the work it is reporting on."""
    patch = {
        "job_id": job_id,
        "stage": stage_name,
        "progress": stage_progress(stage_name, fraction),
    }
    try:
        # An empty status means "leave it"; the catalog treats it that way.
        await mcp_client.call_tool("catalog", "update_job_status", {
            **patch, "status": status,
        })
    except Exception:
        logger.warning("could not report progress for %s", job_id, exc_info=True)


# What a run reads as when it is over and the stages after it have nothing
# to do: an error the editor has to see, or a stop the editor asked for.
_STOPPED_STATUSES = ("failed", "cancelled", "cancelling")


def stage(name: str, skip_if_failed: bool = False):
    """Mark the job failed if a stage raises, instead of leaving it running.

    A stage that dies takes its progress reporting with it, so the job keeps the
    status it had and reads as still working for ever — which is what a
    container going down mid-response looks like from here. Recording the
    failure is what turns that into something the editor can see and retry.

    ``skip_if_failed`` is for the stages that only make sense after the ones
    before them: the pipeline is a sequence of agents, and a stage that
    returned an error does not stop the next one being asked. When the
    download failed, the analysis then found nothing, the finish wrote "the
    analysis produced no moments" over the real reason, and the editor was
    told to re-run a job whose link had expired. Ingest is left out — a
    re-run starts there on a job that is failed by definition.

    ``cancelled`` counts the same way, and for a sharper reason: cancelling
    is what an editor does to a run they want stopped, and the stages after
    the cancelled one carried on and marked the job *failed* with "the
    analysis produced no moments" — the one thing cancelling promises not to
    do is report the run as broken.
    """
    def decorate(func):
        @functools.wraps(func)
        async def run(*args, **kwargs):
            job_id = kwargs.get("job_id") or (args[0] if args else "")
            if skip_if_failed and job_id:
                job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
                if job.get("status") in _STOPPED_STATUSES:
                    logger.info("stage %s skipped: job %s is %s",
                                name, job_id, job.get("status"))
                    return {"status": "skipped", "job_id": job_id,
                            "job_status": job.get("status"),
                            "error": job.get("error")
                            or f"the run was {job.get('status')} before this stage"}
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                logger.exception("stage %s failed for job %s", name, job_id)
                if job_id:
                    await _emit(job_id, name, f"Stage failed: {detail}", level="error")
                    try:
                        await mcp_client.call_tool(
                            "catalog", "update_job_status",
                            {"job_id": job_id, "status": "failed",
                             "stage": name, "error": detail},
                        )
                    except Exception:
                        # Reporting the failure failed too; the log is all that
                        # is left, so do not lose the original either.
                        logger.exception("could not record the failure of job %s", job_id)
                return {"status": "error", "job_id": job_id, "error": detail}

        return run

    return decorate


@stage("ingest")
async def inspect_source(job_id: str, tool_context: ToolContext) -> dict:
    """Probe the uploaded video and work out how it will be segmented.

    Reads the job's source URI, measures duration, resolution and frame rate,
    and returns the analysis plan without starting the analysis.

    Args:
        job_id: Identifier of the job to inspect.

    Returns:
        dict with the media properties and the planned segment windows.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    gcs_uri = (job.get("source") or {}).get("gcsUri")
    if not gcs_uri and job.get("kind") == "hls" and job.get("hlsUrl"):
        # An HLS job is registered with a URL and no object. The download is
        # the first thing ingest does, and once it has an object the job is
        # an upload like any other.
        fetched = await _download_hls_source(job_id, job["hlsUrl"])
        if fetched.get("status") != "success":
            return fetched
        gcs_uri = fetched["gcs_uri"]
        job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    if not gcs_uri:
        return {"status": "error", "error": f"Job {job_id} has no source video."}

    await _progress(job_id, "ingest", 0.2, status="analyzing")
    await _emit(job_id, "ingest", "Checking the upload is a video we can process.")

    # Validated against the bytes, not the filename or the content type the
    # browser sent — both of those are supplied by whoever uploaded the file.
    check = await mcp_client.call_tool(
        "media",
        "validate_media",
        {
            "gcs_uri": gcs_uri,
            "declared_content_type": (job.get("source") or {}).get("contentType", ""),
        },
    )
    if check.get("status") != "accepted":
        reasons = check.get("reasons") or ["the file could not be read as media"]
        detail = "; ".join(reasons)
        await _emit(job_id, "ingest", f"Rejected the upload: {detail}", level="error")
        await mcp_client.call_tool(
            "catalog",
            "update_job_status",
            {"job_id": job_id, "status": "rejected", "stage": "ingest", "error": detail},
        )
        return {"status": "rejected", "job_id": job_id, "reasons": reasons}

    probe = check.get("media") or {}
    duration = float(probe.get("duration_sec") or 0.0)
    segments = plan_segments(duration)

    await mcp_client.call_tool(
        "catalog",
        "record_media_info",
        {"job_id": job_id, "media": probe, "segment_count": len(segments)},
    )
    await _emit(
        job_id,
        "ingest",
        f"{duration / 60:.0f} minutes of video, split into {len(segments)} segments.",
        duration_sec=duration,
        segments=len(segments),
    )

    await _progress(job_id, "ingest", 1.0)

    tool_context.state["job_id"] = job_id
    tool_context.state["gcs_uri"] = gcs_uri
    tool_context.state["duration_sec"] = duration

    return {
        "status": "success",
        "job_id": job_id,
        # Returned so the stage that reports it can say it, and so the stages
        # after it inherit the fact rather than asking for it. The analysis
        # stage once stopped and asked which sport this was, in a pipeline with
        # nobody to answer.
        "sport": job.get("sport", ""),
        "gcs_uri": gcs_uri,
        "media": probe,
        "segment_count": len(segments),
        "segments": [s.model_dump() for s in segments],
    }


# The download's share of the ingest band, as fractions of the stage: it
# starts here and ends here, and the proxy encode and the probe take the rest.
# The opening fraction is a tenth rather than a twentieth so the bar shows a
# point at once: 0.05 of a ten-point band rounds to zero, and a bar at zero
# for the three minutes the download job takes to start reads as nothing
# running.
_DOWNLOAD_BAND = (0.1, 0.5)


# How many polls in a row may fail to reach the media service before a wait
# gives up. A poll is a question about work happening elsewhere — on a Cloud
# Run Job or on Transcoder — so the service being unreachable for a while
# says nothing about that work.
_MAX_UNREACHABLE_POLLS = 10


async def _poll_tool(server: str, tool: str, args: dict) -> dict:
    """A status call that reports the service being unreachable as a status.

    ``{"status": "unreachable"}`` rather than an exception, so a wait loop can
    keep waiting: the first playback wait to meet a retired media instance
    took the exception as a dead encode and marked the job failed with
    "ConnectError: " while Transcoder carried on for another half hour.
    """
    try:
        return await mcp_client.call_tool(server, tool, args)
    except Exception as exc:  # noqa: BLE001
        logger.warning("poll %s.%s failed: %s", server, tool, exc)
        return {"status": "unreachable", "error": f"{type(exc).__name__}: {exc}"}


async def _download_hls_source(job_id: str, hls_url: str) -> dict:
    """Fetch an HLS playlist into the uploads bucket and put it on the job.

    Runs `jobs/hls2mp4` as a Cloud Run Job through the media server and waits
    on it here, widening the poll as it goes — the same shape as waiting on a
    Transcoder encode. Once the object is in the bucket the 1 fps 480p proxy
    is made from it by a Transcoder job (`_make_analysis_proxy`), which is
    what the analysis reads: the same picture Gemini samples anyway, at a
    fraction of the bytes, so a long recording no longer needs to be cut to
    fit under the fetch limit.
    """
    settings = get_settings()
    await _progress(job_id, "ingest", _DOWNLOAD_BAND[0], status="analyzing")
    await _emit(job_id, "ingest", "Downloading the HLS stream into storage.", hls_url=hls_url)

    async def fail(reason: str) -> dict:
        await mcp_client.call_tool("catalog", "update_job_status", {
            "job_id": job_id, "status": "failed", "stage": "ingest", "error": reason})
        await _emit(job_id, "ingest", reason, level="error")
        return {"status": "error", "job_id": job_id, "error": reason}

    started = await mcp_client.call_tool(
        "media", "download_hls", {"job_id": job_id, "hls_url": hls_url})
    if started.get("status") != "started":
        return await fail(
            f"The HLS download could not be started: {started.get('error', 'unknown error')}")

    execution = started.get("execution", "")
    await _emit(job_id, "ingest",
                "The download job is starting; the first segments arrive once its container "
                "is up, about three minutes.")
    deadline = time.monotonic() + settings.hls_download_timeout_seconds
    last_note = time.monotonic()
    interval = 15.0
    quarters_noted = 0
    unreachable = 0
    while True:
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 60.0)
        probe = await _poll_tool(
            "media", "hls_download_status", {"execution": execution, "job_id": job_id})
        state = probe.get("status")
        if state == "unreachable":
            unreachable += 1
            if unreachable > _MAX_UNREACHABLE_POLLS:
                return await fail(f"The media service could not be reached: {probe.get('error')}")
            continue
        unreachable = 0
        if state == "succeeded":
            break
        if state in ("failed", "error"):
            return await fail(
                f"The HLS download failed: {probe.get('error') or 'the job did not succeed'}")
        if time.monotonic() > deadline:
            return await fail("The HLS download did not finish in time.")
        fraction = float(probe.get("fraction") or 0.0)
        if fraction:
            # The download's own count, from the job's log: it streams into
            # one object that appears only when it is done, so this is the
            # only thing there is to show. The download owns the first half
            # of the ingest band; the proxy takes most of the rest.
            await _progress(job_id, "ingest", _DOWNLOAD_BAND[0]
                            + (_DOWNLOAD_BAND[1] - _DOWNLOAD_BAND[0]) * fraction)
            crossed = min(3, int(fraction * 4))
            if crossed > quarters_noted:
                # One note per poll, however many quarter marks it crossed —
                # a poll at 53% wrote the same count twice.
                quarters_noted = crossed
                await _emit(job_id, "ingest",
                            f"Downloaded {probe.get('segments_done')} of "
                            f"{probe.get('segments_total')} segments.")
        if time.monotonic() - last_note > 300:
            await _emit(job_id, "ingest", "Still downloading the stream.")
            # An event is not a heartbeat: the watchdog reads the job's own
            # `updatedAt`, and a download that wrote nothing there for fifteen
            # minutes was restarted mid-download, by a tick that could not
            # tell it from a dead run. Same fraction again — progress only
            # goes forward, so this moves the clock and not the bar.
            await _progress(job_id, "ingest", _DOWNLOAD_BAND[0])
            last_note = time.monotonic()

    gcs_uri = probe.get("gcs_uri", "")
    # The object goes on the job before the proxy is attempted: a proxy that
    # fails leaves an upload like any other, which the analysis can still cut
    # into windows, and a restart finds the download already done.
    await mcp_client.call_tool("catalog", "set_source", {
        "job_id": job_id,
        "gcs_uri": gcs_uri,
        "original_name": probe.get("original_name", ""),
        "size_bytes": int(probe.get("bytes") or 0),
        "content_type": probe.get("content_type", ""),
    })
    size_gb = int(probe.get("bytes") or 0) / 1e9
    await _emit(job_id, "ingest",
                f"Downloaded {size_gb:.2f} GB as {probe.get('original_name') or 'the source'}.",
                bytes=int(probe.get("bytes") or 0))
    await _progress(job_id, "ingest", _DOWNLOAD_BAND[1])

    if started.get("audio_playlist_url"):
        muxed = await _mux_audio(job_id, gcs_uri, started["audio_playlist_url"], deadline)
        if muxed:
            gcs_uri = muxed["gcs_uri"]
            await mcp_client.call_tool("catalog", "set_source", {
                "job_id": job_id, "gcs_uri": gcs_uri,
                "size_bytes": int(muxed.get("bytes") or 0),
                "content_type": muxed.get("content_type", ""),
            })

    analysis_uri = await _make_analysis_proxy(job_id, gcs_uri)
    return {"status": "success", "job_id": job_id, "gcs_uri": gcs_uri,
            "analysis_uri": analysis_uri}


async def _mux_audio(job_id: str, gcs_uri: str, audio_playlist_url: str,
                     deadline: float) -> dict | None:
    """Mux a separate audio rendition into a silent recording, on a job.

    Returns the muxed object, or None when the recording stays as it was.
    Silent is a warning rather than a failure — the analysis, the preview and
    the stills all still work, without sound — and the feed says so.
    """
    await _emit(job_id, "ingest", "Adding the audio rendition to the recording.")
    started = await mcp_client.call_tool("media", "mux_audio", {
        "job_id": job_id, "gcs_uri": gcs_uri, "audio_playlist_url": audio_playlist_url})
    if started.get("status") != "started":
        await _emit(job_id, "ingest",
                    "The audio could not be added; the recording stays silent "
                    f"({started.get('error', 'unknown error')}).", level="warning")
        return None

    execution = started.get("execution", "")
    interval = 15.0
    since_heartbeat = 0.0
    unreachable = 0
    while True:
        await asyncio.sleep(interval)
        since_heartbeat += interval
        interval = min(interval * 1.5, 60.0)
        probe = await _poll_tool("media", "mux_status", {
            "execution": execution, "output_uri": started.get("output_uri", ""),
            "original_uri": gcs_uri})
        state = probe.get("status")
        if state == "unreachable" and unreachable < _MAX_UNREACHABLE_POLLS:
            unreachable += 1
            continue
        unreachable = 0
        if state == "succeeded":
            await _emit(job_id, "ingest", "Added the audio rendition to the recording.",
                        bytes=int(probe.get("bytes") or 0))
            return probe
        if state in ("failed", "error") or time.monotonic() > deadline:
            await _emit(job_id, "ingest",
                        "The audio could not be added; the recording stays silent "
                        f"({probe.get('error') or 'the mux did not finish'}).", level="warning")
            return None
        if since_heartbeat >= _HEARTBEAT_SECONDS:
            await _progress(job_id, "ingest", _DOWNLOAD_BAND[1])
            since_heartbeat = 0.0


async def _make_analysis_proxy(job_id: str, gcs_uri: str) -> str:
    """Make the 1 fps proxy on Transcoder and record it on the job.

    Returns the proxy's URI, or "" when there is none. Not having one is a
    warning rather than a failure: the analysis falls back to cutting the
    source into windows, which is how every uploaded match is read, so the
    run goes on — slower and at the source's own size, and the feed says so.
    """
    await _emit(job_id, "ingest", "Making the 1 fps copy the analysis reads.")
    started = await mcp_client.call_tool(
        "media", "make_analysis_proxy", {"gcs_uri": gcs_uri, "job_id": job_id})
    if started.get("status") != "started":
        await _emit(job_id, "ingest",
                    "The 1 fps copy could not be started; the analysis will cut the "
                    f"source into windows instead ({started.get('error', 'unknown error')}).",
                    level="warning")
        return ""

    outcome = await _await_transcode(job_id, started["transcoder_job"],
                                     stage="ingest", what="1 fps copy",
                                     heartbeat=("ingest", _DOWNLOAD_BAND[1]))
    if not outcome.get("succeeded"):
        await _emit(job_id, "ingest",
                    "The 1 fps copy failed; the analysis will cut the source into "
                    f"windows instead ({outcome.get('error') or outcome.get('state', 'unknown')}).",
                    level="warning")
        return ""

    analysis_uri = started.get("analysis_uri", "")
    await mcp_client.call_tool("catalog", "set_source", {
        "job_id": job_id, "gcs_uri": gcs_uri, "analysis_uri": analysis_uri})
    await _emit(job_id, "ingest", "Made the 1 fps copy the analysis reads.",
                analysis_uri=analysis_uri)
    await _progress(job_id, "ingest", 0.9)
    return analysis_uri


async def _compose_live_source(job_id: str) -> str:
    """Join a live event's captured chunks into one object, and put it on the job.

    Returns the composed object's URI, or "" when there is nothing to join
    yet. Every chunk the recorder has closed goes in, in index order — a
    chunk still being analysed is already a complete file.
    """
    listing = await mcp_client.call_tool("catalog", "list_live_chunks", {"job_id": job_id})
    chunks = sorted((listing.get("chunks") or []), key=lambda c: int(c.get("index", 0)))
    uris = [c.get("muxedUri") or c.get("gcsUri") for c in chunks
            if c.get("muxedUri") or c.get("gcsUri")]
    if not uris:
        return ""

    await _emit(job_id, "playback",
                f"Joining {len(uris)} captured chunk(s) into one recording to package.")
    composed = await mcp_client.call_tool(
        "media", "compose_live_source", {"job_id": job_id, "chunk_uris": uris})
    if composed.get("status") != "success":
        await _emit(job_id, "playback",
                    f"The chunks could not be joined: {composed.get('error', 'unknown error')}",
                    level="error")
        return ""

    await mcp_client.call_tool("catalog", "set_source", {
        "job_id": job_id,
        "gcs_uri": composed["gcs_uri"],
        "original_name": composed.get("original_name", ""),
        "size_bytes": int(composed.get("bytes") or 0),
        "content_type": composed.get("content_type", ""),
    })
    return composed["gcs_uri"]


@stage("playback")
async def prepare_playback(job_id: str, tool_context: ToolContext) -> dict:
    """Encode the uploaded video to a 480p HLS preview behind the CDN.

    This is what the editor actually plays. Reviewing a key moment is a seek
    within this one stream, so nothing has to be rendered to watch a
    moment — and 480p is enough to judge one. Independent of the analysis,
    so the two run concurrently.

    The encode runs on Transcoder API and takes minutes on a full match, so this
    starts it and waits, reporting each state change into the job's feed.

    Args:
        job_id: Identifier of the job to prepare.

    Returns:
        dict with the CDN playback URL and the poster frame.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    gcs_uri = (job.get("source") or {}).get("gcsUri")
    if not gcs_uri and job.get("kind") == "live":
        # A live event has no source video, it has a row of chunks. Joining
        # them is a server-side compose in the bucket they are already in, so
        # it costs a few API calls whatever the event's length — and it can be
        # asked for again as the event grows, which is how a twelve-hour
        # broadcast is watchable before it ends.
        gcs_uri = await _compose_live_source(job_id)
    if not gcs_uri:
        return {"status": "error", "error": f"Job {job_id} has no source video."}

    # A recorded playback URL is a claim, not a package. A delete that removed
    # the media and then failed leaves the record pointing at objects that are
    # gone, and short-circuiting on the record alone made that unrecoverable:
    # the editor was told playback was ready while the CDN returned 403, and
    # asking for it again did nothing.
    # What the job was before this started. Packaging is a stage of a run and
    # also a button an editor presses on a match that finished days ago, and on
    # that second path "transcoding" is a lie the job would keep telling: the
    # status is restored at the end. The LeMieux event sat on it overnight —
    # complete, played back, and reading as a run in progress.
    was = job.get("status") or ""
    finished_before = was in ("ready", "complete", "clips_ready", "needs_attention",
                              "failed", "cancelled")

    existing = job.get("playback") or {}
    if existing.get("hlsUrl"):
        check = await mcp_client.call_tool("media", "playback_ready", {"job_id": job_id})
        if check.get("ready"):
            return {"status": "success", "job_id": job_id, "already_prepared": True, **existing}
        await _emit(
            job_id, "transcode",
            "The recorded playback package is missing from the bucket; encoding it again.",
            level="warning",
        )

    await _progress(job_id, "transcode", 0.1, status="transcoding")
    await _emit(job_id, "transcode", "Starting a 480p preview encode for playback.")

    started = await mcp_client.call_tool(
        "media", "transcode_hls", {"gcs_uri": gcs_uri, "job_id": job_id}
    )
    if started.get("status") != "started":
        await _emit(
            job_id, "transcode", "Could not start the preview encode.",
            level="error", detail=started.get("error"),
        )
        # Playback is how the editor reviews a moment, but the analysis is
        # still worth having, so this failure does not fail the job — and a
        # match that was already finished goes back to being finished.
        if finished_before:
            await mcp_client.call_tool("catalog", "update_job_status", {
                "job_id": job_id, "status": was, "stage": "complete", "progress": 100})
        return {"status": "error", "job_id": job_id, "error": started.get("error")}

    # The poster comes from one range-read frame, so it is ready long before the
    # encode and gives the editor something to look at meanwhile.
    poster = await mcp_client.call_tool(
        "media", "generate_poster", {"gcs_uri": gcs_uri, "job_id": job_id}
    )

    outcome = await _await_transcode(job_id, started["transcoder_job"])
    if not outcome.get("succeeded"):
        await _emit(
            job_id, "transcode", "The preview encode did not finish.",
            level="error", detail=outcome.get("error") or outcome.get("state"),
        )
        if finished_before:
            await mcp_client.call_tool("catalog", "update_job_status", {
                "job_id": job_id, "status": was, "stage": "complete", "progress": 100})
        return {
            "status": "error", "job_id": job_id,
            "error": outcome.get("error") or f"encode ended in {outcome.get('state')}",
        }

    await mcp_client.call_tool(
        "catalog",
        "record_playback",
        {
            "job_id": job_id,
            "playback_url": started.get("playback_url", ""),
            "poster_url": poster.get("poster_url", ""),
            "renditions": started.get("renditions", []),
            "segment_seconds": started.get("segment_seconds", 6),
        },
    )
    await _progress(job_id, "transcode", 1.0,
                    status=was if finished_before else "")
    if finished_before:
        # ...and the stage it was on, or the strip shows a finished match
        # sitting in Playback for ever.
        await mcp_client.call_tool("catalog", "update_job_status", {
            "job_id": job_id, "status": was, "stage": "complete", "progress": 100})
    await _emit(job_id, "transcode", "Playback ready at 480p.",
                renditions=started.get("renditions", []))

    tool_context.state["playback_url"] = started.get("playback_url", "")

    return {
        "status": "success",
        "job_id": job_id,
        "playback_url": started.get("playback_url"),
        "poster_url": poster.get("poster_url"),
        "renditions": started.get("renditions", []),
        "segment_seconds": started.get("segment_seconds"),
    }


# Transcoder charges by encode, so polling is cheap next to the work it watches.
# The interval opens out because a match-length encode takes minutes, and a
# tight poll on it is just requests spent asking the same question.
_POLL_FIRST_SECONDS = 10
_POLL_MAX_SECONDS = 60
# Four hours, not two. An eight-hour recording is a real input here, and giving
# up on an encode that is still running reports a failure for a job that then
# turns out to have a package — `playback_ready` finds it later and the editor
# has been told the wrong thing in between.
_POLL_CEILING_SECONDS = 4 * 60 * 60


# How often a wait on Transcoder touches the job while nothing changes. The
# watchdog restarts a running job whose `updatedAt` is fifteen minutes old,
# and an encode reports nothing between "running" and "succeeded".
_HEARTBEAT_SECONDS = 300


async def _await_transcode(job_id: str, transcoder_job: str, stage: str = "transcode",
                           what: str = "Preview encode",
                           heartbeat: tuple[str, float] = ("transcode", 0.1)) -> dict:
    """Wait for a Transcoder job, reporting each state change into the job's feed.

    ``heartbeat`` is the (stage, fraction) re-reported every few minutes so
    the job's clock moves while the encode runs — the same fraction the
    caller last reported, since progress only goes forward.
    """
    waited = 0.0
    since_heartbeat = 0.0
    interval = float(_POLL_FIRST_SECONDS)
    last_state = ""

    while waited < _POLL_CEILING_SECONDS:
        await asyncio.sleep(interval)
        waited += interval
        since_heartbeat += interval
        interval = min(interval * 1.5, _POLL_MAX_SECONDS)
        if since_heartbeat >= _HEARTBEAT_SECONDS:
            await _progress(job_id, *heartbeat)
            since_heartbeat = 0.0

        status = await _poll_tool(
            "media", "transcode_status", {"transcoder_job": transcoder_job}
        )
        if status.get("status") in ("error", "unreachable"):
            # A failed poll is not a failed encode; the job may well still be
            # running. Keep waiting rather than declaring it dead.
            logger.warning("could not poll %s: %s", transcoder_job, status.get("error"))
            continue

        state = status.get("state", "")
        if state != last_state:
            last_state = state
            await _emit(job_id, stage, f"{what} {state.lower()}.")
        if status.get("done"):
            return status

    return {
        "done": False,
        "succeeded": False,
        "state": last_state or "UNKNOWN",
        "error": f"gave up watching the encode after {_POLL_CEILING_SECONDS // 3600}h",
    }


@stage("analysis", skip_if_failed=True)
async def analyze_match(job_id: str, tool_context: ToolContext, sport: str = "") -> dict:
    """Analyse the whole match and save the key moments it finds.

    Splits the video into segments, sends each to Gemini concurrently, merges the
    per-segment results into one timeline, embeds every moment for semantic
    search, and writes them to Firestore. Safe to call once per job.

    Args:
        job_id: Identifier of the job to analyse.
        sport: Leave this empty. The sport is recorded on the job at upload and
            is read from there; it is only accepted at all so an older caller
            still works.

    Returns:
        dict summarising how many moments were found and the strongest ones.
    """
    settings = get_settings()

    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})

    # The sport belongs to the job, not to the conversation. It was a required
    # argument, and with one sport registered a model could safely guess it;
    # with two it correctly stopped guessing and asked instead — "What sport is
    # being played in the video?" — in a pipeline with nobody to answer. The
    # run then continued through every later stage on zero moments and reported
    # itself finished. A stored fact must not be a parameter a model fills in.
    sport = (job.get("sport") or sport or "").strip()
    if not sport:
        return {
            "status": "error",
            "error": f"Job {job_id} does not say what sport it is.",
            "supported_sports": list_sports(),
        }

    try:
        profile = get_profile(sport)
    except KeyError:
        return {
            "status": "error",
            "error": f"No profile for sport {sport!r}.",
            "supported_sports": list_sports(),
        }

    gcs_uri = (job.get("source") or {}).get("gcsUri")
    # What the model reads. For an HLS source that is the 1 fps proxy the
    # download produced — the picture it samples anyway at a fraction of the
    # bytes. Thumbnails still come from the source, which is why
    # this is a second variable rather than a replacement.
    analysis_uri = (job.get("source") or {}).get("analysisUri") or gcs_uri
    duration = float((job.get("media") or {}).get("durationSec") or 0.0)

    if not gcs_uri:
        return {"status": "error", "error": f"Job {job_id} has no source video."}
    if duration <= 0:
        return {
            "status": "error",
            "error": "Duration is unknown. Run inspect_source before analyze_match.",
        }

    segment_count = len(plan_segments(duration))
    await mcp_client.call_tool(
        "catalog", "update_job_status", {"job_id": job_id, "status": "analyzing", "stage": "analysis"}
    )
    await _emit(
        job_id,
        "analysis",
        f"Analysing {segment_count} segments of {profile.display_name} with {settings.analysis_model}.",
        segments=segment_count,
        model=settings.analysis_model,
    )

    async def segment_done(done: int, total: int) -> None:
        span = 1 - CUT_SHARE - THUMB_SHARE
        await _progress(job_id, "analysis", CUT_SHARE + span * done / total)
        await _emit(job_id, "analysis", f"Analysed segment {done} of {total}.",
                    segments_done=done, segments_total=total)

    if await _cancelled(job_id):
        await mcp_client.call_tool("catalog", "update_job_status", {
            "job_id": job_id, "status": "cancelled", "stage": "analysis", "progress": 0,
        })
        await _emit(job_id, "analysis", "Cancelled before the analysis started.",
                    level="warning")
        return {"status": "cancelled", "job_id": job_id}

    await _progress(job_id, "analysis", 0.0, status="analyzing")
    # Fixed on the job at registration, not read from whoever is looking now.
    metadata_language = job.get("metadataLanguage", "en")

    # Cut the match into real files first. Gemini fetches the whole object to
    # serve a request whatever offsets it is given, so a source over about 2 GiB
    # fails every window — slicing by time alone does not make the bytes
    # smaller. An empty result falls back to offsets, which is right for a
    # source small enough not to need this.
    segment_uris = await _cut_segments(job_id, analysis_uri, duration)

    result = await analyse_segments(
        analysis_uri, duration, sport=sport,
        metadata_language=metadata_language,
        on_segment_done=segment_done,
        # Asked before every window, so a cancel stops the run within one
        # window rather than at the end of the whole recording.
        should_stop=lambda: _cancelled(job_id),
        segment_uris=segment_uris,
    )
    if result["status"] == "error":
        await mcp_client.call_tool(
            "catalog",
            "update_job_status",
            {"job_id": job_id, "status": "failed", "stage": "analysis", "error": result["error"]},
        )
        await _emit(job_id, "analysis", result["error"], level="error")
        return result

    moments = [Moment.model_validate({**m, "job_id": job_id}) for m in result["moments"]]

    # Who is playing does not change during a match, but reading it off a score
    # bug once per segment does not give one answer. Settle it here so every
    # record agrees, and record it on the job as the match's own fact.
    #
    # Only where it is one fixture. An equestrian stream is a day of rounds by
    # different riders, so the same step would relabel every competitor as
    # whoever appeared most — there the per-moment reading is the only correct
    # one, and the graphic naming them is per round.
    home, away = ("", "")
    if profile.teams_are_constant:
        home, away = resolve_team_names(moments)
        moments = apply_team_names(moments, home, away)
        if home or away:
            await mcp_client.call_tool(
                "catalog", "record_teams",
                {"job_id": job_id, "home": home, "away": away},
            )
            await _emit(job_id, "analysis",
                        f"Scoreboard reads {home or '?'} v {away or '?'}.",
                        team1=home, team2=away)

    for failure in result.get("failures", []):
        await _emit(
            job_id,
            "analysis",
            f"Segment {failure['segment']} failed and was skipped.",
            level="warning",
            **failure,
        )

    await _emit(
        job_id,
        "analysis",
        f"Found {len(moments)} key moments across {result['segments_analysed']} segments.",
        moments=len(moments),
    )

    if await _cancelled(job_id):
        # The segments are done and paid for, so they are saved rather than
        # thrown away — cancelling should not also destroy an hour of work.
        await _persist_moments(job_id, moments)
        await mcp_client.call_tool("catalog", "update_job_status", {
            "job_id": job_id, "status": "cancelled", "stage": "analysis",
        })
        await _emit(job_id, "analysis",
                    f"Cancelled. The {len(moments)} moments found so far were kept.",
                    level="warning")
        return {"status": "cancelled", "job_id": job_id, "moments": len(moments)}

    # Which form of the sport this turned out to be. Stored as the label rather
    # than the code, because that is what is displayed and searched, and the
    # profile normalises it back when it needs the code.
    found_discipline = result.get("discipline") or {}
    found = profile.discipline_by_code(found_discipline.get("code", ""))
    discipline_label = found.label if found else ""
    if discipline_label:
        await _emit(
            job_id, "analysis",
            f"Identified as {discipline_label} "
            f"({round(float(found_discipline.get('confidence', 0.0)) * 100)}% confident).",
            discipline=discipline_label,
        )

    # Every moment learns whose round it happened in, by time, before it is
    # stored. Observed identities only at this point — the start list that
    # names the rounds no graphic did is not consulted until grounding, and
    # those moments are patched afterwards rather than held back until then.
    fused_rides = result.get("rides") or []
    if fused_rides:
        joined = rides_tool.attach_moments([m.model_dump() for m in moments], fused_rides)
        for m, j in zip(moments, joined, strict=True):
            m.rider, m.horse = j.get("rider", ""), j.get("horse", "")
            m.start_number = j.get("start_number", "")
            m.ride_order = j.get("ride_order")
            m.identity_source = j.get("identity_source", "")

    persisted = await _persist_moments(job_id, moments)
    await _drop_segments(job_id)
    # After the moments are saved, because the thumbnail is recorded against a
    # moment that has to exist to carry it.
    thumbnails = await _thumbnail_moments(job_id, gcs_uri, moments)
    await _progress(job_id, "analysis", 1.0)

    await _record_game_details(
        job_id=job_id,
        sport=sport,
        moments=moments,
        segment_summaries=result.get("segment_summaries", []),
        competitions=result.get("competitions", []),
        venues=result.get("venues", []),
        fallback_title=job.get("title", ""),
        chosen=chosen_title(job),
        context_urls=job.get("contextUrls") or [],
        discipline=discipline_label,
        discipline_confidence=float(found_discipline.get("confidence", 0.0)),
        not_confirmed=result.get("not_confirmed", []),
        rides=result.get("rides", []),
        teams_are_constant=profile.teams_are_constant,
    )

    await mcp_client.call_tool(
        "catalog",
        "update_job_status",
        {"job_id": job_id, "status": "analyzed", "stage": "moments"},
    )

    tool_context.state["job_id"] = job_id
    tool_context.state["moment_count"] = len(moments)

    top = sorted(moments, key=lambda m: m.highlight_score, reverse=True)[:10]
    by_type: dict[str, int] = {}
    for m in moments:
        by_type[m.moment_type] = by_type.get(m.moment_type, 0) + 1

    return {
        "status": result["status"],
        "job_id": job_id,
        "sport": sport,
        "moments_found": len(moments),
        "moments_saved": persisted,
        "thumbnails_saved": thumbnails,
        "segments_analysed": result["segments_analysed"],
        "segments_planned": result["segments_planned"],
        "failed_segments": result.get("failures", []),
        "moments_by_type": by_type,
        "top_moments": [
            {
                "moment_id": m.moment_id,
                "type": m.moment_type,
                "label": m.label,
                "start_sec": m.start_sec,
                "end_sec": m.end_sec,
                "score": m.highlight_score,
                "description": m.description,
            }
            for m in top
        ],
    }


async def _patch_moment_identities(
    job_id: str, moments: list[Moment], rides: list[dict], *,
    announce: str = "{n} moments named from the published start list.",
    stage: str = "analysis",
) -> int:
    """Re-join stored moments to rides the schedule has just named.

    Only moments whose identity actually changed are written: a round the
    graphic already named is unchanged by grounding, and rewriting every moment
    of a day to change forty of them is the kind of write that gets throttled.
    The ride order counts as identity too — the event tree groups by it, so a
    moment whose ride was renumbered and not re-joined would sit under the
    wrong rider. Never allowed to fail the stage — a moment without a rider is
    still a moment.

    ``announce`` is the activity-feed line, with ``{n}`` for the count; a live
    event joining moments to rides the graphics named says so differently.
    """
    try:
        joined = rides_tool.attach_moments([m.model_dump() for m in moments], rides)
        changed = [
            {"moment_id": j["moment_id"], "rider": j.get("rider", ""), "horse": j.get("horse", ""),
             "start_number": j.get("start_number", ""), "ride_order": j.get("ride_order"),
             "identity_source": j.get("identity_source", "")}
            for m, j in zip(moments, joined, strict=True)
            if (j.get("rider", ""), j.get("horse", ""), j.get("start_number", ""), j.get("ride_order"))
               != (m.rider, m.horse, m.start_number, m.ride_order)
        ]
        if not changed:
            return 0
        res = await mcp_client.call_tool(
            "catalog", "update_moment_identity", {"job_id": job_id, "identities": changed})
        n = int(res.get("updated") or 0) if res.get("status") == "success" else 0
        if n:
            await _emit(job_id, stage, announce.format(n=n), patched=n)
        return n
    except Exception:
        logger.exception("could not patch moment identities for %s", job_id)
        return 0


def chosen_title(job: dict) -> str:
    """The name a person gave this match, or nothing.

    A job's title is either what someone typed into the ingest panel (or a
    rename) or what was taken off a filename — `titleSource` says which, and
    only the first beats the title the game record composes from the screen.
    Absent on a job from before that was recorded, it means "derived", so
    nothing already on the desk changes its name.
    """
    return (job.get("title") or "").strip() if job.get("titleSource") == "editor" else ""


async def record_game_facts(
    *, job_id: str, sport: str, moments: list[Moment],
    segment_summaries: list[dict], competitions: list[str], venues: list[str],
    fallback_title: str = "", chosen: str = "",
    discipline: str = "", discipline_confidence: float = 0.0,
    rides: list[dict] | None = None,
    teams_are_constant: bool = True,
) -> None:
    """The match-level record from observed facts alone — no model, no search.

    A live event's record cannot wait for the event to end: that is twelve
    hours in which the desk lists nothing for a match with hundreds of
    moments already found, and "No games yet" reads as an analysis that has
    produced nothing. Assembling the facts is pure; the judgement and the
    grounding are the expensive half and stay at the finish, which overwrites
    this with the complete record.
    """
    try:
        game = game_summary.assemble(
            job_id=job_id, sport=sport, moments=moments,
            segment_summaries=segment_summaries,
            competitions=competitions, venues=venues,
            fallback_title=fallback_title, chosen_title=chosen,
            discipline=discipline, discipline_confidence=discipline_confidence,
            not_confirmed=[], rides=rides or [],
            teams_are_constant=teams_are_constant,
        )
        await mcp_client.call_tool("catalog", "upsert_game", {
            "job_id": job_id,
            "game": game.model_dump(),
            "embed_text": game_summary.embed_text(game),
        })
    except Exception:
        # A record that could not be written is a match missing from the desk
        # for a while, not a failed analysis.
        logger.warning("could not write the interim game record for %s", job_id, exc_info=True)


def _classes_for(job: dict, game: GameDetails, context_urls: list[str]) -> list[Any]:
    """The competitions a recording turned out to hold.

    Empty when it held one, which is every handball match and every day that
    ran a single class — and when Equipe cannot be reached, or cannot say which
    show this was. A day that stays one event is what the desk did before any
    of this, so nothing here is allowed to be fatal.
    """
    try:
        show, classes = equipe.find_classes(
            job=job, context_urls=context_urls or [],
            competition=game.competition or game.title, discipline=game.discipline)
        if not classes:
            return []
        runs = equipe.assign_classes(
            [dict(r) for r in game.rides], classes,
            recorded_from=equipe.recording_started(job))
        for run in runs:
            run.show = show
        return runs if len(runs) > 1 else []
    except Exception:
        logger.warning("could not read the show timetable", exc_info=True)
        return []


def _published_class(show_class: Any) -> Any:
    """A class's published panel, start list and results, or nothing.

    Best effort by design: a class still being ridden has a start list and no
    results, a show that publishes late has neither, and a recording is worth
    having either way.
    """
    try:
        return equipe.results_for(show_class)
    except Exception:
        logger.warning("could not read the published results for %s", show_class.class_id,
                       exc_info=True)
        return None


def _moments_of(run: Any, moments: list[Moment]) -> list[Moment]:
    """The moments that happened inside one class.

    By the ride they belong to, because that is what the class was decided on;
    a moment outside every ride falls back to the window the class occupied, so
    the minutes between two rounds go to the class they sat in rather than
    being dropped.
    """
    orders = {r.get("order") for r in run.rides if r.get("order") is not None}
    starts = [float(r.get("start_sec") or 0) for r in run.rides]
    ends = [float(r.get("end_sec") or 0) for r in run.rides]
    first, last = (min(starts) if starts else 0.0), (max(ends) if ends else 0.0)
    out = []
    for moment in moments:
        if moment.ride_order is not None and moment.ride_order in orders:
            out.append(moment)
        elif moment.ride_order is None and first <= moment.start_sec <= last:
            out.append(moment)
    return out


async def _tag_moments_with_class(job_id: str, runs: list[Any],
                                  moments: list[Moment]) -> int:
    """Write which competition each moment happened in.

    The event tree filters a class's moments by this, so a moment left untagged
    on a split day is a moment that shows under no competition at all.
    """
    identities = []
    for run in runs:
        class_id = str(run.show_class.class_id)
        for moment in _moments_of(run, moments):
            identities.append({
                "moment_id": moment.moment_id,
                "rider": moment.rider or "",
                "horse": moment.horse or "",
                "start_number": moment.start_number or "",
                "ride_order": moment.ride_order,
                "identity_source": moment.identity_source or "",
                "class_id": class_id,
            })
    if not identities:
        return 0
    try:
        written = await mcp_client.call_tool(
            "catalog", "update_moment_identity",
            {"job_id": job_id, "identities": identities})
        return int(written.get("updated") or written.get("count") or 0)
    except Exception:
        logger.warning("could not tag moments with their class", exc_info=True)
        return 0


async def _record_game_details(
    *, job_id: str, sport: str, moments: list[Moment],
    segment_summaries: list[dict], competitions: list[str], venues: list[str],
    fallback_title: str = "", chosen: str = "",
    discipline: str = "", discipline_confidence: float = 0.0,
    not_confirmed: list[dict] | None = None,
    rides: list[dict] | None = None,
    context_urls: list[str] | None = None,
    teams_are_constant: bool = True,
) -> GameDetails | None:
    """Build and store the match-level record.

    Never allowed to fail the analysis: a job with moments and no game summary
    is still a useful job, whereas losing several hundred detections because a
    summary call failed is not a trade anyone would choose.
    """
    try:
        game = game_summary.assemble(
            job_id=job_id, sport=sport, moments=moments,
            segment_summaries=segment_summaries,
            competitions=competitions, venues=venues,
            fallback_title=fallback_title, chosen_title=chosen,
            discipline=discipline, discipline_confidence=discipline_confidence,
            not_confirmed=not_confirmed or [],
            rides=rides or [],
            teams_are_constant=teams_are_constant,
        )

        judgement = await _judge_game(sport, moments, segment_summaries)
        scoreboards = [m.scoreboard or "" for m in moments if m.scoreboard]

        # Decided by the data rather than the sport's name: a recording that
        # produced rides is a competition day, and what identifies one is the
        # published class results, not a fixture. Everything else is a match.
        if game.rides:
            found = await grounding.identify_show(
                discipline=game.discipline, competition=game.competition,
                venue=game.venue, rides=game.rides, scoreboards=scoreboards,
                context_urls=context_urls or [],
            )
            source = "equipe" if found.get("from_equipe") else "web"
            grounded_rides, placed = game.rides, {"anchors": 0, "offset_sec": None, "named": 0}
            if found.get("grounded"):
                # The start list first: it can name a round no graphic did,
                # and a named round is one the results can then be attached to.
                grounded_rides, placed = rides_tool.align_schedule(
                    game.rides, found.get("start_list") or [])
                grounded_rides = rides_tool.apply_grounding(
                    grounded_rides, found.get("rides") or [], source=source)
                # Moments were stored with what the graphics said; rounds the
                # schedule has since named get their moments patched in place.
                await _patch_moment_identities(job_id, moments, grounded_rides)
            update = {
                "rides": grounded_rides,
                "show_title": found.get("show", ""),
                "location": found.get("location", ""),
                "equipe_url": found.get("equipe_url", ""),
                "judges": found.get("judges") or [],
                "start_list": found.get("start_list") or [],
                "schedule_anchors": int(placed.get("anchors") or 0),
                "schedule_offset_sec": placed.get("offset_sec"),
                "grounded_competition": " — ".join(
                    x for x in (found.get("competition", ""), found.get("class_name", "")) if x)
                    or found.get("show", ""),
                "grounded_venue": found.get("venue", ""),
                "grounded_home_team": "",
                "grounded_away_team": "",
                "match_date": found.get("match_date", ""),
            }
        else:
            found = await grounding.identify_fixture(
                sport=sport,
                home_team=game.home_team, away_team=game.away_team,
                final_score=game.final_score,
                competition=game.competition, venue=game.venue,
                scoreboards=scoreboards,
            )
            update = {
                "grounded_competition": found.get("competition", ""),
                "grounded_venue": found.get("venue", ""),
                "grounded_home_team": found.get("home_team_full_name", ""),
                "grounded_away_team": found.get("away_team_full_name", ""),
                "match_date": found.get("match_date", ""),
            }

        game = game.model_copy(update={
            "sentiment": (judgement.get("sentiment") or game.sentiment),
            "mood": (judgement.get("mood") or game.mood),
            "summary": (judgement.get("summary") or game.summary),
            "grounded": bool(found.get("grounded")),
            "grounding_sources": found.get("sources", []),
            "grounding_queries": found.get("queries", []),
            "context_urls": list(context_urls or []),
            **update,
        })
        if found.get("reason", "").startswith("answer did not come from"):
            await _emit(job_id, "analysis",
                        "Grounding was refused: the search answered from a different show "
                        "than the one the context links name. Nothing from it was stored.",
                        level="warning")

        # A live URL points at an arena, and the camera runs through class
        # after class. Where the timetable says this recording held several,
        # each one is an event of its own — its own name, rides, judges and
        # start list — rather than a single day with a running order of
        # twenty-four and two riders nobody could name.
        job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
        runs = _classes_for(job if isinstance(job, dict) else {}, game, list(context_urls or []))
        if runs:
            await _store_classes(job_id, game, runs, moments)
            return game

        await mcp_client.call_tool("catalog", "upsert_game", {
            "job_id": job_id,
            "game": game.model_dump(),
            "embed_text": game_summary.embed_text(game),
        })
        await _emit(
            job_id, "analysis",
            f"Game summary saved{' with grounded fixture details' if game.grounded else ''}.",
            grounded=game.grounded,
        )
        return game
    except Exception:
        logger.exception("could not build the game summary for %s", job_id)
        await _emit(job_id, "analysis", "Could not build the game summary.", level="warning")
        return None


async def _store_classes(job_id: str, game: GameDetails, runs: list[Any],
                         moments: list[Moment]) -> None:
    """Store one record per competition the recording held.

    Each carries only its own rides and only the moments that happened in
    them, and is named for the class rather than for the day: "FAIRFAX SADDLES
    PSG FREESTYLE GOLD CHAMPIONSHIP" is what that event is, and the day's own
    name becomes the show it belonged to.

    The whole-day record is not kept beside them. Two answers to "what is this
    recording" is how a desk ends up showing a day twice, once whole and once
    in pieces.
    """
    await _tag_moments_with_class(job_id, runs, moments)
    for run in runs:
        show_class = run.show_class
        show = getattr(run, "show", None)
        mine = _moments_of(run, moments)
        published = _published_class(show_class)
        rides = list(run.rides)
        judges, start_list = [], []
        if published:
            judges = [o.as_dict() for o in published.officials]
            start_list = [s.as_row() for s in published.starts]
            # The published record against what was read in the arena. The
            # start list names rounds no graphic did; the results confirm or
            # contradict the totals that were shown. Neither overwrites an
            # observed value — they land in their own fields, because a
            # caption and a results page are different kinds of fact.
            rides, _ = rides_tool.align_schedule(rides, start_list)
            rides = rides_tool.apply_grounding(rides, start_list, source="equipe")
            await _patch_moment_identities(
                job_id, mine, rides,
                announce="{n} moments named from the published start list.")
        part = game.model_copy(update={
            "judges": judges or game.judges,
            "start_list": start_list,
            "equipe_url": show_class.url,
            "class_no": show_class.class_no,
            "arena": show_class.arena,
            "test_name": show_class.test_name,
            "test_movements": show_class.movements,
            "results_final": bool(published and published.final),
            "title": show_class.name or game.title,
            "competition": show_class.name or game.competition,
            "show_title": (show.name if show else "") or game.show_title,
            "rides": rides,
            "moment_count": len(mine),
            "highlight_count": sum(1 for m in mine if (m.highlight_score or 0) >= 0.6),
            "class_id": str(show_class.class_id),
            "class_name": show_class.name,
            "class_url": show_class.url,
            "class_start_at": show_class.start_at.isoformat() if show_class.start_at else "",
            "class_decided_by": run.decided_by,
            "show_id": show.show_id if show else 0,
            "show_url": show.url if show else "",
        })
        await mcp_client.call_tool("catalog", "upsert_game", {
            "job_id": job_id,
            "class_id": str(show_class.class_id),
            "game": part.model_dump(),
            "embed_text": game_summary.embed_text(part),
        })
    # The whole-day record goes as the classes arrive. A live event writes an
    # interim one after every tick that analysed a chunk, so without this the
    # desk would show the day twice once it finished — once whole, once in
    # pieces — and a search would answer with both.
    await mcp_client.call_tool("catalog", "delete_game", {"job_id": job_id})
    names = ", ".join(run.show_class.name for run in runs)
    await _emit(job_id, "analysis",
                f"This recording covered {len(runs)} classes, saved as separate events: {names}.",
                classes=len(runs))


async def split_event_classes(job_id: str) -> dict:
    """Split a recording that was stored as one event into the classes it held.

    A live URL points at an arena, and the camera runs through class after
    class — so a day's capture recorded before this was understood came back as
    a single event with one running order across every competition in it. This
    reads the show's published timetable, works out which class each round was
    in, and stores one event per class: its own name, its own rides, its own
    moments, its own page on the results site.

    Nothing is re-analysed and no moment is lost: the rounds and the detections
    are the ones already on record, filed under the competition they happened
    in. A recording that held one class is left exactly as it is.

    Args:
        job_id: Identifier of the recording to split.

    Returns:
        dict naming the classes it was split into, or saying why it was not.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    if job.get("status") == "error":
        return job

    found = await mcp_client.call_tool("catalog", "get_game", {"job_id": job_id})
    if found.get("status") == "error":
        return {"status": "error", "job_id": job_id,
                "error": "This recording has no game record to split."}
    rides = await mcp_client.call_tool("catalog", "list_game_rides", {"job_id": job_id})
    # Every moment, not the ranked shortlist: each one has to be told which
    # class it happened in, and an untagged moment shows under no competition.
    stored = await mcp_client.call_tool(
        "catalog", "list_moments", {"job_id": job_id, "limit": 2000, "min_score": 0.0})

    game = GameDetails.model_validate({
        **{k: v for k, v in found.items() if k not in ("status", "type")},
        "job_id": job_id,
        "rides": rides.get("rides") or [],
    })
    if not game.rides:
        return {"status": "idle", "job_id": job_id,
                "message": "Only a competition day is split into classes, and this has no rounds."}

    moments = [Moment.model_validate(m) for m in _moments_from(stored)]
    runs = _classes_for(job, game, list(job.get("contextUrls") or []))
    if not runs:
        return {"status": "idle", "job_id": job_id,
                "message": ("This recording covers one class, or its show could not be found "
                            "on the published timetable. Nothing was changed.")}

    await _store_classes(job_id, game, runs, moments)
    return {
        "status": "success",
        "job_id": job_id,
        "classes": [
            {"class_id": str(run.show_class.class_id), "name": run.show_class.name,
             "rides": len(run.rides), "decided_by": run.decided_by,
             "url": run.show_class.url}
            for run in runs
        ],
    }


def _moments_from(listing: dict) -> list[dict]:
    """The stored moments, whichever shape the listing came back in."""
    for key in ("moments", "action_plays"):
        rows = listing.get(key)
        if isinstance(rows, list):
            return rows
    return []


async def summarise_match(job_id: str) -> dict:
    """Write a match's game record from the moments already stored.

    The record is normally written at the end of a run, so a run that died
    after its moments were saved leaves the match with hundreds of detections
    and nothing on the desk — "No games yet", which reads as an analysis that
    found nothing. This rebuilds it without re-analysing anything: the
    moments are read back, the facts are assembled from them, and only the
    judgement and the grounding are asked for again.

    Args:
        job_id: Identifier of the job to summarise.

    Returns:
        dict with the title that was recorded, or the reason there is none.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    if job.get("status") == "error":
        return {"status": "error", "job_id": job_id, "error": job.get("error", "no such job")}
    sport = (job.get("sport") or "").strip()
    try:
        profile = get_profile(sport)
    except KeyError:
        return {"status": "error", "job_id": job_id, "error": f"No profile for sport {sport!r}."}

    listing = await mcp_client.call_tool(
        "catalog", "list_moments", {"job_id": job_id, "limit": 2000, "min_score": 0.0})
    moments: list[Moment] = []
    for raw in listing.get("moments") or []:
        try:
            moments.append(Moment.model_validate({**raw, "job_id": job_id}))
        except Exception:  # noqa: BLE001
            continue
    if not moments:
        return {"status": "error", "job_id": job_id,
                "error": "This match has no moments, so there is nothing to summarise."}

    # A live event keeps a summary per chunk; an upload keeps none, and the
    # judgement works from the moments themselves.
    summaries: list[dict] = []
    competitions: list[str] = []
    venues: list[str] = []
    discipline, confidence = "", 0.0
    if job.get("kind") == "live":
        chunks = (await mcp_client.call_tool(
            "catalog", "list_live_chunks", {"job_id": job_id})).get("chunks") or []
        analysed = [c for c in chunks if c.get("status") == "analysed"]
        summaries = [{"index": int(c.get("index", 0)), "summary": c.get("summary", "")}
                     for c in analysed if c.get("summary")]
        competitions = [c["competition"] for c in analysed if c.get("competition")]
        venues = [c["venue"] for c in analysed if c.get("venue")]
        best = max(analysed, key=lambda c: float(c.get("disciplineConfidence") or 0.0), default=None)
        discipline = (best or {}).get("discipline", "") or ""
        confidence = float((best or {}).get("disciplineConfidence") or 0.0)

    game = await _record_game_details(
        job_id=job_id, sport=sport, moments=moments,
        segment_summaries=summaries, competitions=competitions, venues=venues,
        fallback_title=job.get("title", ""),
        chosen=chosen_title(job),
        discipline=discipline, discipline_confidence=confidence,
        context_urls=list(job.get("contextUrls") or []),
        teams_are_constant=getattr(profile, "teams_are_constant", True),
    )
    if game is None:
        return {"status": "error", "job_id": job_id,
                "error": "The game summary could not be built; the moments are unchanged."}
    return {"status": "success", "job_id": job_id, "title": game.title,
            "moments": len(moments), "grounded": game.grounded}


async def _judge_game(sport: str, moments: list[Moment], segment_summaries: list[dict]) -> dict:
    """Sentiment, mood and a summary — the only fields a model is asked to invent."""
    from google.genai import types

    from sprtz_agents.tools.analysis import _get_client

    digest = game_summary.build_digest(moments, segment_summaries)
    try:
        response = await _get_client().aio.models.generate_content(
            model=get_settings().model,
            contents=game_summary.judgement_prompt(sport, digest),
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                # JSON Schema rather than the class — see analysis.py.
                response_json_schema=game_summary.Judgement.model_json_schema(),
                http_options=types.HttpOptions(timeout=2 * 60 * 1000),
            ),
        )
    except Exception:
        logger.warning("game judgement failed for a %s match", sport, exc_info=True)
        return {}

    try:
        return game_summary.Judgement.model_validate_json(
            (getattr(response, "text", "") or "").strip()).model_dump()
    except Exception:
        logger.warning("game judgement was not parseable for a %s match", sport, exc_info=True)
        return {}


async def _persist_moments(job_id: str, moments: list[Moment], batch_size: int = 100) -> int:
    """Embed and store moments in batches.

    What goes into the vector is the whole ActionPlay — the class, the
    category, the outcome, the participant and their role, how it was ridden
    and by whom, then the prose. Embedding the description alone answers "a
    keeper diving left" but not "double save", "who scored from the wing" or
    "Joynson\'s half-pass", because those facts live in the structured fields
    beside the prose rather than inside it.

    **That list lives in `store.action_play_text`, not here.** This used to
    compose its own and send it as `embed_text`, which the catalog prefers over
    its own function — so the catalog's definition was dead code, and drifted
    two fields behind without anything failing: execution details and the
    harmony index never reached a single equestrian vector, which in a sport
    judged on form is most of what anyone searches by.
    """
    if not moments:
        return 0

    saved = 0
    for start in range(0, len(moments), batch_size):
        chunk = moments[start : start + batch_size]
        # No embed_text: what a moment's vector carries is decided once, by
        # `store.action_play_text` in the catalog that writes it. This used to
        # compose its own and it always won (`embed_text or action_play_text`),
        # so the catalog's definition was dead code — and two fields behind it.
        # Execution details and the harmony index never reached a single
        # equestrian vector, which is most of what anyone searches a judged
        # sport by, and neither did the rider or the horse.
        payload = [m.model_dump() for m in chunk]
        response = await mcp_client.call_tool(
            "catalog", "upsert_moments", {"job_id": job_id, "moments": payload}
        )
        saved += int(response.get("saved", 0))
    return saved


async def search_moments(job_id: str, query: str, limit: int,
                         sport: str = "", job_ids: str = "") -> dict:
    """Find moments by meaning rather than by type.

    Embeds the query, retrieves the nearest moments by vector similarity, then
    reranks them so the results are ordered by how well they answer the question
    rather than by wording overlap. "The keeper kept them in it" finds double
    saves without the word "save" appearing anywhere.

    Args:
        job_id: Job to search within. Pass an empty string to search every job the user owns.
        query: What to look for, in plain language.
        limit: Maximum number of results.
        sport: With an empty job_id, keep only games of this sport ("handball",
            "equestrian"). Ignored when job_id is set.
        job_ids: With an empty job_id, a comma-separated list of job ids to keep.
            Use it when the editor names which matches to look in.

    Returns:
        dict with the matching moments, best first. Each carries rerank_reason
        explaining why it placed where it did.
    """
    return await mcp_client.call_tool(
        "catalog",
        "knn_search_moments",
        {"job_id": job_id, "query": query, "limit": limit, "rerank": True,
         "sport": sport, "job_ids": [x.strip() for x in job_ids.split(",") if x.strip()]},
    )


async def list_jobs(status: str = "", limit: int = 20) -> dict:
    """List recent jobs, newest first.

    Every job on the desk, not one person's. Use this whenever the editor asks
    what exists, what is running, or what failed, rather than asking them for a
    job_id they have no reason to know.

    Args:
        status: Optional filter. "running" for anything still being worked on,
            or an exact status such as "ready" or "failed". Empty means all.
        limit: Most jobs to return.

    Returns:
        dict with the jobs, each carrying its job_id, title, status and stage.
    """
    result = await mcp_client.call_tool(
        "catalog", "list_jobs", {"limit": limit, "status": status},
    )
    if result.get("status") == "error":
        return result
    return {"status": "success", "jobs": result.get("jobs", []),
            "count": result.get("count", 0)}


async def reanalyse_job(job_id: str) -> dict:
    """Throw away a job's findings so the analysis can be run again from scratch.

    Call this before re-running `analysis_pipeline` on a job that has already
    been analysed. Without it the previous run's moments stay where they are and
    the new ones land beside them — the same play twice, and a count that grows
    with every retry.

    Args:
        job_id: Identifier of the job to reset.

    Returns:
        dict saying what was cleared.
    """
    result = await mcp_client.call_tool("catalog", "clear_analysis", {"job_id": job_id})
    if result.get("status") == "error":
        return result
    await _emit(job_id, "ingest", "Cleared the previous analysis; starting again.")
    return {"status": "success", "job_id": job_id, "cleared": True,
            "moments_removed": result.get("moments", 0)}


# A run whose last write is older than this is dead, and how many times the
# watchdog restarts one before it gives up. Both mirror the catalog's figures.
STALL_MINUTES = 15
MAX_RECOVERIES = 2


async def recover_job(job_id: str) -> dict:
    """Prepare a run that died with its process to be started again.

    Nothing on the engine survives a deploy or a killed container, and the
    job then keeps the status it had — "analysing", for ever. The watchdog
    tick finds such jobs by their silence and calls this. It checks the run
    really is dead rather than slow, counts the restart on the job so a job
    that dies every time is not restarted every time, clears the previous
    run's findings, and says whether to start `analysis_pipeline` again.

    Args:
        job_id: The job whose run has gone quiet.

    Returns:
        dict with ``restart`` — true means call `analysis_pipeline` with this
        job_id now; false means leave it, and ``message`` says why.
    """
    from sprtz_agents.tools.live import now as _now
    from sprtz_agents.tools.live import parse_time

    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    if job.get("status") == "error":
        return job
    if job.get("kind") == "live":
        return {"status": "idle", "restart": False, "job_id": job_id,
                "message": "A live event is looked after by its own tick, not by this."}
    status = job.get("status")
    if status not in ("uploaded", "transcoding", "analyzing"):
        return {"status": "idle", "restart": False, "job_id": job_id,
                "message": f"The job is {status}, not running; nothing to recover."}

    updated = parse_time(job.get("updatedAt") or job.get("updated_at"))
    quiet_min = int((_now() - updated).total_seconds() // 60) if updated else None
    if quiet_min is not None and quiet_min < STALL_MINUTES:
        return {"status": "running", "restart": False, "job_id": job_id,
                "message": f"The run wrote {quiet_min} minutes ago; it is slow, not dead."}

    attempts = int((job.get("recovery") or {}).get("attempts") or 0)
    if attempts >= MAX_RECOVERIES:
        reason = (f"The run died {attempts} times and was restarted each time; not "
                  f"restarting again. Retry by hand once the cause is known.")
        await mcp_client.call_tool("catalog", "update_job_status", {
            "job_id": job_id, "status": "failed", "stage": job.get("stage") or "analysis",
            "error": reason})
        await _emit(job_id, "recovery", reason, level="error")
        return {"status": "failed", "restart": False, "job_id": job_id, "message": reason}

    reason = (f"No progress for {quiet_min if quiet_min is not None else '?'} minutes: the run "
              f"died with its process.")
    noted = await mcp_client.call_tool(
        "catalog", "note_recovery", {"job_id": job_id, "reason": reason})
    await _emit(
        job_id, "recovery",
        f"{reason} Restarting the analysis (attempt {noted.get('attempts', attempts + 1)} "
        f"of {MAX_RECOVERIES}).",
        level="warning", attempt=noted.get("attempts", attempts + 1),
    )
    await mcp_client.call_tool("catalog", "clear_analysis", {"job_id": job_id})
    return {"status": "restart", "restart": True, "job_id": job_id,
            "attempt": noted.get("attempts", attempts + 1)}


async def cancel_job(job_id: str) -> dict:
    """Ask a running analysis to stop.

    The run is a sequence of calls with no handle to interrupt, so this sets a
    flag the stages check between steps. Tell the editor it stops at the next
    boundary rather than instantly — a segment already in flight finishes.

    Args:
        job_id: Identifier of the job to cancel.
    """
    result = await mcp_client.call_tool("catalog", "request_cancel", {"job_id": job_id})
    if result.get("status") == "error":
        return result
    await _emit(job_id, "analysis", "Cancellation requested; stopping after the current step.",
                level="warning")
    return {"status": "success", "job_id": job_id, "cancelling": True,
            "note": "Stops at the next stage boundary, not instantly."}


async def delete_job(job_id: str) -> dict:
    """Delete a job entirely: the video, the moments and the game record.

    This cannot be undone and the uploaded video goes with it, so confirm with
    the editor before calling it unless they have already been explicit.

    Args:
        job_id: Identifier of the job to delete.

    Returns:
        dict describing what was removed.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    gcs_uri = (job.get("source") or {}).get("gcsUri", "")

    # A live event's recorder is a Cloud Run Job execution with the job's id
    # in its environment and nothing else. Deleting the job under it leaves
    # it recording chunks for a document that is gone, until the event's end
    # — hours of objects nothing refers to. Stop it first; a recorder that
    # has already finished is not an error.
    capture = (job.get("live") or {}).get("capture") or {}
    if job.get("kind") == "live" and capture.get("execution") \
            and capture.get("state") not in ("finished", "failed", "cancelled"):
        stopped = await mcp_client.call_tool(
            "media", "cancel_live_capture", {"execution": capture["execution"]})
        if stopped.get("status") == "error":
            logger.warning("could not stop the recorder for %s: %s", job_id, stopped.get("error"))

    # Media first: a failure here leaves a job pointing at its video, which is
    # recoverable. The other order leaves orphaned gigabytes nothing refers to.
    media = await mcp_client.call_tool(
        "media", "delete_job_media", {"job_id": job_id, "gcs_uri": gcs_uri}
    )
    if media.get("status") == "error":
        return {"status": "error", "job_id": job_id,
                "error": f"could not delete the media: {media.get('error')}"}

    removed = await mcp_client.call_tool("catalog", "delete_job", {"job_id": job_id})
    if removed.get("status") == "error":
        return removed

    return {
        "status": "success",
        "job_id": job_id,
        "deleted": True,
        "source_deleted": media.get("source_deleted", False),
        "hls_objects_removed": media.get("hls_objects", 0),
        "moments_removed": removed.get("moments", 0),
        "game_removed": bool(removed.get("game", 0)),
    }


# How the analysis stage's share of the bar is split. Cutting thirteen windows
# out of a three-hour match takes a minute or two, and the first segment
# analysis takes several more — so with the whole band given to segment
# completions the bar sat at the start of the stage for five minutes with
# nothing to say. Cutting is a countable operation; giving it the first quarter
# means the bar moves from the moment the run starts.
CUT_SHARE = 0.25

# The tail of the same band, for the same reason: cutting a still per moment is
# a countable operation over a couple of hundred items, so it can say where it
# is. Twelve percent of the analysis band is roughly what it costs — one range
# read each against an hour of Gemini calls.
THUMB_SHARE = 0.12

# How many moments one thumbnail request covers. Each is its own range read of
# the source, so a request for all of them would run for minutes, report nothing
# while it did, and name no particular moment when it failed.
THUMB_BATCH = 10


async def _thumbnail_moments(job_id: str, gcs_uri: str, moments: list[Moment],
                             in_run: bool = True) -> int:
    """Cut a still for every moment and record it against the moment.

    The frame is the first I-frame at or after the moment's peak — the decisive
    frame the analysis named, rather than its in point, which is deliberately a
    second or two of run-up and shows the play about to happen rather than the
    play.

    Never fatal. A moment with no picture is still a moment, and the editor's
    list falls back to the placeholder it used before any of this existed;
    failing an hour of analysis over a still would be the wrong trade by a wide
    margin.
    """
    if not moments:
        return 0

    # Re-analysing mints new moment ids, so the previous run's files are not
    # overwritten by this one's — they would just accumulate under a job that no
    # longer refers to them. Filling gaps in an existing run is the exception:
    # there the files still standing are the ones being kept.
    if in_run:
        try:
            await mcp_client.call_tool("media", "delete_moment_thumbnails", {"job_id": job_id})
        except Exception:
            logger.warning("could not clear old thumbnails for %s", job_id, exc_info=True)

    batches = [moments[i : i + THUMB_BATCH] for i in range(0, len(moments), THUMB_BATCH)]
    await _emit(job_id, "analysis", f"Cutting thumbnails for {len(moments)} moments.",
                thumbnails=len(moments))

    saved = 0
    for done, chunk in enumerate(batches, start=1):
        try:
            result = await mcp_client.call_tool(
                "media", "generate_moment_thumbnails",
                {
                    "gcs_uri": gcs_uri,
                    "job_id": job_id,
                    "moments": [
                        {"moment_id": m.moment_id, "at_sec": m.peak_sec} for m in chunk
                    ],
                },
            )
            written = {
                t["moment_id"]: t["gcs_uri"]
                for t in (result.get("thumbnails") or [])
                if t.get("moment_id") and t.get("gcs_uri")
            }
            if written:
                recorded = await mcp_client.call_tool(
                    "catalog", "record_moment_thumbnails",
                    {"job_id": job_id, "thumbnails": written},
                )
                saved += int(recorded.get("saved", 0))
        except Exception:
            # One batch that would not cut is ten moments without a picture, not
            # a failed analysis.
            logger.warning("thumbnail batch %d failed for job %s", done, job_id, exc_info=True)

        # Only inside the run. Filling gaps on a finished job would set its
        # stage back to "analysis", and the strip would say it is analysing.
        if in_run:
            await _progress(job_id, "analysis",
                            (1 - THUMB_SHARE) + THUMB_SHARE * done / len(batches))

    missing = len(moments) - saved
    await _emit(
        job_id, "analysis",
        f"Thumbnails ready for {saved} of {len(moments)} moments."
        + (f" {missing} could not be read." if missing else ""),
        level="warning" if missing else "info",
        thumbnails_saved=saved, thumbnails_missing=missing,
    )
    return saved


async def _cut_segments(job_id: str, gcs_uri: str, duration: float) -> dict[int, str]:
    """Cut the source into one file per analysis window.

    Returns index -> URI, empty when nothing could be cut. Empty is not a
    failure: analysis then falls back to time offsets into the whole match,
    which works for anything small enough that Gemini will fetch it.

    One request per window rather than one for all of them. It reports progress
    as each lands, keeps any single request short enough not to approach the
    client's timeout on a long match, and makes a failure name the window it
    happened in instead of ending the batch.
    """
    windows = [
        {"index": p.index, "start_sec": p.start_sec, "end_sec": p.end_sec}
        for p in plan_segments(duration)
    ]
    if not windows:
        return {}

    await _emit(job_id, "analysis",
                f"Cutting the match into {len(windows)} segments for analysis.",
                segments=len(windows))

    uris: dict[int, str] = {}
    failed = 0
    for done, window in enumerate(windows, start=1):
        result = await mcp_client.call_tool(
            "media", "split_for_analysis",
            {"gcs_uri": gcs_uri, "job_id": job_id, "windows": [window]},
        )
        for seg in result.get("segments") or []:
            if seg.get("gcs_uri"):
                uris[int(seg["index"])] = seg["gcs_uri"]

        if result.get("status") != "success":
            # A window that would not cut is analysed from the whole file
            # instead, which is worse but not fatal. Ending the run over it
            # would throw away the twelve that did cut.
            failed += 1
            logger.warning("could not cut window %s: %s",
                           window["index"], result.get("error"))

        await _progress(job_id, "analysis", CUT_SHARE * done / len(windows))
        await _emit(job_id, "analysis", f"Cut segment {done} of {len(windows)}.",
                    segments_cut=done, segments_total=len(windows))

    if failed:
        await _emit(
            job_id, "analysis",
            f"{failed} of {len(windows)} segments could not be cut; those windows "
            "will be read from the full file.",
            level="warning",
        )
    return uris


async def _drop_segments(job_id: str) -> None:
    """Remove the cut segments once the analysis has read them.

    They are a derived copy of the whole match — as many gigabytes again — and
    nothing needs them after the run. A re-analysis cuts them afresh, which
    costs range reads rather than storage held for weeks.
    """
    try:
        await mcp_client.call_tool(
            "media", "delete_analysis_segments", {"job_id": job_id})
    except Exception:
        logger.warning("could not remove analysis segments for %s", job_id, exc_info=True)


async def _cancelled(job_id: str) -> bool:
    """Whether a stop has been asked for. Cheap enough to check between steps."""
    try:
        result = await mcp_client.call_tool("catalog", "cancel_requested", {"job_id": job_id})
        return bool(result.get("cancelling"))
    except Exception:
        # A failed check must not stop a run that nobody asked to stop.
        logger.warning("could not check cancellation for %s", job_id, exc_info=True)
        return False


async def generate_thumbnails(job_id: str) -> dict:
    """Cut the still image for every moment in a match that has none.

    Use this when a match's moments show no picture — an analysis that ran
    before thumbnails existed, or one where the stills failed. Cutting them is
    minutes of range reads; re-analysing to get them would be an hour spent on
    the wrong thing, and would replace moments the editor may already have
    worked from.

    Args:
        job_id: Identifier of the job.

    Returns:
        dict saying how many moments needed a still and how many now have one.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    if job.get("status") == "error":
        return job

    gcs_uri = (job.get("source") or {}).get("gcsUri")
    if not gcs_uri:
        return {"status": "error", "job_id": job_id,
                "error": f"Job {job_id} has no source video."}

    # list_moments rather than list_action_plays: the ActionPlay projection is
    # the export shape and does not carry the thumbnail, which is what decides
    # whether a moment needs one.
    listing = await mcp_client.call_tool(
        "catalog", "list_moments", {"job_id": job_id, "limit": 1000, "min_score": 0.0},
    )
    raw = listing.get("moments", [])
    if not raw:
        return {"status": "empty", "job_id": job_id,
                "message": "This match has no moments to illustrate."}

    # Only the ones without a picture. Re-cutting the whole match to add the
    # twenty that failed is minutes of reads nobody asked for.
    missing = [Moment.model_validate(m) for m in raw if not m.get("thumb_uri")]
    if not missing:
        return {"status": "success", "job_id": job_id, "moments": len(raw),
                "needed": 0, "thumbnails_saved": 0,
                "message": "Every moment already has a thumbnail."}

    # Not part of a run: the moments that already have a picture keep it, and
    # the job's stage is whatever it finished as.
    saved = await _thumbnail_moments(job_id, gcs_uri, missing, in_run=False)
    return {
        "status": "success",
        "job_id": job_id,
        "moments": len(raw),
        "needed": len(missing),
        "thumbnails_saved": saved,
    }


async def get_game_details(job_id: str) -> dict:
    """The overall details of one match: teams, competition, venue, score, outcome, mood.

    Use this when the editor asks about the game itself rather than the plays
    inside it — who played, how it ended, what kind of match it was.

    Args:
        job_id: Identifier of the job.

    Returns:
        dict with the GameDetails record, including any grounded fixture
        details and the sources they came from.
    """
    result = await mcp_client.call_tool("catalog", "get_game", {"job_id": job_id})
    if result.get("status") == "error":
        return result
    return {"status": "success", "game": result.get("game", {})}


async def find_games(query: str, limit: int = 5) -> dict:
    """Find whole matches by description — teams, competition, venue, how it felt.

    This searches games, not the plays inside them. Use it for "the Sweden
    Denmark match" or "that intense final"; use `search_moments` for a play.

    Args:
        query: Plain-language description of the match.
        limit: Most matches to return.

    Returns:
        dict with the matching games, most relevant first, and how they were
        found — by name or by meaning.
    """
    # A named fixture first. "FAG v TVB — DAIKIN HBL" is abbreviations and a
    # sponsor, so its embedding sits beside every other fixture in the league
    # and meaning-search answers with a plausible neighbour rather than the
    # match that was asked for. Comparing the text answers it exactly or not at
    # all, which is the right failure for a name.
    named = await mcp_client.call_tool(
        "catalog", "match_games_by_title", {"query": query, "limit": limit},
    )
    if named.get("status") != "error" and named.get("games"):
        return {"status": "success", "games": named["games"],
                "count": named.get("count", 0), "matched": "title"}

    result = await mcp_client.call_tool(
        "catalog", "knn_search_games", {"query": query, "limit": limit},
    )
    if result.get("status") == "error":
        return result
    return {"status": "success", "games": result.get("games", []),
            "count": result.get("count", 0), "matched": "meaning"}


async def list_rides(
    job_id: str,
    min_score: float = 0.0,
    watchlist: str = "",
    high_scoring_only: bool = False,
) -> dict:
    """Return a competition day's rounds, in running order, optionally narrowed.

    An equestrian recording is a day of rounds rather than one contest, so this
    is the list an editor actually works from: who rode, when, and what they
    scored. Use it for "the tests that scored over 75", "show me Becky
    Moody's round", or just to see the running order.

    Args:
        job_id: Identifier of the job.
        min_score: Only rides whose displayed total is at least this percentage.
            0 returns every ride, scored or not.
        watchlist: Comma-separated riders, horses or combinations of interest.
            Matches either half of a combination, so a horse's name finds the
            ride as readily as its rider's.
        high_scoring_only: Apply the standard bars instead of `min_score` — 75%
            for a straight test, 80% for a freestyle, because an artistic mark
            lifts a freestyle total and the two are not the same achievement.

    Returns:
        dict with `rides`, each carrying order, rider, horse, start_sec,
        end_sec, test_type, judge_marks, total_pct, rank, score_check and
        score_source. A ride whose `score_check` reports a mismatch has a total
        that does not equal the mean of its own displayed judge marks: something
        was misread, and the number should not be acted on without someone
        looking.
    """
    # Not get_game: that is the game's shape for the agents' context, and it
    # leaves the rides out — this tool answered "no rides recorded" for every
    # event that had them. list_game_rides reads just the rides, one document.
    result = await mcp_client.call_tool("catalog", "list_game_rides", {"job_id": job_id})
    if result.get("status") == "error":
        return result

    found = result.get("rides") or []
    if not found:
        return {"status": "success", "job_id": job_id, "rides": [], "count": 0,
                "note": "No rides recorded for this job. Only equestrian "
                        "recordings are split into rounds."}

    narrowed = rides_tool.high_scoring(found) if high_scoring_only else found
    if min_score > 0 and not high_scoring_only:
        narrowed = [
            r for r in narrowed
            if r.get("total_pct") is not None
            and not rides_tool.untrusted(r.get("score_check", ""))
            and float(r["total_pct"]) >= min_score
        ]
    wanted = [w.strip() for w in watchlist.split(",") if w.strip()]
    if wanted:
        narrowed = rides_tool.match_watchlist(narrowed, wanted)

    return {
        "status": "success",
        "job_id": job_id,
        "rides": narrowed,
        "count": len(narrowed),
        "total_rides": len(found),
    }


def _brief_moments(moments: list[dict], limit: int) -> list[dict]:
    """A ride's moments, best first, cut to what an answer needs.

    The tree carries every field of every moment; a day of forty rides would
    put all of it in the model's context. The id is kept so a follow-up can
    fetch the moment itself.
    """
    ranked = sorted(moments, key=lambda m: float(m.get("highlightScore") or 0.0), reverse=True)
    out = []
    for m in ranked[:max(0, limit)]:
        brief = {
            "momentId": m.get("momentId"),
            "label": m.get("label") or m.get("momentType"),
            "at": format_timecode(float(m.get("startSec") or 0.0)),
            "summary": m.get("summary") or m.get("description") or "",
            "highlightScore": m.get("highlightScore"),
        }
        if m.get("requiresHumanReview"):
            brief["requiresHumanReview"] = True
        out.append(brief)
    return out


CROP_ASPECTS = ("9:16", "4:5", "1:1")


async def list_reels(limit: int = 20) -> dict:
    """The reels on the desk, most recently worked on first.

    Args:
        limit: How many to return.
    """
    found = await mcp_client.call_tool("catalog", "list_reels", {"limit": limit})
    reels = found.get("reels") or []
    # Cut down for a model's context the way list_rides is: a reel's fifty cuts
    # are not what is being asked about when someone asks which reels exist.
    return {"reels": [{
        "reel_id": r.get("reelId"),
        "title": r.get("title"),
        "cuts": r.get("cutCount"),
        "duration_ms": r.get("durationMs"),
        "matches": len(r.get("jobIds") or []),
        "render": (r.get("render") or {}).get("status") or "none",
        "shapes": sorted((r.get("crops") or {}).keys()),
    } for r in reels]}


async def find_reels(query: str, limit: int = 5) -> dict:
    """Find a reel the editor named, by comparing the name rather than its meaning.

    A name is what a vector search is worst at, and a reel's name is usually a
    match's name with a word on the end, so its embedding would sit on top of
    the event it was cut from. Use this when the editor names one; use
    `list_reels` when they ask what exists.

    Args:
        query: The editor's words, which may contain a reel's name.
        limit: How many to return.
    """
    found = await mcp_client.call_tool("catalog", "find_reels",
                                       {"query": query, "limit": limit})
    return {"reels": [{
        "reel_id": r.get("reelId"),
        "title": r.get("title"),
        "cuts": r.get("cutCount"),
        "duration_ms": r.get("durationMs"),
        "render": (r.get("render") or {}).get("status") or "none",
        "shapes": sorted((r.get("crops") or {}).keys()),
        "published": bool((r.get("publish") or {}).get("url")),
    } for r in (found.get("reels") or [])]}


async def write_reel_copy(reel_id: str) -> dict:
    """Write the copy a reel would go out with: title, description, keywords, hashtags.

    Returns it rather than saving it. The editor reads it before anything is
    published, and overwriting a description they had already written would be
    the worst possible moment to be helpful.

    Args:
        reel_id: Reel to write copy for.
    """
    found = await mcp_client.call_tool("catalog", "write_reel_copy", {"reel_id": reel_id})
    if found.get("status") != "success":
        return {"status": "error", "reel_id": reel_id,
                "error": found.get("error") or "The copy could not be written."}
    return {k: v for k, v in found.items() if k != "status"}


async def publish_reel(reel_id: str, title: str = "", description: str = "",
                       privacy: str = "private") -> dict:
    """Upload a reel that has already been rendered to the YouTube channel.

    **This cannot be undone and it posts under the desk's own channel**, so
    confirm with the editor before calling it unless they have already said
    plainly that they want it published — the same rule `delete_job` follows,
    for the same reason.

    It publishes a reel that already exists. It does not choose what goes in
    one and it does not render one: if the reel has not been rendered, say so
    rather than rendering it, because what would go out is then something
    nobody has watched.

    Leave `privacy` at "private" unless the editor has said otherwise in as
    many words. Public is not a default and is not yours to choose.

    Args:
        reel_id: The reel to publish. It must already be rendered.
        title: What to call it on the channel. Empty uses the reel's own name.
        description: The copy. Empty uses what `write_reel_copy` composes.
        privacy: "private", "unlisted" or "public".
    """
    if privacy not in ("private", "unlisted", "public"):
        return {"status": "error", "reel_id": reel_id,
                "error": 'Choose "private", "unlisted" or "public".'}

    found = await mcp_client.call_tool("catalog", "get_reel", {"reel_id": reel_id})
    reel = found.get("reel") or {}
    if not reel:
        return {"status": "error", "reel_id": reel_id, "error": f"No reel {reel_id}."}

    render = reel.get("render") or {}
    if render.get("status") != "ready" or not render.get("reelUri"):
        return {"status": "error", "reel_id": reel_id,
                "error": "That reel has not been rendered yet, so there is nothing "
                         "to upload. Rendering it is the editor's to ask for."}

    copy: dict[str, Any] = {}
    if not title or not description:
        written = await mcp_client.call_tool(
            "catalog", "write_reel_copy", {"reel_id": reel_id})
        if written.get("status") == "success":
            copy = written

    result = await mcp_client.call_tool("media", "publish_youtube", {
        "clip_uri": render["reelUri"],
        "title": (title or copy.get("title") or reel.get("title") or "Highlights")[:100],
        "description": description or copy.get("description") or "",
        "privacy": privacy,
        "tags": (copy.get("tags") or [])[:15],
    })
    if result.get("status") != "success":
        # The reason travels: a revoked refresh token or a channel over quota
        # is the editor's to act on, not something to flatten.
        return {"status": "error", "reel_id": reel_id,
                "error": result.get("error") or "YouTube would not accept the upload."}

    publish = {"status": "done", "videoId": result.get("video_id", ""),
               "url": result.get("url", ""), "privacy": result.get("privacy", privacy),
               "error": ""}
    await mcp_client.call_tool("catalog", "set_reel_publish",
                               {"reel_id": reel_id, "publish": publish})
    return {"status": "success", "reel_id": reel_id, **publish}


async def reframe_reel(reel_id: str, aspect: str, focus_x: float = 0.5,
                       fill: str = "crop") -> dict:
    """Cut an existing reel to another shape: 9:16, 4:5 or 1:1.

    For a reel that has already been rendered — the other shapes are cut from
    that render, so there has to be one. This does not choose what goes in a
    reel and does not publish it; it makes a rendered reel a shape a phone feed
    wants.

    Args:
        reel_id: The reel to cut. It must already be rendered.
        aspect: "9:16" for a Short, "4:5" for a feed post, "1:1" for a square.
        focus_x: Where the middle of the crop sits across the picture, 0 at the
            left edge and 1 at the right. Sport is why this exists — the action
            is rarely in the middle of an arena. Leave it at 0.5 unless the
            editor has said which side the play is on.
        fill: "crop" fills the frame and loses the sides; "blur" keeps the
            whole picture over a blurred copy of itself and loses nothing.
    """
    if aspect not in CROP_ASPECTS:
        return {"status": "error", "reel_id": reel_id,
                "error": f"Choose one of {', '.join(CROP_ASPECTS)}."}

    found = await mcp_client.call_tool("catalog", "get_reel", {"reel_id": reel_id})
    reel = found.get("reel") or {}
    if not reel:
        return {"status": "error", "reel_id": reel_id, "error": f"No reel {reel_id}."}

    render = reel.get("render") or {}
    if render.get("status") != "ready" or not render.get("reelUri"):
        return {"status": "error", "reel_id": reel_id,
                "error": "That reel has not been rendered yet, and the other "
                         "shapes are cut from the render."}

    result = await mcp_client.call_tool("media", "reframe_reel", {
        "reel_uri": render["reelUri"], "reel_id": reel_id,
        "aspect": aspect, "fill": fill, "focus_x": focus_x,
    })
    if result.get("status") != "success":
        return {"status": "error", "reel_id": reel_id,
                "error": result.get("error") or "The reframe did not succeed."}

    crop = {"uri": result.get("reel_uri", ""), "fill": fill,
            "focusX": focus_x, "bytes": result.get("bytes", 0)}
    await mcp_client.call_tool("catalog", "set_reel_crop",
                               {"reel_id": reel_id, "aspect": aspect, "crop": crop})
    return {"status": "success", "reel_id": reel_id, "aspect": aspect, **crop}


async def get_event(job_id: str, riders: str = "", max_moments_per_ride: int = 5) -> dict:
    """Return an event as event → rides → moments: every ride (a rider on one
    horse) in running order, with the moments that happened while they were in
    the arena.

    Use it for "what did Keller do in the freestyle?", "each rider's best
    moments", or "which rounds had nothing worth showing". For the running
    order and scores alone, `list_rides` is lighter; for every moment in match
    order, `list_action_plays`.

    Args:
        job_id: Identifier of the job.
        riders: Comma-separated riders, horses or combinations to narrow to.
            Empty for every ride.
        max_moments_per_ride: The most moments listed under each ride, best
            first. `momentCount` still says how many there were.

    Returns:
        dict with `event`: title, discipline, competition, venue, date,
        outcome, `riders` and `unassignedMoments`. Each ride carries order,
        startNumber, rider, horse, identitySource, start and end as MM:SS,
        testType, result (totalPct, place, scoreCheck), momentCount and its
        `moments`. identitySource "schedule" means the name came from the
        published start list rather than a graphic — say so. Moments outside
        every ride are in `unassignedMoments`, not dropped. A job with no rides
        has an empty `riders`.
    """
    result = await mcp_client.call_tool("catalog", "get_event_tree", {"job_id": job_id})
    if result.get("status") == "error":
        return result
    event = dict(result.get("event") or {})
    found = event.get("riders") or []

    wanted = [w.strip() for w in riders.split(",") if w.strip()]
    chosen = rides_tool.match_watchlist(found, wanted) if wanted else found

    event["riders"] = [
        {
            **{k: v for k, v in ride.items() if k not in ("moments", "startSec", "endSec")},
            "start": format_timecode(float(ride.get("startSec") or 0.0)),
            "end": format_timecode(float(ride.get("endSec") or 0.0)),
            "moments": _brief_moments(ride.get("moments") or [], max_moments_per_ride),
        }
        for ride in chosen
    ]
    unassigned = event.get("unassignedMoments") or []
    event["unassignedMomentCount"] = len(unassigned)
    event["unassignedMoments"] = [] if wanted else _brief_moments(unassigned, max_moments_per_ride)
    out: dict[str, Any] = {"status": "success", "job_id": job_id, "event": event}
    if not found:
        out["note"] = ("No rides recorded for this job. Only equestrian recordings are "
                       "split into rounds; every moment is under unassignedMoments.")
    elif wanted and not chosen:
        out["note"] = f"No ride matched {', '.join(wanted)}."
    return out


async def list_top_moments(limit: int = 20, sport: str = "", job_ids: str = "") -> dict:
    """The key moments across every game on the desk, best first, each naming its game.

    Use this for "show all key moments", "the best moments", "highlights" and
    the like when the editor has not named a match — the question is about the
    desk, not the open job. Each result carries `game` (title, sport,
    discipline); say which match every moment is from.

    Args:
        limit: Most moments to return.
        sport: Keep only games of this sport, e.g. "equestrian". Empty for all.
        job_ids: Comma-separated job ids to keep. Empty for every game.

    Returns:
        dict with `moments` (best first, each with `game`), `running` (job ids
        still analysing — their moments do not exist yet, which is not the same
        as having none) and `games_searched`.
    """
    result = await mcp_client.call_tool("catalog", "list_top_moments", {
        "limit": limit, "sport": sport,
        "job_ids": [x.strip() for x in job_ids.split(",") if x.strip()],
    })
    if result.get("status") == "error":
        return result
    running = result.get("running") or []
    return {
        "status": "success",
        "moments": result.get("moments", []),
        "count": len(result.get("moments", [])),
        "games_searched": result.get("games_searched", 0),
        "running": running,
        "note": (f"{len(running)} game(s) are still analysing and have no moments yet."
                 if running else ""),
    }


async def list_action_plays(job_id: str, limit: int = 500) -> dict:
    """Return every detected moment for a job as ActionPlay records, in match order.

    Use this when the editor asks for the structured log of a match rather than
    a shortlist — what happened, when, who did it and how it ended.

    Args:
        job_id: Identifier of the job.
        limit: Most records to return.

    Returns:
        dict with `action_plays`, each carrying timeOffsetStart/End as MM:SS into
        the match, actionCategory, actionClass, actionResult, participant,
        participantRole, description and a 0-100 confidenceScore.
    """
    result = await mcp_client.call_tool(
        "catalog", "list_action_plays", {"job_id": job_id, "limit": limit, "min_score": 0.0}
    )
    if result.get("status") == "error":
        return result
    return {
        "status": "success",
        "job_id": job_id,
        "action_plays": result.get("action_plays", []),
        "count": result.get("count", 0),
    }


async def get_job_summary(job_id: str) -> dict:
    """Read a job's current state: status, media properties, and what has been found so far.

    Args:
        job_id: Identifier of the job.

    Returns:
        dict describing the job.
    """
    job, moments = await asyncio.gather(
        mcp_client.call_tool("catalog", "get_job", {"job_id": job_id}),
        mcp_client.call_tool("catalog", "list_moments", {"job_id": job_id, "limit": 20, "min_score": 0.0}),
    )
    top = moments.get("moments", [])
    # A job still analysing has written no moments yet — they land when every
    # segment has finished. Without this note the tool result is an empty list
    # beside a status field, and the model reads the list and says "none were
    # found", which is the wrong answer: nothing has been looked for yet.
    note = ""
    if not top and job.get("status") in ("uploaded", "transcoding", "analyzing"):
        note = (f"Analysis is still running (stage {job.get('stage') or 'analysis'}, "
                f"{round(float(job.get('progress') or 0))}%). Moments are written when it "
                "finishes; an empty list now does not mean none were found.")

    return {
        "status": "success",
        "job": job,
        "top_moments": top,
        "note": note,
    }


def describe_taxonomy(sport: str) -> dict:
    """List the moment types recognised for a sport, with what each one means.

    Args:
        sport: Sport name, for example "handball".

    Returns:
        dict of moment types grouped by category.
    """
    try:
        profile = get_profile(sport)
    except KeyError:
        return {"status": "error", "error": f"Unknown sport {sport!r}.", "supported_sports": list_sports()}

    return {
        "status": "success",
        "sport": profile.sport,
        "display_name": profile.display_name,
        "moment_types": [
            {
                "code": m.code,
                "category": m.category,
                "label": m.label,
                "description": m.description,
                "base_score": m.base_score,
            }
            for m in profile.moment_types
        ],
    }


@stage("finalize", skip_if_failed=True)
async def finalize_job(job_id: str) -> dict:
    """Close the run out on what the analysis found.

    Args:
        job_id: Identifier of the job.

    Returns:
        dict with the job's final state and how many moments it holds.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    counts = job.get("counts") or {}
    analysed = int(counts.get("moments") or 0)

    # A run that found nothing is not a finished run. Reporting it as ready
    # reads as a quiet match, and it is far more often an analysis that never
    # produced anything — which needs someone to look at it.
    if not analysed:
        reason = (
            "The analysis stage produced no moments. Re-run it; if it happens "
            "again the segment analysis is failing rather than the match being "
            "quiet."
        )
        await mcp_client.call_tool("catalog", "update_job_status", {
            "job_id": job_id, "status": "failed", "stage": "complete",
            "progress": 100, "error": reason,
        })
        await _emit(job_id, "finalize", reason, level="error")
        return {"status": "error", "job_id": job_id, "error": reason, "moments": 0}

    await mcp_client.call_tool(
        "catalog",
        "update_job_status",
        {"job_id": job_id, "status": "ready", "stage": "complete", "progress": 100},
    )
    await _emit(job_id, "finalize", f"{analysed} moments found.", moments=analysed)

    return {"status": "success", "job_id": job_id, "job_status": "ready",
            "moments": analysed}
