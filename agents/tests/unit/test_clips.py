"""Clip window selection."""

from __future__ import annotations

import pytest

from sprtz_agents.schemas import Moment
from sprtz_agents.sports import get_profile
from sprtz_agents.tools.clips import (
    MAX_CLIP_SECONDS,
    MIN_CLIP_SECONDS,
    build_clip_suggestions,
    plan_clip_window,
)


def _moment(**kwargs) -> Moment:
    base = {
        "moment_id": "m1",
        "job_id": "job1",
        "moment_type": "jump_shot",
        "category": "offense",
        "label": "Jump Shot",
        "start_sec": 100.0,
        "end_sec": 106.0,
        "peak_sec": 103.0,
        "confidence": 0.9,
        "excitement": 0.5,
        "highlight_score": 0.7,
        "description": "Rises over the line and finishes high.",
    }
    base.update(kwargs)
    return Moment(**base)


def test_window_contains_everything_the_analysis_reported():
    m = _moment(start_sec=100.0, end_sec=106.0, peak_sec=103.0)
    start, end = plan_clip_window(m, sport="handball", video_duration_sec=3600.0)
    assert start <= m.start_sec
    assert end >= m.end_sec


def test_window_respects_the_minimum_length():
    m = _moment(start_sec=200.0, end_sec=201.0, peak_sec=200.5)
    start, end = plan_clip_window(m, sport="handball", video_duration_sec=3600.0)
    assert end - start >= MIN_CLIP_SECONDS


def test_window_is_capped_and_keeps_the_payoff():
    m = _moment(start_sec=100.0, end_sec=180.0, peak_sec=175.0)
    start, end = plan_clip_window(m, sport="handball", video_duration_sec=3600.0)
    assert end - start <= MAX_CLIP_SECONDS
    # Trimming happens at the front, so the decisive frame survives.
    assert start <= m.peak_sec <= end


def test_window_never_runs_past_the_end_of_the_video():
    m = _moment(start_sec=3595.0, end_sec=3600.0, peak_sec=3598.0)
    start, end = plan_clip_window(m, sport="handball", video_duration_sec=3600.0)
    assert end <= 3600.0
    assert start >= 0.0


def test_window_never_starts_before_zero():
    m = _moment(start_sec=1.0, end_sec=4.0, peak_sec=2.0)
    start, end = plan_clip_window(m, sport="handball", video_duration_sec=3600.0)
    assert start >= 0.0


def test_moment_type_lead_in_is_applied():
    """A card needs the foul that earned it; a wing shot does not."""
    profile = get_profile("handball")
    card = profile.by_code("red_blue_card")
    wing = profile.by_code("wing_shot")
    assert card.lead_in_seconds > wing.lead_in_seconds

    card_start, _ = plan_clip_window(
        _moment(moment_type="red_blue_card", label="Red / Blue Card", category="officiating"),
        sport="handball",
        video_duration_sec=3600.0,
    )
    wing_start, _ = plan_clip_window(
        _moment(moment_type="wing_shot", label="Wing Shot"),
        sport="handball",
        video_duration_sec=3600.0,
    )
    assert card_start < wing_start


def test_high_excitement_earns_a_longer_tail():
    calm = plan_clip_window(_moment(excitement=0.2), sport="handball", video_duration_sec=3600.0)
    wild = plan_clip_window(_moment(excitement=0.95), sport="handball", video_duration_sec=3600.0)
    assert wild[1] > calm[1]


def test_overlapping_moments_yield_one_clip_and_the_stronger_wins():
    moments = [
        _moment(moment_id="a", highlight_score=0.9, peak_sec=100.0, start_sec=98.0, end_sec=104.0),
        _moment(
            moment_id="b",
            moment_type="block",
            label="The Block",
            category="defense",
            highlight_score=0.6,
            peak_sec=101.0,
            start_sec=99.0,
            end_sec=105.0,
        ),
    ]
    clips = build_clip_suggestions(moments, sport="handball", job_id="job1", video_duration_sec=3600.0)
    assert len(clips) == 1
    assert clips[0].moment_id == "a"


def test_suggestions_are_capped_and_returned_in_match_order():
    moments = [
        _moment(moment_id=f"m{i}", peak_sec=100.0 + i * 60, start_sec=98.0 + i * 60, end_sec=104.0 + i * 60,
                highlight_score=0.5 + (i % 5) / 10)
        for i in range(30)
    ]
    clips = build_clip_suggestions(
        moments, sport="handball", job_id="job1", max_clips=10, video_duration_sec=3600.0
    )
    assert len(clips) == 10
    assert [c.start_sec for c in clips] == sorted(c.start_sec for c in clips)


def test_every_clip_is_publishable_length():
    moments = [
        _moment(moment_id=f"m{i}", peak_sec=100.0 + i * 90, start_sec=99.0 + i * 90, end_sec=101.0 + i * 90)
        for i in range(8)
    ]
    clips = build_clip_suggestions(moments, sport="handball", job_id="job1", video_duration_sec=3600.0)
    assert clips
    for clip in clips:
        assert MIN_CLIP_SECONDS <= clip.duration_sec <= MAX_CLIP_SECONDS
        assert clip.duration_sec == pytest.approx(clip.end_sec - clip.start_sec, abs=0.01)


def test_no_clips_from_no_moments():
    assert build_clip_suggestions([], sport="handball", job_id="job1") == []
