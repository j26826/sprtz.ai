"""Asking the model to write in a chosen language.

The distinction this protects: prose is translated, observations are not. A team
name read off a score bug is something the model *read*, and translating it
invents a name that was never on screen — the same failure the no-guessing rules
elsewhere exist to prevent, arriving by a different route.
"""

from __future__ import annotations

import pytest

from sprtz_agents.sports import get_profile
from sprtz_agents.sports.prompt import (
    METADATA_LANGUAGES,
    build_segment_prompt,
    build_system_instruction,
)


@pytest.fixture
def profile():
    return get_profile("handball")


class TestLanguageRule:
    def test_every_offered_language_names_itself_in_the_prompt(self, profile):
        for code, name in METADATA_LANGUAGES.items():
            instruction = build_system_instruction(profile, code)
            assert name in instruction, code

    def test_an_unknown_code_falls_back_to_english(self, profile):
        # A stored preference from an older build, or a typo, must not produce a
        # prompt asking for a language that does not exist.
        assert "English" in build_system_instruction(profile, "kl")

    def test_the_prose_fields_are_named(self, profile):
        instruction = build_system_instruction(profile, "de")
        for field in ("description", "evidence", "segment_summary"):
            assert field in instruction

    def test_observed_text_is_explicitly_exempt(self, profile):
        instruction = build_system_instruction(profile, "fr")
        # Team names, captions and the score bug are read, not written.
        assert "exactly as it appears" in instruction

    def test_the_machine_readable_fields_stay_english(self, profile):
        # action_result feeds Counter() and search facets; a German "Tor" and an
        # English "Goal" in one corpus is two categories for one thing.
        instruction = build_system_instruction(profile, "es")
        assert "action_result" in instruction
        assert "in English too" in instruction


class TestPromptBudget:
    def test_the_rule_lives_in_the_cached_instruction(self, profile):
        # Identical for every segment of a job, so it belongs where it caches.
        segment = build_segment_prompt(profile, index=2, total=13,
                                       start_sec=1740, end_sec=2640, overlap_lead_sec=20)
        assert "German" not in segment
        assert len(segment) < 2000

    def test_the_segment_prompt_is_language_independent(self, profile):
        # It carries only where this window sits and how to report time, so it
        # is byte-identical whatever the metadata language is.
        a = build_segment_prompt(profile, index=1, total=5, start_sec=0, end_sec=900)
        b = build_segment_prompt(profile, index=1, total=5, start_sec=0, end_sec=900)
        assert a == b

    def test_instructions_differ_per_language_so_caching_is_per_language(self, profile):
        assert build_system_instruction(profile, "de") != build_system_instruction(profile, "fr")
