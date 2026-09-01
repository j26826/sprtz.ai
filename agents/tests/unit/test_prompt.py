"""The analysis prompt is the product. These guard the parts that change behaviour."""

from __future__ import annotations

import pytest

from sprtz_agents.sports import get_profile, list_sports
from sprtz_agents.sports.prompt import build_segment_prompt, build_system_instruction


@pytest.fixture
def profile():
    return get_profile("handball")


def test_handball_is_registered():
    assert "handball" in list_sports()


def test_unknown_sport_names_what_is_available():
    with pytest.raises(KeyError, match="handball"):
        get_profile("underwater_hockey")


def test_every_moment_type_appears_in_the_catalogue(profile):
    system = build_system_instruction(profile)
    for moment_type in profile.moment_types:
        assert f"`{moment_type.code}`" in system, f"{moment_type.code} missing from the catalogue"
        assert moment_type.label in system


def test_all_five_categories_are_covered(profile):
    assert profile.categories == ("offense", "defense", "transition", "officiating", "tactical")
    system = build_system_instruction(profile)
    for category in ("Offense", "Defense", "Transition", "Officiating", "Tactical"):
        assert f"## {category}" in system


def test_prompt_demands_clip_relative_timecodes(profile):
    prompt = build_segment_prompt(profile, index=3, total=5, start_sec=2640, end_sec=3540)
    assert "within this clip" in prompt.lower()
    assert "MM:SS" in prompt
    # The absolute window is stated so the model knows where in the match it is,
    # which is what makes "last-second" judgements possible.
    assert "44:00" in prompt and "59:00" in prompt
    # And the clip length, so it knows what an out-of-range timecode looks like.
    assert "15:00" in prompt


def test_segment_prompt_stays_short(profile):
    """Guards a real regression.

    An earlier version inlined the 18-type catalogue into every segment prompt.
    On real footage the model then stopped reporting observed timestamps and
    emitted a sequential counter instead — thirty moments inside three seconds.
    The catalogue belongs in the system instruction, which also lets it cache.
    """
    prompt = build_segment_prompt(profile, index=2, total=13, start_sec=1740, end_sec=2640,
                                  overlap_lead_sec=20)
    assert len(prompt) < 2000, "segment prompt is growing back toward the degenerate version"
    for moment_type in profile.moment_types:
        assert f"`{moment_type.code}`" not in prompt, (
            f"{moment_type.code} leaked into the per-segment prompt; keep the catalogue "
            "in the system instruction"
        )


def test_system_instruction_is_identical_across_segments(profile):
    """It must be byte-stable or the context cache never hits."""
    assert build_system_instruction(profile) == build_system_instruction(profile)


def test_segment_index_is_presented_one_based(profile):
    prompt = build_segment_prompt(profile, index=0, total=5, start_sec=0, end_sec=900)
    assert "segment 1 of 5" in prompt


def test_first_segment_gets_no_overlap_guidance(profile):
    prompt = build_segment_prompt(profile, index=0, total=5, start_sec=0, end_sec=900)
    assert "This is the first segment" in prompt
    assert "overlaps the previous one" not in prompt


def test_later_segments_are_told_about_the_overlap(profile):
    prompt = build_segment_prompt(
        profile, index=2, total=5, start_sec=1740, end_sec=2640, overlap_lead_sec=20
    )
    assert "overlaps the previous one by 20 seconds" in prompt


def test_exclusions_are_stated(profile):
    system = build_system_instruction(profile)
    assert "Never report" in system
    assert "Replays" in system


def test_empty_result_is_explicitly_allowed(profile):
    """Without this the model pads segments where nothing happened."""
    prompt = build_segment_prompt(profile, index=1, total=5, start_sec=880, end_sec=1780)
    assert "empty" in prompt.lower()


def test_system_instruction_carries_the_court_context(profile):
    system = build_system_instruction(profile)
    assert "6 m" in system or "6-metre" in system
    assert "9 m" in system or "9-metre" in system
    assert "Handball" in system
