"""Segment planning and cross-boundary merge.

These are the two places where a long match can silently lose moments, so they
are tested against real durations rather than round numbers.
"""

from __future__ import annotations

import pytest

from sprtz_agents.config import get_settings
from sprtz_agents.schemas import DetectedMoment, SegmentAnalysis, SegmentPlan
from sprtz_agents.tools.analysis import merge_segment_results, plan_segments, score_moment


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_short_video_is_one_segment():
    plans = plan_segments(600.0)
    assert len(plans) == 1
    assert plans[0].start_sec == 0.0
    assert plans[0].end_sec == 600.0
    assert plans[0].overlap_lead_sec == 0.0


def test_segments_cover_the_whole_video_with_no_gaps():
    duration = 95 * 60.0  # a full match with build-up and half time
    plans = plan_segments(duration)

    assert plans[0].start_sec == 0.0
    assert plans[-1].end_sec == pytest.approx(duration)

    for previous, current in zip(plans, plans[1:], strict=False):
        # The next window must start before the previous one ends, or footage
        # between them is never sent to the model.
        assert current.start_sec < previous.end_sec, "gap between segments"


def test_adjacent_segments_overlap_by_the_configured_lead():
    settings = get_settings()
    plans = plan_segments(70 * 60.0)
    assert len(plans) > 1

    for previous, current in zip(plans, plans[1:], strict=False):
        overlap = previous.end_sec - current.start_sec
        assert overlap == pytest.approx(settings.segment_overlap_seconds, abs=0.01)
        assert current.overlap_lead_sec == pytest.approx(settings.segment_overlap_seconds)


def test_trailing_sliver_is_absorbed_rather_than_analysed_alone():
    settings = get_settings()
    # Just past a segment boundary: the tail would be a few seconds on its own.
    duration = settings.segment_seconds * 2 - settings.segment_overlap_seconds + 5
    plans = plan_segments(duration)

    assert all(p.duration_sec > settings.segment_overlap_seconds for p in plans)
    assert plans[-1].end_sec == pytest.approx(duration)


def test_zero_duration_plans_nothing():
    assert plan_segments(0.0) == []


def _tc(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m:02d}:{s:02d}"


def _detected(**kwargs) -> DetectedMoment:
    """Build a detection from second offsets, which read better in a test than
    raw timecodes. Converted to the MM:SS the model actually returns."""
    for field_name in ("start_sec", "peak_sec", "end_sec"):
        if field_name in kwargs:
            kwargs[field_name.replace("_sec", "_tc")] = _tc(kwargs.pop(field_name))
    base = {
        "moment_type": "jump_shot",
        "start_tc": "00:10",
        "end_tc": "00:16",
        "peak_tc": "00:13",
        "confidence": 0.8,
        "excitement": 0.6,
        "description": "Back-court player rises over the 6 m line and finishes high.",
        "evidence": ["take-off outside the arc"],
    }
    base.update(kwargs)
    return DetectedMoment(**base)


def test_merge_converts_segment_relative_timestamps_to_absolute():
    plan = SegmentPlan(index=2, start_sec=1740.0, end_sec=2640.0, overlap_lead_sec=20.0)
    analysis = SegmentAnalysis(moments=[_detected(start_sec=30.0, peak_sec=33.0, end_sec=36.0)])

    merged = merge_segment_results([(plan, analysis)], sport="handball", job_id="job1")

    assert len(merged) == 1
    assert merged[0].start_sec == pytest.approx(1770.0)
    assert merged[0].peak_sec == pytest.approx(1773.0)
    assert merged[0].end_sec == pytest.approx(1776.0)


def test_moment_seen_in_both_segments_is_merged_once():
    # A moment sitting in the 20s overlap: segment 0 sees it at 885s, segment 1
    # sees the same action 880s into the match at its own offset.
    first = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
    second = SegmentPlan(index=1, start_sec=880.0, end_sec=1780.0, overlap_lead_sec=20.0)

    a = SegmentAnalysis(moments=[_detected(start_sec=885.0, peak_sec=888.0, end_sec=893.0, confidence=0.7)])
    b = SegmentAnalysis(moments=[_detected(start_sec=5.0, peak_sec=8.0, end_sec=14.0, confidence=0.9)])

    merged = merge_segment_results([(first, a), (second, b)], sport="handball", job_id="job1")

    assert len(merged) == 1, "the same action was kept twice across the boundary"
    survivor = merged[0]
    assert survivor.confidence == 0.9, "the more confident detection should win"
    assert survivor.segment_indexes == [0, 1]
    # The union of both views, so nothing observed is trimmed away.
    assert survivor.start_sec == pytest.approx(885.0)
    assert survivor.end_sec == pytest.approx(894.0)


def test_different_moment_types_at_the_same_time_are_kept_separate():
    plan = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
    analysis = SegmentAnalysis(
        moments=[
            _detected(moment_type="jump_shot", start_sec=100.0, peak_sec=103.0, end_sec=106.0),
            _detected(moment_type="block", start_sec=100.5, peak_sec=103.5, end_sec=106.5),
        ]
    )

    merged = merge_segment_results([(plan, analysis)], sport="handball", job_id="job1")
    assert {m.moment_type for m in merged} == {"jump_shot", "block"}


