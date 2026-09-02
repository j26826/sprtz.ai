"""Coarse-grained tools the agents call.

Each one wraps a whole stage of the analysis so the model orchestrates the run
without the match's data passing through its context. A 90-minute handball match
yields a few hundred moments carrying 768-dimension embeddings; that belongs in
Firestore, not in a prompt.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.adk.tools import ToolContext

from sprtz_agents.config import get_settings
from sprtz_agents.schemas import Moment
from sprtz_agents.sports import get_profile, list_sports
from sprtz_agents.tools import mcp_client
from sprtz_agents.tools.analysis import analyse_segments, plan_segments
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


async def prepare_playback(job_id: str, tool_context: ToolContext) -> dict:
    """Transcode the uploaded video to HLS and publish it behind the CDN.

    This is what the editor actually plays. Reviewing a key moment is a seek
    within this one stream, so no per-clip render is needed to watch a
    suggestion. Independent of the analysis, so the two run concurrently.

    Args:
        job_id: Identifier of the job to prepare.

    Returns:
        dict with the CDN playback URL and the rendition ladder.
    """
    job = await mcp_client.call_tool("catalog", "get_job", {"job_id": job_id})
    gcs_uri = (job.get("source") or {}).get("gcsUri")
    if not gcs_uri:
        return {"status": "error", "error": f"Job {job_id} has no source video."}

    existing = job.get("playback") or {}
    if existing.get("hlsUrl"):
        return {"status": "success", "job_id": job_id, "already_prepared": True, **existing}

    await _emit(job_id, "transcode", "Packaging the video for streaming playback.")

    result = await mcp_client.call_tool(
        "media", "transcode_hls", {"gcs_uri": gcs_uri, "job_id": job_id}
    )
    if result.get("status") != "success":
        await _emit(
            job_id, "transcode", "Could not package the video for playback.",
            level="error", detail=result.get("error"),
        )
        # Playback is how the editor reviews suggestions, but the analysis is
        # still worth having, so this failure does not fail the job.
        return {"status": "error", "job_id": job_id, "error": result.get("error")}

    await mcp_client.call_tool(
        "catalog",
        "record_playback",
        {
            "job_id": job_id,
            "playback_url": result.get("playback_url", ""),
            "poster_url": result.get("poster_url", ""),
            "renditions": result.get("renditions", []),
            "segment_seconds": result.get("segment_seconds", 2),
        },
    )
    await _emit(
        job_id,
        "transcode",
        f"Playback ready in {len(result.get('renditions', []))} renditions.",
        renditions=result.get("renditions", []),
    )

    tool_context.state["playback_url"] = result.get("playback_url", "")

    return {
        "status": "success",
        "job_id": job_id,
        "playback_url": result.get("playback_url"),
        "poster_url": result.get("poster_url"),
        "renditions": result.get("renditions", []),
        "segment_seconds": result.get("segment_seconds"),
        "files": result.get("files"),
    }


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

    result = await analyse_segments(gcs_uri, duration, sport=sport)
    if result["status"] == "error":
        await mcp_client.call_tool(
            "catalog",
            "update_job_status",
            {"job_id": job_id, "status": "failed", "stage": "analysis", "error": result["error"]},
        )
        await _emit(job_id, "analysis", result["error"], level="error")
        return result

    moments = [Moment.model_validate({**m, "job_id": job_id}) for m in result["moments"]]
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

    persisted = await _persist_moments(job_id, moments)

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


async def _persist_moments(job_id: str, moments: list[Moment], batch_size: int = 100) -> int:
    """Embed and store moments in batches.

    Embeddings are generated from the moment's own description so semantic
    search matches on what happened, not on the type label alone.
    """
    if not moments:
        return 0

    saved = 0
    for start in range(0, len(moments), batch_size):
        chunk = moments[start : start + batch_size]
        payload = [
            {
                **m.model_dump(),
                "embed_text": f"{m.label}. {m.description}",
            }
            for m in chunk
        ]
        response = await mcp_client.call_tool(
            "catalog", "upsert_moments", {"job_id": job_id, "moments": payload}
        )
        saved += int(response.get("saved", 0))
    return saved


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
        "catalog", "update_job_status", {"job_id": job_id, "status": "clips_ready", "stage": "captions"}
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

    await mcp_client.call_tool(
        "catalog",
        "update_job_status",
        {"job_id": job_id, "status": status, "stage": "complete"},
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
