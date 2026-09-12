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
# Who the organiser published as down to ride in each class, keyed by class id
# as strings because JSON has no integer keys.
ENTRANTS = {int(k): v for k, v in
            json.loads((HERE / "fixtures_lemieux_entrants.json").read_text()).items()}

# The camera. Every one of these captures is the same Castr URL on the same ring.
CAMERA = "LeMieux Arena"

FRIDAY, SATURDAY = "c1f902bbcd5e484c", "4b0e98e0cdb4403b"
STARTED = {
    FRIDAY: datetime.datetime(2026, 9, 11, 13, 23, 53, tzinfo=datetime.UTC),
    SATURDAY: datetime.datetime(2026, 9, 12, 7, 9, 27, tzinfo=datetime.UTC),
}


def _show():
    return equipe.parse_schedule(SCHEDULE)


def _runs(job: str, arena: str, entrants=None):
    show = _show()
    day = equipe.local_day(STARTED[job], show)
    rides = sorted(RIDES[job], key=lambda r: int(r.get("order") or 0))
    return equipe.assign_classes(
        rides, equipe.classes_on(show, day, arena=arena),
        recorded_from=STARTED[job], entrants=entrants)


def _spans(runs):
    """Each run as (class id, first order, last order)."""
    return [(r.show_class.class_id,
             min(int(x["order"]) for x in r.rides),
             max(int(x["order"]) for x in r.rides)) for r in runs]


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


class TestTheStartListSettlesTheBoundaryRide:
    """Who was down to ride is a record; a timetable is an estimate.

    The clock cannot see a class run over or start early, so the one ride
    either side of a change lands on the wrong side of it. Both real days have
    exactly that, and in both the ride in question is in the published start
    list of exactly one class.
    """

    def test_fridays_first_freestyle_ride_stops_being_an_intermediate_one(self):
        # The Intermediate I was a 42-horse class that began at 08:05 and was
        # still running when the recorder started at 14:23 — so the clock is
        # right about rides 1-6 and wrong only about the changeover. Olivia
        # Oakeley is in the freestyle's list and in no other.
        before = _spans(_runs(FRIDAY, CAMERA))
        after = _spans(_runs(FRIDAY, CAMERA, ENTRANTS))
        assert before == [(1278770, 1, 7), (1278771, 8, 24)]
        assert after == [(1278770, 1, 6), (1278771, 7, 24)]

    def test_saturdays_first_grand_prix_ride_stops_being_a_young_horse(self):
        before = _spans(_runs(SATURDAY, CAMERA))
        after = _spans(_runs(SATURDAY, CAMERA, ENTRANTS))
        assert before == [(1278778, 1, 14), (1278779, 15, 36)]
        assert after == [(1278778, 1, 13), (1278779, 14, 36)]

    def test_the_record_says_the_start_list_moved_it(self):
        runs = _runs(SATURDAY, CAMERA, ENTRANTS)
        moved = next(r for r in runs if r.show_class.class_id == 1278779)
        assert moved.decided_by == "start list"

    def test_a_rider_entered_in_both_is_left_to_the_clock(self):
        # Most riders here are down for two classes, so the lists rule out far
        # more than they rule in. Greg Sims rode in both of Saturday's, and his
        # three rides are placed by time exactly as they were.
        runs = _runs(SATURDAY, CAMERA, ENTRANTS)
        placed = {int(x["order"]): r.show_class.class_id for r in runs for x in r.rides}
        assert placed[13] == 1278778
        assert placed[22] == 1278779 and placed[24] == 1278779

    def test_a_rider_in_no_list_narrows_nothing(self):
        # A name the graphic never showed, or one nobody could read. Every
        # class stays a candidate and the clock decides, which is the answer
        # this had before start lists existed.
        classes = equipe.classes_on(_show(), "2026-09-12", arena=CAMERA)
        assert len(equipe._entered_in({"rider": ""}, classes, ENTRANTS)) == len(classes)
        assert len(equipe._entered_in({"rider": "Nobody At All"}, classes, ENTRANTS)) == len(classes)

    def test_no_start_lists_at_all_changes_nothing(self):
        assert _spans(_runs(SATURDAY, CAMERA, {})) == _spans(_runs(SATURDAY, CAMERA))


class TestMatchingARider:
    def test_a_truncated_lower_third_still_finds_its_entrant(self):
        assert equipe._same_person("Alexander Harrison", "Alexander Harrison-West")
        assert equipe._same_person("Alexander Harrison-West", "Alexander Harrison")

    def test_one_word_is_never_enough(self):
        # "Sue" must not answer for "Sue Carson", and a surname shared by two
        # riders must not decide which class either of them was in.
        assert not equipe._same_person("Sue", "Sue Carson")
        assert not equipe._same_person("Morgan", "Dannie Morgan")

    def test_two_different_people_are_two_different_people(self):
        assert not equipe._same_person("Greg Sims", "Greg Simmons")
        assert not equipe._same_person("Laura Tomlinson", "Laura Thomlinson")
        assert not equipe._same_person("", "Greg Sims")
