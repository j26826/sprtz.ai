"""A match appears on the desk while it is being analysed, not only after.

The game record was written once, at the end of a run. A live event that
runs for twelve hours therefore showed "No games yet" on the desk beside
four hundred moments already found, and a run that died after saving its
moments left the match invisible for good.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline

MOMENT = {
    "moment_id": "m1", "job_id": "j1", "moment_type": "jump_shot", "category": "shot",
    "label": "Jump shot", "start_sec": 10.0, "end_sec": 16.0, "peak_sec": 13.0,
    "confidence": 0.9, "excitement": 0.8, "highlight_score": 0.9, "description": "d",
    "evidence": [], "is_goal": False,
}


def _responder(state, job, moments=None, chunks=None):
    async def call(server, tool, args=None):
        state["calls"].append((tool, args or {}))
        if tool == "get_job":
            return job
        if tool == "list_moments":
            return {"moments": moments if moments is not None else [dict(MOMENT)]}
        if tool == "list_live_chunks":
            return {"chunks": chunks or []}
        return {"status": "success"}
    return call


def _calls(state, tool):
    return [a for t, a in state["calls"] if t == tool]


class TestTheFactsOnlyRecord:
    @pytest.mark.asyncio
    async def test_it_writes_the_record_without_asking_a_model(self):
        state = {"calls": []}
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=_responder(state, {}))), \
             patch.object(pipeline, "_judge_game", AsyncMock()) as judge, \
             patch.object(pipeline.grounding, "identify_fixture", AsyncMock()) as ground:
            await pipeline.record_game_facts(
                job_id="j1", sport="handball",
                moments=[pipeline.Moment.model_validate(dict(MOMENT))],
                segment_summaries=[], competitions=[], venues=[], fallback_title="A match")
        assert _calls(state, "upsert_game"), "the record is written"
        judge.assert_not_called()
        ground.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_write_that_fails_is_not_an_error(self):
        async def boom(server, tool, args=None):
            raise ConnectionError("catalog down")

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=boom)):
            await pipeline.record_game_facts(
                job_id="j1", sport="handball",
                moments=[pipeline.Moment.model_validate(dict(MOMENT))],
                segment_summaries=[], competitions=[], venues=[])


class TestRebuildingARecord:
    @pytest.mark.asyncio
    async def test_it_summarises_from_the_moments_already_stored(self):
        state = {"calls": []}
        job = {"job_id": "j1", "sport": "handball", "title": "A match", "kind": "upload"}
        recorded = SimpleNamespace(title="SWE v DEN", grounded=True)
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=_responder(state, job))), \
             patch.object(pipeline, "_record_game_details", AsyncMock(return_value=recorded)) as write:
            out = await pipeline.summarise_match("j1")
        assert out["status"] == "success"
        assert out["title"] == "SWE v DEN" and out["moments"] == 1
        assert write.call_args.kwargs["fallback_title"] == "A match"
        assert not _calls(state, "split_for_analysis"), "nothing is re-analysed"

    @pytest.mark.asyncio
    async def test_a_live_event_brings_its_chunk_summaries(self):
        state = {"calls": []}
        job = {"job_id": "j1", "sport": "equestrian", "title": "Day 1", "kind": "live"}
        chunks = [{"index": 0, "status": "analysed", "summary": "A clear round.",
                   "competition": "CDI", "venue": "Somerford",
                   "discipline": "Dressage", "disciplineConfidence": 0.9}]
        with patch.object(pipeline.mcp_client, "call_tool",
                          AsyncMock(side_effect=_responder(state, job, chunks=chunks))), \
             patch.object(pipeline, "_record_game_details",
                          AsyncMock(return_value=SimpleNamespace(title="Dressage — CDI", grounded=False))) as write:
            out = await pipeline.summarise_match("j1")
        assert out["status"] == "success"
        kw = write.call_args.kwargs
        assert kw["segment_summaries"] == [{"index": 0, "summary": "A clear round."}]
        assert kw["competitions"] == ["CDI"] and kw["venues"] == ["Somerford"]
        assert kw["discipline"] == "Dressage"

    @pytest.mark.asyncio
    async def test_a_match_with_no_moments_says_so_rather_than_writing_an_empty_record(self):
        state = {"calls": []}
        job = {"job_id": "j1", "sport": "handball", "kind": "upload"}
        with patch.object(pipeline.mcp_client, "call_tool",
                          AsyncMock(side_effect=_responder(state, job, moments=[]))), \
             patch.object(pipeline, "_record_game_details", AsyncMock()) as write:
            out = await pipeline.summarise_match("j1")
        assert out["status"] == "error"
        assert "no moments" in out["error"]
        write.assert_not_called()

    def test_the_root_agent_can_call_it(self):
        from pathlib import Path as P

        body = P(pipeline.__file__).parent.parent.joinpath("agent.py").read_text()
        assert "pipeline.summarise_match," in body
