"""A poll that cannot reach the media service is not news about the work.

The playback wait met a retired Cloud Run instance one poll in — the media
service had been replaced by a deploy twenty minutes earlier — took the
exception as a dead encode, and marked the job failed with "ConnectError: "
while Transcoder carried on for another half hour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sprtz_agents.tools import mcp_client, pipeline


class TestTheTranscoderWait:
    @pytest.mark.asyncio
    async def test_one_unreachable_poll_does_not_end_the_wait(self):
        polls = {"n": 0}

        async def call(server, tool, args=None):
            if tool == "transcode_status":
                polls["n"] += 1
                if polls["n"] == 1:
                    raise ConnectionError("All connection attempts failed")
                return {"status": "success", "state": "SUCCEEDED", "done": True, "succeeded": True}
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)), \
             patch.object(pipeline.asyncio, "sleep", AsyncMock()):
            out = await pipeline._await_transcode("j1", "projects/p/locations/l/jobs/x")
        assert out["succeeded"]
        assert polls["n"] == 2


class TestTheDownloadWait:
    @pytest.mark.asyncio
    async def test_a_few_unreachable_polls_are_waited_through(self):
        polls = {"n": 0}

        async def call(server, tool, args=None):
            if tool == "download_hls":
                return {"status": "started", "execution": "e"}
            if tool == "hls_download_status":
                polls["n"] += 1
                if polls["n"] <= 3:
                    raise ConnectionError("refused")
                return {"status": "succeeded", "gcs_uri": "gs://u/hls/j1/source/source.ts",
                        "original_name": "source.ts", "bytes": 10, "content_type": "video/mp2t"}
            if tool == "make_analysis_proxy":
                return {"status": "error", "error": "off"}
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)), \
             patch.object(pipeline.asyncio, "sleep", AsyncMock()):
            out = await pipeline._download_hls_source("j1", "https://x/v.m3u8")
        assert out["status"] == "success"

    @pytest.mark.asyncio
    async def test_a_service_that_stays_down_fails_the_job_with_that_reason(self):
        calls = []

        async def call(server, tool, args=None):
            calls.append((tool, args or {}))
            if tool == "download_hls":
                return {"status": "started", "execution": "e"}
            if tool == "hls_download_status":
                raise ConnectionError("refused")
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)), \
             patch.object(pipeline.asyncio, "sleep", AsyncMock()):
            out = await pipeline._download_hls_source("j1", "https://x/v.m3u8")
        assert out["status"] == "error"
        assert "could not be reached" in out["error"]
        failed = [a for t, a in calls if t == "update_job_status" and a.get("status") == "failed"]
        assert failed and "could not be reached" in failed[0]["error"]


class TestTheClientRetriesAConnectionThatWasNeverMade:
    @pytest.mark.asyncio
    async def test_a_connect_error_is_retried_and_a_sent_request_is_not(self):
        import httpx

        attempts = {"n": 0}
        ok = MagicMock()
        ok.raise_for_status = MagicMock()
        ok.text = '{"jsonrpc":"2.0","id":1,"result":{"structuredContent":{"status":"success"}}}'

        async def post(url, json=None, headers=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("All connection attempts failed")
            return ok

        client = MagicMock()
        client.post = post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        settings = MagicMock(mcp_media_url="https://media.test", mcp_catalog_url="https://catalog.test")
        with patch.object(mcp_client, "get_settings", return_value=settings), \
             patch.object(mcp_client, "_auth_headers", AsyncMock(return_value={})), \
             patch("httpx.AsyncClient", return_value=client), \
             patch.object(mcp_client.asyncio, "sleep", AsyncMock()):
            out = await mcp_client.call_tool("media", "transcode_status", {"transcoder_job": "x"})
        assert out == {"status": "success"}
        assert attempts["n"] == 3

    @pytest.mark.asyncio
    async def test_it_gives_up_after_the_last_attempt(self):
        import httpx

        async def post(url, json=None, headers=None):
            raise httpx.ConnectError("refused")

        client = MagicMock()
        client.post = post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        settings = MagicMock(mcp_media_url="https://media.test", mcp_catalog_url="https://catalog.test")
        with patch.object(mcp_client, "get_settings", return_value=settings), \
             patch.object(mcp_client, "_auth_headers", AsyncMock(return_value={})), \
             patch("httpx.AsyncClient", return_value=client), \
             patch.object(mcp_client.asyncio, "sleep", AsyncMock()):
            with pytest.raises(httpx.ConnectError):
                await mcp_client.call_tool("media", "transcode_status", {"transcoder_job": "x"})
