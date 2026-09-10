"""The live tick — what happens to a live event, once a minute.

A live event is a job with a playlist URL and a time window instead of a
file. Nothing about it runs as one long process on the engine, because
nothing on the engine survives a deploy and a live event lasts hours. The
recorder — ``media_server.live_capture``, a Cloud Run Job — is the one process
that must not stop, and it only writes: chunk files to the media bucket, and
one record per closed chunk under ``jobs/{job}/chunks``. Everything else is
this tick, which Cloud Scheduler drives through the API once a minute and
which is safe to run twice, late, or after a restart, because the state it
resumes from is in Firestore rather than in memory. That is the shape the
dressage live driver validated: a recorder, a checkpoint, idempotent ticks.

One tick does whichever of these applies:

- A scheduled event whose start is ``live_lead_seconds`` away starts its
  capture. Chunk 0 therefore holds the lead-in, and the analysis has seen the
  arena before the first competitor is in it.
- A running event analyses every chunk the recorder has closed since the
  last tick, each as one segment through the same call an uploaded match's
  windows go through, with its timestamps offset by where the chunk sits in
  the recording. Before analysing a chunk it checks it against the previous
  chunk *as stored*: media-sequence numbers that do not follow on are a gap,
  whatever the recorder thought — a recorder restart is exactly the case the
  recorder cannot see.
- An event whose recorder has finished, with nothing left to analyse, gets
  its game record and is marked complete.

A tick holds a lock on the job while it works. The scheduler fires on the
minute whatever the previous tick is doing, and a chunk claimed by two ticks
is five minutes of moments saved twice; the claim itself is also a
transaction, so the lock is the cheap check and the claim is the guarantee.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sprtz_agents.config import get_settings
from sprtz_agents.schemas import Moment, SegmentPlan
from sprtz_agents.sports import get_profile
from sprtz_agents.tools import mcp_client
from sprtz_agents.tools.analysis import _analyse_one, merge_segment_results
from sprtz_agents.tools.pipeline import (
    _cancelled,
    _emit,
    _persist_moments,
    _record_game_details,
)

logger = logging.getLogger(__name__)

# A tick that has not released its lock in this long has died with its
# process; the next tick takes over.
LOCK_MINUTES = 15
# Two chunks whose wall-clock times disagree by more than this do not follow
# on, even if the sequence numbers claim to.
PDT_TOLERANCE_SEC = 2.0
# A recorder that has not reported in this long is hung, whatever the
# execution says: it reports at least once a minute while it runs.
RECORDER_STALL_MINUTES = 5
# How many times a dead recorder is started again during one event.
MAX_CAPTURE_RESTARTS = 3
# Analysis owns this band of the bar while the event runs; the game record
# and completion take the rest.
_PROGRESS_START = 5
_PROGRESS_END = 95


def now() -> datetime:
    return datetime.now(UTC)


def parse_time(value: Any) -> datetime | None:
    """ISO 8601 from Firestore or the API, always returned aware and in UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def expected_chunks(start: datetime, end: datetime, lead_sec: int, chunk_sec: int) -> int:
    """How many chunks the whole recording — lead-in included — will be."""
    span = (end - start).total_seconds() + lead_sec
    return max(1, math.ceil(span / max(1, chunk_sec)))


def live_progress(done: int, expected: int) -> int:
    share = min(1.0, done / max(1, expected))
    return int(_PROGRESS_START + (_PROGRESS_END - _PROGRESS_START) * share)


