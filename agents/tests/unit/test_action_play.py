"""The ActionPlay projection.

This shape leaves the system — it is what an editor exports and what a
downstream consumer reads — so its field names and units are a contract, not an
internal detail. Two of them are easy to get quietly wrong: the timecodes are
into the match rather than into the segment a moment was found in, and the score
is 0-100 here while confidence is a 0-1 probability everywhere inside.
"""

from __future__ import annotations

import pytest

from sprtz_agents.schemas import DetectedMoment, Moment, format_timecode, parse_timecode


def _moment(**kwargs) -> Moment:
    base = {
        "moment_id": "m1",
        "job_id": "j1",
        "moment_type": "double_save",
        "category": "defense",
        "label": "Double Save",
        "start_sec": 2832.0,
        "end_sec": 2841.5,
        "peak_sec": 2835.0,
        "confidence": 0.87,
        "excitement": 0.9,
        "highlight_score": 0.8,
        "description": "Keeper stops the seven-metre then turns the rebound over the bar.",
        "action_result": "Save",
        "participant": "#12 blue",
        "participant_role": "Goalkeeper",
        "team1": "SWE",
        "team2": "DEN",
        "score_team1": 24,
        "score_team2": 23,
        "action_team": "SWE",
    }
    base.update(kwargs)
    return Moment(**base)


class TestShape:
    def test_every_requested_field_is_present(self):
        play = _moment().as_action_play()

        assert set(play) == {
            "type", "timeOffsetStart", "timeOffsetEnd", "actionCategory",
            "actionClass", "actionResult", "participant", "participantRole",
            "team1", "team2", "scoreTeam1", "scoreTeam2", "actionTeam",
            "description", "confidenceScore",
        }

    def test_the_type_is_constant(self):
        assert _moment().as_action_play()["type"] == "ActionPlay"

    def test_category_and_class_are_distinct(self):
        play = _moment().as_action_play()

        # Category is one of the sport's five groupings; class is the specific
        # event. Collapsing them loses the axis an editor filters on.
        assert play["actionCategory"] == "defense"
        assert play["actionClass"] == "Double Save"


class TestTimecodes:
    def test_offsets_are_mmss_into_the_match(self):
        play = _moment().as_action_play()

        # 2832s is 47:12 — the whole point is that a consumer of this has no
        # idea the analysis worked in segments.
        assert play["timeOffsetStart"] == "47:12"
        assert play["timeOffsetEnd"] == "47:22"

    def test_past_the_hour_grows_an_hour_field_rather_than_wrapping(self):
        # A handball match plus stoppages runs past 60 minutes. Wrapping to
        # 00:xx would put a second-half moment at the start of the match.
        assert format_timecode(3725) == "1:02:05"

    def test_format_and_parse_round_trip(self):
        for seconds in (0, 59, 60, 611, 3599, 3600, 7325):
            assert parse_timecode(format_timecode(seconds)) == seconds


class TestConfidenceScore:
    def test_it_is_zero_to_one_hundred(self):
        assert _moment(confidence=0.87).as_action_play()["confidenceScore"] == 87

    def test_the_bounds_map_to_the_bounds(self):
        assert _moment(confidence=0.0).as_action_play()["confidenceScore"] == 0
        assert _moment(confidence=1.0).as_action_play()["confidenceScore"] == 100

    def test_it_is_an_integer(self):
        # "confidenceScore": 87.00000000000001 is what float scaling gives you.
        assert isinstance(_moment(confidence=0.87).as_action_play()["confidenceScore"], int)


class TestParticipantIsObservedNotInvented:
    def test_an_unreadable_participant_stays_empty(self):
        play = _moment(participant="", participant_role="").as_action_play()

        # An empty field is honest. A guessed name gets published.
        assert play["participant"] == ""
        assert play["participantRole"] == ""

    def test_the_model_is_told_not_to_guess(self):
        described = DetectedMoment.model_fields["participant"].description

        assert "never guess" in described.lower()

    def test_the_new_fields_default_rather_than_being_required(self):
        # A segment where the model reports nothing about the participant must
        # still parse; a validation error would lose the whole detection.
        parsed = DetectedMoment(
            moment_type="jump_shot", start_tc="00:10", peak_tc="00:12", end_tc="00:15",
            confidence=0.8, excitement=0.5, description="A shot.",
        )
        assert parsed.action_result == ""
        assert parsed.participant == ""
        assert parsed.participant_role == ""


