"""A cancel stops the windows, not just the edges of the run.

The check used to sit before and after `analyse_segments`, so a run
cancelled one second after it began carried on through every window of a
three-hour recording. The editor had meanwhile started the job again, and
two full analyses ran inside one engine worker — which killed it with no
traceback, twice.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sprtz_agents.tools import analysis


class TestCancelStopsTheWindows:
    @pytest.mark.asyncio
    async def test_windows_after_the_cancel_are_not_analysed(self):
        analysed: list[int] = []
        cancelled_after = 2

        async def fake_one(uri, plan, total, sport, sem, language, segment_uri=""):
            analysed.append(plan.index)
            return plan, None, "no"

        async def should_stop() -> bool:
            return len(analysed) >= cancelled_after

        with patch.object(analysis, "_analyse_one", fake_one):
            out = await analysis.analyse_segments(
                "gs://m/v.mp4", 3 * 3600.0, sport="handball", should_stop=should_stop)

        assert out["status"] in ("success", "error")
        assert len(analysed) <= cancelled_after + analysis.get_settings().max_concurrent_segments, \
            "the run kept going past the cancel"

    @pytest.mark.asyncio
    async def test_without_a_cancel_every_window_is_analysed(self):
        analysed: list[int] = []

        async def fake_one(uri, plan, total, sport, sem, language, segment_uri=""):
            analysed.append(plan.index)
            return plan, None, "no"

        with patch.object(analysis, "_analyse_one", fake_one):
            await analysis.analyse_segments("gs://m/v.mp4", 3 * 3600.0, sport="handball",
                                            retry_failed=False)
        assert len(analysed) == len(analysis.plan_segments(3 * 3600.0))

    @pytest.mark.asyncio
    async def test_a_check_that_raises_does_not_stop_the_run(self):
        analysed: list[int] = []

        async def fake_one(uri, plan, total, sport, sem, language, segment_uri=""):
            analysed.append(plan.index)
            return plan, None, "no"

        async def should_stop() -> bool:
            raise RuntimeError("catalog unreachable")

        with patch.object(analysis, "_analyse_one", fake_one):
            await analysis.analyse_segments("gs://m/v.mp4", 3 * 3600.0, sport="handball",
                                            retry_failed=False, should_stop=should_stop)
        assert len(analysed) == len(analysis.plan_segments(3 * 3600.0))

    def test_the_pipeline_passes_the_check(self):
        from pathlib import Path

        from sprtz_agents.tools import pipeline

        src = Path(pipeline.__file__).read_text()
        assert "should_stop=lambda: _cancelled(job_id)" in src
