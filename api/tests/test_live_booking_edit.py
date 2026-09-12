"""Editing a live event that has not started.

A booking is made hours ahead, and the window and the playlist URL are the two
things most likely to be wrong by the time it comes round. What matters here is
the line it will not cross: once the recorder is running, the window is what the
recorder was started with and the chunks are numbered against it, so moving it
would leave the event's timeline disagreeing with the recording of it.
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import jobs

NOW = datetime.datetime.now(datetime.UTC)
LATER = NOW + datetime.timedelta(hours=3)


def run(coro):
    return asyncio.run(coro)


def booking(**over) -> dict:
    job = {
        "job_id": "job-1",
        "kind": "live",
        "status": "scheduled",
        "title": "Ring 1",
        "hlsUrl": "https://stream.example/live.m3u8",
        "live": {
            "eventStart": (NOW + datetime.timedelta(hours=2)).isoformat(),
            "eventEnd": LATER.isoformat(),
        },
    }
    job.update(over)
    return job


def calls(job, wrote=None):
    async def call(server, tool, args=None):
        if tool == "get_job":
            return job
        if tool == "update_live_booking" and wrote is not None:
            wrote.append(args)
        return {"status": "success", "job_id": "job-1", "changed": ["live.eventStart"]}
    return AsyncMock(side_effect=call)


def edit(**fields) -> jobs.LiveBookingRequest:
    return jobs.LiveBookingRequest(**fields)


class TestWhatCanBeEdited:
    def test_a_scheduled_event_takes_a_new_window(self):
        wrote: list[dict] = []
        start = NOW + datetime.timedelta(hours=4)
        end = NOW + datetime.timedelta(hours=6)
        with patch.object(jobs.clients, "call_mcp", calls(booking(), wrote)):
            run(jobs.update_live_booking(
                "job-1", edit(event_start=start, event_end=end), user=None))

        assert wrote[-1]["event_start"] == start.isoformat()
        assert wrote[-1]["event_end"] == end.isoformat()

    def test_one_field_at_a_time_leaves_the_rest_alone(self):
        # An edit is a correction, not a re-entry of the whole form.
        wrote: list[dict] = []
        with patch.object(jobs.clients, "call_mcp", calls(booking(), wrote)):
            run(jobs.update_live_booking(
                "job-1", edit(hls_url="https://stream.example/other.m3u8"), user=None))

        sent = wrote[-1]
        assert sent["hls_url"].endswith("other.m3u8")
        assert "title" not in sent and "sport" not in sent
        # The window travels anyway, because it is what the validity checks ran
        # against and the store should write what was judged.
        assert sent["event_start"] and sent["event_end"]

    def test_context_links_can_be_emptied(self):
        # None means "leave them"; an empty list means "there are none now".
        wrote: list[dict] = []
        with patch.object(jobs.clients, "call_mcp", calls(booking(), wrote)):
            run(jobs.update_live_booking("job-1", edit(context_urls=[]), user=None))

        assert wrote[-1]["context_urls"] == []


class TestWhatItRefuses:
    def _refused(self, job, body=None):
        with patch.object(jobs.clients, "call_mcp", calls(job)):
            with pytest.raises(HTTPException) as caught:
                run(jobs.update_live_booking("job-1", body or edit(title="New"), user=None))
        return caught.value

    def test_an_event_that_has_started(self):
        error = self._refused(booking(status="recording"))
        assert error.status_code == 409
        assert "already started" in error.detail

    def test_an_event_whose_recorder_exists_even_if_the_status_lags(self):
        # The tick starts the capture before the status write lands.
        error = self._refused(booking(live={
            "eventStart": NOW.isoformat(), "eventEnd": LATER.isoformat(),
            "capture": {"execution": "projects/p/locations/l/jobs/j/executions/e"},
        }))
        assert error.status_code == 409
        assert "recorder" in error.detail

    def test_a_match_that_is_not_a_live_event(self):
        error = self._refused(booking(kind="upload"))
        assert error.status_code == 409

    def test_a_window_that_ends_before_it_starts(self):
        error = self._refused(booking(), edit(
            event_start=NOW + datetime.timedelta(hours=5),
            event_end=NOW + datetime.timedelta(hours=4)))
        assert error.status_code == 400
        assert "end after it starts" in error.detail

    def test_a_window_longer_than_the_ceiling(self):
        error = self._refused(booking(), edit(
            event_start=NOW, event_end=NOW + datetime.timedelta(hours=20)))
        assert error.status_code == 400

    def test_a_window_that_is_already_over(self):
        error = self._refused(booking(), edit(
            event_start=NOW - datetime.timedelta(hours=3),
            event_end=NOW - datetime.timedelta(hours=1)))
        assert error.status_code == 400
        assert "already ended" in error.detail

    def test_a_naive_time_is_a_time_in_somebody_s_head(self):
        with pytest.raises(ValueError):
            edit(event_start=datetime.datetime(2027, 1, 1, 12, 0))