class TestPromptCarriesTheFields:
    def test_the_guidance_lives_in_the_cached_system_instruction(self):
        from sprtz_agents.sports import get_profile
        from sprtz_agents.sports.prompt import build_segment_prompt, build_system_instruction

        profile = get_profile("handball")
        system = build_system_instruction(profile)
        segment = build_segment_prompt(profile, index=2, total=13,
                                       start_sec=1740, end_sec=2640, overlap_lead_sec=20)

        for field in ("action_result", "participant", "participant_role"):
            assert field in system
        # The segment prompt is sent per window and is the one that degenerates
        # when it grows; this guidance is identical every time, so it belongs in
        # the instruction that caches.
        assert len(segment) < 2000


class TestTeamsAndScore:
    def test_the_teams_and_scoreline_are_reported(self):
        play = _moment().as_action_play()

        assert play["team1"] == "SWE"
        assert play["team2"] == "DEN"
        assert play["scoreTeam1"] == 24
        assert play["scoreTeam2"] == 23

    def test_the_action_team_names_a_side(self):
        # It has to match team1 or team2 to be joinable against them.
        play = _moment().as_action_play()
        assert play["actionTeam"] in (play["team1"], play["team2"])

    def test_an_unreadable_score_is_null_not_zero(self):
        play = _moment(score_team1=None, score_team2=None).as_action_play()

        # Nil-nil is a real scoreline. Reporting 0 for "could not read the bug"
        # invents a scoreline that was never on screen.
        assert play["scoreTeam1"] is None
        assert play["scoreTeam2"] is None

    def test_nil_nil_survives_as_a_real_score(self):
        play = _moment(score_team1=0, score_team2=0).as_action_play()

        assert play["scoreTeam1"] == 0
        assert play["scoreTeam2"] == 0

    def test_a_neutral_action_has_no_team(self):
        assert _moment(action_team="").as_action_play()["actionTeam"] == ""

    def test_the_model_is_told_not_to_infer_the_teams(self):
        described = DetectedMoment.model_fields["team1"].description
        assert "never infer" in described.lower()

    def test_the_score_fields_default_to_unknown(self):
        parsed = DetectedMoment(
            moment_type="jump_shot", start_tc="00:10", peak_tc="00:12", end_tc="00:15",
            confidence=0.8, excitement=0.5, description="A shot.",
        )
        assert parsed.score_team1 is None
        assert parsed.team1 == ""
        assert parsed.action_team == ""


class TestTeamNameConsensus:
    """Team names do not change during a match; thirteen readings of a score bug do."""

    def test_the_most_frequent_reading_wins(self):
        from sprtz_agents.tools.analysis import resolve_team_names

        moments = [
            _moment(team1="SWE", team2="DEN"),
            _moment(team1="SWE", team2="DEN"),
            _moment(team1="SVE", team2="DEN"),   # one bad read
        ]
        assert resolve_team_names(moments) == ("SWE", "DEN")

    def test_segments_that_read_nothing_do_not_win(self):
        from sprtz_agents.tools.analysis import resolve_team_names

        # Most segments have no legible bug. Empty is the majority answer and
        # is exactly the answer that must not be chosen.
        moments = [_moment(team1="", team2="") for _ in range(5)]
        moments.append(_moment(team1="SWE", team2="DEN"))

        assert resolve_team_names(moments) == ("SWE", "DEN")

    def test_no_legible_bug_anywhere_stays_empty(self):
        from sprtz_agents.tools.analysis import resolve_team_names

        assert resolve_team_names([_moment(team1="", team2="")]) == ("", "")

    def test_every_moment_ends_up_with_the_same_pairing(self):
        from sprtz_agents.tools.analysis import apply_team_names, resolve_team_names

        moments = [
            _moment(team1="SWE", team2="DEN"),
            _moment(team1="", team2=""),
            _moment(team1="SVE", team2="DEM"),
        ]
        home, away = resolve_team_names(moments)
        applied = apply_team_names(moments, home, away)

        assert {(m.team1, m.team2) for m in applied} == {("SWE", "DEN")}

    def test_per_moment_scores_are_left_alone(self):
        from sprtz_agents.tools.analysis import apply_team_names

        # The scoreline changes through the match, so consensus would be wrong:
        # a moment's own reading is the one that belongs beside its timestamp.
        moments = [_moment(score_team1=1, score_team2=0),
                   _moment(score_team1=24, score_team2=23)]
        applied = apply_team_names(moments, "SWE", "DEN")

        assert [m.score_team1 for m in applied] == [1, 24]
