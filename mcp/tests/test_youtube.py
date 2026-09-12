"""Publishing to YouTube, against fakes.

The two failures worth covering cannot be reached against the real API from a
test: a refresh token that has been revoked, and an upload YouTube starts and
then rejects. Both are the editor's to act on, so what is asserted is mostly
what the message says.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import youtube  # noqa: E402


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Doc:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Db:
    def __init__(self, data):
        self._data = data

    def collection(self, _name):
        return self

    def document(self, _name):
        return self

    def get(self):
        return _Doc(self._data)


CREDS = {"clientId": "id", "clientSecret": "secret", "refreshToken": "refresh"}


class TestCredentials:
    def test_the_stored_channel_beats_the_deployment_client(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "from-terraform")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "also-from-terraform")
        creds = youtube.credentials(_Db({"clientId": "typed-in", "refreshToken": "r"}))

        assert creds["clientId"] == "typed-in"
        # Not overridden in settings, so the deployment's own client stands.
        assert creds["clientSecret"] == "also-from-terraform"
        assert creds["refreshToken"] == "r"

    def test_an_unconfigured_deployment_is_empty_rather_than_a_crash(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
        monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
        creds = youtube.credentials(_Db(None))

        assert youtube.missing_fields(creds) == ["clientId", "clientSecret", "refreshToken"]

    def test_missing_fields_are_named_in_the_order_they_are_asked_for(self):
        assert youtube.missing_fields({"clientId": "a"}) == ["clientSecret", "refreshToken"]


class TestAccessToken:
    def test_it_exchanges_the_refresh_token(self):
        seen = {}

        def post(url, data=None, timeout=None):
            seen.update({"url": url, "data": data})
            return _Response(200, {"access_token": "ya29.x"})

        assert youtube.access_token(CREDS, post=post) == "ya29.x"
        assert seen["url"] == youtube.TOKEN_URL
        assert seen["data"]["grant_type"] == "refresh_token"

    def test_a_revoked_token_says_to_reconnect(self):
        def post(url, data=None, timeout=None):
            return _Response(400, {"error": "invalid_grant",
                                   "error_description": "Token has been expired or revoked."})

        with pytest.raises(youtube.YouTubeError) as caught:
            youtube.access_token(CREDS, post=post)

        # The code says nothing an editor can act on; the sentence does.
        assert "reconnect the channel" in str(caught.value).lower()

    def test_it_refuses_before_the_network_when_nothing_is_configured(self):
        def post(*_args, **_kwargs):  # pragma: no cover - must not be called
            raise AssertionError("asked YouTube with no credentials")

        with pytest.raises(youtube.YouTubeError) as caught:
            youtube.access_token({}, post=post)

        assert "not configured" in str(caught.value)


class TestUpload:
    def _mp4(self, tmp_path, size=2048):
        path = tmp_path / "cut.mp4"
        path.write_bytes(b"\0" * size)
        return path

    def test_it_uploads_and_returns_the_watch_url(self, tmp_path):
        calls = {}

        def post(url, params=None, headers=None, data=None, timeout=None):
            calls["start"] = {"url": url, "params": params, "headers": headers, "body": data}
            return _Response(200, {}, {"Location": "https://upload.example/session"})

        def put(url, headers=None, data=None, timeout=None):
            calls["put"] = {"url": url, "headers": headers}
            return _Response(200, {"id": "abc123"})

        out = youtube.upload(self._mp4(tmp_path), token="t", title="Halt and salute",
                             description="At 01:12.", privacy="unlisted", post=post, put=put)

        assert out == {"video_id": "abc123", "url": youtube.WATCH_URL + "abc123",
                       "privacy": "unlisted"}
        assert calls["start"]["params"]["uploadType"] == "resumable"
        assert calls["put"]["url"] == "https://upload.example/session"
        assert '"privacyStatus": "unlisted"' in calls["start"]["body"]

    def test_a_title_past_a_hundred_characters_is_cut_not_rejected(self, tmp_path):
        body = {}

        def post(url, params=None, headers=None, data=None, timeout=None):
            body["json"] = data
            return _Response(200, {}, {"Location": "https://upload.example/session"})

        def put(url, headers=None, data=None, timeout=None):
            return _Response(200, {"id": "x"})

        youtube.upload(self._mp4(tmp_path), token="t", title="A" * 200, post=post, put=put)

        import json as _json
        assert len(_json.loads(body["json"])["snippet"]["title"]) == 100

    def test_a_rejected_upload_carries_youtubes_own_reason(self, tmp_path):
        def post(url, params=None, headers=None, data=None, timeout=None):
            return _Response(200, {}, {"Location": "https://upload.example/session"})

        def put(url, headers=None, data=None, timeout=None):
            return _Response(403, {"error": {"message": "The user has exceeded the number of "
                                                        "videos they may upload.",
                                             "errors": [{"reason": "uploadLimitExceeded"}]}})

        with pytest.raises(youtube.YouTubeError) as caught:
            youtube.upload(self._mp4(tmp_path), token="t", title="One", post=post, put=put)

        assert "exceeded" in str(caught.value)
        assert "uploadLimitExceeded" in str(caught.value)

    def test_a_start_with_no_session_url_is_an_error_not_a_silent_success(self, tmp_path):
        def post(url, params=None, headers=None, data=None, timeout=None):
            return _Response(200, {}, {})

        def put(*_args, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("uploaded with no session")

        with pytest.raises(youtube.YouTubeError):
            youtube.upload(self._mp4(tmp_path), token="t", title="One", post=post, put=put)

    def test_a_privacy_it_does_not_know_never_reaches_the_api(self, tmp_path):
        def post(*_args, **_kwargs):  # pragma: no cover - must not be called
            raise AssertionError("asked YouTube with a bad privacy value")

        with pytest.raises(youtube.YouTubeError):
            youtube.upload(self._mp4(tmp_path), token="t", title="One",
                           privacy="everyone", post=post)

    def test_the_error_detail_never_quotes_the_whole_body(self):
        # The body of a failed upload start carries the request back, and that
        # request has an access token in it.
        detail = youtube._error_detail(_Response(401, {"error": {"message": "Invalid Credentials"}}))
        assert detail == "Invalid Credentials"