def check_continuity(chunk: dict, previous: dict | None) -> dict:
    """Whether ``chunk`` follows straight on from ``previous``.

    Three independent witnesses, because each fails differently. The
    media-sequence numbers are the origin's own count and catch a gap the
    recorder never saw — one between two recorder executions. The programme
    date-time catches a numbering that continued across a break in the
    broadcast. And the recorder's own notes catch what it saw slide past it
    mid-chunk, which the two numbers on the chunk's edges cannot show.
    """
    issues: list[str] = []
    missing = 0
    gap_sec: float | None = None

    if previous is not None:
        prev_last = previous.get("lastSeq")
        first = chunk.get("firstSeq")
        if prev_last is not None and first is not None and first != prev_last + 1:
            missing = max(0, int(first) - int(prev_last) - 1)
            issues.append(
                f"{missing} segment(s) missing between chunks {previous.get('index')} "
                f"and {chunk.get('index')}" if missing
                else f"segment numbering went backwards at chunk {chunk.get('index')}"
            )
        prev_end = parse_time(previous.get("lastPdtEnd"))
        first_pdt = parse_time(chunk.get("firstPdt"))
        if prev_end is not None and first_pdt is not None:
            gap_sec = round((first_pdt - prev_end).total_seconds(), 3)
            if abs(gap_sec) > PDT_TOLERANCE_SEC:
                issues.append(f"{gap_sec:+.1f}s of wall clock between the chunks")

    before = chunk.get("gapBefore") or None
    if before:
        issues.append(
            f"the recorder missed {before.get('missedSegments', '?')} segment(s) "
            f"before this chunk")
    inside = chunk.get("gapsInside") or []
    if inside:
        lost = sum(int(g.get("missedSegments") or 0) for g in inside)
        issues.append(f"{lost} segment(s) slid past the recorder inside the chunk")
    if chunk.get("discontinuities"):
        issues.append(f"{chunk['discontinuities']} discontinuity marker(s) inside the chunk")

    if previous is None and int(chunk.get("index") or 0) > 0 and not issues:
        status = "unknown"
    else:
        status = "gap" if issues else ("first" if previous is None else "ok")
    return {
        "status": status,
        "previousIndex": previous.get("index") if previous else None,
        "missingSegments": missing,
        "gapSec": gap_sec,
        "issues": issues,
    }


async def _set_live(job_id: str, patch: dict[str, Any]) -> None:
    await mcp_client.call_tool("catalog", "update_live", {"job_id": job_id, "patch": patch})


async def _status(job_id: str, status: str, stage: str = "live",
                  error: str | None = None, progress: int | None = None) -> None:
    args: dict[str, Any] = {"job_id": job_id, "status": status, "stage": stage}
    if error is not None:
        args["error"] = error
    if progress is not None:
        args["progress"] = progress
    await mcp_client.call_tool("catalog", "update_job_status", args)


async def _fail(job_id: str, reason: str) -> dict:
    await _status(job_id, "failed", error=reason)
    await _set_live(job_id, {"state": "failed"})
    await _emit(job_id, "live", reason, level="error")
    return {"status": "error", "job_id": job_id, "error": reason}


# --- The tick -----------------------------------------------------------------


async def live_tick(job_id: str) -> dict:
    """Advance a live event by one step: start its capture when due, analyse
    any chunk the recorder has closed, and finish it when the recorder has.

    Safe to call as often as you like; it locks the job while it works and
    does nothing when there is nothing to do.

    Args:
        job_id: The live event's job.

    Returns:
        dict saying what was done: ``scheduled`` (not due yet, with when),
        ``started``, ``live`` (with how many chunks were analysed now),
        ``complete``, ``busy`` (another tick holds the job) or ``error``.
    """
    settings = get_settings()
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    if job.get("status") == "error":
        return job
    if job.get("kind") != "live":
        return {"status": "error", "job_id": job_id,
                "error": f"Job {job_id} is not a live event."}

    live = job.get("live") or {}
    state = live.get("state") or "scheduled"
    if state not in ("scheduled", "live"):
        return {"status": "idle", "job_id": job_id, "state": state,
                "message": f"The live event is {state}; there is nothing to do."}

    at = now()
    lock = parse_time(live.get("tickLockUntil"))
    if lock and lock > at:
        return {"status": "busy", "job_id": job_id,
                "message": f"Another tick holds this event until {lock:%H:%M:%S} UTC."}

    await _set_live(job_id, {"tickLockUntil": (at + timedelta(minutes=LOCK_MINUTES)).isoformat()})
    try:
        if state == "scheduled":
            return await _start_if_due(job_id, job, live, at, settings)
        return await _advance(job_id, job, live, at, settings)
    finally:
        try:
            await _set_live(job_id, {"tickLockUntil": None})
        except Exception:
            logger.warning("could not release the tick lock on %s", job_id, exc_info=True)


