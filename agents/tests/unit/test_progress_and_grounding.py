"""Progress reporting, and the shape of grounded answers.

The progress bar never moved because nothing ever wrote `progress` — the field
existed, the UI read it, and no stage set it. These fix the arithmetic in place
so the next person to add a stage sees what the weights mean.
"""

from __future__ import annotations

import pytest

from sprtz_agents.tools import grounding
from sprtz_agents.tools.pipeline import (
    CUT_SHARE,
    STAGE_ORDER,
    STAGE_SPANS,
    THUMB_SHARE,
    stage_progress,
)


class TestStageWeights:
    def test_the_run_starts_at_zero_and_ends_at_one_hundred(self):
        assert stage_progress(STAGE_ORDER[0], 0.0) == 0
        assert stage_progress(STAGE_ORDER[-1], 1.0) == 100

    def test_the_stages_tile_without_gaps_or_overlaps(self):
        # A gap makes the bar jump; an overlap makes it go backwards.
        bounds = [STAGE_SPANS[name] for name in STAGE_ORDER]
        for (_, end), (start, _) in zip(bounds[:-1], bounds[1:], strict=True):
            assert end == start

    def test_analysis_owns_most_of_the_bar(self):
        # It is an hour of Gemini calls against minutes for everything else, so
        # equal slices would park the bar for most of a run.
        analysis = STAGE_SPANS["analysis"]
        assert analysis[1] - analysis[0] > 50

    def test_progress_rises_with_segments_completed(self):
        readings = [stage_progress("analysis", n / 13) for n in range(14)]
        assert readings == sorted(readings)
        assert readings[0] < readings[-1]

    def test_a_fraction_past_the_end_is_clamped(self):
        assert stage_progress("analysis", 5.0) == STAGE_SPANS["analysis"][1]

    def test_an_unknown_stage_does_not_crash_the_bar(self):
        assert stage_progress("nonsense", 0.5) == 0


class TestTheAnalysisBandIsShared:
    """Three countable things happen inside one stage, in this order: the match
    is cut into windows, the windows are analysed, and a still is taken for each
    moment found. Each owns a slice, because a stage that reports nothing while
    it works reads as a dead run — and was reported as one."""

    def _fraction(self, name: str, done: int, total: int) -> float:
        if name == "cut":
            return CUT_SHARE * done / total
        if name == "segments":
            return CUT_SHARE + (1 - CUT_SHARE - THUMB_SHARE) * done / total
        return (1 - THUMB_SHARE) + THUMB_SHARE * done / total

    def test_the_three_slices_fill_the_band_exactly(self):
        assert self._fraction("cut", 0, 13) == 0.0
        assert self._fraction("thumbs", 13, 13) == 1.0

    def test_they_hand_over_without_a_gap_or_a_jump_back(self):
        assert self._fraction("cut", 13, 13) == self._fraction("segments", 0, 13)
        assert self._fraction("segments", 13, 13) == pytest.approx(
            self._fraction("thumbs", 0, 20))

    def test_the_bar_only_goes_forward_across_all_three(self):
        readings = (
            [self._fraction("cut", n, 13) for n in range(14)]
            + [self._fraction("segments", n, 13) for n in range(14)]
            + [self._fraction("thumbs", n, 20) for n in range(21)]
        )
        assert readings == sorted(readings)

    def test_the_segments_still_own_most_of_the_band(self):
        # They are the hour. Cutting is minutes and the stills are minutes, so
        # giving either of them a large share would park the bar again.
        assert 1 - CUT_SHARE - THUMB_SHARE > 0.5


class TestGroundedAnswers:
    def test_the_json_object_is_pulled_out_of_prose(self):
        # A grounded reply is prose plus citations, not clean JSON.
        text = 'Based on the search:\n{"competition": "EHF Euro", "venue": ""}\nSources: ...'
        assert grounding.parse_fixture(text)["competition"] == "EHF Euro"

    def test_an_unparseable_reply_is_empty_rather_than_an_error(self):
        assert grounding.parse_fixture("I could not find this fixture.") == {}
        assert grounding.parse_fixture("") == {}

    def test_the_evidence_handed_to_the_search_is_what_was_observed(self):
        lines = grounding.observed_lines(
            home_team="SWE", away_team="DEN", final_score="24-23",
            competition="", venue="", scoreboards=["SWE 24-23 DEN 58:41"],
        )
        assert "SWE v DEN" in lines
        assert "24-23" in lines

    def test_nothing_observed_says_so(self):
        lines = grounding.observed_lines(
            home_team="", away_team="", final_score="",
            competition="", venue="", scoreboards=[],
        )
        # With nothing read off the screen there is nothing to ground on, and a
        # search would be answering from the sport alone.
        assert "Nothing legible" in lines

    def test_sources_are_extracted_from_grounding_metadata(self):
        class Web:
            uri, title = "https://example.com/match", "Match report"

        class Chunk:
            web = Web()

        class Meta:
            grounding_chunks = [Chunk(), Chunk()]
            web_search_queries = ["SWE DEN handball"]

        class Candidate:
            grounding_metadata = Meta()

        class Response:
            candidates = [Candidate()]

        sources = grounding.extract_sources(Response())
        # Deduplicated: the same page cited twice is one source.
        assert sources == [{"title": "Match report", "uri": "https://example.com/match"}]
        assert grounding.search_queries(Response()) == ["SWE DEN handball"]

    def test_a_response_without_grounding_yields_no_sources(self):
        class Response:
            candidates = []

        assert grounding.extract_sources(Response()) == []
