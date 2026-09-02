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
from typing import Any

from google.adk.tools import ToolContext

from sprtz_agents.config import get_settings
from sprtz_agents.schemas import GameDetails, Moment
from sprtz_agents.sports import get_profile, list_sports
from sprtz_agents.tools import game_summary, grounding, mcp_client
from sprtz_agents.tools.analysis import (
    analyse_segments,
    apply_team_names,
    plan_segments,
    resolve_team_names,
)
from sprtz_agents.tools.clips import build_clip_suggestions

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
    "ingest": (0, 5),
    "transcode": (5, 20),
    "analysis": (20, 80),
    "clips": (80, 95),
    "captions": (95, 100),
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


def stage(name: str):
    """Mark the job failed if a stage raises, instead of leaving it running.

    A stage that dies takes its progress reporting with it, so the job keeps the
    status it had and reads as still working for ever — which is what a
    container going down mid-response looks like from here. Recording the
    failure is what turns that into something the editor can see and retry.
    """
    def decorate(func):
        @functools.wraps(func)
        async def run(*args, **kwargs):
            job_id = kwargs.get("job_id") or (args[0] if args else "")
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
        "gcs_uri": gcs_uri,
        "media": probe,
        "segment_count": len(segments),
        "segments": [s.model_dump() for s in segments],
    }


@stage("playback")
async def prepare_playback(job_id: str, tool_context: ToolContext) -> dict:
    """Encode the uploaded video to a 480p HLS preview behind the CDN.

    This is what the editor actually plays. Reviewing a key moment is a seek
    within this one stream, so no per-clip render is needed to watch a
    suggestion — and 480p is enough to judge one. Independent of the analysis,
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
    if not gcs_uri:
        return {"status": "error", "error": f"Job {job_id} has no source video."}

    # A recorded playback URL is a claim, not a package. A delete that removed
    # the media and then failed leaves the record pointing at objects that are
    # gone, and short-circuiting on the record alone made that unrecoverable:
    # the editor was told playback was ready while the CDN returned 403, and
    # asking for it again did nothing.
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
        # Playback is how the editor reviews suggestions, but the analysis is
        # still worth having, so this failure does not fail the job.
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
    await _progress(job_id, "transcode", 1.0)
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
_POLL_CEILING_SECONDS = 2 * 60 * 60


async def _await_transcode(job_id: str, transcoder_job: str) -> dict:
    """Wait for a Transcoder job, reporting progress into the job's feed."""
    waited = 0.0
    interval = float(_POLL_FIRST_SECONDS)
    last_state = ""

    while waited < _POLL_CEILING_SECONDS:
        await asyncio.sleep(interval)
        waited += interval
        interval = min(interval * 1.5, _POLL_MAX_SECONDS)

        status = await mcp_client.call_tool(
            "media", "transcode_status", {"transcoder_job": transcoder_job}
        )
        if status.get("status") == "error":
            # A failed poll is not a failed encode; the job may well still be
            # running. Keep waiting rather than declaring it dead.
            logger.warning("could not poll %s: %s", transcoder_job, status.get("error"))
            continue

        state = status.get("state", "")
        if state != last_state:
            last_state = state
            await _emit(job_id, "transcode", f"Preview encode {state.lower()}.")
        if status.get("done"):
            return status

    return {
        "done": False,
        "succeeded": False,
        "state": last_state or "UNKNOWN",
        "error": f"gave up watching the encode after {_POLL_CEILING_SECONDS // 3600}h",
    }