async def _start_if_due(job_id: str, job: dict, live: dict, at: datetime, settings) -> dict:
    start = parse_time(live.get("eventStart"))
    end = parse_time(live.get("eventEnd"))
    if start is None or end is None:
        return await _fail(job_id, "The live event has no valid start or end time.")
    if at >= end:
        return await _fail(job_id, "The event ended before its capture could start.")

    if await _cancelled(job_id):
        await _status(job_id, "cancelled", progress=0)
        await _set_live(job_id, {"state": "cancelled"})
        await _emit(job_id, "live", "Cancelled before the capture started.", level="warning")
        return {"status": "cancelled", "job_id": job_id}

    due_at = start - timedelta(seconds=settings.live_lead_seconds)
    if at < due_at:
        return {"status": "scheduled", "job_id": job_id, "due_at": due_at.isoformat(),
                "message": f"Capture is due at {due_at:%H:%M} UTC, "
                           f"{settings.live_lead_seconds // 60} minutes before the start."}

    chunk_sec = int(live.get("chunkSec") or settings.live_chunk_seconds)
    started = await mcp_client.call_tool("media", "start_live_capture", {
        "job_id": job_id,
        "hls_url": job.get("hlsUrl", ""),
        "event_end": end.isoformat(),
        "chunk_sec": chunk_sec,
    })
    if started.get("status") != "started":
        return await _fail(
            job_id, f"The live capture could not be started: {started.get('error', 'unknown error')}")

    chunk_sec = int(started.get("chunk_sec") or chunk_sec)
    await _set_live(job_id, {
        "state": "live",
        "chunkSec": chunk_sec,
        "capture": {"execution": started.get("execution", ""),
                    "startedAt": at.isoformat(), "state": "starting"},
    })
    await _status(job_id, "analyzing", progress=_PROGRESS_START)
    lead_min = settings.live_lead_seconds // 60
    await _emit(
        job_id, "live",
        f"Live capture started {lead_min} minutes before the scheduled start; each "
        f"{chunk_sec // 60}-minute chunk is analysed as soon as the recorder closes it.",
        execution=started.get("execution", ""), chunk_sec=chunk_sec,
        expected_chunks=expected_chunks(start, end, settings.live_lead_seconds, chunk_sec),
    )
    return {"status": "started", "job_id": job_id, "execution": started.get("execution", "")}


async def _advance(job_id: str, job: dict, live: dict, at: datetime, settings) -> dict:
    sport = (job.get("sport") or "").strip()
    try:
        profile = get_profile(sport)
    except KeyError:
        return await _fail(job_id, f"No profile for sport {sport!r}.")

    start = parse_time(live.get("eventStart")) or at
    end = parse_time(live.get("eventEnd")) or at
    chunk_sec = int(live.get("chunkSec") or settings.live_chunk_seconds)
    expected = expected_chunks(start, end, settings.live_lead_seconds, chunk_sec)
    language = job.get("metadataLanguage", "en")

    capture = live.get("capture") or {}
    execution = capture.get("execution") or ""
    exec_state = "running"
    if execution:
        probe = await mcp_client.call_tool("media", "live_capture_status", {"execution": execution})
        if probe.get("status") in ("running", "succeeded", "failed"):
            exec_state = probe["status"]
        else:
            # Assumed running, which is the safe reading — but say so: a
            # recorder whose state cannot be read is one the event can never
            # see finish, and the first such was a bare execution id the API
            # took for a project.
            logger.warning("could not read the recorder's state for %s: %s",
                           job_id, probe.get("error") or probe)

    # The recorder's health, before anything else: a dead recorder during the
    # event is minutes of broadcast lost for every minute it stays dead.
    restarted = await _keep_recorder_alive(job_id, job, live, capture, exec_state, at, end)
    if restarted is not None:
        return restarted

    chunks = await _chunks(job_id)
    # A chunk whose analysis failed is asked for again, a bounded number of
    # times; the analysis itself retries the call, so what reaches here is a
    # window the model would not answer, which usually answers later. A chunk
    # still claimed from longer ago than any tick may run belongs to a tick
    # that died with its process — a deploy replaced the engine — and is put
    # back the same way, or it would sit "analyzing" for the rest of the event.
    for chunk in chunks:
        if chunk.get("status") in ("failed", "analyzing"):
            reset = await mcp_client.call_tool(
                "catalog", "reset_live_chunk",
                {"job_id": job_id, "index": int(chunk.get("index", 0)),
                 "stale_after_minutes": LOCK_MINUTES})
            if reset.get("reset"):
                chunk["status"] = "captured"
    by_index = {int(c.get("index", -1)): c for c in chunks}
    results: list[dict] = []
    for chunk in chunks:
        if chunk.get("status") != "captured":
            continue
        if await _cancelled(job_id):
            break
        index = int(chunk.get("index", 0))
        claim = await mcp_client.call_tool(
            "catalog", "claim_live_chunk", {"job_id": job_id, "index": index})
        if not claim.get("claimed"):
            continue
        results.append(await _analyse_chunk(
            job_id, sport, chunk, by_index.get(index - 1), expected, language))
        by_index[index] = {**chunk, "status": "analysed"}

    done = sum(1 for c in by_index.values() if c.get("status") in ("analysed", "failed"))
    await _status(job_id, "", progress=live_progress(done, expected))

    if await _cancelled(job_id):
        if execution:
            await mcp_client.call_tool("media", "cancel_live_capture", {"execution": execution})
        await _status(job_id, "cancelled", progress=0)
        await _set_live(job_id, {"state": "cancelled"})
        await _emit(job_id, "live", "Cancelled; the chunks already analysed are kept.",
                    level="warning")
        return {"status": "cancelled", "job_id": job_id}

    if exec_state in ("succeeded", "failed"):
        # The recorder closes its final chunk right before it exits, so a
        # listing taken while it was still running can predate that chunk.
        fresh = await _chunks(job_id)
        if any(c.get("status") == "captured" for c in fresh):
            return {"status": "live", "job_id": job_id, "analysed_now": len(results),
                    "message": "The recorder has finished; its last chunk is analysed next tick."}
        return await _finish(job_id, job, sport, profile, fresh, exec_state)

    return {
        "status": "live", "job_id": job_id, "analysed_now": len(results),
        "chunks_done": done, "expected_chunks": expected, "results": results,
    }


