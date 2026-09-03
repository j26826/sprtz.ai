"""Equestrian: one sport, ten disciplines, detected per job.

Registering ten profiles would have put "which discipline?" in the upload panel,
where the person filling it in is least able to answer and most likely to guess.
It is a question about the footage — the tack, the obstacles, the movement — so
the analysis answers it, every segment reports what it saw, and the readings are
consensused the way team names are.
"""

from __future__ import annotations

import pytest

from sprtz_agents.schemas import EquestrianMoment, EquestrianSegmentAnalysis
from sprtz_agents.sports import get_profile, list_sports
from sprtz_agents.sports.prompt import build_segment_prompt, build_system_instruction
from sprtz_agents.tools.analysis import resolve_discipline


@pytest.fixture
def profile():
    return get_profile("equestrian")


class TestTheProfile:
    def test_it_is_one_sport_not_ten(self):
        assert "equestrian" in list_sports()
        assert not [s for s in list_sports() if s.startswith("equestrian_")]

    def test_all_ten_disciplines_are_declared(self, profile):
        assert {d.code for d in profile.disciplines} == {
            "dressage", "para_dressage", "jumping", "hunter", "eventing",
            "endurance", "driving", "vaulting", "western", "general_performance",
        }

    def test_every_discipline_has_moments_to_look_for(self, profile):
        for discipline in profile.disciplines:
            assert profile.types_for(discipline.code), f"{discipline.code} has no moment types"

    def test_every_moment_belongs_to_a_declared_discipline(self, profile):
        codes = {d.code for d in profile.disciplines}
        for moment in profile.moment_types:
            assert moment.discipline in codes or moment.discipline == "", moment.code

    def test_moment_codes_are_unique(self, profile):
        codes = [m.code for m in profile.moment_types]
        assert len(codes) == len(set(codes))

    def test_the_codes_do_not_collide_with_another_sport(self):
        # by_code is per profile, but a shared code would make a stored moment
        # ambiguous about which taxonomy wrote it.
        equestrian = {m.code for m in get_profile("equestrian").moment_types}
        handball = {m.code for m in get_profile("handball").moment_types}
        assert not equestrian & handball


class TestFilteringByDiscipline:
    def test_a_discipline_gets_only_its_own(self, profile):
        codes = {m.code for m in profile.types_for("western")}
        assert "sliding_stop" in codes
        # A sliding stop does not happen in a dressage test, and offering the
        # whole catalogue is how a model reports one that did not.
        assert "piaffe" not in codes

    def test_the_label_resolves_as_well_as_the_code(self, profile):
        # The game record stores the label, because that is what is displayed
        # and searched. It has to normalise back.
        assert profile.discipline_by_code("Para-Dressage").code == "para_dressage"
        assert profile.discipline_by_code("General Performance").code == "general_performance"

    def test_an_unplaced_video_keeps_the_whole_catalogue(self, profile):
        # Answering an unidentified discipline with an empty catalogue would
        # report no moments at all in a video that plainly has some.
        assert profile.types_for("") == profile.moment_types
        assert profile.types_for("polo") == profile.moment_types


class TestTheConsensus:
    def test_the_confident_majority_wins(self):
        analyses = [
            EquestrianSegmentAnalysis(discipline="jumping", discipline_confidence=0.9),
            EquestrianSegmentAnalysis(discipline="jumping", discipline_confidence=0.8),
            EquestrianSegmentAnalysis(discipline="dressage", discipline_confidence=0.3),
        ]
        assert resolve_discipline(analyses) == ("jumping", 0.85)

    def test_hesitant_numbers_do_not_outvote_a_sure_one(self):
        # An eventing broadcast shows all three phases, so segments legitimately
        # disagree. Counting readings alone would let four unsure glimpses of
        # the dressage phase outvote two confident cross-country ones.
        analyses = [
            EquestrianSegmentAnalysis(discipline="dressage", discipline_confidence=0.2),
            EquestrianSegmentAnalysis(discipline="dressage", discipline_confidence=0.2),
            EquestrianSegmentAnalysis(discipline="dressage", discipline_confidence=0.2),
            EquestrianSegmentAnalysis(discipline="eventing", discipline_confidence=0.95),
        ]
        assert resolve_discipline(analyses)[0] == "eventing"

    def test_nothing_identified_is_reported_as_nothing(self):
        assert resolve_discipline([]) == ("", 0.0)
        assert resolve_discipline([EquestrianSegmentAnalysis()]) == ("", 0.0)

    def test_the_confidence_returned_is_of_the_winner(self):
        analyses = [
            EquestrianSegmentAnalysis(discipline="vaulting", discipline_confidence=0.4),
            EquestrianSegmentAnalysis(discipline="vaulting", discipline_confidence=0.6),
        ]
        assert resolve_discipline(analyses) == ("vaulting", 0.5)


