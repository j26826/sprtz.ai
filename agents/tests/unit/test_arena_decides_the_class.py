"""A recording is of one ring, and that decides which classes it can hold.

A championship runs several arenas at once — LeMieux ran three — and a fixed
camera points at exactly one of them. `assign_classes` places a ride under the
last class to have *started*, which is right for a ring and meaningless for a
showground: at any moment three classes have started and the most recent is
usually in a ring the camera has never seen.

Handed every class of 12 September, it filed thirty-one of thirty-six LeMieux
Arena rides under two Vector Arena classes. This is that day and the one before
it, with the real published timetable and the real rides both days produced.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from sprtz_agents.tools import equipe

HERE = Path(__file__).parent
SCHEDULE = json.loads((HERE / "fixtures_lemieux_schedule.json").read_text())
RIDES = json.loads((HERE / "fixtures_lemieux_arenas.json").read_text())

# The camera. Every one of these captures is the same Castr URL on the same ring.
CAMERA = "LeMieux Arena"

FRIDAY, SATURDAY = "c1f902bbcd5e484c", "4b0e98e0cdb4403b"
STARTED = {
    FRIDAY: datetime.datetime(2026, 9, 11, 13, 23, 53, tzinfo=datetime.UTC),
    SATURDAY: datetime.datetime(2026, 9, 12, 7, 9, 27, tzinfo=datetime.UTC),
}


def _show():
    return equipe.parse_schedule(SCHEDULE)


def _runs(job: str, arena: str):
    show = _show()
    day = equipe.local_day(STARTED[job], show)
    rides = sorted(RIDES[job], key=lambda r: int(r.get("order") or 0))
    return equipe.assign_classes(
        rides, equipe.classes_on(show, day, arena=arena), recorded_from=STARTED[job])


class TestTheShowKeepsItsOwnClock:
    def test_a_class_time_stays_in_the_zone_it_was_published_in(self):
        # Equipe stamps "+0100" and that is the show's own clock. Normalising
        # it away on the way in loses the only thing that can say which local
        # day a class is on.
        show = _show()
        assert equipe.show_offset(show) == datetime.timezone(datetime.timedelta(hours=1))

    def test_the_day_is_the_showgrounds_day_not_a_utc_one(self):
        show = _show()
        # 23:30 UTC is half past midnight at the show: tomorrow's classes.
        after_midnight = datetime.datetime(2026, 9, 11, 23, 30, tzinfo=datetime.UTC)
        assert equipe.local_day(after_midnight, show) == "2026-09-12"
        assert equipe.local_day(STARTED[SATURDAY], show) == "2026-09-12"

    def test_an_instant_is_still_the_same_instant(self):
        # Keeping the offset must not move any time. 15:20 +0100 is 14:20 UTC
        # and every comparison in this module is between aware datetimes.
        gold = next(c for c in _show().classes if c.class_id == 1278771)
        assert gold.start_at == datetime.datetime(2026, 9, 11, 14, 20, tzinfo=datetime.UTC)


class TestOnlyOneRingIsACandidate:
    def test_the_showground_runs_three_rings_at_once(self):
        show = _show()
        assert equipe.arenas_on(show, "2026-09-12") == [
            "LeMieux Arena", "Kudos Arena", "Vector Arena"]

    def test_saturday_is_two_classes_of_the_camera_s_own_ring(self):
        # 07:53 Young Horses 7YR, running when the recorder started at 08:09,
        # then Grand Prix Gold from 10:05. Nothing else was in this ring.
        runs = _runs(SATURDAY, CAMERA)
        assert [r.show_class.class_id for r in runs] == [1278778, 1278779]
        assert [len(r.rides) for r in runs] == [14, 22]
        assert {r.show_class.arena for r in runs} == {CAMERA}

    def test_saturday_unfiltered_files_most_of_it_in_a_ring_the_camera_never_saw(self):
        # The bug, pinned. Without an arena the clock reaches across the whole
        # showground and two of the three classes are Vector Arena's.
        runs = _runs(SATURDAY, "")
        arenas = [r.show_class.arena for r in runs]
        assert "Vector Arena" in arenas
        wrong = sum(len(r.rides) for r in runs if r.show_class.arena != CAMERA)
        assert wrong == 31

    def test_friday_keeps_its_freestyle(self):
        runs = _runs(FRIDAY, CAMERA)
        gold = next(r for r in runs if r.show_class.class_id == 1278771)
        assert len(gold.rides) == 17
        assert {r.show_class.arena for r in runs} == {CAMERA}


class TestNamingTheRing:
    def test_a_name_matches_the_ring_however_much_of_it_was_typed(self):
        assert equipe.same_arena("LeMieux", "LeMieux Arena")
        assert equipe.same_arena("lemieux arena", "LeMieux Arena")
        assert equipe.same_arena("Vector Arena", "Vector")

    def test_one_ring_never_answers_for_another(self):
        # The whole argument exists to stop this.
        assert not equipe.same_arena("Vector", "Kudos Arena")
        assert not equipe.same_arena("Kudos", "LeMieux Arena")
        assert not equipe.same_arena("", "LeMieux Arena")

    def test_the_scoreboards_can_name_the_ring_when_nobody_did(self):
        show = _show()
        rides = RIDES[FRIDAY]
        assert equipe.pick_arena(show, "2026-09-11", rides) == CAMERA

    def test_no_evidence_names_no_ring(self):
        # Saturday's captions never matched a class cleanly enough, and saying
        # so is the point: the day then stays one event, which is recoverable,
        # rather than being split under a ring chosen by noise.
        show = _show()
        assert equipe.pick_arena(show, "2026-09-12", RIDES[SATURDAY]) == ""
        assert equipe.pick_arena(show, "2026-09-12", []) == ""