async def _keep_recorder_alive(job_id: str, job: dict, live: dict, capture: dict,
                               exec_state: str, at: datetime, end: datetime) -> dict | None:
    """Restart a recorder that has died or hung while the event is still on.

    Dead is an execution that ended without the event ending, or a recorder
    that reported its own failure. Hung is an execution still running whose
    recorder has not reported in ``RECORDER_STALL_MINUTES`` — it reports at
    least once a minute. Either way the fix is the same: a fresh execution,
    which resumes the chunk numbering from what is already recorded. Bounded,
    because a stream that is genuinely gone should be said to be gone.

    Returns the tick's result when it acted, None when the recorder is fine.
    """
    if exec_state == "succeeded":
        return None
    over = at >= end - timedelta(seconds=60)
    dead = exec_state == "failed" or capture.get("state") == "failed"
    last = parse_time(capture.get("lastPollAt"))
    hung = (exec_state == "running" and capture.get("state") == "recording"
            and last is not None and at - last > timedelta(minutes=RECORDER_STALL_MINUTES))
    if not (dead or hung):
        return None
    why = (f"the recorder failed: {capture.get('error') or 'no reason recorded'}" if dead
           else f"the recorder has not reported for {RECORDER_STALL_MINUTES} minutes")
    if over:
        # Nothing left to record; let the normal finish handle what exists.
        return None
    restarts = int(live.get("captureRestarts") or 0)
    if restarts >= MAX_CAPTURE_RESTARTS:
        return await _fail(
            job_id, f"The recorder was restarted {restarts} times and {why}; giving up. "
                    f"Chunks already analysed are kept.")
    if hung and capture.get("execution"):
        try:
            await mcp_client.call_tool(
                "media", "cancel_live_capture", {"execution": capture["execution"]})
        except Exception:
            logger.warning("could not cancel the hung recorder for %s", job_id, exc_info=True)
    started = await mcp_client.call_tool("media", "start_live_capture", {
        "job_id": job_id, "hls_url": job.get("hlsUrl", ""), "event_end": end.isoformat(),
        "chunk_sec": int(live.get("chunkSec") or 0),
    })
    if started.get("status") != "started":
        return await _fail(job_id, f"{why[0].upper()}{why[1:]}, and it could not be restarted: "
                                   f"{started.get('error', 'unknown error')}")
    await _set_live(job_id, {
        "captureRestarts": restarts + 1,
        "capture": {"execution": started.get("execution", ""), "startedAt": at.isoformat(),
                    "state": "starting", "restartedAfter": why,
                    "captureStart": capture.get("captureStart")},
    })
    await _emit(
        job_id, "live",
        f"Recorder restarted (attempt {restarts + 1} of {MAX_CAPTURE_RESTARTS}): {why}. "
        f"Segments broadcast while it was down are lost; the next chunk will say so.",
        level="warning", restarts=restarts + 1,
    )
    return {"status": "live", "job_id": job_id, "restarted": True, "reason": why}


