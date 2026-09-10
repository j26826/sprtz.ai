"""An HLS source: a job that starts with a URL and ends with an object.

The download runs as a Cloud Run Job through the media server and the ingest
stage waits on it; the 1 fps proxy is then a Transcoder job the stage waits
on the same way. What is checked here is the hand-over — that the object and
then the proxy land on the job — that a proxy that fails is a warning rather
than a failed run, and that the analysis reads the proxy while thumbnails and
clips keep reading the source.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline

PIPELINE = Path(pipeline.__file__).read_text()

DOWNLOADED = {
    "status": "succeeded", "gcs_uri": "gs://uploads/hls/j1/source/source.ts",
    "original_name": "source.ts", "bytes": 6_800_000_000, "content_type": "video/mp2t",
}


def responder(state, proxy_state="SUCCEEDED", proxy_start="started"):
    async def call(server, tool, args=None):
        state["calls"].append((tool, args or {}))
        if tool == "download_hls":
            return {"status": "started", "execution": "exec-9"}
        if tool == "hls_download_status":
            state["polls"] += 1
            return {"status": "running"} if state["polls"] < 3 else DOWNLOADED
        if tool == "make_analysis_proxy":
            if proxy_start != "started":
                return {"status": "error", "error": "MEDIA_BUCKET is not configured."}
            return {"status": "started", "transcoder_job": "projects/p/locations/l/jobs/px",
                    "analysis_uri": "gs://media/jobs/j1/proxy/proxy_1fps.mp4"}
        if tool == "transcode_status":
            state["proxy_polls"] += 1
            if state["proxy_polls"] < 2:
                return {"status": "success", "state": "RUNNING", "done": False, "succeeded": False}
            return {"status": "success", "state": proxy_state, "done": True,
                    "succeeded": proxy_state == "SUCCEEDED",
                    **({"error": "input has no video stream"} if proxy_state == "FAILED" else {})}
        return {"status": "success"}
    return call


@pytest.fixture
def media():
    state = {"polls": 0, "proxy_polls": 0, "calls": []}
    with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=responder(state))), \
         patch.object(pipeline.asyncio, "sleep", AsyncMock()):
        yield state


def set_sources(state):
    return [a for t, a in state["calls"] if t == "set_source"]


class TestTheDownload:
    @pytest.mark.asyncio
    async def test_the_object_lands_on_the_job_before_the_proxy_is_attempted(self, media):
        out = await pipeline._download_hls_source("j1", "https://x.test/vod.m3u8")
        assert out["status"] == "success"
        first = set_sources(media)[0]
        assert first["gcs_uri"].endswith("source.ts")
        assert first["size_bytes"] == 6_800_000_000
        assert "analysis_uri" not in first
        order = [t for t, _ in media["calls"]]
        assert order.index("set_source") < order.index("make_analysis_proxy")

    @pytest.mark.asyncio
    async def test_it_polls_rather_than_assuming(self, media):
        await pipeline._download_hls_source("j1", "https://x.test/vod.m3u8")
        assert media["polls"] == 3

    @pytest.mark.asyncio
    async def test_a_failed_download_fails_the_job_with_the_reason(self, media):
        async def call(server, tool, args=None):
            media["calls"].append((tool, args or {}))
            if tool == "download_hls":
                return {"status": "started", "execution": "exec-9"}
            if tool == "hls_download_status":
                return {"status": "failed", "error": "the download finished but wrote no source object"}
            return {"status": "success"}

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            out = await pipeline._download_hls_source("j1", "https://x.test/vod.m3u8")
        assert out["status"] == "error"
        failed = [a for t, a in media["calls"] if t == "update_job_status" and a.get("status") == "failed"]
        assert failed, "the job was left running"
        assert "wrote no source object" in failed[0]["error"]
        assert not [t for t, _ in media["calls"] if t == "make_analysis_proxy"]


class TestTheProxy:
    @pytest.mark.asyncio
    async def test_it_is_made_on_transcoder_from_the_downloaded_object(self, media):
        out = await pipeline._download_hls_source("j1", "https://x.test/vod.m3u8")
        started = [a for t, a in media["calls"] if t == "make_analysis_proxy"][0]
        assert started["gcs_uri"] == "gs://uploads/hls/j1/source/source.ts"
        assert media["proxy_polls"] == 2, "the encode is polled, not assumed"
        assert out["analysis_uri"].endswith("proxy_1fps.mp4")
        assert set_sources(media)[-1]["analysis_uri"] == out["analysis_uri"]

    @pytest.mark.asyncio
    async def test_a_failed_encode_is_a_warning_and_the_run_goes_on(self, media):
        state = {"polls": 0, "proxy_polls": 0, "calls": []}
        with patch.object(pipeline.mcp_client, "call_tool",
                          AsyncMock(side_effect=responder(state, proxy_state="FAILED"))):
            out = await pipeline._download_hls_source("j1", "https://x.test/vod.m3u8")
        assert out["status"] == "success"
        assert out["analysis_uri"] == ""
        assert all("analysis_uri" not in a for a in set_sources(state))
        assert not [a for t, a in state["calls"] if t == "update_job_status" and a.get("status") == "failed"]
        warnings = [a for t, a in state["calls"] if t == "emit_event" and a.get("level") == "warning"]
        assert warnings and "no video stream" in warnings[0]["message"]

    @pytest.mark.asyncio
    async def test_an_encode_that_cannot_start_is_the_same_warning(self, media):
        state = {"polls": 0, "proxy_polls": 0, "calls": []}
        with patch.object(pipeline.mcp_client, "call_tool",
                          AsyncMock(side_effect=responder(state, proxy_start="error"))):
            out = await pipeline._download_hls_source("j1", "https://x.test/vod.m3u8")
        assert out["status"] == "success"
        assert out["analysis_uri"] == ""
        assert state["proxy_polls"] == 0


class TestWhatReadsWhat:
    def test_the_analysis_reads_the_proxy_when_there_is_one(self):
        assert 'analysis_uri = (job.get("source") or {}).get("analysisUri") or gcs_uri' in PIPELINE
        assert "_cut_segments(job_id, analysis_uri, duration)" in PIPELINE
        assert "analyse_segments(\n        analysis_uri, duration" in PIPELINE

    def test_thumbnails_still_come_from_the_source(self):
        # The proxy is 480p at 1 fps; a still cut from it is not a still.
        assert "_thumbnail_moments(job_id, gcs_uri, moments" in PIPELINE

    def test_ingest_downloads_before_it_validates(self):
        head = PIPELINE.index("async def inspect_source")
        body = PIPELINE[head:PIPELINE.index('await _emit(job_id, "ingest", "Checking the upload', head)]
        assert "_download_hls_source(job_id, job[\"hlsUrl\"])" in body