@stage("analysis")
async def analyze_match(job_id: str, sport: str, tool_context: ToolContext) -> dict:
    """Analyse the whole match and save the key moments it finds.

    Splits the video into segments, sends each to Gemini concurrently, merges the
    per-segment results into one timeline, embeds every moment for semantic
    search, and writes them to Firestore. Safe to call once per job.

    Args:
        job_id: Identifier of the job to analyse.
        sport: Sport played in the video, for example "handball".

    Returns:
        dict summarising how many moments were found and the strongest ones.
    """
    settings = get_settings()

    try:
        profile = get_profile(sport)
    except KeyError:
        return {
            "status": "error",
            "error": f"No profile for sport {sport!r}.",
            "supported_sports": list_sports(),
        }

    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    gcs_uri = (job.get("source") or {}).get("gcsUri")
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
        f"Analysing {segment_count} segments of {profile.display_name} with {settings.model}.",
        segments=segment_count,
        model=settings.model,
    )

    async def segment_done(done: int, total: int) -> None:
        await _progress(job_id, "analysis", done / total)
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
    result = await analyse_segments(
        gcs_uri, duration, sport=sport,
        metadata_language=metadata_language,
        on_segment_done=segment_done,
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
    home, away = resolve_team_names(moments)
    moments = apply_team_names(moments, home, away)
    if home or away:
        await mcp_client.call_tool(
            "catalog", "record_teams",
            {"job_id": job_id, "home": home, "away": away},
        )
        await _emit(job_id, "analysis", f"Scoreboard reads {home or '?'} v {away or '?'}.",
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

    persisted = await _persist_moments(job_id, moments)
    await _progress(job_id, "analysis", 1.0)

    await _record_game_details(
        job_id=job_id,
        sport=sport,
        moments=moments,
        segment_summaries=result.get("segment_summaries", []),
        competitions=result.get("competitions", []),
        venues=result.get("venues", []),
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


async def _record_game_details(
    *, job_id: str, sport: str, moments: list[Moment],
    segment_summaries: list[dict], competitions: list[str], venues: list[str],
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
        )

        judgement = await _judge_game(sport, moments, segment_summaries)
        found = await grounding.identify_fixture(
            sport=sport,
            home_team=game.home_team, away_team=game.away_team,
            final_score=game.final_score,
            competition=game.competition, venue=game.venue,
            scoreboards=[m.scoreboard or "" for m in moments if m.scoreboard],
        )

        game = game.model_copy(update={
            "sentiment": (judgement.get("sentiment") or game.sentiment),
            "mood": (judgement.get("mood") or game.mood),
            "summary": (judgement.get("summary") or game.summary),
            "grounded": bool(found.get("grounded")),
            "grounded_competition": found.get("competition", ""),
            "grounded_venue": found.get("venue", ""),
            "grounded_home_team": found.get("home_team_full_name", ""),
            "grounded_away_team": found.get("away_team_full_name", ""),
            "match_date": found.get("match_date", ""),
            "grounding_sources": found.get("sources", []),
        })

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
                response_schema=game_summary.Judgement,
                http_options=types.HttpOptions(timeout=2 * 60 * 1000),
            ),
        )
    except Exception:
        logger.warning("game judgement failed for a %s match", sport, exc_info=True)
        return {}

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, game_summary.Judgement):
        return parsed.model_dump()
    return {}


async def _persist_moments(job_id: str, moments: list[Moment], batch_size: int = 100) -> int:
    """Embed and store moments in batches.

    What goes into the vector is the whole ActionPlay: the class, the category,
    the outcome, the participant and their role, then the description. Embedding
    the description alone answers "a keeper diving left" but not "double save"
    or "who scored from the wing", because those facts live in the structured
    fields beside the prose rather than inside it.
    """
    if not moments:
        return 0

    saved = 0
    for start in range(0, len(moments), batch_size):
        chunk = moments[start : start + batch_size]
        payload = [
            {
                **m.model_dump(),
                "embed_text": ". ".join(
                    part for part in (
                        m.label, m.category, m.action_result,
                        m.participant_role, m.participant, m.description,
                    ) if part and part.strip()
                ),
            }
            for m in chunk
        ]
        response = await mcp_client.call_tool(
            "catalog", "upsert_moments", {"job_id": job_id, "moments": payload}
        )
        saved += int(response.get("saved", 0))
    return saved


