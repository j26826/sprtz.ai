"""Reels: an ordered set of cuts, and turning them into one video.

A reel is top-level rather than a job's, because it may draw on several
matches, so this is its own router at its own prefix rather than more routes
under `/api/jobs`. That also sidesteps the declaration-order trap over there,
where every literal path has to be declared above `/{job_id}` or FastAPI
captures it as a job id.

Nothing here trusts a time it was sent. Every cut is resolved against the
moment it names by the catalog's `plan_cut`, which is the one place that rule
lives now — see `mcp/catalog_server/store.py`.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core import clients
from app.core.auth import CallerIdentity, current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reels", tags=["reels"])

# A reel id lands in an object path under the media bucket, the same reasoning
# as `_MOMENT_ID` next door: anything outside this set is refused rather than
# rewritten, because a rewritten id addresses somebody else's object.
_REEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")

# The shapes a reel can be cut to. 16:9 is the render's own and is not a crop.
CROP_ASPECTS = ("9:16", "4:5", "1:1")
_MOMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


def _upstream(result: dict, fallback: str) -> HTTPException:
    """A failure from a service behind this one, said in a sentence."""
    logger.warning("upstream failure: %s", result.get("error"))
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=fallback)


class CutIn(BaseModel):
    """One chosen moment, and optionally where to trim it.

    The times are a request, not a fact: `plan_cut` measures them against the
    moment's own record and clamps. Omitting them means the moment as analysed.
    """

    job_id: str
    moment_id: str
    start_sec: float | None = Field(default=None, ge=0)
    end_sec: float | None = Field(default=None, ge=0)


class ReelCreate(BaseModel):
    title: str = Field(default="", max_length=100)
    # The ceiling is the store's; this one keeps an absurd request from
    # becoming fifty Firestore reads before anything checks it.
    cuts: list[CutIn] = Field(min_length=1, max_length=50)
    aspect: str = Field(default="16:9")


class ReelPatch(BaseModel):
    """What an editor may change.

    `render` and `publish` are deliberately absent: they are records of what
    happened rather than fields anyone sets. The catalog rejects them too — this
    is the near door, not the only one.
    """

    title: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=30)
    hashtags: list[str] | None = Field(default=None, max_length=30)
    privacy: str | None = None
    aspect: str | None = None
    cuts: list[CutIn] | None = Field(default=None, max_length=50)


async def _plan(cuts: list[CutIn]) -> list[dict]:
    """Resolve every cut against the record of the moment it names.

    One catalog call per cut. That is a read apiece and a reel is at most fifty
    of them, which is the cost of not taking a caller's word for what "this
    moment" means.
    """
    planned: list[dict] = []
    for cut in cuts:
        if not _MOMENT_ID.match(cut.moment_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="No such moment.")
        result = await clients.call_mcp("catalog", "plan_cut", {
            "job_id": cut.job_id,
            "moment_id": cut.moment_id,
            "start_sec": cut.start_sec,
            "end_sec": cut.end_sec,
        })
        if result.get("status") != "success":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"That moment is not on the desk any more: {cut.moment_id}.")
        planned.append({
            "jobId": cut.job_id,
            "momentId": cut.moment_id,
            "startMs": result["start_ms"],
            "endMs": result["end_ms"],
            # What the analysis found, beside what was asked for, so the editor
            # can show a trim as a trim and offer the way back.
            "detectedStartMs": result.get("detected_start_ms", result["start_ms"]),
            "detectedEndMs": result.get("detected_end_ms", result["end_ms"]),
            "label": result.get("label") or "",
        })
    return planned


async def _reel(reel_id: str) -> dict:
    """Read a reel, or 404. Every route that names one starts here."""
    if not _REEL_ID.match(reel_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such reel.")
    result = await clients.call_mcp("catalog", "get_reel", {"reel_id": reel_id})
    if result.get("status") != "success":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such reel.")
    return result.get("reel") or {}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_reel(body: ReelCreate,
                      user: CallerIdentity = Depends(current_user)) -> dict:
    """Open a reel from the moments an editor has chosen."""
    cuts = await _plan(body.cuts)
    result = await clients.call_mcp("catalog", "create_reel", {
        "owner_uid": user.uid,
        "title": body.title,
        "cuts": cuts,
        "aspect": body.aspect,
    })
    if result.get("status") != "success":
        raise _upstream(result, "The reel could not be created just now.")
    return {"reel": result.get("reel") or {}}


@router.get("")
async def list_reels(limit: int = 50,
                     user: CallerIdentity = Depends(current_user)) -> dict:
    """The desk's reels, most recently worked on first."""
    result = await clients.call_mcp("catalog", "list_reels", {"limit": max(1, min(limit, 200))})
    if result.get("status") != "success":
        raise _upstream(result, "The reels could not be read just now.")
    return {"reels": result.get("reels") or []}


@router.get("/{reel_id}")
async def get_reel(reel_id: str, user: CallerIdentity = Depends(current_user)) -> dict:
    """One reel, with its cuts and its copy."""
    return {"reel": await _reel(reel_id)}


@router.patch("/{reel_id}")
async def update_reel(reel_id: str, body: ReelPatch,
                      user: CallerIdentity = Depends(current_user)) -> dict:
    """Change a reel's copy, its order, or its trims."""
    await _reel(reel_id)
    patch = body.model_dump(exclude_none=True)
    if "cuts" in patch:
        patch["cuts"] = await _plan(body.cuts or [])
    if not patch:
        return {"reel": await _reel(reel_id)}

    result = await clients.call_mcp("catalog", "update_reel",
                                    {"reel_id": reel_id, "patch": patch})
    if result.get("status") != "success":
        raise _upstream(result, "That change could not be saved just now.")
    return {"reel": result.get("reel") or {}}


