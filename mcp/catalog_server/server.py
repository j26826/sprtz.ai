"""mcp-catalog — Firestore, embeddings and semantic search.

Private Cloud Run service. Every write the agents make to the job's data goes
through here, which is also what makes the UI's realtime listeners update.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from catalog_server import store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mcp-catalog")

mcp = FastMCP("sprtz-catalog")


def _fail(exc: Exception, **context: Any) -> dict:
    logger.exception("tool failed: %s", context)
    return {"status": "error", "error": f"{type(exc).__name__}: {exc}", **context}


@mcp.tool
def create_job(job_id: str, owner_uid: str, title: str, sport: str, gcs_uri: str,
               original_name: str, size_bytes: int, content_type: str = "") -> dict:
    """Open a new analysis job for an uploaded video.

    Args:
        job_id: Identifier to create the job under.
        owner_uid: Identity Platform uid of the owner.
        title: Human-readable title.
        sport: Sport in the video, for example "handball".
        gcs_uri: gs:// URI of the uploaded source.
        original_name: The file name the user uploaded.
        size_bytes: Size of the upload.
        content_type: Content type the client declared, checked at ingest.
    """
    try:
        return {"status": "success", **store.create_job(
            job_id, owner_uid, title, sport, gcs_uri, original_name, size_bytes, content_type)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def get_job(job_id: str) -> dict:
    """Read a job document.

    Args:
        job_id: Identifier of the job.
    """
    try:
        return store.get_job(job_id)
    except KeyError as exc:
        return {"status": "error", "error": str(exc), "job_id": job_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_jobs(owner_uid: str, limit: int = 20, status: str = "") -> dict:
    """List an owner's recent jobs, newest first.

    Args:
        owner_uid: Identity Platform uid whose jobs to list.
        limit: Most jobs to return.
        status: Optional filter. "running" means anything the pipeline still
            owes an answer for; otherwise an exact status such as "ready".
    """
    try:
        jobs = store.list_jobs(owner_uid, limit=limit, status=status)
        return {"status": "success", "jobs": jobs, "count": len(jobs)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, owner_uid=owner_uid)


@mcp.tool
def update_job_status(job_id: str, status: str, stage: str = "", error: str = "",
                      progress: int = -1) -> dict:
    """Move a job to a new status, and optionally a new stage.

    Args:
        job_id: Identifier of the job.
        status: New status, e.g. uploaded, transcoding, analyzing, analyzed, clips_ready, ready, failed.
        stage: Current pipeline stage. Empty string leaves it unchanged.
        error: Failure message. Empty string leaves it unchanged.
        progress: Percent complete 0-100. Pass -1 to leave unchanged.
    """
    try:
        return {"status": "success", **store.update_job_status(
            job_id, status,
            stage=stage or None,
            error=error or None,
            progress=None if progress < 0 else progress,
        )}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def record_media_info(job_id: str, media: dict, segment_count: int) -> dict:
    """Persist the probe results onto a job.

    Args:
        job_id: Identifier of the job.
        media: Output of the media server's probe_media tool.
        segment_count: How many analysis segments the video will be split into.
    """
    try:
        return {"status": "success", **store.record_media_info(job_id, media, segment_count)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def record_playback(job_id: str, playback_url: str, poster_url: str,
                    renditions: list[str], segment_seconds: int) -> dict:
    """Save the CDN HLS URL for a job so the editor can play it.

    Args:
        job_id: Identifier of the job.
        playback_url: CDN URL of the HLS master playlist.
        poster_url: CDN URL of the poster image.
        renditions: Rendition names in the ladder.
        segment_seconds: HLS segment duration.
    """
    try:
        return {"status": "success", **store.record_playback(
            job_id, playback_url, poster_url, renditions, segment_seconds)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def record_teams(job_id: str, home: str, away: str) -> dict:
    """Save the two teams as read from the score bug.

    Args:
        job_id: Identifier of the job.
        home: Home team, the first side on the bug.
        away: Away team, the second side on the bug.
    """
    try:
        return {"status": "success", **store.record_teams(job_id, home, away)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def emit_event(job_id: str, stage: str, level: str, message: str, data: dict) -> dict:
    """Append a line to the job's live activity feed.

    Args:
        job_id: Identifier of the job.
        stage: Pipeline stage the event belongs to.
        level: One of info, warning, error.
        message: Human-readable line shown in the editor.
        data: Any structured detail to attach.
    """
    try:
        return {"status": "success", **store.emit_event(job_id, stage, level, message, data)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def upsert_moments(job_id: str, moments: list[dict]) -> dict:
    """Save key moments, generating an embedding for each.

    Args:
        job_id: Identifier of the job.
        moments: Moment records. Each may carry embed_text to control what is embedded.
    """
    try:
        return {"status": "success", "job_id": job_id, "saved": store.upsert_moments(job_id, moments)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_moments(job_id: str, limit: int, min_score: float) -> dict:
    """List a job's key moments, highest scoring first.

    Args:
        job_id: Identifier of the job.
        limit: Maximum number to return.
        min_score: Lowest highlight score to include.
    """
    try:
        return {"status": "success", "job_id": job_id,
                "moments": store.list_moments(job_id, limit, min_score)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_action_plays(job_id: str, limit: int = 500, min_score: float = 0.0) -> dict:
    """Every detected moment in a job as ActionPlay records, in match order.

    Args:
        job_id: Job whose moments to list.
        limit: Most records to return.
        min_score: Drop anything below this highlight score.
    """
    try:
        plays = store.list_action_plays(job_id, limit=limit, min_score=min_score)
        return {"status": "success", "job_id": job_id, "action_plays": plays,
                "count": len(plays)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def knn_search_moments(query: str, job_id: str, limit: int, owner_uid: str = "",
                       rerank: bool = True) -> dict:
    """Find moments whose meaning matches a plain-language query.

    Embedding search retrieves candidates, then Gemini 2.5 Flash reranks them by
    how well they actually answer the query. Each result carries `similarity`
    (the vector score), `rerank_score` and `rerank_reason` (the model's judgement
    and why), and `rank` (final position). A null `rerank_score` means the
    reranker was unavailable and the vector order stands.

    Args:
        query: What to look for, in plain language.
        job_id: Job to search within. Empty string searches the owner's whole library.
        limit: Maximum results.
        owner_uid: Required only for a library-wide search.
        rerank: Set false to skip reranking and return raw vector order.
    """
    try:
        moments = store.knn_search_moments(query, job_id, owner_uid, limit, rerank=rerank)
        return {
            "status": "success",
            "query": query,
            "reranked": rerank and any(m.get("rerank_score") is not None for m in moments),
            "moments": moments,
        }
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, query=query, job_id=job_id)


@mcp.tool
def upsert_clips(job_id: str, clips: list[dict]) -> dict:
    """Save suggested clips for a job.

    Args:
        job_id: Identifier of the job.
        clips: Clip suggestion records.
    """
    try:
        return {"status": "success", "job_id": job_id, "saved": store.upsert_clips(job_id, clips)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_clips(job_id: str, limit: int) -> dict:
    """List a job's suggested clips, highest scoring first.

    Args:
        job_id: Identifier of the job.
        limit: Maximum number to return.
    """
    try:
        return {"status": "success", "job_id": job_id, "clips": store.list_clips(job_id, limit)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def update_clip(job_id: str, clip_id: str, patch: dict) -> dict:
    """Apply a partial update to one clip.

    Derived fields such as score and momentId are rejected and reported back
    rather than written.

    Args:
        job_id: Identifier of the job.
        clip_id: Identifier of the clip.
        patch: Fields to change.
    """
    try:
        return store.update_clip(job_id, clip_id, patch)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id, clip_id=clip_id)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp-catalog"})


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
