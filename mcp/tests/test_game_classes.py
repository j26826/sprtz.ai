"""One recording, several competitions.

A live URL points at an arena. The camera runs through class after class, so a
job can hold several events — and each is a record of its own, with its own
name, rides and Equipe page. The id says which: `{job}` while a recording holds
one competition, `{job}__{classId}` once it holds more. Nothing already on the
desk changes id.

What is tested here is the seam: which document a reader lands on, and whether
the writers that act on a recording act on all of it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store


def doc(doc_id: str, **fields):
    snap = MagicMock(id=doc_id)
    snap.to_dict.return_value = {"jobId": store.job_of(doc_id), **fields}
    snap.exists = True
    return snap


SILVER = doc("j1__1278777", classId="1278777", title="PSG Silver",
             classStartAt="2026-09-11T13:00:00+00:00", rides=[{"order": 7}])
GOLD = doc("j1__1278771", classId="1278771", title="PSG Freestyle Gold",
           classStartAt="2026-09-11T14:20:00+00:00", rides=[{"order": 14}])


class TestTheId:
    def test_one_competition_keeps_the_job_id(self):
        # Every handball match and every single-class day. Nothing to migrate.
        assert store.game_id("j1") == "j1"
        assert store.game_id("j1", "") == "j1"

    def test_a_class_is_the_job_and_the_class(self):
        assert store.game_id("j1", "1278771") == "j1__1278771"
        assert store.game_id("j1", 1278771) == "j1__1278771"

    def test_the_job_can_always_be_read_back_out(self):
        assert store.job_of("j1__1278771") == "j1"
        assert store.job_of("j1") == "j1"


class TestWhichRecordAReaderLandsOn:
    def test_the_first_class_answers_for_the_recording(self):
        # "Show me the match" on a day that held three is asking about the day,
        # and the first class to run is where it starts.
        with patch.object(store, "game_docs", return_value=[SILVER, GOLD]):
            assert store.canonical_game("j1") == "j1__1278777"

    def test_an_unsplit_recording_is_its_own_answer(self):
        with patch.object(store, "game_docs", return_value=[]):
            assert store.canonical_game("j1") == "j1"

    def test_classes_come_back_in_running_order(self):
        with patch.object(store, "game_docs", return_value=sorted(
                [GOLD, SILVER], key=store._class_order)):
            games = store.list_games("j1")
        assert [g["className"] for g in games] == ["PSG Silver", "PSG Freestyle Gold"]
        assert [g["rideCount"] for g in games] == [1, 1]

    def test_a_named_class_is_read_directly(self):
        with patch.object(store, "game_ref") as game_ref:
            game_ref.return_value.get.return_value = GOLD
            rides = store.get_rides("j1", class_id="1278771")
        assert [r["order"] for r in rides] == [14]
        game_ref.assert_called_with("j1", "1278771")

    def test_a_class_that_is_not_there_is_an_error(self):
        with patch.object(store, "game_ref") as game_ref:
            game_ref.return_value.get.return_value = MagicMock(exists=False)
            with pytest.raises(KeyError):
                store.get_rides("j1", class_id="nope")


class TestActingOnTheWholeRecording:
    def _job(self):
        ref = MagicMock()
        ref.get.return_value = MagicMock(exists=True, **{"to_dict.return_value": {
            "kind": "live", "status": "scheduled"}})
        return ref

    def test_renaming_reaches_every_class(self):
        # A rename that touched only the first would leave the desk answering
        # with one name for one class and another for the next.
        silver, gold = doc("j1__1278777", classId="1278777"), doc("j1__1278771", classId="1278771")
        with patch.object(store, "job_ref", return_value=self._job()), \
                patch.object(store, "game_docs", return_value=[silver, gold]), \
                patch.object(store, "db", MagicMock()):
            out = store.rename_job("j1", "LeMieux Championships day 2")

        assert out["renamed_game"]
        for record in (silver, gold):
            written = record.reference.update.call_args[0][0]
            # The class keeps what it is called; the day's name becomes the
            # show it belongs to.
            assert written["showTitle"] == "LeMieux Championships day 2"
            assert "title" not in written

    def test_renaming_an_unsplit_match_still_sets_its_title(self):
        game = MagicMock()
        game.get.return_value = MagicMock(exists=True)
        db = MagicMock()
        db.return_value.collection.return_value.document.return_value = game
        with patch.object(store, "job_ref", return_value=self._job()), \
                patch.object(store, "game_docs", return_value=[]), \
                patch.object(store, "db", db):
            store.rename_job("j1", "SWE v DEN")

        assert game.update.call_args[0][0]["title"] == "SWE v DEN"

    def test_deleting_takes_every_class_with_it(self):
        # Left behind they would be unreachable, still indexed for search, and
        # still on the desk for a job that no longer exists.
        silver, gold = doc("j1__a"), doc("j1__b")
        with patch.object(store, "job_ref", return_value=MagicMock()), \
                patch.object(store, "game_docs", return_value=[silver, gold]), \
                patch.object(store, "game_ref") as game_ref, \
                patch.object(store, "_delete_collection", return_value=0):
            out = store.delete_job("j1")

        assert out["game"] == 2
        assert silver.reference.delete.called and gold.reference.delete.called
        # The bare id is only consulted when there were no class records.
        assert not game_ref.return_value.delete.called

    def test_re_analysing_clears_every_class(self):
        silver, gold = doc("j1__a"), doc("j1__b")
        ref = MagicMock()
        ref.collection.return_value = MagicMock()
        with patch.object(store, "job_ref", return_value=ref), \
                patch.object(store, "game_docs", return_value=[silver, gold]), \
                patch.object(store, "game_ref", return_value=MagicMock()), \
                patch.object(store, "_delete_collection", return_value=0):
            out = store.clear_analysis("j1")

        assert out["game"] == 2


class TestTheDeskList:
    def test_a_split_recording_is_named_by_its_first_class(self):
        # `get_games_by_ids` is keyed by job, and a day that held three classes
        # has three records under one job id. Which one names the recording has
        # to be decided, or it is whichever document streamed last.
        streamed = [GOLD, SILVER]
        db = MagicMock()
        db.return_value.collection.return_value.where.return_value.stream.return_value = streamed
        with patch.object(store, "db", db):
            found = store.get_games_by_ids(["j1"])

        assert found["j1"]["title"] == "PSG Silver"
        assert found["j1"]["class_id"] == "1278777"
        assert "_at" not in found["j1"]