@router.delete("/{reel_id}")
async def delete_reel(reel_id: str, user: CallerIdentity = Depends(current_user)) -> dict:
    """Remove a reel. Never the moments it was cut from, nor the matches."""
    await _reel(reel_id)
    result = await clients.call_mcp("catalog", "delete_reel", {"reel_id": reel_id})
    if result.get("status") != "success":
        raise _upstream(result, "The reel could not be removed just now.")
    return {"reel_id": reel_id, "deleted": True}


@router.post("/{reel_id}/render")
async def render_reel(reel_id: str, user: CallerIdentity = Depends(current_user)) -> dict:
    """Start the encode. Returns as soon as Transcoder has accepted it.

    A match-length encode behind an open connection is how the one-hour request
    ceiling gets rediscovered, so this reports the job and the browser polls
    `GET /render` — the same shape `prepare_playback` uses.
    """
    reel = await _reel(reel_id)
    cuts = reel.get("cuts") or []
    if not cuts:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This reel has no cuts in it yet.")

    # Every match the cuts name, and where its video is. A live event that has
    # never been composed has chunks rather than a source, which is the same
    # 409 the moment download gives.
    sources: dict[str, str] = {}
    missing: list[str] = []
    for job_id in reel.get("jobIds") or []:
        found = await clients.call_mcp("catalog", "get_job", {"job_id": job_id})
        # A deleted match is not an error to be swallowed: it is the reason the
        # reel cannot render, and the editor needs to be told which one.
        if found.get("status") == "error":
            missing.append(job_id)
            continue
        source = (found.get("source") or {}).get("gcsUri") or ""
        if source:
            sources[job_id] = source
        else:
            missing.append(job_id)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Some matches in this reel have no source video yet, or have been "
                   "deleted. Prepare playback for them first.")

    started = await clients.call_mcp("media", "render_reel", {
        "reel_id": reel_id,
        "cuts": cuts,
        "sources": sources,
        "aspect": reel.get("aspect") or "16:9",
    })
    if started.get("status") != "started":
        raise _upstream(started, "The reel could not be rendered just now.")

    render = {
        "status": "running",
        "transcoderJob": started.get("transcoder_job", ""),
        "reelUri": started.get("reel_uri", ""),
        "aspect": reel.get("aspect") or "16:9",
        "durationMs": started.get("duration_ms", 0),
        "error": "",
    }
    await clients.call_mcp("catalog", "set_reel_render",
                           {"reel_id": reel_id, "render": render})
    return {"reel_id": reel_id, "render": render}


class CropRequest(BaseModel):
    aspect: str
    # "crop" takes a window out of the picture; "blur" keeps the whole frame
    # over a blurred copy of itself.
    fill: str = "crop"
    # Where the middle of the window sits, 0 at the left edge and 1 at the
    # right. Bounded here as well as in the media server: it lands in an ffmpeg
    # filter string, and a value outside the picture is a failed encode rather
    # than a bad framing.
    focus_x: float = Field(default=0.5, ge=0.0, le=1.0)


@router.post("/{reel_id}/crop")
async def crop_reel(reel_id: str, body: CropRequest,
                    user: CallerIdentity = Depends(current_user)) -> dict:
    """Cut the rendered reel to another shape.

    Derived from the render, so there has to be one: cutting a 9:16 out of a
    reel that was never rendered would be cutting it out of nothing.
    """
    reel = await _reel(reel_id)
    if body.aspect not in CROP_ASPECTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Choose one of {', '.join(CROP_ASPECTS)}.")

    render = reel.get("render") or {}
    if render.get("status") != "ready" or not render.get("reelUri"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Render the reel first — the other shapes are cut from it.")

    result = await clients.call_mcp("media", "reframe_reel", {
        "reel_uri": render["reelUri"],
        "reel_id": reel_id,
        "aspect": body.aspect,
        "fill": body.fill,
        "focus_x": body.focus_x,
    })
    if result.get("status") != "success":
        raise _upstream(result, "That shape could not be cut just now.")

    crop = {
        "uri": result.get("reel_uri", ""),
        "fill": body.fill,
        "focusX": body.focus_x,
        "bytes": result.get("bytes", 0),
    }
    saved = await clients.call_mcp("catalog", "set_reel_crop",
                                   {"reel_id": reel_id, "aspect": body.aspect, "crop": crop})
    return {"reel_id": reel_id, "aspect": body.aspect, "crop": crop,
            "reel": saved.get("reel") or reel}


@router.get("/{reel_id}/render")
async def render_status(reel_id: str, user: CallerIdentity = Depends(current_user)) -> dict:
    """How the encode is doing, recording the answer on the reel as it lands."""
    reel = await _reel(reel_id)
    render = dict(reel.get("render") or {})
    job = render.get("transcoderJob") or ""
    if not job:
        return {"reel_id": reel_id, "render": render or {"status": "none"}}
    if render.get("status") in ("ready", "failed"):
        return {"reel_id": reel_id, "render": render}

    state = await clients.call_mcp("media", "reel_status", {
        "transcoder_job": job, "reel_uri": render.get("reelUri") or "",
    })
    if state.get("status") == "error":
        raise _upstream(state, "The render could not be checked just now.")

    if state.get("succeeded"):
        render["status"] = "ready"
    elif state.get("done"):
        render["status"] = "failed"
        render["error"] = state.get("error") or "The encode did not succeed."
    else:
        render["status"] = "running"

    await clients.call_mcp("catalog", "set_reel_render",
                           {"reel_id": reel_id, "render": render})
    return {"reel_id": reel_id, "render": render}
