"""Connecting a YouTube channel, and what the browser is told about it.

The refresh token is a standing permission to post to someone's channel. It
goes from Google to this service to Firestore and out only to the media service
that uploads with it — never to the browser, and never into a log.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.routers import integrations


def run(coro):
    return asyncio.run(coro)


def settings(**over) -> Settings:
    base = {"youtube_client_id": "", "youtube_client_secret": "", "youtube_redirect_uri": ""}
    return Settings(**{**base, **over})


def catalog(config=None, record=None):
    async def call(server, tool, args=None):
        if tool == "get_config":
            return {"status": "success", "config": config or {}}
        if record is not None:
            record.append((tool, args))
        return {"status": "success"}
    return AsyncMock(side_effect=call)


class TestStatus:
    def test_it_says_what_is_set_and_never_what_it_is(self):
        stored = {"clientId": "id", "clientSecret": "shhh", "refreshToken": "1//secret",
                  "channelTitle": "Arena TV"}
        with patch.object(integrations.clients, "call_mcp", catalog(stored)):
            out = run(integrations.youtube_status(user=None, settings=settings()))

        assert out["client_configured"] and out["connected"]
        assert out["channel_title"] == "Arena TV"
        assert "shhh" not in str(out) and "1//secret" not in str(out)

    def test_a_deployment_supplied_client_is_reported_as_such(self):
        with patch.object(integrations.clients, "call_mcp", catalog({})):
            out = run(integrations.youtube_status(
                user=None,
                settings=settings(youtube_client_id="from-tf", youtube_client_secret="tf"),
            ))

        assert out["client_configured"]
        assert out["client_from_deployment"]
        assert not out["connected"]

    def test_connecting_needs_a_redirect_uri_as_well_as_a_client(self):
        with patch.object(integrations.clients, "call_mcp", catalog({})):
            out = run(integrations.youtube_status(
                user=None,
                settings=settings(youtube_client_id="i", youtube_client_secret="s"),
            ))
        assert out["client_configured"]
        # Registered by hand on the client; without it Google refuses the flow.
        assert not out["can_connect"]


class TestTheConsentUrl:
    def test_it_asks_for_a_refresh_token_every_time(self):
        with patch.object(integrations.clients, "call_mcp", catalog({})):
            out = run(integrations.youtube_auth_url(
                user=None,
                settings=settings(youtube_client_id="i", youtube_client_secret="s",
                                  youtube_redirect_uri="https://desk.example/api/integrations/youtube/callback"),
            ))

        # Without prompt=consent, reconnecting returns an access token that
        # expires in an hour and nothing that outlives it.
        assert "access_type=offline" in out["url"]
        assert "prompt=consent" in out["url"]
        assert "youtube.upload" in out["url"]

    def test_it_refuses_before_google_when_there_is_no_client(self):
        with patch.object(integrations.clients, "call_mcp", catalog({})):
            with pytest.raises(HTTPException) as caught:
                run(integrations.youtube_auth_url(user=None, settings=settings()))
        assert caught.value.status_code == 409


class TestSaving:
    def test_a_pasted_token_forgets_the_previous_channels_name(self):
        wrote = []
        with patch.object(integrations.clients, "call_mcp", catalog({}, wrote)):
            run(integrations.save_youtube_settings(
                integrations.YouTubeSettings(refresh_token="1//new"), user=None))

        _, args = wrote[-1]
        # The token belongs to whatever channel granted it, and this cannot
        # know which — the old name beside a new token is worse than no name.
        assert args["values"]["channelTitle"] == ""

    def test_fields_left_out_are_left_alone(self):
        wrote = []
        with patch.object(integrations.clients, "call_mcp", catalog({}, wrote)):
            run(integrations.save_youtube_settings(
                integrations.YouTubeSettings(privacy="unlisted"), user=None))

        _, args = wrote[-1]
        assert set(args["values"]) == {"privacy"}

    def test_disconnecting_keeps_the_client_and_drops_the_channel(self):
        wrote = []
        with patch.object(integrations.clients, "call_mcp", catalog({}, wrote)):
            out = run(integrations.disconnect_youtube(user=None))

        tool, args = wrote[-1]
        assert tool == "clear_config"
        assert args["fields"] == ["refreshToken", "channelTitle"]
        assert out["connected"] is False


class TestTheCallback:
    def test_a_refusal_from_google_is_a_page_not_a_crash(self):
        page = run(integrations.youtube_callback(code="", error="access_denied",
                                                 settings=settings()))
        assert page.status_code == 200
        assert b"not connected" in page.body

    def test_an_approval_with_no_refresh_token_says_what_to_do(self):
        class _Response:
            status_code = 200

            def json(self):
                return {"access_token": "ya29.x"}

        class _Http:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, *_args, **_kwargs):
                return _Response()

        with patch.object(integrations.httpx, "AsyncClient", lambda **_: _Http()), \
                patch.object(integrations.clients, "call_mcp", catalog({})):
            page = run(integrations.youtube_callback(
                code="4/abc",
                settings=settings(youtube_client_id="i", youtube_client_secret="s",
                                  youtube_redirect_uri="https://desk.example/cb"),
            ))

        # Google returns one only with consent freshly granted, so the fix is
        # to withdraw the app's access and approve it again.
        assert b"no refresh token" in page.body.lower()
        assert b"Google account" in page.body
