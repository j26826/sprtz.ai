"""Deleting a live event stops its recorder.

The recorder is a Cloud Run Job execution that knows the job only by id.
Deleting the document under it left it recording chunks for nothing until
the event's end — the one seen was still retrying a dead playlist a minute
after its job had gone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline

EXECUTION = "projects/p/locations/l/jobs/live-capture/executions/e1"


def _calls(kind="live", capture_state="recording", execution=EXECUTION):
    state = {"calls": []}

    async def call(server, tool, args=None):
        state["calls"].append((tool, args or {}))
        if tool == "get_job":
            live = {"capture": {"execution": execution, "state": capture_state}} if kind == "live" else {}
            return {"job_id": args["job_id"], "kind": kind, "source": {"gcsUri": "gs://u/v"}, "live": live}
        if tool == "cancel_live_capture":
            return {"status": "cancelled", "execution": args["execution"]}
        return {"status": "success"}
    return state, AsyncMock(side_effect=call)


class TestDeletingALiveEvent:
    @pytest.mark.asyncio
    async def test_the_recorder_is_stopped_before_the_media_goes(self):
        state, mock = _calls()
        with patch.object(pipeline.mcp_client, "call_tool", mock):
            out = await pipeline.delete_job("j1")
        assert out["status"] == "success"
        tools = [t for t, _ in state["calls"]]
        assert "cancel_live_capture" in tools
        assert tools.index("cancel_live_capture") < tools.index("delete_job_media")
        cancel = [a for t, a in state["calls"] if t == "cancel_live_capture"][0]
        assert cancel["execution"] == EXECUTION

    @pytest.mark.asyncio
    async def test_a_recorder_that_already_finished_is_left_alone(self):
        state, mock = _calls(capture_state="finished")
        with patch.object(pipeline.mcp_client, "call_tool", mock):
            await pipeline.delete_job("j1")
        assert "cancel_live_capture" not in [t for t, _ in state["calls"]]

    @pytest.mark.asyncio
    async def test_an_upload_has_no_recorder_to_stop(self):
        state, mock = _calls(kind="upload")
        with patch.object(pipeline.mcp_client, "call_tool", mock):
            await pipeline.delete_job("j1")
        assert "cancel_live_capture" not in [t for t, _ in state["calls"]]