@stage("clips")
async def propose_clips(
    job_id: str,
    max_clips: int,
    min_score: float,
    tool_context: ToolContext,
) -> dict:
    """Turn the saved key moments into publishable short-form clip suggestions.

    Picks the highest scoring moments, sets in and out points with the right
    lead-in and follow-through for each moment type, and saves one suggestion per
    clip. Captions are written separately by the caption stage.

    Args:
        job_id: Identifier of the job.
        max_clips: How many suggestions to produce.
        min_score: Lowest highlight score worth suggesting, between 0 and 1.

    Returns:
        dict listing the clips that were created.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    sport = job.get("sport") or "handball"
    duration = float((job.get("media") or {}).get("durationSec") or 0.0)

    listing = await mcp_client.call_tool(
        "catalog",
        "list_moments",
        {"job_id": job_id, "min_score": min_score, "limit": max(max_clips * 3, 60)},
    )
    raw = listing.get("moments", [])
    if not raw:
        return {
            "status": "empty",
            "job_id": job_id,
            "message": f"No moments at or above a score of {min_score}.",
        }

    moments = [Moment.model_validate(m) for m in raw]
    clips = build_clip_suggestions(
        moments, sport=sport, job_id=job_id, max_clips=max_clips, video_duration_sec=duration
    )

    await mcp_client.call_tool(
        "catalog",
        "upsert_clips",
        {"job_id": job_id, "clips": [c.model_dump() for c in clips]},
    )
    await _emit(job_id, "clips", f"Proposed {len(clips)} clips.", clips=len(clips))
    await mcp_client.call_tool(
        "catalog", "update_job_status",
        {"job_id": job_id, "status": "clips_ready", "stage": "captions",
         "progress": stage_progress("clips", 1.0)}
    )

    tool_context.state["clip_count"] = len(clips)

    return {
        "status": "success",
        "job_id": job_id,
        "clips_created": len(clips),
        "clips": [
            {
                "clip_id": c.clip_id,
                "moment_id": c.moment_id,
                "start_sec": c.start_sec,
                "end_sec": c.end_sec,
                "duration_sec": c.duration_sec,
                "title": c.title,
                "hook_text": c.hook_text,
                "score": c.score,
                "rationale": c.rationale,
            }
            for c in clips
        ],
    }


async def search_moments(job_id: str, query: str, limit: int) -> dict:
    """Find moments by meaning rather than by type.

    Embeds the query, retrieves the nearest moments by vector similarity, then
    reranks them so the results are ordered by how well they answer the question
    rather than by wording overlap. "The keeper kept them in it" finds double
    saves without the word "save" appearing anywhere.

    Args:
        job_id: Job to search within. Pass an empty string to search every job the user owns.
        query: What to look for, in plain language.
        limit: Maximum number of results.

    Returns:
        dict with the matching moments, best first. Each carries rerank_reason
        explaining why it placed where it did.
    """
    return await mcp_client.call_tool(
        "catalog",
        "knn_search_moments",
        {"job_id": job_id, "query": query, "limit": limit, "rerank": True},
    )


async def list_jobs(tool_context: ToolContext, status: str = "", limit: int = 20) -> dict:
    """List the editor's own recent jobs, newest first.

    Use this whenever they ask what exists, what is running, or what failed,
    rather than asking them for a job_id they have no reason to know.

    Args:
        status: Optional filter. "running" for anything still being worked on,
            or an exact status such as "ready" or "failed". Empty means all.
        limit: Most jobs to return.

    Returns:
        dict with the jobs, each carrying its job_id, title, status and stage.
    """
    # The uid comes from the signed-in session ADK opened, never from the model:
    # a job_id or uid the model can supply is a job_id it can guess at, and
    # these documents belong to one tenant each.
    owner_uid = tool_context.user_id
    if not owner_uid:
        return {"status": "error", "error": "No signed-in user on this session."}

    result = await mcp_client.call_tool(
        "catalog", "list_jobs",
        {"owner_uid": owner_uid, "limit": limit, "status": status},
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
            "moments_removed": result.get("moments", 0),
            "clips_removed": result.get("clips", 0)}


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
    """Delete a job entirely: the video, the moments, the clips and the game record.

    This cannot be undone and the uploaded video goes with it, so confirm with
    the editor before calling it unless they have already been explicit.

    Args:
        job_id: Identifier of the job to delete.

    Returns:
        dict describing what was removed.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    gcs_uri = (job.get("source") or {}).get("gcsUri", "")

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
        "clips_removed": removed.get("clips", 0),
        "game_removed": bool(removed.get("game", 0)),
    }


async def _cancelled(job_id: str) -> bool:
    """Whether a stop has been asked for. Cheap enough to check between steps."""
    try:
        result = await mcp_client.call_tool("catalog", "cancel_requested", {"job_id": job_id})
        return bool(result.get("cancelling"))
    except Exception:
        # A failed check must not stop a run that nobody asked to stop.
        logger.warning("could not check cancellation for %s", job_id, exc_info=True)
        return False


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


