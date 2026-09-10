"""The live tick.

Cloud Scheduler calls this once a minute. It is the only clock a live event
has: the job is a document with a start, an end and a playlist URL, and the
work — starting the capture five minutes before the start, analysing each
chunk the recorder closes, wrapping up when the recorder stops — is done by
the agent's `live_event_agent`, which this route wakes for each job that has
something to do.

Why the API and not the scheduler talks to the agent: the engine is reachable
only with the SDK and a session, which is what this service already holds for
the editor's conversations. And why the agent and not this service does the
work: the analysis of a chunk is the same code path as a segment of an
uploaded match, and that lives on the engine.

Most minutes there is nothing due, and the route answers without touching the
engine at all.
"""

from __future__ import annotations

import asyncio
import datetime
import logging

from fastapi import APIRouter, Depends

from app.core import clients
from app.core.auth import scheduler_caller
from app.core.config import Settings, get_settings
from app.routers.agent import _agent_engine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/live", tags=["live"])

TICK_USER = "live-scheduler"


def _parse(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def due_jobs(jobs: list[dict], now: datetime.datetime, lead_seconds: int) -> list[dict]:
    """Which live jobs are worth waking the agent for right now.

    A scheduled event is due from `lead_seconds` before its start. A running
    one is always due — the recorder may have closed a chunk. A job whose
    last tick is still holding its lock is skipped: the lock is how a slow
    tick keeps the next minute's from running the same chunk twice.
    """
    due = []
    for job in jobs:
        live = job.get("live") or {}
        state = live.get("state")
        lock = _parse(live.get("tickLockUntil"))
        if lock and lock > now:
            continue
        if state == "live":
            due.append(job)
        elif state == "scheduled":
            start = _parse(live.get("eventStart"))
            if start is not None and start - datetime.timedelta(seconds=lead_seconds) <= now:
                due.append(job)
    return due


# A dead run is restarted at most this often, so a job that dies every time
# is not restarted every minute; the agent caps the total.
RECOVERY_SPACING = datetime.timedelta(minutes=20)


def stalled_due(jobs: list[dict], now: datetime.datetime) -> list[dict]:
    """Which dead runs to hand to the agent this minute."""
    due = []
    for job in jobs:
        last = _parse((job.get("recovery") or {}).get("lastAttemptAt"))
        if last and now - last < RECOVERY_SPACING:
            continue
        due.append(job)
    return due


def _wake(engine, job_id: str, message: str) -> str:
    """One synchronous agent turn for one job. Runs off the event loop."""
    session = engine.create_session(user_id=TICK_USER)
    session_id = session.get("id") if isinstance(session, dict) else session.id
    last = ""
    for event in engine.stream_query(
        user_id=TICK_USER, session_id=session_id,
        message=f"[job_id: {job_id}]\n{message}",
    ):
        content = event.get("content") if isinstance(event, dict) else None
        for part in (content or {}).get("parts", []) or []:
            if isinstance(part, dict) and part.get("text"):
                last = part["text"]
    return last.strip()[:400]


@router.post("/tick")
async def tick(
    _caller: str = Depends(scheduler_caller),
    settings: Settings = Depends(get_settings),
) -> dict:
    now = datetime.datetime.now(datetime.UTC)
    listed = await clients.call_mcp("catalog", "list_live_jobs", {})
    jobs = listed.get("jobs") or []
    work = [(job, "Run the live event tick for this job.")
            for job in due_jobs(jobs, now, settings.live_lead_seconds)]

    # The same clock watches uploaded matches. A run that dies with its
    # process keeps the status it had, so silence is the only symptom; the
    # agent's recover_job decides whether it is dead or merely slow.
    stalled = await clients.call_mcp("catalog", "list_stalled_jobs", {"minutes": 15})
    work += [(job, "Recover this job: its run has stalled.")
             for job in stalled_due(stalled.get("jobs") or [], now)]
    if not work:
        return {"live": len(jobs), "stalled": len(stalled.get("jobs") or []), "ticked": []}

    engine = _agent_engine(settings)

    async def one(job: dict, message: str) -> dict:
        job_id = job["job_id"]
        try:
            reply = await asyncio.to_thread(_wake, engine, job_id, message)
            return {"job_id": job_id, "reply": reply}
        except Exception as exc:
            logger.exception("tick failed for %s", job_id)
            return {"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"}

    results = await asyncio.gather(*(one(job, message) for job, message in work))
    return {"live": len(jobs), "stalled": len(stalled.get("jobs") or []), "ticked": results}
