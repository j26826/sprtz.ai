"""A live event's chunks become one recording so it can be watched.

A live event has no source video — it has a row of five-minute chunks — so
`prepare_playback` refused it with "has no source video" and every moment
the analysis had found opened on "This match has not been packaged for
playback yet". Joining the chunks is a server-side compose in the bucket
they already live in, so it costs a few API calls whatever the length, and
it can be asked for again as the event grows.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline


def _chunks(n: int, muxed: bool = False):
    return [{"index": i, "status": "analysed",
             "gcsUri": f"gs://media/jobs/j1/live/chunks/chunk_{i:04d}.ts",
             **({"muxedUri": f"gs://media/jobs/j1/live/chunks/chunk_{i:04d}_muxed.ts"} if muxed else {})}
            for i in range(n)]


def _calls(state, tool):
    return [a for t, a in state["calls"] if t == tool]


def _responder(state, chunks, compose_ok=True):
    async def call(server, tool, args=None):
        state["calls"].append((tool, args or {}))
        if tool == "list_live_chunks":
            return {"status": "success", "chunks": chunks}
        if tool == "compose_live_source":
            if not compose_ok:
                return {"status": "error", "error": "compose cannot cross buckets"}
            return {"status": "success", "gcs_uri": "gs://media/jobs/j1/live/source.ts",
                    "bytes": 900_000_000, "original_name": "source.ts",
                    "content_type": "video/mp2t", "chunks": len(chunks)}
        return {"status": "success"}
    return call


class TestJoiningTheChunks:
    @pytest.mark.asyncio
    async def test_every_closed_chunk_goes_in_index_order(self):
        state = {"calls": []}
        chunks = list(reversed(_chunks(4)))  # the listing's order must not matter
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=_responder(state, chunks))):
            uri = await pipeline._compose_live_source("j1")
        assert uri == "gs://media/jobs/j1/live/source.ts"
        sent = _calls(state, "compose_live_source")[0]["chunk_uris"]
        assert sent == [f"gs://media/jobs/j1/live/chunks/chunk_{i:04d}.ts" for i in range(4)]

    @pytest.mark.asyncio
    async def test_a_muxed_chunk_is_the_one_with_the_sound_in_it(self):
        state = {"calls": []}
        with patch.object(pipeline.mcp_client, "call_tool",
                          AsyncMock(side_effect=_responder(state, _chunks(2, muxed=True)))):
            await pipeline._compose_live_source("j1")
        sent = _calls(state, "compose_live_source")[0]["chunk_uris"]
        assert all(u.endswith("_muxed.ts") for u in sent)

    @pytest.mark.asyncio
    async def test_the_recording_lands_on_the_job(self):
        state = {"calls": []}
        with patch.object(pipeline.mcp_client, "call_tool",
                          AsyncMock(side_effect=_responder(state, _chunks(3)))):
            await pipeline._compose_live_source("j1")
        source = _calls(state, "set_source")[0]
        assert source["gcs_uri"].endswith("live/source.ts")
        assert source["size_bytes"] == 900_000_000

    @pytest.mark.asyncio
    async def test_an_event_with_no_closed_chunk_yet_has_nothing_to_join(self):
        state = {"calls": []}
        with patch.object(pipeline.mcp_client, "call_tool",
                          AsyncMock(side_effect=_responder(state, []))):
            assert await pipeline._compose_live_source("j1") == ""
        assert not _calls(state, "compose_live_source")
        assert not _calls(state, "set_source")

    @pytest.mark.asyncio
    async def test_a_compose_that_fails_says_so_and_records_nothing(self):
        state = {"calls": []}
        with patch.object(pipeline.mcp_client, "call_tool",
                          AsyncMock(side_effect=_responder(state, _chunks(2), compose_ok=False))):
            assert await pipeline._compose_live_source("j1") == ""
        assert not _calls(state, "set_source")
        errors = [a for a in _calls(state, "emit_event") if a.get("level") == "error"]
        assert errors and "could not be joined" in errors[0]["message"]


class TestPreparePlaybackReachesForIt:
    def test_a_live_job_without_a_source_composes_one_first(self):
        from pathlib import Path as P

        body = P(pipeline.__file__).read_text()
        start = body.index("async def prepare_playback")
        stage = body[start:body.index("async def _await_transcode")]
        assert 'job.get("kind") == "live"' in stage
        assert "_compose_live_source(job_id)" in stage
        assert 'has no source video' in stage, "an upload with no source still says so"
