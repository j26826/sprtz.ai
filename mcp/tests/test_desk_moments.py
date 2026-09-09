"""The desk's key moments: several games' shortlists as one, best first.

The fan-out reads Firestore; the merge does not. These test the merge, which
is where the answer is actually decided.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server.store import _merge_top  # noqa: E402


def m(job, score, label="Save"):
    return {"job_id": job, "label": label, "highlight_score": score, "moment_id": f"{job}-{score}"}


GAMES = {
    "a": {"job_id": "a", "title": "SWE v DEN", "sport": "handball", "discipline": ""},
    "b": {"job_id": "b", "title": "Kudos GP", "sport": "equestrian", "discipline": "Dressage"},
}


def test_best_first_across_games_not_per_game():
    per_job = {"a": [m("a", 0.9), m("a", 0.5)], "b": [m("b", 0.8), m("b", 0.7)]}
    out = _merge_top(per_job, GAMES, limit=10)
    assert [x["highlight_score"] for x in out] == [0.9, 0.8, 0.7, 0.5]


def test_every_moment_names_its_game():
    out = _merge_top({"b": [m("b", 0.8)]}, GAMES, limit=10)
    assert out[0]["game"]["title"] == "Kudos GP"
    assert out[0]["game"]["discipline"] == "Dressage"


def test_the_limit_is_over_the_union():
    per_job = {"a": [m("a", 0.9), m("a", 0.5)], "b": [m("b", 0.8), m("b", 0.7)]}
    assert [x["job_id"] for x in _merge_top(per_job, GAMES, limit=2)] == ["a", "b"]


def test_sport_narrows_through_the_joined_game():
    per_job = {"a": [m("a", 0.9)], "b": [m("b", 0.95)]}
    out = _merge_top(per_job, GAMES, sport="Handball", limit=10)
    assert [x["job_id"] for x in out] == ["a"], "case-insensitive, and the higher-scoring other sport is gone"


def test_a_game_with_no_record_fails_a_sport_filter_but_not_no_filter():
    per_job = {"z": [m("z", 0.99)]}
    assert _merge_top(per_job, GAMES, sport="handball", limit=10) == []
    out = _merge_top(per_job, GAMES, limit=10)
    assert out[0]["game"] == {"job_id": "z", "title": "", "sport": "", "discipline": ""}


def test_nothing_in_is_nothing_out():
    assert _merge_top({}, GAMES, limit=10) == []
    assert _merge_top({"a": []}, GAMES, limit=10) == []
