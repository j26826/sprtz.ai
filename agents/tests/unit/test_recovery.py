"""The watchdog's half on the agent: a dead run is restarted, a slow one is not.

A run that dies with its process keeps the status it had, so silence is the
only symptom. `recover_job` decides from the job's own last write, counts what
it does on the job, and refuses after enough tries — a job that dies every
time is telling you something, not asking to be restarted every minute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def catalog():
    state = {
        "job": {"job_id": "j1", "kind": "upload", "status": "analyzing", "stage": "analysis",
                "updatedAt": (NOW - timedelta(minutes=40)).isoformat(), "recovery": {}},
        "calls": [],
    }

    async def call(server, tool, args=None):
        state["calls"].append((tool, args or {}))
        if tool == "get_job":
            return state["job"]
        if tool == "note_recovery":
            n = int(state["job"]["recovery"].get("attempts", 0)) + 1
            state["job"]["recovery"]["attempts"] = n
            return {"attempts": n}
        return {"status": "success"}

    with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)), \
         patch("sprtz_agents.tools.live.now", lambda: NOW):
        yield state


def _tools(state):
    return [t for t, _ in state["calls"]]


class TestRecoverJob:
    @pytest.mark.asyncio
    async def test_a_quiet_run_is_cleared_and_restarted(self, catalog):
        out = await pipeline.recover_job("j1")
        assert out["restart"] is True
        assert out["attempt"] == 1
        assert "note_recovery" in _tools(catalog)
        assert "clear_analysis" in _tools(catalog), "restarting without clearing doubles every moment"

    @pytest.mark.asyncio
    async def test_a_run_that_wrote_recently_is_left_alone(self, catalog):
        catalog["job"]["updatedAt"] = (NOW - timedelta(minutes=3)).isoformat()
        out = await pipeline.recover_job("j1")
        assert out["restart"] is False
        assert "clear_analysis" not in _tools(catalog)

    @pytest.mark.asyncio
    async def test_a_finished_job_is_not_a_dead_one(self, catalog):
        catalog["job"]["status"] = "complete"
        assert (await pipeline.recover_job("j1"))["restart"] is False

    @pytest.mark.asyncio
    async def test_too_many_restarts_fails_the_job_and_says_so(self, catalog):
        catalog["job"]["recovery"] = {"attempts": pipeline.MAX_RECOVERIES}
        out = await pipeline.recover_job("j1")
        assert out["restart"] is False
        failed = [a for t, a in catalog["calls"] if t == "update_job_status"]
        assert failed and failed[0]["status"] == "failed"
        assert "not" in failed[0]["error"] and "restarting" in failed[0]["error"]

    @pytest.mark.asyncio
    async def test_a_live_event_is_someone_elses_business(self, catalog):
        catalog["job"]["kind"] = "live"
        out = await pipeline.recover_job("j1")
        assert out["restart"] is False
        assert "note_recovery" not in _tools(catalog)


class TestTheAnalysisRetriesFailedSegments:
    """The second pass inside analyse_segments, read from the source.

    A failed window is asked once more on its own after the burst; with the
    quota free it usually answers. The pass is off only when asked.
    """

    def test_the_second_pass_exists_and_is_on_by_default(self):
        from pathlib import Path

        source = Path(pipeline.__file__).with_name("analysis.py").read_text()
        assert "retry_failed: bool = True" in source
        assert "retrying %d segment(s) that failed" in source
