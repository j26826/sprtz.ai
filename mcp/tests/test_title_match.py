"""Finding a match by the name someone typed.

A name is what a vector search is worst at. "FAG v TVB — DAIKIN HBL" is two
abbreviations and a sponsor, so its embedding sits beside every other fixture in
the same league and meaning-search answers with a plausible neighbour instead of
the match that was asked for. Comparing the text answers it exactly or not at
all, which is the right failure for a name.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store  # noqa: E402


def _doc(job_id: str, **fields):
    doc = MagicMock()
    doc.id = job_id
    doc.to_dict.return_value = {"jobId": job_id, **fields}
    return doc


def _stream(collection, docs):
    selected = MagicMock()
    selected.stream.return_value = iter(docs)
    collection.select.return_value = selected


@pytest.fixture
def games():
    """A desk with three fixtures, and get_game answering for any of them."""
    collection = MagicMock()
    _stream(collection, [
        _doc("j1", title="FAG v TVB — DAIKIN HBL", homeTeam="FAG", awayTeam="TVB"),
        _doc("j2", title="TBV v LEI — DAIKIN HBL", homeTeam="TBV", awayTeam="LEI"),
        _doc("j3", title="SWE v DEN — EHF Euro", homeTeam="Sweden", awayTeam="Denmark"),
    ])
    db = MagicMock()
    db.collection.return_value = collection

    with (
        patch.object(store, "db", return_value=db),
        patch.object(store, "get_game", side_effect=lambda jid: {"jobId": jid}),
    ):
        yield collection


class TestMatching:
    def test_a_title_inside_a_sentence(self, games):
        found = store.match_games_by_title("Show all moments of FAG v TVB — DAIKIN HBL")
        assert [g["jobId"] for g in found] == ["j1"]

    def test_the_other_fixture_in_the_same_league(self, games):
        found = store.match_games_by_title("Show all moments of TBV v LEI — DAIKIN HBL")
        assert [g["jobId"] for g in found] == ["j2"]

    def test_punctuation_and_case_are_noise(self, games):
        # Titles are composed with an em dash and typed back with a hyphen.
        found = store.match_games_by_title("moments of fag v tvb - daikin hbl please")
        assert [g["jobId"] for g in found] == ["j1"]

    def test_the_fixture_without_the_competition(self, games):
        assert [g["jobId"] for g in store.match_games_by_title("moments of FAG v TVB")] == ["j1"]

    def test_team_names_rather_than_the_code(self, games):
        found = store.match_games_by_title("the Sweden vs Denmark game")
        assert [g["jobId"] for g in found] == ["j3"]

    def test_nothing_named_matches_nothing(self, games):
        assert store.match_games_by_title("show me the best moments") == []

    def test_an_empty_query_is_not_a_match_for_everything(self, games):
        assert store.match_games_by_title("") == []


class TestPrecision:
    def test_a_single_word_is_not_a_name(self, games):
        # A one-word team name would otherwise answer any sentence containing it.
        _stream(games, [_doc("j4", title="Lions", homeTeam="Lions", awayTeam="")])
        assert store.match_games_by_title("show me the lions of the match") == []

    def test_the_longest_title_wins(self, games):
        # A fixture whose title is a prefix of another's must not answer for it.
        _stream(games, [
            _doc("short", title="FAG v TVB"),
            _doc("long", title="FAG v TVB — DAIKIN HBL"),
        ])
        found = store.match_games_by_title("moments of FAG v TVB — DAIKIN HBL")
        assert [g["jobId"] for g in found][0] == "long"


class TestTheRead:
    def test_the_embeddings_are_left_in_firestore(self, games):
        # This scans every game, and a 768-float vector each is almost all of
        # the bytes. The field mask is what makes the scan affordable.
        store.match_games_by_title("anything")
        fields = games.select.call_args.args[0]
        assert "embedding" not in fields
        assert "title" in fields and "jobId" in fields
