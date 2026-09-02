"""The game-level record.

The split this file exists to protect: facts are copied from what was observed,
judgements are generated. A model asked for "the game details" in one go returns
a coherent-sounding record whose teams never played each other, in a competition
that does not include them, at a venue in the wrong sport. Assembling the facts
in code is what makes that impossible.
"""

from __future__ import annotations

from sprtz_agents.schemas import GameDetails, Moment
from sprtz_agents.tools import game_summary


def _moment(**kwargs) -> Moment:
    base = {
        "moment_id": "m1", "job_id": "j1", "moment_type": "goal",
        "category": "offense", "label": "Goal", "start_sec": 100.0,
        "end_sec": 110.0, "peak_sec": 105.0, "confidence": 0.9,
        "excitement": 0.8, "highlight_score": 0.7, "description": "A goal.",
        "team1": "SWE", "team2": "DEN",
    }
    base.update(kwargs)
    return Moment(**base)


class TestFinalScore:
    def test_it_is_the_latest_readable_scoreline(self):
        moments = [
            _moment(start_sec=100, score_team1=1, score_team2=0),
            _moment(start_sec=3400, score_team1=24, score_team2=23),
            _moment(start_sec=2000, score_team1=12, score_team2=11),
        ]
        assert game_summary.final_score_from(moments)[0] == "24-23"

    def test_it_is_not_a_count_of_detected_goals(self):
        # Counting what the analysis happened to detect is not the scoreboard,
        # and presenting it as the final score states a result nobody displayed.
        moments = [_moment(start_sec=i * 100, action_result="Goal") for i in range(9)]
        assert game_summary.final_score_from(moments)[0] == ""

    def test_it_falls_back_to_the_raw_score_bug(self):
        moments = [_moment(start_sec=3400, scoreboard="SWE 31-29 DEN 60:00")]
        assert game_summary.final_score_from(moments)[0] == "31-29"

    def test_an_en_dash_bug_still_parses(self):
        moments = [_moment(start_sec=10, scoreboard="SWE 31–29 DEN")]
        assert game_summary.final_score_from(moments)[0] == "31-29"

    def test_no_legible_score_anywhere_is_empty_not_nil_nil(self):
        # "0-0" is a real result. Reporting it for "never saw the bug" invents one.
        assert game_summary.final_score_from([_moment()])[0] == ""


class TestOutcome:
    def test_the_higher_score_wins(self):
        assert game_summary.outcome_from("SWE", "DEN", 24, 23) == "SWE win"
        assert game_summary.outcome_from("SWE", "DEN", 23, 24) == "DEN win"

    def test_equal_scores_are_a_draw(self):
        assert game_summary.outcome_from("SWE", "DEN", 22, 22) == "Draw"

    def test_no_score_means_no_claimed_winner(self):
        # A result asserted without a scoreline behind it is the kind of thing
        # that gets published.
        assert game_summary.outcome_from("SWE", "DEN", None, None) == ""

    def test_nil_nil_is_a_draw_not_an_unknown(self):
        assert game_summary.outcome_from("SWE", "DEN", 0, 0) == "Draw"


class TestAssembly:
    def _assembled(self, **kwargs):
        defaults = dict(
            job_id="j1", sport="handball",
            moments=[_moment(start_sec=3400, score_team1=24, score_team2=23)],
            segment_summaries=[{"index": 0, "summary": "End to end."}],
            competitions=[], venues=[],
        )
        defaults.update(kwargs)
        return game_summary.assemble(**defaults)

    def test_the_facts_come_from_the_observations(self):
        game = self._assembled()

        assert game.home_team == "SWE"
        assert game.away_team == "DEN"
        assert game.final_score == "24-23"
        assert game.event_outcome == "SWE win"

    def test_competition_and_venue_are_consensused(self):
        game = self._assembled(competitions=["", "EHF Euro", "EHF Euro"],
                               venues=["", "", "Royal Arena"])

        assert game.competition == "EHF Euro"
        assert game.venue == "Royal Arena"

    def test_nothing_observed_leaves_them_empty(self):
        game = self._assembled(competitions=["", ""], venues=[])

        assert game.competition == ""
        assert game.venue == ""

    def test_the_judgement_fills_only_the_interpretive_fields(self):
        game = self._assembled(judgement={
            "sentiment": "Positive", "mood": "Intense", "summary": "A tight match.",
        })

        assert game.mood == "Intense"
        assert game.sentiment == "Positive"
        # And has not touched anything factual.
        assert game.final_score == "24-23"

    def test_a_failed_judgement_still_leaves_a_usable_record(self):
        game = self._assembled(judgement={})

        assert game.sentiment == "Neutral"
        assert game.home_team == "SWE"


class TestGameRecord:
    def test_it_carries_every_requested_attribute(self):
        record = GameDetails(
            job_id="j1", sport="handball", home_team="SWE", away_team="DEN",
            competition="EHF Euro", venue="Royal Arena", final_score="24-23",
            event_outcome="SWE win", sentiment="Positive", mood="Intense",
        ).as_game_record()

        for key in ("sport", "homeTeam", "awayTeam", "sentiment", "competition",
                    "mood", "finalScore", "venue", "eventOutcome"):
            assert key in record, key

    def test_grounded_values_are_kept_apart_from_observed_ones(self):
        record = GameDetails(
            job_id="j1", sport="handball", home_team="SWE",
            competition="", grounded=True, grounded_competition="EHF Euro 2026",
        ).as_game_record()

        # The screen said nothing; a search said EHF Euro 2026. Merging them
        # would make it impossible to tell which was which afterwards.
        assert record["competition"] == ""
        assert record["groundedCompetition"] == "EHF Euro 2026"
        assert record["grounded"] is True


class TestGameEmbedding:
    def test_a_game_is_findable_by_the_words_people_use(self):
        text = game_summary.embed_text(GameDetails(
            job_id="j1", sport="handball", home_team="SWE", away_team="DEN",
            grounded_home_team="Sweden", grounded_away_team="Denmark",
            competition="EHF Euro", venue="Royal Arena", mood="Intense",
            event_outcome="SWE win",
        ))

        for fragment in ("handball", "Sweden", "Denmark", "EHF Euro",
                         "Royal Arena", "Intense"):
            assert fragment in text

    def test_the_pairing_is_indexed_as_a_phrase(self):
        # "the Sweden Denmark match" is how people name a game.
        text = game_summary.embed_text(GameDetails(
            job_id="j1", sport="handball", home_team="SWE", away_team="DEN",
        ))
        assert "SWE v DEN" in text
