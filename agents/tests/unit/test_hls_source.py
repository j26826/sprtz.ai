"""An HLS source: a job that starts with a URL and ends with an object.

The download runs as a Cloud Run Job through the media server and the ingest
stage waits on it. What is checked here is the hand-over — that the object
and the 1 fps proxy land on the job — and that the analysis reads the proxy
while thumbnails and clips keep reading the source.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline

PIPELINE = Path(pipeline.__file__).read_text()


@pytest.fixture
def media():
    state = {"polls": 0, "calls": []}

    async def call(server, tool, args=None):
        state["calls"].append((tool, args or {}))
        if tool == "download_hls":
            return {"status": "started", "execution": "exec-9"}
        if tool == "hls_download_status":
            state["polls"] += 1
            if state["polls"] < 3:
                return {"status": "running"}
            return {
                "status": "succeeded", "gcs_uri": "gs://uploads/hls/j1/source/source.mp4",
                "analysis_uri": "gs://media/jobs/j1/proxy/source/source_proxy_1fps.mp4",
                "original_name": "source.mp4", "bytes": 1_500_000_000, "content_type": "video/mp4",
            }
        return {"status": "success"}

    with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)), \
         patch.object(pipeline.asyncio, "sleep", AsyncMock()):
        yield state


class TestTheDownload:
    @pytest.mark.asyncio
    async def test_the_object_and_the_proxy_land_on_the_job(self, media):
        out = await pipeline._download_hls_source("j1", "https://x.test/vod.m3u8")
        assert out["status"] == "success"
        set_source = [a for t, a in media["calls"] if t == "set_source"][0]
        assert set_source["gcs_uri"].endswith("source.mp4")
        assert set_source["analysis_uri"].endswith("_proxy_1fps.mp4")
        assert set_source["size_bytes"] == 1_500_000_000

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
        status = failed[0]
        assert "wrote no source object" in status["error"]


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
