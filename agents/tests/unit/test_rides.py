"""Ride fusion: the rules ported from the dressage pipeline, without its data.

The original carried a dict of the eighteen riders in one broadcast. These tests
exist mostly to prove the same answers come out without one.
"""

import pytest

from sprtz_agents.tools.rides import (
    canonical_identity, check_total, fuse, high_scoring, match_watchlist, normalise_name,
)


def frag(seg, start, end, rider="", horse="", **kw):
    return {"segment": seg, "start_sec": start, "end_sec": end,
            "rider": rider, "horse": horse, **kw}


class TestIdentityWithoutACanon:
    def test_a_misread_does_not_split_a_ride(self):
        """One bad reading of a name is the case the modal vote exists for."""
        rides = fuse([
            frag(0, 100, 200, "Becky Moody", "James Bond II"),
            frag(0, 200, 300, "Becky Moody", "James Bond II"),
            frag(1, 300, 400, "Becky Moodv", "James Bond ll"),
        ])
        assert len(rides) == 1
        assert rides[0]["rider"] == "Becky Moody", "the modal spelling wins, not the last"
        assert rides[0]["horse"] == "James Bond II"

    def test_accents_do_not_split_a_combination(self):
        assert normalise_name("Anne-Marie Bork Eppers") == normalise_name(
            "Anne Marie Børk Eppers")

    @pytest.mark.parametrize("pair", [
        ("Børk", "Bork"), ("Sætre", "Saetre"), ("Åberg", "Aberg"),
        ("Straßer", "Strasser"), ("Løvholt", "Lovholt"),
    ])
    def test_letters_nfkd_will_not_decompose(self, pair):
        """These are letters, not accented vowels, so NFKD leaves them whole and
        the character class below would drop them entirely. This sport's results
        live on Scandinavian platforms; the names are ordinary there."""
        assert normalise_name(pair[0]) == normalise_name(pair[1])

    def test_the_canon_comes_from_the_readings(self):
        rider, horse = canonical_identity(
            [("Gareth Hughes", ""), ("Gareth Hughes", "Lufada MVL"), ("Gareth Hughe", "")])
        assert (rider, horse) == ("Gareth Hughes", "Lufada MVL"), (
            "a segment that caught only one line of the graphic still contributes it"
        )


class TestSegmentation:
    def test_fragments_across_a_segment_boundary_become_one_ride(self):
        rides = fuse([
            frag(0, 800, 900, "Alexander Harrison", "Kickback"),
            frag(1, 900, 1100, "Alexander Harrison", "Kickback"),
        ])
        assert len(rides) == 1
        assert (rides[0]["start_sec"], rides[0]["end_sec"]) == (800, 1100)
        assert rides[0]["segments"] == [0, 1]

    def test_the_same_rider_in_a_later_class_is_a_separate_ride(self):
        """Name alone must not weld two rides two hours apart into one."""
        rides = fuse([
            frag(0, 100, 400, "Richard Davison", "Intero"),
            frag(7, 8000, 8400, "Richard Davison", "Intero"),
        ])
        assert len(rides) == 2

    def test_consecutive_different_combinations_stay_separate(self):
        rides = fuse([
            frag(0, 100, 400, "Sadie Smith", "Swanmore Dantina"),
            frag(0, 410, 700, "Lewis Carrier", "Diego V"),
        ])
        assert [r["order"] for r in rides] == [1, 2]
        assert rides[0]["rider"] == "Sadie Smith"

    def test_running_order_is_by_time_not_by_arrival(self):
        rides = fuse([
            frag(0, 900, 1000, "B Rider", "B Horse"),
            frag(0, 100, 200, "A Rider", "A Horse"),
        ])
        assert [r["rider"] for r in rides] == ["A Rider", "B Rider"]
        assert [r["order"] for r in rides] == [1, 2]


class TestScores:
    def test_a_total_that_matches_its_marks_passes(self):
        assert check_total([71.196, 69.022, 68.478, 68.587, 67.5], 68.957) == "ok"

    def test_a_single_misread_digit_fails(self):
        """The invariant's whole purpose: 71.196 misread as 78.196."""
        result = check_total([78.196, 69.022, 68.478, 68.587, 67.5], 68.957)
        assert result.startswith("mismatch")

    def test_nothing_to_check_is_not_a_failure(self):
        assert check_total([], None) == ""
        assert check_total([70.0], 70.0) == "", "one mark is not a panel"

    def test_an_empty_later_reading_does_not_erase_the_scores(self):
        """The graphic is shown once; most fragments of a ride have no scores."""
        rides = fuse([
            frag(0, 100, 200, "R", "H", judge_marks=[70.0, 71.0], total_pct=70.5),
            frag(0, 200, 300, "R", "H"),
        ])
        assert rides[0]["total_pct"] == 70.5
        assert rides[0]["score_check"] == "ok"
        assert rides[0]["score_source"] == "observed"


class TestTheClipRequirements:
    def test_straight_tests_clear_at_75_and_freestyle_at_80(self):
        rides = [
            {"rider": "a", "total_pct": 76.0, "test_type": "straight", "score_check": "ok"},
            {"rider": "b", "total_pct": 76.0, "test_type": "freestyle", "score_check": "ok"},
            {"rider": "c", "total_pct": 81.0, "test_type": "freestyle", "score_check": "ok"},
        ]
        assert [r["rider"] for r in high_scoring(rides)] == ["a", "c"]

    def test_an_unverified_total_is_never_clipped_on(self):
        rides = [{"rider": "a", "total_pct": 99.0, "test_type": "straight",
                  "score_check": "mismatch: marks average 71.000, total shown as 99.000"}]
        assert high_scoring(rides) == [], (
            "a number that failed its own consistency check must not be why "
            "something gets published"
        )

    def test_a_ride_with_no_score_is_not_clipped_on(self):
        assert high_scoring([{"total_pct": None, "test_type": "straight"}]) == []

    def test_watchlist_matches_either_half_of_the_combination(self):
        rides = fuse([
            frag(0, 100, 200, "Becky Moody", "James Bond II"),
            frag(0, 300, 400, "Gareth Hughes", "Lufada MVL"),
        ])
        assert [r["rider"] for r in match_watchlist(rides, ["Becky Moody"])] == ["Becky Moody"]
        assert [r["rider"] for r in match_watchlist(rides, ["Lufada MVL"])] == ["Gareth Hughes"]

    def test_an_empty_watchlist_matches_nothing_rather_than_everything(self):
        rides = fuse([frag(0, 100, 200, "Becky Moody", "James Bond II")])
        assert match_watchlist(rides, []) == []
        assert match_watchlist(rides, ["  "]) == []


class TestItIsActuallyWired:
    """Three modules in this repo have shipped correct and uncalled. Not a fourth."""

    def test_the_fusion_runs_inside_the_analysis_merge(self):
        import inspect
        from sprtz_agents.tools import analysis
        assert "_rides_of" in inspect.getsource(analysis.merge_segment_results) or \
            "_rides_of" in inspect.getsource(analysis), "fusion has no call site"

    def test_the_agent_can_call_list_rides(self):
        from sprtz_agents import agent
        names = [getattr(t, "__name__", "") for t in agent._build_tools()]
        assert "list_rides" in names

    def test_rides_survive_the_firestore_write(self):
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parents[3]
               / "mcp" / "catalog_server" / "store.py").read_text()
        body = re.search(r"def upsert_game\(.*?(?=\ndef )", src, re.S)
        assert body and '"rides"' in body.group(0), (
            "rides are dropped at the boundary — the payload is built key by key"
        )
