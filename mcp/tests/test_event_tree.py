"""An event as event -> rides -> moments.

The grouping an editor reads a competition day by: who rode, and what happened
while they were in the arena. Every moment has to land somewhere — a moment
the tree drops is a moment nobody finds.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store  # noqa: E402
from catalog_server.event_tree import build_event_tree  # noqa: E402


def _ride(order, rider, horse, start, end, **extra) -> dict:
    return {"order": order, "rider": rider, "horse": horse,
            "start_sec": start, "end_sec": end, **extra}


def _moment(mid, start, peak=None, ride_order=None, **extra) -> dict:
    m = {"momentId": mid, "momentType": "piaffe", "label": "Piaffe",
         "startSec": start, "peakSec": peak if peak is not None else start + 3,
         "endSec": start + 8, "embedding": [0.1] * 4, "ownerUid": "u1",
         "rider": "someone", **extra}
    if ride_order is not None:
        m["rideOrder"] = ride_order
    return m


GAME = {
    "title": "CDI — Grand Prix Freestyle", "sport": "equestrian",
    "discipline": "Dressage", "disciplineConfidence": 0.93,
    "competition": "", "groundedCompetition": "CDI 3*", "venue": "Arena",
    "judges": [{"position": "C", "name": "S. Albrecht"}],
    "notConfirmed": [{"momentType": "pirouette", "notes": ["near 07:40"]}],
    "rides": [
        _ride(2, "Jonas Keller", "Falkenstein", 1180, 1570, test_type="freestyle",
              judge_marks=[76.0, 76.1], total_pct=76.02, rank=1, final_place=1,
              score_source="observed", score_check="ok", start_number="7",
              identity_source="observed"),
        _ride(1, "Anna Berger", "Lumière", 310, 700, total_pct=74.37, rank=2),
    ],
}


class TestTheShape:
    def test_event_then_riders_then_moments(self):
        tree = build_event_tree("j1", {}, GAME, [
            _moment("a", 480, ride_order=1),
            _moment("b", 1240, ride_order=2),
        ])
        event = tree["event"]
        assert event["jobId"] == "j1"
        assert event["discipline"] == "Dressage"
        assert [r["rider"] for r in event["riders"]] == ["Anna Berger", "Jonas Keller"]
        assert [m["momentId"] for m in event["riders"][0]["moments"]] == ["a"]
        assert [m["momentId"] for m in event["riders"][1]["moments"]] == ["b"]
        assert event["unassignedMoments"] == []

    def test_riders_are_in_running_order_not_stored_order(self):
        event = build_event_tree("j1", {}, GAME, [])["event"]
        assert [r["order"] for r in event["riders"]] == [1, 2]

    def test_a_ride_carries_its_result_and_identity(self):
        keller = build_event_tree("j1", {}, GAME, [])["event"]["riders"][1]
        assert keller["startNumber"] == "7"
        assert keller["identitySource"] == "observed"
        assert keller["testType"] == "freestyle"
        assert keller["result"]["totalPct"] == 76.02
        assert keller["result"]["judgeMarks"] == [76.0, 76.1]
        assert keller["result"]["place"] == 1

    def test_a_ride_says_which_analysis_windows_saw_it(self):
        game = {"rides": [_ride(1, "A", "B", 0, 900, segments=[0, 1, "x", None])]}
        assert build_event_tree("j1", {}, game, [])["event"]["riders"][0]["segments"] == [0, 1]

    def test_the_screen_rank_stands_in_when_no_placing_was_published(self):
        berger = build_event_tree("j1", {}, GAME, [])["event"]["riders"][0]
        assert berger["result"]["place"] == 2

    def test_event_details_fall_back_to_grounded_readings(self):
        event = build_event_tree("j1", {}, GAME, [])["event"]
        assert event["competition"] == "CDI 3*"
        assert event["judges"] == [{"position": "C", "name": "S. Albrecht"}]
        assert event["notConfirmed"][0]["momentType"] == "pirouette"


class TestWhereAMomentGoes:
    def test_the_stored_ride_order_wins_over_time(self):
        # Grounding can re-join a moment after the fact; the tree must agree
        # with what the tile already shows.
        tree = build_event_tree("j1", {}, GAME, [_moment("a", 1240, ride_order=1)])
        assert [m["momentId"] for m in tree["event"]["riders"][0]["moments"]] == ["a"]

    def test_without_a_stored_order_the_peak_decides(self):
        tree = build_event_tree("j1", {}, GAME, [_moment("a", 1175, peak=1185)])
        assert tree["event"]["riders"][1]["moments"][0]["momentId"] == "a"

    def test_a_moment_between_rides_is_kept_not_dropped(self):
        tree = build_event_tree("j1", {}, GAME, [_moment("gap", 900)])
        event = tree["event"]
        assert [m["momentId"] for m in event["unassignedMoments"]] == ["gap"]
        assert all(r["momentCount"] == 0 for r in event["riders"])
        assert event["momentCount"] == 1

    def test_a_stored_order_with_no_matching_ride_falls_back_to_time(self):
        tree = build_event_tree("j1", {}, GAME, [_moment("a", 480, ride_order=9)])
        assert tree["event"]["riders"][0]["moments"][0]["momentId"] == "a"

    def test_moments_are_in_time_order_within_a_ride(self):
        tree = build_event_tree("j1", {}, GAME, [
            _moment("late", 600, ride_order=1), _moment("early", 320, ride_order=1),
        ])
        assert [m["momentId"] for m in tree["event"]["riders"][0]["moments"]] == ["early", "late"]

    def test_the_vector_and_the_repeated_ride_fields_are_left_behind(self):
        m = build_event_tree("j1", {}, GAME, [_moment("a", 480, ride_order=1)])["event"]["riders"][0]["moments"][0]
        assert "embedding" not in m
        assert "ownerUid" not in m
        assert "rider" not in m
        assert m["label"] == "Piaffe"


class TestWithoutRides:
    def test_a_handball_match_has_no_riders_and_keeps_every_moment(self):
        tree = build_event_tree("h1", {"title": "SWE v DEN", "sport": "handball"}, {},
                                [_moment("a", 10), _moment("b", 20)])
        event = tree["event"]
        assert event["title"] == "SWE v DEN"
        assert event["sport"] == "handball"
        assert event["riders"] == []
        assert [m["momentId"] for m in event["unassignedMoments"]] == ["a", "b"]

    def test_a_ride_list_with_junk_in_it_is_tolerated(self):
        tree = build_event_tree("j1", {}, {"rides": [None, "x", _ride(1, "A", "B", 0, 10)]}, [])
        assert [r["rider"] for r in tree["event"]["riders"]] == ["A"]


class TestTheStoreRead:
    def test_reads_the_raw_game_so_the_rides_survive(self):
        game_snap = MagicMock(exists=True)
        game_snap.to_dict.return_value = GAME
        moment_doc = MagicMock()
        moment_doc.to_dict.return_value = _moment("a", 480, ride_order=1)
        query = MagicMock()
        query.order_by.return_value.limit.return_value.stream.return_value = [moment_doc]
        job = MagicMock()
        job.collection.return_value = query

        with patch.object(store, "get_job", return_value={"job_id": "j1", "title": "t"}), \
             patch.object(store, "game_ref") as game_ref, \
             patch.object(store, "job_ref", return_value=job):
            game_ref.return_value.get.return_value = game_snap
            tree = store.event_tree("j1")

        query.order_by.assert_called_once_with("startSec")
        assert tree["event"]["riders"][0]["moments"][0]["momentId"] == "a"

    def test_no_game_record_yet_still_answers(self):
        missing = MagicMock(exists=False)
        query = MagicMock()
        query.order_by.return_value.limit.return_value.stream.return_value = []
        job = MagicMock()
        job.collection.return_value = query

        with patch.object(store, "get_job", return_value={"title": "Upload", "sport": "equestrian"}), \
             patch.object(store, "game_ref") as game_ref, \
             patch.object(store, "job_ref", return_value=job):
            game_ref.return_value.get.return_value = missing
            tree = store.event_tree("j1")

        assert tree["event"]["title"] == "Upload"
        assert tree["event"]["riders"] == []


class TestTheRidesRead:
    """list_game_rides reads the rides the game summary shape leaves out."""

    def _read(self, snapshot):
        with patch.object(store, "game_ref") as game_ref:
            game_ref.return_value.get.return_value = snapshot
            return store.get_rides("j1")

    def test_the_rides_come_back_as_stored(self):
        snap = MagicMock(exists=True)
        snap.to_dict.return_value = GAME
        rides = self._read(snap)
        assert [r["rider"] for r in rides] == ["Jonas Keller", "Anna Berger"]
        assert rides[0]["total_pct"] == 76.02

    def test_the_game_summary_really_does_leave_them_out(self):
        # Why the separate read exists. If _game_out ever carries rides, this
        # tool can go and list_rides can read get_game again.
        assert "rides" not in store._game_out(GAME)

    def test_junk_in_the_list_is_dropped(self):
        snap = MagicMock(exists=True)
        snap.to_dict.return_value = {"rides": [None, "x", {"order": 1, "rider": "A"}]}
        assert self._read(snap) == [{"order": 1, "rider": "A"}]

    def test_no_game_record_is_an_error_not_an_empty_day(self):
        with pytest.raises(KeyError):
            self._read(MagicMock(exists=False))


class TestTheChunkKeepsItsRides:
    """A live chunk's ride sightings are what the day's rides are stitched from."""

    def _finish(self, **kwargs):
        chunk, job = MagicMock(), MagicMock()
        with patch.object(store, "_chunk_ref", return_value=chunk), \
             patch.object(store, "job_ref", return_value=job):
            store.finish_live_chunk("j1", 3, moments=2, **kwargs)
        return chunk.update.call_args.args[0]

    def test_fragments_and_negatives_are_stored_on_the_chunk(self):
        fragment = {"segment": 3, "start_sec": 910.0, "end_sec": 1180.0,
                    "rider": "Anna Berger", "horse": "Lumière"}
        negative = {"momentType": "pirouette", "notes": ["[segment 3] a corner"]}
        patch_ = self._finish(ride_fragments=[fragment], not_confirmed=[negative])
        assert patch_["rideFragments"] == [fragment]
        assert patch_["notConfirmed"] == [negative]
        assert patch_["status"] == "analysed"

    def test_a_sport_without_rounds_stores_empty_lists_not_nothing(self):
        patch_ = self._finish()
        assert patch_["rideFragments"] == []
        assert patch_["notConfirmed"] == []
