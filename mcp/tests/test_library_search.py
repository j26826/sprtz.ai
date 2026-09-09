"""Searching the whole desk: what narrows, and what the ranker is shown.

The library-wide vector index is on the embedding alone and moments do not
carry their sport, so narrowing is a Python pass over an over-read set with the
game joined on. These are the pure pieces of that; the Firestore call itself is
not exercised here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server.store import _candidate_line, _chunks, _filter_candidates  # noqa: E402


def m(job, sport="", title="", **kw):
    return {"job_id": job, "label": "Save", "description": "keeper stops it",
            "game": {"job_id": job, "title": title, "sport": sport, "discipline": ""}, **kw}


class TestNarrowing:
    def test_by_sport_reads_the_joined_game_not_the_moment(self):
        rows = [m("a", "handball"), m("b", "equestrian"), m("c", "Handball")]
        assert [r["job_id"] for r in _filter_candidates(rows, sport="handball")] == ["a", "c"]

    def test_by_games(self):
        rows = [m("a"), m("b"), m("c")]
        assert [r["job_id"] for r in _filter_candidates(rows, job_ids=["c", "a"])] == ["a", "c"]

    def test_both_narrow_together(self):
        rows = [m("a", "handball"), m("b", "handball"), m("c", "equestrian")]
        assert [r["job_id"] for r in _filter_candidates(rows, sport="handball", job_ids=["b", "c"])] == ["b"]

    def test_nothing_asked_changes_nothing(self):
        rows = [m("a"), m("b")]
        assert _filter_candidates(rows) == rows

    def test_a_moment_with_no_game_joined_fails_a_sport_filter(self):
        """Unknown is not a match. A sport filter keeps what is known to be that sport."""
        row = {"job_id": "x", "label": "Save", "description": "d"}
        assert _filter_candidates([row], sport="handball") == []
        assert _filter_candidates([row]) == [row]


class TestWhatTheRankerSees:
    def test_the_game_is_on_every_line(self):
        line = _candidate_line(3, m("a", "handball", "SWE v DEN — EHF Euro"))
        assert line.startswith("3. [Save]")
        assert "in SWE v DEN — EHF Euro / handball" in line

    def test_discipline_beats_sport_when_known(self):
        row = m("a", "equestrian", "Kudos Grand Prix"); row["game"]["discipline"] = "Dressage"
        assert "Kudos Grand Prix / Dressage" in _candidate_line(0, row)

    def test_rider_and_horse_are_named(self):
        row = m("a", "equestrian", "GP", rider="Becky Moody", horse="James Bond II")
        assert "Becky Moody / James Bond II" in _candidate_line(0, row)

    def test_the_summary_is_preferred_to_the_description(self):
        row = m("a", summary="Moody holds the pirouette for eight strides")
        assert "Moody holds the pirouette" in _candidate_line(0, row)
        assert "keeper stops it" not in _candidate_line(0, row)

    def test_no_game_no_where_clause(self):
        line = _candidate_line(0, {"job_id": "x", "label": "Goal", "description": "d", "is_goal": True})
        # Exact, because "resulted in a goal" itself contains " in " and a
        # substring check tripped on it. The point is that no "in <game>"
        # clause appears when there is no game to name.
        assert line == "0. [Goal] d (resulted in a goal)"


def test_in_queries_are_chunked_at_thirty():
    ids = [f"j{i}" for i in range(65)]
    chunks = _chunks(ids)
    assert [len(c) for c in chunks] == [30, 30, 5]
    assert sum(chunks, []) == ids
