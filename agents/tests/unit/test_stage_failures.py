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


class TestAFailedRunStops:
    """The pipeline is a sequence of agents; an error return does not stop the next one.

    The download failed on an expired link, the analysis then found nothing,
    and the finish wrote "the analysis produced no moments" over the real
    reason — so the editor was told to re-run a job whose link was dead.
    """

    def _job(self, status, error=""):
        async def call(server, tool, args=None):
            if tool == "get_job":
                return {"job_id": args["job_id"], "status": status, "error": error}
            return {"status": "success"}
        return AsyncMock(side_effect=call)

    @pytest.mark.asyncio
    async def test_a_later_stage_skips_a_job_that_already_failed(self):
        ran = []

        @pipeline.stage("finalize", skip_if_failed=True)
        async def later(job_id: str) -> dict:
            ran.append(job_id)
            return {"status": "success"}

        mock = self._job("failed", "The HLS download failed: HTTP 403")
        with patch.object(pipeline.mcp_client, "call_tool", mock):
            out = await later("job-1")

        assert out["status"] == "skipped"
        assert "HTTP 403" in out["error"]
        assert not ran
        assert not _status_updates(mock), "the reason on the job is the earlier stage's"

    @pytest.mark.asyncio
    async def test_a_cancelled_job_stops_the_stages_after_it(self):
        # Cancelling is what an editor does to a run they want stopped, and
        # the stages after the cancelled one carried on and marked the job
        # failed with "the analysis produced no moments" — the one thing
        # cancelling promises not to do is report the run as broken.
        ran = []

        @pipeline.stage("captions", skip_if_failed=True)
        async def later(job_id: str) -> dict:
            ran.append(job_id)
            return {"status": "success"}

        for status in ("cancelled", "cancelling"):
            ran.clear()
            mock = self._job(status)
            with patch.object(pipeline.mcp_client, "call_tool", mock):
                out = await later("job-1")
            assert out["status"] == "skipped", status
            assert not ran, status
            assert not _status_updates(mock), "the row keeps the cancel, not a failure"

    @pytest.mark.asyncio
    async def test_a_running_job_goes_through(self):
        @pipeline.stage("finalize", skip_if_failed=True)
        async def later(job_id: str) -> dict:
            return {"status": "success", "ran": True}

        with patch.object(pipeline.mcp_client, "call_tool", self._job("analyzing")):
            assert (await later("job-1")).get("ran")

    @pytest.mark.asyncio
    async def test_ingest_runs_on_a_failed_job_because_that_is_what_a_re_run_is(self):
        @pipeline.stage("ingest")
        async def first(job_id: str) -> dict:
            return {"status": "success", "ran": True}

        with patch.object(pipeline.mcp_client, "call_tool", self._job("failed", "old reason")):
            assert (await first("job-1")).get("ran")

    def test_the_stages_after_ingest_are_the_ones_that_skip(self):
        from pathlib import Path

        src = Path(pipeline.__file__).read_text()
        for name in ("analysis", "finalize"):
            assert f'@stage("{name}", skip_if_failed=True)' in src, name
        assert '@stage("ingest")\nasync def inspect_source' in src
        # Playback is also a tool the editor calls on its own, on a job whose
        # analysis may well have failed; it does not skip.
        assert '@stage("playback")\nasync def prepare_playback' in src