def test_replays_are_dropped():
    plan = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
    analysis = SegmentAnalysis(
        moments=[
            _detected(start_sec=100.0, peak_sec=103.0, end_sec=106.0),
            _detected(start_sec=130.0, peak_sec=133.0, end_sec=138.0, is_replay=True),
        ]
    )

    merged = merge_segment_results([(plan, analysis)], sport="handball", job_id="job1")
    assert len(merged) == 1
    assert merged[0].start_sec == pytest.approx(100.0)


def test_unknown_moment_type_is_dropped_not_persisted():
    plan = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
    analysis = SegmentAnalysis(moments=[_detected(moment_type="slam_dunk")])

    merged = merge_segment_results([(plan, analysis)], sport="handball", job_id="job1")
    assert merged == []


def test_moment_type_is_normalised_from_loose_model_output():
    assert DetectedMoment(
        moment_type="Jump Shot",
        start_tc="00:01",
        end_tc="00:02",
        peak_tc="00:02",
        confidence=0.5,
        excitement=0.5,
        description="x",
    ).moment_type == "jump_shot"


def test_output_is_ordered_by_match_time():
    plan = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
    analysis = SegmentAnalysis(
        moments=[
            _detected(moment_type="block", start_sec=400.0, peak_sec=402.0, end_sec=404.0),
            _detected(moment_type="jump_shot", start_sec=100.0, peak_sec=102.0, end_sec=104.0),
            _detected(moment_type="wing_shot", start_sec=250.0, peak_sec=252.0, end_sec=254.0),
        ]
    )

    merged = merge_segment_results([(plan, analysis)], sport="handball", job_id="job1")
    assert [m.start_sec for m in merged] == sorted(m.start_sec for m in merged)


class TestScoring:
    def test_low_confidence_is_penalised_hard(self):
        confident = score_moment(base_score=0.8, confidence=0.9, excitement=0.7, is_goal=False)
        unsure = score_moment(base_score=0.8, confidence=0.2, excitement=0.7, is_goal=False)
        assert confident > unsure
        assert unsure < 0.55, "an unsure detection should not rank near the top"

    def test_score_stays_in_range(self):
        assert 0.0 <= score_moment(base_score=1.0, confidence=1.0, excitement=1.0, is_goal=True) <= 1.0
        assert 0.0 <= score_moment(base_score=0.0, confidence=0.0, excitement=0.0, is_goal=False) <= 1.0

    def test_excitement_separates_two_instances_of_the_same_type(self):
        routine = score_moment(base_score=0.72, confidence=0.9, excitement=0.3, is_goal=True)
        decisive = score_moment(base_score=0.72, confidence=0.9, excitement=1.0, is_goal=True)
        assert decisive - routine > 0.15


class TestTimecodeHandling:
    """The model reports MM:SS within the clip; these guard the parsing and the
    range checks that stop an out-of-window guess reaching Firestore."""

    def test_parses_mm_ss_and_h_mm_ss(self):
        from sprtz_agents.schemas import parse_timecode

        assert parse_timecode("00:00") == 0.0
        assert parse_timecode("01:30") == 90.0
        assert parse_timecode("14:59") == 899.0
        assert parse_timecode("1:02:03") == 3723.0

    def test_unparseable_timecode_is_none_not_zero(self):
        from sprtz_agents.schemas import parse_timecode

        # Zero would silently place the moment at the start of the segment.
        assert parse_timecode("about halfway") is None
        assert parse_timecode("") is None
        assert parse_timecode(None) is None

    def test_peak_past_the_end_of_the_clip_is_rejected(self):
        # Observed on real footage: a 15-minute clip returning timecodes past 15:00.
        plan = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
        analysis = SegmentAnalysis(
            moments=[_detected(start_tc="17:40", peak_tc="17:45", end_tc="17:50")]
        )
        assert merge_segment_results([(plan, analysis)], sport="handball", job_id="j") == []

    def test_moment_inside_the_clip_survives(self):
        plan = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
        analysis = SegmentAnalysis(
            moments=[_detected(start_tc="14:40", peak_tc="14:45", end_tc="14:50")]
        )
        merged = merge_segment_results([(plan, analysis)], sport="handball", job_id="j")
        assert len(merged) == 1
        assert merged[0].peak_sec == pytest.approx(885.0)

    def test_missing_start_falls_back_to_the_peak(self):
        plan = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
        analysis = SegmentAnalysis(
            moments=[_detected(start_tc="nonsense", peak_tc="05:00", end_tc="05:06")]
        )
        merged = merge_segment_results([(plan, analysis)], sport="handball", job_id="j")
        assert len(merged) == 1
        assert merged[0].start_sec == pytest.approx(300.0)

    def test_end_before_start_is_repaired(self):
        plan = SegmentPlan(index=0, start_sec=0.0, end_sec=900.0)
        analysis = SegmentAnalysis(
            moments=[_detected(start_tc="05:00", peak_tc="05:04", end_tc="04:00")]
        )
        merged = merge_segment_results([(plan, analysis)], sport="handball", job_id="j")
        assert len(merged) == 1
        assert merged[0].end_sec >= merged[0].peak_sec >= merged[0].start_sec
