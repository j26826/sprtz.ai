"""What a stage does when it dies.

The media server is memory-bound and its container can go down mid-response.
From the agent's side that arrives as an exception out of a tool call, and
without this guard the job kept whatever status it had and read as still
working for ever — the editor sees a progress bar that never moves and no
reason anywhere.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline


@pytest.fixture
def calls():
    mock = AsyncMock(return_value={"status": "success"})
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        yield mock


def _status_updates(mock):
    return [
        call.args[2] for call in mock.await_args_list
        if call.args[1] == "update_job_status"
    ]


class TestFailureIsRecorded:
    @pytest.mark.asyncio
    async def test_a_crashed_stage_marks_the_job_failed(self, calls):
        @pipeline.stage("analysis")
        async def boom(job_id: str) -> dict:
            raise ConnectionError("peer closed the connection")

        await boom("job-1")

        updates = _status_updates(calls)
        assert updates, "a job left running is a job nobody knows is dead"
        assert updates[0]["status"] == "failed"
        assert updates[0]["job_id"] == "job-1"

    @pytest.mark.asyncio
    async def test_the_reason_survives(self, calls):
        @pipeline.stage("analysis")
        async def boom(job_id: str) -> dict:
            raise ConnectionError("peer closed the connection")

        result = await boom("job-1")

        assert "peer closed the connection" in result["error"]
        assert "peer closed the connection" in _status_updates(calls)[0]["error"]

    @pytest.mark.asyncio
    async def test_the_stage_name_is_recorded(self, calls):
        @pipeline.stage("playback")
        async def boom(job_id: str) -> dict:
            raise RuntimeError("ffmpeg died")

        await boom("job-1")

        assert _status_updates(calls)[0]["stage"] == "playback"

    @pytest.mark.asyncio
    async def test_the_error_is_returned_not_raised(self, calls):
        # The pipeline runs as a sequence of agent tools; an exception escaping
        # here ends the run without the later stages reporting anything.
        @pipeline.stage("ingest")
        async def boom(job_id: str) -> dict:
            raise RuntimeError("nope")

        result = await boom("job-1")

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_the_job_id_is_found_when_passed_by_keyword(self, calls):
        @pipeline.stage("ingest")
        async def boom(job_id: str) -> dict:
            raise RuntimeError("nope")

        await boom(job_id="job-kw")

        assert _status_updates(calls)[0]["job_id"] == "job-kw"


class TestSuccessIsUntouched:
    @pytest.mark.asyncio
    async def test_a_stage_that_works_is_passed_through(self, calls):
        @pipeline.stage("ingest")
        async def fine(job_id: str) -> dict:
            return {"status": "success", "job_id": job_id, "segments": 13}

        result = await fine("job-1")

        assert result == {"status": "success", "job_id": "job-1", "segments": 13}
        assert not _status_updates(calls), "a working stage must not touch status"

    @pytest.mark.asyncio
    async def test_the_wrapped_name_is_preserved(self):
        # ADK builds the tool's schema from the function, so a wrapper that
        # replaced its name would rename the tool.
        assert pipeline.inspect_source.__name__ == "inspect_source"
        assert "Probe" in (pipeline.inspect_source.__doc__ or "")


class TestReportingFailureIsSurvivable:
    @pytest.mark.asyncio
    async def test_an_unreportable_failure_still_returns_the_original_error(self):
        # If the catalog is what died, recording the failure fails too. The
        # original reason must not be lost behind that second error.
        async def always_down(*args, **kwargs):
            raise ConnectionError("catalog unreachable")

        with patch.object(pipeline.mcp_client, "call_tool", always_down):
            @pipeline.stage("analysis")
            async def boom(job_id: str) -> dict:
                raise RuntimeError("the original problem")

            result = await boom("job-1")

        assert result["status"] == "error"
        assert "the original problem" in result["error"]
