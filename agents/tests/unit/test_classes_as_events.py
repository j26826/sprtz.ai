"""A recording that crossed several classes is stored as several events.

The failure this answers is on the desk: the LeMieux capture of 11 September
came back as one event with a running order of twenty-four, two riders nobody
could name, and a prize-giving card counted as a round. It was three
competitions, and they are three events.

What must hold in both directions — a day that held one class is stored exactly
as it always was, and a timetable that cannot be read is not allowed to fail an
analysis.
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.schemas import GameDetails, Moment
from sprtz_agents.tools import equipe, pipeline


def ride(order, start, end, rider="Someone"):
    return {"order": order, "start_sec": start, "end_sec": end, "rider": rider, "horse": "H"}


def moment(moment_id, start, ride_order=None):
    return Moment(
        moment_id=moment_id, job_id="j1", moment_type="halt_and_salute",
        category="movement", label="Halt and salute", start_sec=start, end_sec=start + 8, peak_sec=start + 4,
        confidence=0.9, excitement=0.5, highlight_score=0.7, description="",
        ride_order=ride_order,
    )


def show_class(class_id, name, hour):
    return equipe.ShowClass(
        class_id=class_id, name=name,
        start_at=datetime.datetime(2026, 9, 11, hour, tzinfo=datetime.UTC), date="2026-09-11")


SILVER = show_class(1278777, "FAIRFAX SADDLES PRIX ST.GEORGE SILVER CHAMPIONSHIP", 13)
GOLD = show_class(1278771, "FAIRFAX SADDLES PSG FREESTYLE GOLD CHAMPIONSHIP", 14)

LIVE_JOB = {
    "kind": "live",
    "live": {"capture": {"captureStart": "2026-09-11T12:23:53+00:00"}},
}


def game_with(rides):
    return GameDetails(
        job_id="j1", sport="equestrian", discipline="Dressage",
        title="2026 Sep 11, LeMieux National Dressage Championships",
        competition="LeMieux National Dressage Championships", rides=rides,
    )


class TestWhenARecordingIsSplit:
    def _runs(self, rides, classes=(SILVER, GOLD)):
        show = equipe.Show(82348, "LeMieux National Dressage Championships 2026")
        with patch.object(equipe, "find_classes", return_value=(show, list(classes))):
            return pipeline._classes_for(LIVE_JOB, game_with(rides), [])

    def test_a_day_that_crossed_two_classes_becomes_two(self):
        runs = self._runs([ride(1, 60 * 40, 60 * 46), ride(2, 60 * 130, 60 * 137)])
        assert [run.show_class.class_id for run in runs] == [SILVER.class_id, GOLD.class_id]

    def test_a_day_that_stayed_in_one_class_is_not_split(self):
        # One event is one record, under the job's own id, exactly as before.
        runs = self._runs([ride(1, 60 * 40, 60 * 46), ride(2, 60 * 50, 60 * 56)])
        assert runs == []

    def test_a_timetable_that_cannot_be_read_is_not_a_failure(self):
        with patch.object(equipe, "find_classes", side_effect=RuntimeError("network")):
            assert pipeline._classes_for(LIVE_JOB, game_with([ride(1, 0, 10)]), []) == []

    def test_a_show_that_cannot_be_identified_leaves_the_day_whole(self):
        with patch.object(equipe, "find_classes", return_value=(None, [])):
            assert pipeline._classes_for(LIVE_JOB, game_with([ride(1, 0, 10)]), []) == []


class TestWhichMomentsBelongToAClass:
    def test_a_moment_goes_with_the_ride_it_was_in(self):
        runs = [equipe.ClassRun(show_class=SILVER, rides=[ride(1, 0, 100), ride(2, 200, 300)])]
        moments = [moment("m1", 50, ride_order=1), moment("m2", 250, ride_order=2),
                   moment("m3", 5000, ride_order=9)]
        assert [m.moment_id for m in pipeline._moments_of(runs[0], moments)] == ["m1", "m2"]

    def test_a_moment_in_no_ride_goes_by_the_window_it_sat_in(self):
        # The minutes between two rounds belong to the class they sat inside,
        # rather than to nothing at all.
        run = equipe.ClassRun(show_class=SILVER, rides=[ride(1, 0, 100), ride(2, 200, 300)])
        assert [m.moment_id for m in pipeline._moments_of(run, [moment("m4", 150)])] == ["m4"]

    def test_a_moment_outside_every_class_window_is_left_out(self):
        run = equipe.ClassRun(show_class=SILVER, rides=[ride(1, 0, 100)])
        assert pipeline._moments_of(run, [moment("m5", 9000)]) == []


class TestWhatIsWritten:
    @pytest.mark.asyncio
    async def test_each_class_is_its_own_record_named_for_the_class(self):
        written = []

        async def call(server, tool, args=None):
            if tool == "upsert_game":
                written.append(args)
            return {"status": "success"}

        show = equipe.Show(82348, "LeMieux National Dressage Championships 2026")
        runs = [equipe.ClassRun(show_class=SILVER, rides=[ride(7, 60 * 40, 60 * 46)]),
                equipe.ClassRun(show_class=GOLD, rides=[ride(14, 60 * 130, 60 * 137)],
                                decided_by="caption")]
        for run in runs:
            run.show = show

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            await pipeline._store_classes(
                "j1", game_with([ride(7, 60 * 40, 60 * 46), ride(14, 60 * 130, 60 * 137)]),
                runs, [moment("m1", 60 * 41, ride_order=7), moment("m2", 60 * 131, ride_order=14)])

        assert [w["class_id"] for w in written] == ["1278777", "1278771"]
        first, second = (w["game"] for w in written)
        # Named for the class; the day's own name becomes the show it was part of.
        assert first["title"] == SILVER.name
        assert first["show_title"] == "LeMieux National Dressage Championships 2026"
        # Each holds only its own rides and only its own moments.
        assert [r["order"] for r in first["rides"]] == [7]
        assert first["moment_count"] == 1 and second["moment_count"] == 1
        assert second["class_url"].endswith(str(GOLD.class_id))
        assert second["class_decided_by"] == "caption"

    @pytest.mark.asyncio
    async def test_the_whole_day_is_not_kept_beside_the_classes(self):
        # Two answers to "what is this recording" is how a desk shows a day
        # twice, once whole and once in pieces.
        written = []

        async def call(server, tool, args=None):
            if tool == "upsert_game":
                written.append(args)
            return {"status": "success"}

        run = equipe.ClassRun(show_class=SILVER, rides=[ride(1, 0, 60)])
        run.show = None
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            await pipeline._store_classes("j1", game_with([ride(1, 0, 60)]), [run], [])

        assert all(w.get("class_id") for w in written)

    @pytest.mark.asyncio
    async def test_every_moment_is_told_which_class_it_was_in(self):
        # The event tree filters a class's moments by this; untagged, a moment
        # shows under no competition at all.
        sent = []

        async def call(server, tool, args=None):
            if tool == "update_moment_identity":
                sent.append(args)
            return {"status": "success", "updated": 2}

        runs = [equipe.ClassRun(show_class=SILVER, rides=[ride(7, 0, 100)]),
                equipe.ClassRun(show_class=GOLD, rides=[ride(14, 200, 300)])]
        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            await pipeline._tag_moments_with_class(
                "j1", runs, [moment("m1", 50, ride_order=7), moment("m2", 250, ride_order=14)])

        identities = sent[0]["identities"]
        assert {i["moment_id"]: i["class_id"] for i in identities} == {
            "m1": "1278777", "m2": "1278771"}

    @pytest.mark.asyncio
    async def test_tagging_that_fails_does_not_take_the_record_with_it(self):
        async def call(server, tool, args=None):
            raise RuntimeError("firestore is having a day")

        with patch.object(pipeline.mcp_client, "call_tool", AsyncMock(side_effect=call)):
            assert await pipeline._tag_moments_with_class(
                "j1", [equipe.ClassRun(show_class=SILVER, rides=[ride(1, 0, 10)])],
                [moment("m1", 5, ride_order=1)]) == 0