async def _chunks(job_id: str) -> list[dict]:
    listing = await mcp_client.call_tool("catalog", "list_live_chunks", {"job_id": job_id})
    chunks = list(listing.get("chunks") or [])
    chunks.sort(key=lambda c: int(c.get("index", 0)))
    return chunks


async def _analyse_chunk(job_id: str, sport: str, chunk: dict, previous: dict | None,
                         expected: int, language: str) -> dict:
    index = int(chunk.get("index", 0))
    start_sec = float(chunk.get("startSec") or 0.0)
    duration = float(chunk.get("durationSec") or 0.0)
    uri = chunk.get("muxedUri") or chunk.get("gcsUri") or ""
    muxed_uri = chunk.get("muxedUri") or ""
    if uri and chunk.get("audioUri") and not muxed_uri:
        # The stream keeps its audio apart from the video; the recorder
        # closed both, and the analysis wants one file with sound in it.
        muxed = await mcp_client.call_tool("media", "mux_chunk", {
            "job_id": job_id, "index": index,
            "video_uri": chunk.get("gcsUri"), "audio_uri": chunk.get("audioUri")})
        if muxed.get("status") == "success" and muxed.get("gcs_uri"):
            uri = muxed_uri = muxed["gcs_uri"]
        else:
            await _emit(job_id, "live",
                        f"Chunk {index} is analysed without its audio: "
                        f"{muxed.get('error') or 'the mux did not succeed'}.",
                        level="warning", chunk=index)

    continuity = check_continuity(chunk, previous)
    if continuity["status"] == "gap":
        await _emit(
            job_id, "live",
            f"Chunk {index} does not follow straight on from the one before: "
            + "; ".join(continuity["issues"]) + ". Moments at the boundary may be missed.",
            level="warning", chunk=index, **{k: v for k, v in continuity.items() if k != "issues"},
        )

    if not uri or duration <= 0:
        error = "the chunk record has no file or no duration"
        await _finish_chunk(job_id, index, error=error, continuity=continuity)
        return {"index": index, "status": "failed", "error": error}

    # The chunk is its own file, starting at 00:00 — exactly the case the
    # segment prompt describes — and its place in the recording is the plan's
    # start, which is what makes the merged timestamps absolute.
    plan = SegmentPlan(index=index, start_sec=start_sec, end_sec=start_sec + duration)
    _, analysis, error = await _analyse_one(
        uri, plan, expected, sport, asyncio.Semaphore(1), language, segment_uri=uri)
    if analysis is None:
        await _finish_chunk(job_id, index, error=error or "analysis failed", continuity=continuity,
                            muxed_uri=muxed_uri)
        await _emit(job_id, "live", f"Chunk {index} failed and was skipped: {error}",
                    level="warning", chunk=index)
        return {"index": index, "status": "failed", "error": error}

    moments = merge_segment_results([(plan, analysis)], sport=sport, job_id=job_id)
    saved = await _persist_moments(job_id, moments)
    await _thumbnails_for_chunk(job_id, uri, start_sec, moments)
    await _finish_chunk(
        job_id, index, moments=saved, continuity=continuity,
        summary=getattr(analysis, "segment_summary", "") or "",
        competition=getattr(analysis, "competition", "") or "",
        venue=getattr(analysis, "venue", "") or "",
        discipline=getattr(analysis, "discipline", "") or "",
        discipline_confidence=float(getattr(analysis, "discipline_confidence", 0.0) or 0.0),
        muxed_uri=muxed_uri,
    )
    await _emit(
        job_id, "live",
        f"Analysed live chunk {index + 1} of about {expected}: {saved} key moments.",
        chunk=index, moments=saved, continuity=continuity["status"],
    )
    return {"index": index, "status": "analysed", "moments": saved,
            "continuity": continuity["status"]}


async def _finish_chunk(job_id: str, index: int, **fields: Any) -> None:
    try:
        await mcp_client.call_tool(
            "catalog", "finish_live_chunk", {"job_id": job_id, "index": index, **fields})
    except Exception:
        logger.warning("could not record chunk %s of %s", index, job_id, exc_info=True)