class TestThePrompt:
    def test_the_discipline_step_comes_before_the_catalogue(self, profile):
        text = build_system_instruction(profile)
        assert text.index("identify the discipline") < text.index("# Moment catalogue")

    def test_every_discipline_carries_the_cues_that_identify_it(self, profile):
        text = build_system_instruction(profile)
        for discipline in profile.disciplines:
            assert f"`{discipline.code}`" in text
            assert discipline.cues.split(";")[0][:30] in text

    def test_the_catalogue_is_grouped_by_discipline(self, profile):
        text = build_system_instruction(profile)
        assert "## Jumping" in text and "## Western" in text

    def test_a_sport_without_disciplines_is_not_asked_to_pick_one(self):
        text = build_system_instruction(get_profile("handball"))
        assert "identify the discipline" not in text
        # And it keeps its own grouping.
        assert "## Offense" in text

    def test_each_sport_brings_its_own_vocabulary(self, profile):
        equestrian = build_system_instruction(profile)
        handball = build_system_instruction(get_profile("handball"))

        assert "Horse-Rider Pair" in equestrian and "Goalkeeper" not in equestrian
        assert "Goalkeeper" in handball and "Horse-Rider Pair" not in handball
        # A round has one competitor, so there is no scoreline to read.
        assert "no scoreline to read" in equestrian
        assert "left-hand side" in handball

    def test_the_segment_prompt_stays_short(self, profile):
        # The same guard handball has, for the same reason: a long per-segment
        # prompt made the model emit a sequential counter instead of observed
        # timestamps. The catalogue lives in the cached system instruction.
        prompt = build_segment_prompt(
            profile, index=3, total=13, start_sec=2640.0, end_sec=3540.0,
            overlap_lead_sec=20.0,
        )
        assert len(prompt) < 2000, len(prompt)
        assert "piaffe" not in prompt


class TestTheResponseShape:
    def test_form_is_asked_for_where_it_is_judged(self):
        # A response schema is part of the prompt, so these are a separate model
        # rather than optional fields on the general one — otherwise a handball
        # analysis writes paragraphs about a jump shot's balance for nobody.
        assert "execution_details" in EquestrianMoment.model_fields
        assert "harmony_index" in EquestrianMoment.model_fields

        from sprtz_agents.schemas import DetectedMoment
        assert "execution_details" not in DetectedMoment.model_fields

    def test_the_profile_names_its_own_schema(self, profile):
        assert profile.segment_schema is EquestrianSegmentAnalysis
        # Handball uses the general one, which is what None means.
        assert get_profile("handball").segment_schema is None


class TestAWholeCompetitionDay:
    """What the real recordings turned out to be.

    Five samples, 6.3 to 8.45 hours each, 7 to 12.6 GB: a fixed camera on one
    ring for a whole day, many competitors in sequence, promotional films cut in
    between classes, and long stretches of empty arena. Not a broadcast of one
    round, which is what the record's shape had assumed.
    """

    def test_a_competitor_is_not_consensused_across_the_day(self, profile):
        # Every round has a different rider. Taking the most frequent reading
        # would relabel all of them as whoever had the longest go.
        assert profile.teams_are_constant is False
        assert get_profile("handball").teams_are_constant is True

    def test_the_game_record_leaves_the_teams_empty(self):
        from sprtz_agents.schemas import Moment
        from sprtz_agents.tools.game_summary import assemble

        def _round(rider: str, at: float) -> Moment:
            return Moment(
                moment_id=f"m{at}", job_id="j", moment_type="clear_jump",
                category="obstacle", label="Clear Jump", start_sec=at,
                end_sec=at + 6, peak_sec=at + 3, confidence=0.9, excitement=0.7,
                highlight_score=0.8, description="A clear round", team1=rider,
            )

        # Three rounds, one rider going twice. "Whoever appeared most" is not
        # the home team; it is not a fact about the recording at all.
        game = assemble(
            job_id="j", sport="equestrian",
            moments=[_round("Rider A", 100), _round("Rider A", 400), _round("Rider B", 900)],
            segment_summaries=[], competitions=["Dublin"], venues=[],
            discipline="Jumping", teams_are_constant=False,
        )
        assert game.home_team == ""
        assert game.away_team == ""
        # And it is still named, because the discipline and the competition are
        # facts about the whole recording even when nobody is.
        assert game.title == "Jumping — Dublin"

    def test_the_moments_keep_their_own_readings(self):
        # Skipping the consensus must not also blank what each round showed.
        from sprtz_agents.schemas import Moment
        from sprtz_agents.tools.analysis import apply_team_names

        moments = [
            Moment(moment_id="a", job_id="j", moment_type="clear_jump",
                   category="obstacle", label="Clear Jump", start_sec=1, end_sec=7,
                   peak_sec=4, confidence=0.9, excitement=0.7, highlight_score=0.8,
                   description="", team1="Rider A"),
        ]
        # apply_team_names is simply not called for this sport; the reading
        # survives untouched.
        assert moments[0].team1 == "Rider A"
        assert apply_team_names(moments, "", "")[0].team1 == "Rider A"


class TestTheRecordingsFit:
    def test_eight_and_a_half_hours_is_accepted(self):
        # All five samples were rejected at validation before anything else ran:
        # the cap was six hours, written when a handball match was the only
        # input anyone had.
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mcp"))
        from media_server import ffmpeg_ops

        longest = 8.45 * 3600
        assert ffmpeg_ops.MAX_DURATION_SEC > longest
        assert ffmpeg_ops.validate({
            "duration_sec": longest, "width": 1920, "height": 1080,
            "video_codec": "h264", "audio_codec": "aac", "fps": 25.0,
        }) == []

    def test_a_runaway_is_still_bounded(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mcp"))
        from media_server import ffmpeg_ops

        reasons = ffmpeg_ops.validate({
            "duration_sec": 24 * 3600, "width": 1920, "height": 1080,
            "video_codec": "h264", "audio_codec": "aac", "fps": 25.0,
        })
        assert any("exceeds" in r for r in reasons)
