"""A match can be analysed without being cut.

A competition day is hundreds of moments, and an editor who wants the log
does not want twenty clip suggestions and a Gemini call each for their
copy. The choice is fixed on the job at registration, like the metadata
language: what a match was analysed for does not change because the
panel's checkbox did.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline


def _responder(state, job):
    async def call(server, tool, args=None):
        state["calls"].append((tool, args or {}))
        if tool == "get_job":
            return job
        if tool == "list_clips":
            return {"clips": state.get("clips", [])}
        if tool == "list_moments":
            return {"moments": []}
        return {"status": "success"}
    return call


def _calls(state, tool):
    return [a for t, a in state["calls"] if t == tool]


class TestTheClipStage:
    @pytest.mark.asyncio
    async def test_it_is_skipped_when_the_match_was_not_registered_for_cutting(self):
        state = {"calls": []}
        job = {"job_id": "j1", "makeClips": False, "counts": {"moments": 327}}
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=_responder(state, job))):
            out = await pipeline.propose_clips("j1", 20, 0.5, tool_context=None)
        assert out["status"] == "skipped"
        assert out["clips"] == []
        assert not _calls(state, "list_moments"), "it does not even read the moments"
        said = [a["message"] for t, a in state["calls"] if t == "emit_event"]
        assert any("not asked for" in m for m in said)

    @pytest.mark.asyncio
    async def test_a_job_with_no_flag_is_cut_as_before(self):
        state = {"calls": []}
        job = {"job_id": "j1", "counts": {"moments": 5}}
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=_responder(state, job))):
            out = await pipeline.propose_clips("j1", 20, 0.5, tool_context=None)
        assert out["status"] != "skipped"


class TestTheFinish:
    @pytest.mark.asyncio
    async def test_no_clips_because_none_were_asked_for_is_a_finished_run(self):
        state = {"calls": []}
        job = {"job_id": "j1", "makeClips": False, "counts": {"moments": 327}}
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=_responder(state, job))):
            out = await pipeline.finalize_job("j1")
        assert out["status"] == "success"
        assert out["job_status"] == "ready"
        assert out["clips_requested"] is False
        status = _calls(state, "update_job_status")[0]
        assert status["status"] == "ready" and status["progress"] == 100
        assert "error" not in status, "a run that did what was asked is not an error"

    @pytest.mark.asyncio
    async def test_no_clips_and_no_moments_still_needs_attention(self):
        state = {"calls": []}
        job = {"job_id": "j1", "makeClips": False, "counts": {"moments": 0}}
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=_responder(state, job))):
            out = await pipeline.finalize_job("j1")
        assert out["job_status"] == "needs_attention"

    @pytest.mark.asyncio
    async def test_a_cutting_job_with_nothing_found_still_says_so(self):
        state = {"calls": []}
        job = {"job_id": "j1", "makeClips": True, "counts": {"moments": 0}}
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=_responder(state, job))):
            out = await pipeline.finalize_job("j1")
        assert out["status"] == "error"
        assert "no moments" in out["error"]
