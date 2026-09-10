"""Merging a match's detections stays cheap as their number grows.

An engine worker was killed between the last window and "Found N key
moments" on a 3.75-hour recording. The merge scanned everything merged so
far for every candidate and then scanned again to find the match's index —
cubic in the worst case, and a model that over-produces on a long
recording is exactly that worst case.
"""

from __future__ import annotations

import time

from sprtz_agents.schemas import DetectedMoment, SegmentAnalysis
from sprtz_agents.tools.analysis import SegmentPlan, merge_segment_results


def _analysis(count: int, *, spread_sec: int = 900) -> SegmentAnalysis:
    moments = []
    for i in range(count):
        at = (i * spread_sec) // max(count, 1)
        tc = lambda sec: "%02d:%02d" % (sec // 60, sec % 60)  # noqa: E731
        moments.append(DetectedMoment(
            moment_type="jump_shot", start_tc=tc(at), peak_tc=tc(at + 2), end_tc=tc(at + 4),
            confidence=0.9, excitement=0.5, description="d",
        ))
    return SegmentAnalysis(moments=moments, segment_summary="")


def _plans(segments: int, per_segment: int):
    return [
        (SegmentPlan(index=i, start_sec=i * 900.0, end_sec=(i + 1) * 900.0),
         _analysis(per_segment))
        for i in range(segments)
    ]


class TestTheMergeScales:
    def test_a_flood_of_detections_merges_quickly(self):
        # 16 windows of 200 detections each — what an untuned model can
        # produce on a competition day. The old shape took minutes here.
        started = time.monotonic()
        merged = merge_segment_results(_plans(16, 200), sport="handball", job_id="j1")
        elapsed = time.monotonic() - started
        assert merged, "the flood still produces moments"
        assert elapsed < 10, f"merging took {elapsed:.1f}s"

    def test_overlapping_detections_still_collapse(self):
        # Two windows detecting the same play in their overlap must merge.
        plans = [
            (SegmentPlan(index=0, start_sec=0.0, end_sec=900.0), _analysis(1, spread_sec=1)),
            (SegmentPlan(index=1, start_sec=0.0, end_sec=900.0), _analysis(1, spread_sec=1)),
        ]
        merged = merge_segment_results(plans, sport="handball", job_id="j1")
        assert len(merged) == 1
        assert merged[0].segment_indexes == [0, 1]

    def test_distinct_plays_are_kept_apart(self):
        merged = merge_segment_results(_plans(1, 20), sport="handball", job_id="j1")
        assert len(merged) == 20