async def _thumbnails_for_chunk(job_id: str, uri: str, start_sec: float,
                                moments: list[Moment]) -> None:
    """A still per moment, cut from the chunk file it was found in.

    The event has no single source to cut from — each chunk is its own object
    — so the peak is translated back into the chunk's own clock. Never fatal.
    """
    if not moments:
        return
    try:
        result = await mcp_client.call_tool("media", "generate_moment_thumbnails", {
            "gcs_uri": uri, "job_id": job_id,
            "moments": [{"moment_id": m.moment_id,
                         "at_sec": max(0.0, round(m.peak_sec - start_sec, 3))} for m in moments],
        })
        written = {
            t["moment_id"]: t["gcs_uri"]
            for t in (result.get("thumbnails") or [])
            if t.get("moment_id") and t.get("gcs_uri")
        }
        if written:
            await mcp_client.call_tool(
                "catalog", "record_moment_thumbnails", {"job_id": job_id, "thumbnails": written})
    except Exception:
        logger.warning("thumbnails for a live chunk of %s failed", job_id, exc_info=True)


async def _finish(job_id: str, job: dict, sport: str, profile, chunks: list[dict],
                  exec_state: str) -> dict:
    analysed = [c for c in chunks if c.get("status") == "analysed"]
    failed = [int(c.get("index", 0)) for c in chunks if c.get("status") == "failed"]
    if exec_state == "failed" and not analysed:
        return await _fail(job_id, "The live capture failed before any chunk was recorded.")

    listing = await mcp_client.call_tool(
        "catalog", "list_moments", {"job_id": job_id, "limit": 2000, "min_score": 0.0})
    moments: list[Moment] = []
    for raw in listing.get("moments") or []:
        try:
            moments.append(Moment.model_validate({**raw, "job_id": job_id}))
        except Exception:  # noqa: BLE001
            continue

    best = max(analysed, key=lambda c: float(c.get("disciplineConfidence") or 0.0), default=None)
    await _record_game_details(
        job_id=job_id, sport=sport, moments=moments,
        segment_summaries=[{"index": int(c.get("index", 0)), "summary": c.get("summary", "")}
                           for c in analysed if c.get("summary")],
        competitions=[c["competition"] for c in analysed if c.get("competition")],
        venues=[c["venue"] for c in analysed if c.get("venue")],
        fallback_title=job.get("title", ""),
        discipline=(best or {}).get("discipline", "") or "",
        discipline_confidence=float((best or {}).get("disciplineConfidence") or 0.0),
        context_urls=list(job.get("contextUrls") or []),
        teams_are_constant=getattr(profile, "teams_are_constant", True),
    )

    await _status(job_id, "complete", stage="complete", progress=100)
    await _set_live(job_id, {"state": "complete"})
    note = ""
    if failed:
        note = f" {len(failed)} chunk(s) failed and are missing from the timeline."
    if exec_state == "failed":
        note += " The recorder stopped early; the event may be incomplete."
    await _emit(
        job_id, "live",
        f"Live event complete: {len(analysed)} chunks analysed, {len(moments)} key moments.{note}",
        level="warning" if note else "info",
        chunks=len(analysed), moments=len(moments), failed_chunks=failed,
    )
    return {"status": "complete", "job_id": job_id, "chunks": len(analysed),
            "moments": len(moments), "failed_chunks": failed}


async def live_status(job_id: str) -> dict:
    """Where a live event is: scheduled and due when, live with how many chunks
    captured and analysed, or complete.

    Args:
        job_id: The live event's job.
    """
    settings = get_settings()
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    if job.get("status") == "error":
        return job
    if job.get("kind") != "live":
        return {"status": "error", "job_id": job_id, "error": f"Job {job_id} is not a live event."}
    live = job.get("live") or {}
    start = parse_time(live.get("eventStart"))
    end = parse_time(live.get("eventEnd"))
    chunks = await _chunks(job_id)
    out = {
        "status": "success", "job_id": job_id, "title": job.get("title", ""),
        "state": live.get("state", "scheduled"),
        "event_start": start.isoformat() if start else None,
        "event_end": end.isoformat() if end else None,
        "chunks_captured": len(chunks),
        "chunks_analysed": sum(1 for c in chunks if c.get("status") == "analysed"),
        "chunks_failed": sum(1 for c in chunks if c.get("status") == "failed"),
        "gaps": [int(c.get("index", 0)) for c in chunks
                 if (c.get("continuity") or {}).get("status") == "gap"],
        "moments": int((job.get("counts") or {}).get("moments") or 0),
    }
    if start and end:
        out["expected_chunks"] = expected_chunks(
            start, end, settings.live_lead_seconds,
            int(live.get("chunkSec") or settings.live_chunk_seconds))
        out["capture_due_at"] = (start - timedelta(seconds=settings.live_lead_seconds)).isoformat()
    return out
