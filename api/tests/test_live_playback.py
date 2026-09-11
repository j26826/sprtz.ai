"""GET /api/jobs/{job_id}/playback for a live event: the stream its recorder writes.

A live event had nothing to play until someone packaged it, so every moment
found while it was on opened on "not packaged for playback yet". Its recorder
now writes the event as a stream beside the recording, and this is where the
player is pointed at it — through the same signed cookie, which is scoped to
the job's whole prefix and so already covers it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.auth import CallerIdentity
from app.routers import jobs

USER = CallerIdentity(uid="u1", email="editor@example.com")
SETTINGS = SimpleNamespace(
    cdn_base_url="https://demo.arenos.ai", cdn_signing_key_name="k",
    cdn_signing_key="MDEyMzQ1Njc4OWFiY2RlZg", cdn_signed_url_ttl=600, cdn_cookie_domain="")

PACKAGE = {"hlsUrl": "http://34.8.154.165/jobs/j1/hls/master.m3u8", "renditions": ["480p"]}


def _live(state="recording", stream="jobs/j1/live/index.m3u8", playback=None):
    return {"job_id": "j1", "kind": "live", "status": "analyzing",
            "playback": playback or {},
            "live": {"capture": {"state": state, "stream": stream}}}


def _play(job):
    request = Request({"type": "http", "method": "GET", "path": "/api/jobs/j1/playback",
                       "headers": [], "server": ("demo.arenos.ai", 443), "scheme": "https"})
    with patch.object(jobs.clients, "call_mcp", AsyncMock(return_value=job)):
        return asyncio.run(jobs.get_playback("j1", request, Response(), USER, SETTINGS))


class TestALiveEventPlays:
    def test_from_its_stream_while_the_event_is_on(self):
        out = _play(_live())
        assert out["hls_url"] == "https://demo.arenos.ai/jobs/j1/live/index.m3u8"
        assert out["source"] == "live"

    def test_and_as_a_recording_once_it_has_finished(self):
        assert _play(_live(state="finished"))["source"] == "recorded"

    def test_the_stream_wins_over_a_package_made_mid_event(self):
        """A package is a snapshot of the chunks when it was made; every moment
        found after it would seek past its end."""
        out = _play(_live(playback=PACKAGE))
        assert out["hls_url"].endswith("/jobs/j1/live/index.m3u8")

    def test_the_url_is_built_from_the_job_not_from_what_was_stored(self):
        """Nothing written to the job can point a player somewhere else."""
        out = _play(_live(stream="jobs/someone-else/live/index.m3u8"))
        assert out["hls_url"] == "https://demo.arenos.ai/jobs/j1/live/index.m3u8"


class TestWhenThereIsNothingToPlay:
    def test_a_live_event_with_no_segment_yet_is_still_being_prepared(self):
        with pytest.raises(HTTPException) as err:
            _play(_live(stream=""))
        assert err.value.status_code == 409

    def test_an_upload_still_plays_its_package(self):
        job = {"job_id": "j1", "kind": "upload", "playback": PACKAGE}
        out = _play(job)
        assert out["hls_url"] == "https://demo.arenos.ai/jobs/j1/hls/master.m3u8"
        assert out["source"] == "package"

    def test_an_upload_is_never_played_from_a_stream_field(self):
        job = {"job_id": "j1", "kind": "upload", "playback": {},
               "live": {"capture": {"stream": "jobs/j1/live/index.m3u8"}}}
        with pytest.raises(HTTPException):
            _play(job)