async def find_games(tool_context: ToolContext, query: str, limit: int = 5) -> dict:
    """Find whole matches by description — teams, competition, venue, how it felt.

    This searches games, not the plays inside them. Use it for "the Sweden
    Denmark match" or "that intense final"; use `search_moments` for a play.

    Args:
        query: Plain-language description of the match.
        limit: Most matches to return.

    Returns:
        dict with the matching games, newest and most relevant first.
    """
    owner_uid = tool_context.user_id
    if not owner_uid:
        return {"status": "error", "error": "No signed-in user on this session."}

    result = await mcp_client.call_tool(
        "catalog", "knn_search_games",
        {"query": query, "owner_uid": owner_uid, "limit": limit},
    )
    if result.get("status") == "error":
        return result
    return {"status": "success", "games": result.get("games", []),
            "count": result.get("count", 0)}


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
    job, moments, clips = await asyncio.gather(
        mcp_client.call_tool("catalog", "get_job", {"job_id": job_id}),
        mcp_client.call_tool("catalog", "list_moments", {"job_id": job_id, "limit": 20, "min_score": 0.0}),
        mcp_client.call_tool("catalog", "list_clips", {"job_id": job_id, "limit": 50}),
    )
    return {
        "status": "success",
        "job": job,
        "top_moments": moments.get("moments", []),
        "clips": clips.get("clips", []),
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


async def list_clips_for_copywriting(job_id: str) -> dict:
    """List the clips on a job that still need caption copy written.

    Args:
        job_id: Identifier of the job.

    Returns:
        dict with one entry per clip, including the moment it came from so the
        copy can describe what actually happens.
    """
    listing = await mcp_client.call_tool("catalog", "list_clips", {"job_id": job_id, "limit": 100})
    clips = listing.get("clips", [])
    pending = [c for c in clips if not (c.get("captions") or {})]

    moments = await mcp_client.call_tool(
        "catalog", "list_moments", {"job_id": job_id, "limit": 200, "min_score": 0.0}
    )
    by_id = {m["moment_id"]: m for m in moments.get("moments", [])}

    return {
        "status": "success",
        "job_id": job_id,
        "pending": len(pending),
        "clips": [
            {
                "clip_id": c["clip_id"],
                "start_sec": c["start_sec"],
                "end_sec": c["end_sec"],
                "duration_sec": c["duration_sec"],
                "score": c.get("score"),
                "moment_type": by_id.get(c.get("moment_id"), {}).get("moment_type"),
                "label": by_id.get(c.get("moment_id"), {}).get("label"),
                "description": by_id.get(c.get("moment_id"), {}).get("description"),
                "scoreboard": by_id.get(c.get("moment_id"), {}).get("scoreboard"),
                "is_goal": by_id.get(c.get("moment_id"), {}).get("is_goal", False),
            }
            for c in pending
        ],
    }


async def save_clip_copy(
    job_id: str,
    clip_id: str,
    title: str,
    hook_text: str,
    caption_tiktok: str,
    caption_instagram: str,
    caption_youtube: str,
    hashtags: list[str],
) -> dict:
    """Save the published copy for one clip.

    Args:
        job_id: Identifier of the job.
        clip_id: Identifier of the clip being written.
        title: Short title shown in the editor and used as the YouTube Shorts title.
        hook_text: Large on-screen text for the first second. Six words at most.
        caption_tiktok: TikTok caption.
        caption_instagram: Instagram Reels caption.
        caption_youtube: YouTube Shorts description.
        hashtags: Hashtags without the leading hash, most relevant first.

    Returns:
        dict confirming the save.
    """
    return await mcp_client.call_tool(
        "catalog",
        "update_clip",
        {
            "job_id": job_id,
            "clip_id": clip_id,
            "patch": {
                "title": title,
                "hookText": hook_text,
                "captions": {
                    "tiktok": caption_tiktok,
                    "instagram": caption_instagram,
                    "youtube": caption_youtube,
                },
                "hashtags": hashtags,
            },
        },
    )


@stage("captions")
async def finalize_job(job_id: str) -> dict:
    """Check every clip is publishable and mark the job ready for export.

    Args:
        job_id: Identifier of the job.

    Returns:
        dict with the job's final state and any clips that were held back.
    """
    listing = await mcp_client.call_tool("catalog", "list_clips", {"job_id": job_id, "limit": 200})
    clips = listing.get("clips", [])

    problems: list[dict] = []
    for clip in clips:
        issues = []
        duration = float(clip.get("duration_sec") or 0)
        if duration < 5:
            issues.append("shorter than 5s — below every platform's floor")
        if duration > 180:
            issues.append("longer than 3 minutes — exceeds the Shorts limit")
        if not (clip.get("captions") or {}):
            issues.append("no caption copy")
        if not clip.get("hookText"):
            issues.append("no on-screen hook")
        if issues:
            problems.append({"clip_id": clip.get("clip_id"), "issues": issues})

    ready = len(clips) - len(problems)
    status = "ready" if ready else "needs_attention"
    # The run is over either way, so the bar is full either way — a job that
    # needs attention is finished, not stuck at 95%.

    await mcp_client.call_tool(
        "catalog",
        "update_job_status",
        {"job_id": job_id, "status": status, "stage": "complete", "progress": 100},
    )
    await _emit(
        job_id,
        "publish",
        f"{ready} of {len(clips)} clips ready to publish.",
        level="info" if not problems else "warning",
        ready=ready,
        held_back=len(problems),
    )

    return {
        "status": "success",
        "job_id": job_id,
        "job_status": status,
        "clips_total": len(clips),
        "clips_ready": ready,
        "clips_with_problems": problems,
    }
