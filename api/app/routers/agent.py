"""Agent conversation proxy.

The browser cannot call Agent Runtime directly — it has no Google credentials
and Agent Runtime has no CORS. This router holds the session and streams the
agent's replies back over SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.auth import CallerIdentity, current_user
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

_engine = None


def _agent_engine(settings: Settings):
    """Resolve the deployed Agent Runtime once and reuse it.

    Prefers an explicit resource name, falling back to a lookup by the display
    name Terraform and the deploy script agree on.
    """
    global _engine
    if _engine is not None:
        return _engine

    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=settings.project_id, location=settings.location)

    resource = settings.agent_engine_resource
    if resource:
        # Terraform's name attribute is not reliably a full path; the SDK only
        # accepts the full form.
        if not resource.startswith("projects/"):
            resource = (
                f"projects/{settings.project_id}/locations/{settings.location}"
                f"/reasoningEngines/{resource.rsplit('/', 1)[-1]}"
            )
        _engine = agent_engines.get(resource)
        return _engine

    display_name = settings.agent_engine_display_name
    if not display_name:
        raise RuntimeError(
            "Neither AGENT_ENGINE_RESOURCE nor AGENT_ENGINE_DISPLAY_NAME is set."
        )

    matches = [
        engine
        for engine in agent_engines.list(filter=f'display_name="{display_name}"')
        if getattr(engine, "display_name", None) == display_name
    ]
    if not matches:
        raise RuntimeError(
            f"No Agent Runtime engine named {display_name!r}. Has deploy-agent run?"
        )
    if len(matches) > 1:
        raise RuntimeError(f"{len(matches)} engines share the name {display_name!r}.")

    _engine = matches[0]
    return _engine


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    job_id: str | None = None


@router.post("/sessions")
async def create_session(
    user: CallerIdentity = Depends(current_user), settings: Settings = Depends(get_settings)
) -> dict:
    """Open an agent session for the signed-in user."""
    try:
        engine = _agent_engine(settings)
        session = await asyncio.to_thread(engine.create_session, user_id=user.uid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not create an agent session")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Agent is unavailable."
        ) from exc
    return {"session_id": session.get("id") if isinstance(session, dict) else session.id}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/messages")
async def send_message(
    body: MessageRequest,
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Send a message and stream the agent's response.

    Streamed rather than buffered because a full analysis runs for minutes; the
    editor needs to see the agent working, not a spinner that eventually times
    out.
    """
    try:
        engine = _agent_engine(settings)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Agent is unavailable."
        ) from exc

    prompt = body.message
    if body.job_id:
        # Give the agent the job in context without making the user restate it.
        prompt = f"[job_id: {body.job_id}]\n{prompt}"

    async def stream() -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pump() -> None:
            try:
                for event in engine.stream_query(
                    user_id=user.uid, session_id=body.session_id, message=prompt
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
            except Exception as exc:  # noqa: BLE001
                logger.exception("agent stream failed")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        # stream_query is blocking, so it runs off the event loop and feeds a queue.
        task = asyncio.create_task(asyncio.to_thread(pump))

        try:
            while True:
                kind, payload = await queue.get()
                if kind == "done":
                    yield _sse("done", {})
                    break
                if kind == "error":
                    yield _sse("error", {"error": payload})
                    break

                for part in (payload.get("content", {}) or {}).get("parts", []):
                    if text := part.get("text"):
                        yield _sse("text", {"text": text})
                    elif call := part.get("function_call"):
                        yield _sse("tool", {"name": call.get("name"), "state": "start"})
                    elif response := part.get("function_response"):
                        yield _sse("tool", {"name": response.get("name"), "state": "end"})
        finally:
            task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class FeedbackRequest(BaseModel):
    job_id: str
    clip_id: str | None = None
    score: float = Field(ge=0.0, le=1.0)
    action: str
    comment: str = ""


@router.post("/feedback")
async def send_feedback(
    body: FeedbackRequest,
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Record what the editor did with a suggestion.

    Which clips get discarded is the strongest signal available on whether the
    scoring priors are right.
    """
    try:
        engine = _agent_engine(settings)
        await asyncio.to_thread(engine.register_feedback, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        logger.warning("feedback not recorded: %s", exc)
        return {"status": "skipped", "reason": "Agent unavailable."}
    return {"status": "success"}
