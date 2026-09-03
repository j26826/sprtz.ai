"""The sport is a stored fact, not a parameter a model fills in.

Job d18ee6b5280c46bc analysed a 6.6-hour showjumping recording and produced
nothing. The reason is in the run's own log:

    [analysis_agent] said: I can analyze the match for you.
                           What sport is being played in the video?

`analyze_match` took the sport as a required argument. With one sport
registered a model could guess it safely; with two it correctly stopped
guessing and asked — in a SequentialAgent, unattended, with nobody to answer.
The stage then produced no tool call, and clips, captions and publish all ran
successfully on zero moments and marked the job finished.

Two things follow. A stored fact must be read from where it is stored, and a
run that analysed nothing must not report itself complete.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.sub_agents import stages
from sprtz_agents.tools import pipeline


class TestTheToolDoesNotNeedTelling:
    def test_sport_is_optional(self):
        signature = inspect.signature(pipeline.analyze_match)
        assert signature.parameters["sport"].default == "", (
            "a required sport is a question the model has to answer, and in this "
            "pipeline there is nobody to answer it"
        )

    @pytest.mark.asyncio
    async def test_it_reads_the_sport_off_the_job(self):
        seen: dict = {}

        async def call(server, tool, args=None):
            if tool == "get_job":
                return {"status": "success", "sport": "equestrian",
                        "source": {"gcsUri": "gs://u/v.mp4"},
                        "media": {"durationSec": 0.0}}
            seen[tool] = args
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            result = await pipeline.analyze_match("job-1", tool_context=_Context())

        # It gets far enough to reject the duration, which is only reachable
        # once the sport resolved to a real profile.
        assert "Duration is unknown" in result["error"]

    @pytest.mark.asyncio
    async def test_a_job_with_no_sport_says_so_rather_than_guessing(self):
        async def call(server, tool, args=None):
            if tool == "get_job":
                return {"status": "success", "sport": "", "source": {"gcsUri": "gs://u/v.mp4"}}
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            result = await pipeline.analyze_match("job-1", tool_context=_Context())

        assert result["status"] == "error"
        assert "does not say what sport" in result["error"]
        assert "handball" in result["supported_sports"]

    @pytest.mark.asyncio
    async def test_the_job_wins_over_anything_passed(self):
        # The record is the authority. A model that supplies one anyway must not
        # be able to analyse a showjumping round with a handball taxonomy.
        async def call(server, tool, args=None):
            if tool == "get_job":
                return {"status": "success", "sport": "equestrian",
                        "source": {"gcsUri": "gs://u/v.mp4"}, "media": {"durationSec": 0.0}}
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            result = await pipeline.analyze_match(
                "job-1", tool_context=_Context(), sport="handball")

        assert "Duration is unknown" in result["error"]


class TestTheInstructionsForbidAsking:
    def test_the_analysis_stage_is_told_not_to(self):
        assert "Never ask which sport" in stages.analysis_agent.instruction

    def test_it_is_told_why(self):
        # A rule without its reason is one the next edit removes.
        text = stages.analysis_agent.instruction
        assert "Nobody is reading your reply" in text

    def test_ingest_names_the_sport_in_its_report(self):
        # Its reply is the context every later stage sees. Leaving the sport out
        # is what made them start guessing.
        assert "what sport the job is" in stages.ingest_agent.instruction


class TestARunThatAnalysedNothingIsNotComplete:
    @pytest.mark.asyncio
    async def test_no_clips_and_no_moments_is_a_failure(self):
        updates: list[dict] = []

        async def call(server, tool, args=None):
            if tool == "list_clips":
                return {"status": "success", "clips": []}
            if tool == "get_job":
                return {"status": "success", "counts": {"moments": 0, "clips": 0}}
            if tool == "update_job_status":
                updates.append(args)
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            result = await pipeline.finalize_job("job-1")

        assert result["status"] == "error"
        assert updates[-1]["status"] == "failed"
        assert "produced no moments" in updates[-1]["error"]

    @pytest.mark.asyncio
    async def test_moments_but_no_clips_still_only_needs_attention(self):
        # A quiet match is a real outcome. Only nothing at all is a failure.
        updates: list[dict] = []

        async def call(server, tool, args=None):
            if tool == "list_clips":
                return {"status": "success", "clips": []}
            if tool == "get_job":
                return {"status": "success", "counts": {"moments": 40, "clips": 0}}
            if tool == "update_job_status":
                updates.append(args)
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            await pipeline.finalize_job("job-1")

        assert updates[-1]["status"] == "needs_attention"


class _Context:
    """Enough of an ADK ToolContext for these paths."""

    def __init__(self):
        self.state: dict = {}
        self.user_id = "uid-1"
