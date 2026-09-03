"""Filling in stills for a match that was analysed without them.

Packaging has the same shape and the same reason: re-running an hour of Gemini
to fix a picture would be an hour spent on the wrong thing, and it would replace
moments the editor may already have worked from.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline


def _moment(moment_id: str, **over) -> dict:
    base = {
        "moment_id": moment_id, "job_id": "job-1", "moment_type": "jump_shot",
        "category": "shot", "label": "Jump shot", "start_sec": 100.0,
        "end_sec": 106.0, "peak_sec": 103.0, "confidence": 0.9,
        "excitement": 0.8, "highlight_score": 0.9, "description": "d",
        "evidence": [], "is_goal": False, "thumb_uri": "",
    }
    base.update(over)
    return base


@pytest.fixture
def catalog():
    """A job with a source and three moments, one of which already has a still."""
    state = {
        "job": {"status": "success", "source": {"gcsUri": "gs://uploads/u/job-1/v.mp4"}},
        "moments": [
            _moment("m1"),
            _moment("m2", thumb_uri="gs://media/jobs/job-1/moments/m2.png"),
            _moment("m3"),
        ],
    }

    async def call(server, tool, args=None):
        if tool == "get_job":
            return state["job"]
        if tool == "list_moments":
            return {"status": "success", "moments": state["moments"]}
        if tool == "generate_moment_thumbnails":
            return {"status": "success", "thumbnails": [
                {"moment_id": m["moment_id"],
                 "gcs_uri": f"gs://media/jobs/job-1/moments/{m['moment_id']}.png"}
                for m in args["moments"]
            ]}
        if tool == "record_moment_thumbnails":
            return {"status": "success", "saved": len(args["thumbnails"])}
        return {"status": "success"}

    mock = AsyncMock(side_effect=call)
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        yield mock, state


def _tool_calls(mock, tool):
    return [c.args[2] for c in mock.await_args_list if c.args[1] == tool]


class TestWhatItCuts:
    @pytest.mark.asyncio
    async def test_only_the_moments_without_one(self, catalog):
        mock, _ = catalog
        result = await pipeline.generate_thumbnails("job-1")

        asked = [m["moment_id"] for call in _tool_calls(mock, "generate_moment_thumbnails")
                 for m in call["moments"]]
        assert asked == ["m1", "m3"], "re-cutting the whole match is minutes nobody asked for"
        assert result["needed"] == 2
        assert result["thumbnails_saved"] == 2
        assert result["moments"] == 3

    @pytest.mark.asyncio
    async def test_the_still_is_taken_at_the_peak(self, catalog):
        mock, state = catalog
        state["moments"] = [_moment("m1", start_sec=100.0, peak_sec=103.0, end_sec=106.0)]
        await pipeline.generate_thumbnails("job-1")

        asked = _tool_calls(mock, "generate_moment_thumbnails")[0]["moments"]
        assert asked[0]["at_sec"] == 103.0

    @pytest.mark.asyncio
    async def test_it_does_not_clear_what_is_already_there(self, catalog):
        mock, _ = catalog
        await pipeline.generate_thumbnails("job-1")
        assert not _tool_calls(mock, "delete_moment_thumbnails"), (
            "the surviving files are the ones being kept"
        )

    @pytest.mark.asyncio
    async def test_it_leaves_the_job_stage_alone(self, catalog):
        # The job finished. Reporting progress here would set its stage back to
        # "analysis" and the strip would say it is analysing.
        mock, _ = catalog
        await pipeline.generate_thumbnails("job-1")
        assert not _tool_calls(mock, "update_job_status")


class TestWhenThereIsNothingToDo:
    @pytest.mark.asyncio
    async def test_every_moment_already_has_one(self, catalog):
        mock, state = catalog
        state["moments"] = [_moment("m1", thumb_uri="gs://media/x.png")]
        result = await pipeline.generate_thumbnails("job-1")

        assert result["needed"] == 0
        assert not _tool_calls(mock, "generate_moment_thumbnails")

    @pytest.mark.asyncio
    async def test_a_match_with_no_moments_says_so(self, catalog):
        _, state = catalog
        state["moments"] = []
        assert (await pipeline.generate_thumbnails("job-1"))["status"] == "empty"

    @pytest.mark.asyncio
    async def test_a_job_whose_source_is_gone(self, catalog):
        _, state = catalog
        state["job"] = {"status": "success", "source": {}}
        result = await pipeline.generate_thumbnails("job-1")
        assert result["status"] == "error"
        assert "no source video" in result["error"]
