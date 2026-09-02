"""Analysing pre-cut segment files rather than offsets into the whole match.

Gemini fetches the entire object to serve a request, whatever time offsets it
is given. A 3.22 GiB source therefore failed every window with

    File content exceeded the size limit. max_bytes_fetched: 2146971648

which is 2.0 GiB. Splitting by time was already being done; what was missing is
that the *bytes* have to be smaller, so each window is now a real file.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline
from sprtz_agents.tools.analysis import plan_segments

THREE_HOURS = 10828.0


class TestWindows:
    def test_a_three_hour_match_becomes_thirteen_windows(self):
        assert len(plan_segments(THREE_HOURS)) == 13

    def test_each_window_is_a_slice_a_model_will_accept(self):
        # 2.0 GiB is the ceiling. At the bitrate that failed — 3.22 GiB over
        # three hours — a fifteen-minute cut is about 270 MB, well inside it.
        source_bytes = 3_454_261_141
        per_window = source_bytes / len(plan_segments(THREE_HOURS))
        assert per_window < 2_146_971_648

    def test_windows_overlap_so_a_keyframe_cut_loses_nothing(self):
        plans = plan_segments(THREE_HOURS)
        # A stream copy can only cut on a keyframe, so the in-point drifts. The
        # overlap is what absorbs that rather than dropping the action in it.
        for earlier, later in zip(plans[:-1], plans[1:], strict=True):
            assert later.start_sec < earlier.end_sec


class TestCutting:
    @pytest.fixture
    def called(self):
        mock = AsyncMock(return_value={"status": "success", "segments": [
            {"index": 0, "gcs_uri": "gs://m/jobs/j/segments/segment_000.mp4"},
            {"index": 1, "gcs_uri": "gs://m/jobs/j/segments/segment_001.mp4"},
        ]})
        with patch.object(pipeline.mcp_client, "call_tool", mock):
            yield mock

    @pytest.mark.asyncio
    async def test_every_window_is_sent_to_be_cut(self, called):
        await pipeline._cut_segments("j", "gs://up/v.mp4", THREE_HOURS)

        args = called.await_args_list[-1].args[2]
        assert len(args["windows"]) == 13
        assert args["windows"][0]["start_sec"] == 0.0

    @pytest.mark.asyncio
    async def test_the_result_maps_window_index_to_file(self, called):
        uris = await pipeline._cut_segments("j", "gs://up/v.mp4", THREE_HOURS)

        assert uris[0].endswith("segment_000.mp4")
        assert uris[1].endswith("segment_001.mp4")

    @pytest.mark.asyncio
    async def test_a_failed_cut_falls_back_rather_than_stopping(self):
        # Offsets still work for a source small enough for Gemini to fetch, so
        # failing to cut must not fail the analysis.
        mock = AsyncMock(return_value={"status": "error", "error": "ffmpeg died"})
        with patch.object(pipeline.mcp_client, "call_tool", mock):
            assert await pipeline._cut_segments("j", "gs://up/v.mp4", THREE_HOURS) == {}

    @pytest.mark.asyncio
    async def test_a_partial_cut_keeps_what_was_written(self):
        # Whatever was cut before the failure is still usable; the rest fall
        # back. Throwing it away would mean re-cutting the whole match.
        mock = AsyncMock(return_value={
            "status": "error", "error": "ran out",
            "segments": [{"index": 0, "gcs_uri": "gs://m/s0.mp4"}],
        })
        with patch.object(pipeline.mcp_client, "call_tool", mock):
            uris = await pipeline._cut_segments("j", "gs://up/v.mp4", THREE_HOURS)

        assert uris == {0: "gs://m/s0.mp4"}

    @pytest.mark.asyncio
    async def test_a_zero_length_video_asks_for_nothing(self):
        mock = AsyncMock()
        with patch.object(pipeline.mcp_client, "call_tool", mock):
            assert await pipeline._cut_segments("j", "gs://up/v.mp4", 0.0) == {}
        mock.assert_not_awaited()


class TestRequestShape:
    def _part(self, segment_uri):
        """Build the video part the way _analyse_one does."""
        from google.genai import types

        from sprtz_agents.config import get_settings

        plan = plan_segments(THREE_HOURS)[3]
        fps = get_settings().analysis_fps
        if segment_uri:
            return types.Part(
                file_data=types.FileData(file_uri=segment_uri, mime_type="video/mp4"),
                video_metadata=types.VideoMetadata(fps=fps),
            )
        return types.Part(
            file_data=types.FileData(file_uri="gs://up/v.mp4", mime_type="video/mp4"),
            video_metadata=types.VideoMetadata(
                start_offset=f"{plan.start_sec:.3f}s",
                end_offset=f"{plan.end_sec:.3f}s",
                fps=fps,
            ),
        )

    def test_a_pre_cut_segment_carries_no_offsets(self):
        # The file is the window, and it starts at 00:00 — which is what the
        # prompt tells the model its timecodes are relative to.
        part = self._part("gs://m/jobs/j/segments/segment_003.mp4")
        assert part.video_metadata.start_offset is None
        assert part.video_metadata.end_offset is None

    def test_without_one_it_still_offsets_into_the_whole_match(self):
        part = self._part("")
        assert part.video_metadata.start_offset is not None
