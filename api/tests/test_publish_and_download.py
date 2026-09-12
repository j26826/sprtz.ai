"""Downloading a moment, and publishing one to YouTube.

Both start from the same place: a trim the browser sends and a moment on
record. The rule worth testing is that the record bounds the trim — a publish
preview exists so an editor can breathe a second either side of a play, not so
"this moment" can become half the match under a moment's name.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import jobs

MOMENT = {
    "moment_id": "m-1",
    "job_id": "job-1",
    "label": "Halt and salute",
    "moment_type": "halt_and_salute",
    "start_sec": 600.0,
    "end_sec": 612.0,
}


def run(coro):
    return asyncio.run(coro)


def _catalog(moment=MOMENT):
    async def call(server, tool, args=None):
        if tool == "get_moment":
            return {"status": "success", "moment": moment} if moment else {"status": "error"}
        return {"status": "success"}
    return AsyncMock(side_effect=call)


class TestTheTrimIsBoundedByTheRecord:
    def test_no_trim_means_the_moments_own_times(self):
        with patch.object(jobs.clients, "call_mcp", _catalog()):
            _, start, end = run(jobs._cut_range("job-1", "m-1", jobs.CutRequest()))
        assert (start, end) == (600.0, 612.0)

    def test_a_small_trim_is_taken_as_asked(self):
        body = jobs.CutRequest(start_sec=597.5, end_sec=615.0)
        with patch.object(jobs.clients, "call_mcp", _catalog()):
            _, start, end = run(jobs._cut_range("job-1", "m-1", body))
        assert (start, end) == (597.5, 615.0)

    def test_a_trim_past_the_slack_is_clamped_rather_than_refused(self):
        # A slider dragged to its end should stop, not fail.
        body = jobs.CutRequest(start_sec=0.0, end_sec=9999.0)
        with patch.object(jobs.clients, "call_mcp", _catalog()):
            _, start, end = run(jobs._cut_range("job-1", "m-1", body))
        assert start == 600.0 - jobs._TRIM_SLACK_SEC
        assert end == 612.0 + jobs._TRIM_SLACK_SEC

    def test_nothing_can_ask_for_more_than_the_ceiling(self):
        far = {**MOMENT, "start_sec": 0.0, "end_sec": 7200.0}
        body = jobs.CutRequest(start_sec=0.0, end_sec=7200.0)
        with patch.object(jobs.clients, "call_mcp", _catalog(far)):
            _, start, end = run(jobs._cut_range("job-1", "m-1", body))
        assert end - start == jobs._MAX_CUT_SEC

    def test_an_inverted_trim_still_produces_a_playable_range(self):
        body = jobs.CutRequest(start_sec=611.0, end_sec=601.0)
        with patch.object(jobs.clients, "call_mcp", _catalog()):
            _, start, end = run(jobs._cut_range("job-1", "m-1", body))
        assert end > start

    def test_a_moment_the_job_does_not_hold_is_a_404(self):
        with patch.object(jobs.clients, "call_mcp", _catalog(None)):
            with pytest.raises(HTTPException) as caught:
                run(jobs._cut_range("job-1", "m-1", jobs.CutRequest()))
        assert caught.value.status_code == 404

    def test_a_moment_id_that_is_not_one_never_reaches_the_catalog(self):
        # The id lands in an object path, so it is checked before it is used.
        async def call(server, tool, args=None):  # pragma: no cover - must not run
            raise AssertionError("asked the catalog for a bad id")

        with patch.object(jobs.clients, "call_mcp", AsyncMock(side_effect=call)):
            with pytest.raises(HTTPException) as caught:
                run(jobs._cut_range("job-1", "../../etc/passwd", jobs.CutRequest()))
        assert caught.value.status_code == 404


class TestCutting:
    def test_a_job_with_no_source_says_what_to_do_about_it(self):
        # A live event has chunks rather than a file until something joins them.
        with pytest.raises(HTTPException) as caught:
            run(jobs._render_cut("job-1", "m-1", {"source": {}}, 1.0, 2.0))
        assert caught.value.status_code == 409
        assert "Prepare playback" in caught.value.detail

    def test_a_failed_cut_carries_the_reason(self):
        async def call(server, tool, args=None):
            return {"status": "error", "error": "ffmpeg: Invalid data found"}

        with patch.object(jobs.clients, "call_mcp", AsyncMock(side_effect=call)):
            with pytest.raises(HTTPException) as caught:
                run(jobs._render_cut("job-1", "m-1", {"source": {"gcsUri": "gs://b/o"}}, 1.0, 2.0))
        assert caught.value.status_code == 502
        assert "ffmpeg" in caught.value.detail

    def test_the_cut_is_asked_for_with_the_resolved_range(self):
        seen = {}

        async def call(server, tool, args=None):
            seen.update(args or {})
            return {"status": "success", "output_uri": "gs://media/jobs/job-1/downloads/m-1.mp4"}

        with patch.object(jobs.clients, "call_mcp", AsyncMock(side_effect=call)):
            uri = run(jobs._render_cut("job-1", "m-1", {"source": {"gcsUri": "gs://b/o"}}, 5.0, 9.0))
        assert uri.endswith("/downloads/m-1.mp4")
        assert (seen["start_sec"], seen["end_sec"]) == (5.0, 9.0)


class TestTheDownloadFilename:
    def test_it_names_the_match_the_moment_and_the_timecode(self):
        name = jobs._download_name({"title": "LeMieux Championships"}, MOMENT)
        assert name == "LeMieux-Championships-Halt-and-salute-10m00s.mp4"

    def test_it_survives_a_title_full_of_punctuation(self):
        name = jobs._download_name({"title": 'SWE v DEN — "quarter" / final'}, MOMENT)
        assert "/" not in name and '"' not in name
        assert name.endswith(".mp4")

    def test_a_nameless_job_still_gets_a_filename(self):
        assert jobs._download_name({}, {}).endswith(".mp4")
