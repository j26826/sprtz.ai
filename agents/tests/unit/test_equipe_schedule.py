"""The show timetable, and which class a ride was in.

Against the real thing: `fixtures_equipe_schedule.json` is Equipe's own answer
for the LeMieux National Dressage Championships on 11 September 2026, the day a
recording of ours crossed three classes and came back as one event with a rider
called "unknown".

Nothing here touches the network. What is tested is the reasoning — which show,
which class, and what happens when the timetable and the arena disagree.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from sprtz_agents.tools import equipe

FIXTURE = json.loads((Path(__file__).parent / "fixtures_equipe_schedule.json").read_text())
DAY = "2026-09-11"


def at(hour: int, minute: int = 0) -> datetime.datetime:
    """A UTC time on the recorded day. The show's own times are BST (+01:00)."""
    return datetime.datetime(2026, 9, 11, hour, minute, tzinfo=datetime.UTC)


@pytest.fixture
def show():
    return equipe.parse_schedule(FIXTURE)


class TestReadingTheTimetable:
    def test_it_reads_the_show_and_its_classes(self, show):
        assert show.show_id == 82348
        assert "LeMieux" in show.name
        assert show.url == "https://online.equipe.com/shows/82348"

    def test_a_timetable_and_a_declarations_list_are_not_classes(self, show):
        # Rows on the same page, carrying no rides. An event made from one
        # would be an event with nothing in it.
        names = [c.name for c in show.classes]
        assert not any("declarations" in n.lower() for n in names)
        assert not any("Timetable" in n for n in names)
        assert len(show.classes) == 6

    def test_classes_come_back_in_the_order_they_run(self, show):
        times = [c.start_at for c in show.classes if c.start_at]
        assert times == sorted(times)

    def test_times_are_read_with_their_zone(self, show):
        gold = next(c for c in show.classes if "FREESTYLE GOLD" in c.name)
        # "2026-09-11 15:20:00 +0100" is 14:20 UTC, and an hour's error here
        # would file a whole class under the one before it.
        assert gold.start_at == at(14, 20)

    def test_a_class_carries_its_own_page(self, show):
        gold = next(c for c in show.classes if "FREESTYLE GOLD" in c.name)
        assert gold.url == f"https://online.equipe.com/meeting_classes/{gold.class_id}"

    def test_nothing_parseable_is_not_a_crash(self):
        assert equipe.parse_schedule(None) is None
        assert equipe.parse_schedule({"meeting_classes": []}) is None
        assert equipe.parse_shows("not a list") == []


class TestWhichShow:
    def test_an_editors_own_link_names_it(self):
        found = equipe.show_id_in([
            "https://www.britishdressage.co.uk/whats-on",
            "https://online.equipe.com/shows/82348",
        ])
        assert found == 82348

    def test_both_spellings_of_the_link_reach_the_same_show(self):
        assert equipe.show_id_in(["https://online.equipe.com/meetings/82348"]) == 82348
        assert equipe.show_id_in(["https://online.equipe.com/en/shows/82348/"]) == 82348

    def test_no_equipe_link_is_no_show(self):
        assert equipe.show_id_in(["https://example.com/results"]) is None
        assert equipe.show_id_in([]) is None

    def test_the_day_narrows_it_and_the_name_decides(self):
        shows = [
            equipe.Show(1, "Onley Grounds Senior British Showjumping", "2026-09-11", "2026-09-11"),
            equipe.Show(2, "LeMieux National Dressage Championships 2026", "2026-09-10", "2026-09-13"),
            equipe.Show(3, "Silkeborg Rideklub", "2026-09-11", "2026-09-12"),
        ]
        picked = equipe.pick_show(shows, on=DAY, name_hint="LeMieux National Dressage Championships")
        assert picked.show_id == 2

    def test_a_show_that_did_not_run_that_day_is_not_it(self):
        shows = [equipe.Show(2, "LeMieux National Dressage Championships", "2026-09-20", "2026-09-22")]
        assert equipe.pick_show(shows, on=DAY, name_hint="LeMieux National Dressage") is None

    def test_a_name_that_matches_nothing_returns_nothing(self):
        # The wrong show's timetable is worse than no timetable: every ride
        # would be filed under a class it was not in.
        shows = [equipe.Show(1, "Silkeborg Rideklub", "2026-09-11", "2026-09-11")]
        assert equipe.pick_show(shows, on=DAY, name_hint="Hickstead Derby Meeting") is None

    def test_one_show_that_day_and_nothing_read_on_screen_is_that_show(self):
        shows = [equipe.Show(7, "Somerford Park", "2026-09-11", "2026-09-11")]
        assert equipe.pick_show(shows, on=DAY).show_id == 7


class TestWhichClass:
    def _rides(self, show):
        silver = next(c for c in show.classes if "PRIX ST.GEORGE SILVER" in c.name)
        gold = next(c for c in show.classes if "FREESTYLE GOLD" in c.name)
        return silver, gold

    def test_the_clock_places_every_ride(self, show):
        silver, gold = self._rides(show)
        # The capture began at 13:23 BST; these offsets are into the recording.
        recorded_from = at(12, 23)
        rides = [
            {"order": 1, "start_sec": 60 * 60},        # 13:23 UTC — Silver (13:00)
            {"order": 2, "start_sec": 60 * 95},        # 13:58 UTC — Silver
            {"order": 3, "start_sec": 60 * 130},       # 14:33 UTC — Gold (14:20)
        ]
        runs = equipe.assign_classes(rides, show.classes, recorded_from=recorded_from)

        placed = {run.show_class.class_id: [r["order"] for r in run.rides] for run in runs}
        assert placed[silver.class_id] == [1, 2]
        assert placed[gold.class_id] == [3]

    def test_a_caption_overrules_a_timetable_that_slipped(self, show):
        silver, gold = self._rides(show)
        recorded_from = at(12, 23)
        # By the clock this ride is still in the Silver class; the arena says
        # otherwise, and the arena is where the competition was.
        rides = [{"order": 9, "start_sec": 60 * 80,
                  "scoreboard": "FAIRFAX SADDLES PSG FS GOLD\n(118) Danny Morgan"}]
        runs = equipe.assign_classes(rides, show.classes, recorded_from=recorded_from)

        assert len(runs) == 1
        assert runs[0].show_class.class_id == gold.class_id
        assert runs[0].decided_by == "caption"

    def test_a_class_that_started_late_still_takes_its_rides(self, show):
        _, gold = self._rides(show)
        # Published 15:20 BST, actually under way at 15:40: the clock places
        # these correctly without help, because a late class still starts
        # before its own rides.
        runs = equipe.assign_classes(
            [{"order": 4, "start_sec": 60 * 197}, {"order": 5, "start_sec": 60 * 210}],
            show.classes, recorded_from=at(12, 23))
        assert [run.show_class.class_id for run in runs] == [gold.class_id]

    def test_a_class_that_runs_over_keeps_its_rides_through_the_caption(self, show):
        # The other direction, and the one the clock cannot answer: the Silver
        # class is still in the arena after the Gold was due to start.
        silver, _ = self._rides(show)
        runs = equipe.assign_classes(
            [{"order": 6, "start_sec": 60 * 125,
              "scoreboard": "FAIRFAX SADDLES PRIX ST GEORGES SILVER\n(325) Sara-Jane Lanning"}],
            show.classes, recorded_from=at(12, 23))
        assert runs[0].show_class.class_id == silver.class_id
        assert runs[0].decided_by == "caption"

    def test_rides_before_anything_was_due_belong_to_the_first_class(self, show):
        runs = equipe.assign_classes(
            [{"order": 1, "start_sec": 0}], show.classes, recorded_from=at(5, 0))
        assert runs and runs[0].rides

    def test_with_no_clock_and_no_caption_the_day_stays_one_event(self, show):
        # An uploaded file has no absolute time. Everything lands in one run,
        # which is what the desk did before any of this existed.
        rides = [{"order": n} for n in range(1, 6)]
        runs = equipe.assign_classes(rides, show.classes, recorded_from=None)
        assert len(runs) == 1
        assert len(runs[0].rides) == 5

    def test_no_classes_is_no_split(self, show):
        assert equipe.assign_classes([{"order": 1}], [], recorded_from=at(13)) == []

    def test_a_class_with_no_rides_is_not_an_event(self, show):
        runs = equipe.assign_classes(
            [{"order": 1, "start_sec": 0}], show.classes, recorded_from=at(12, 23))
        assert all(run.rides for run in runs)


class TestComparingNames:
    def test_a_caption_is_the_sponsor_and_the_grade(self):
        # What is burnt into a broadcast against what a results site publishes.
        assert equipe.same_name(
            "FAIRFAX SADDLES PSG FS GOLD",
            "FAIRFAX SADDLES PSG FREESTYLE GOLD CHAMPIONSHIP - FEI Young Riders",
        ) >= equipe.CAPTION_MATCH

    def test_two_classes_of_the_same_sponsor_are_still_told_apart(self):
        silver = "FAIRFAX SADDLES PRIX ST.GEORGE SILVER CHAMPIONSHIP - FEI Prix St Georges"
        gold = "FAIRFAX SADDLES PSG FREESTYLE GOLD CHAMPIONSHIP - FEI Young Riders"
        caption = "FAIRFAX SADDLES PRIX ST GEORGES SILVER"
        assert equipe.same_name(caption, silver) > equipe.same_name(caption, gold)

    def test_nothing_matches_nothing(self):
        assert equipe.same_name("", "anything") == 0.0


class TestTheDayThisWasBuiltFor:
    """11 September 2026, as it was actually recorded and actually published.

    `fixtures_lemieux_rides.json` is the 24 rides the analysis fused out of
    that capture — every one of them real, including the prize-giving card and
    the two rides the desk could not name. The recording ran 13:23–17:01 BST
    and crossed three classes; it came back as one event with a running order
    of 24 and a rider called "unknown", which is what this whole module exists
    to stop.
    """

    @pytest.fixture
    def day(self, show):
        rides = json.loads((Path(__file__).parent / "fixtures_lemieux_rides.json").read_text())
        return equipe.assign_classes(
            rides["rides"], equipe.classes_on(show, DAY),
            recorded_from=equipe.parse_time(rides["recorded_from"]))

    def test_it_finds_the_three_classes_the_camera_crossed(self, day):
        names = [run.show_class.name for run in day]
        assert len(day) == 3
        assert "BETTALIFE NOVICE GOLD" in names[0]
        assert "PRIX ST.GEORGE SILVER" in names[1]
        assert "PSG FREESTYLE GOLD" in names[2]

    def test_every_ride_is_in_exactly_one_class(self, day):
        orders = [r["order"] for run in day for r in run.rides]
        assert sorted(orders) == list(range(1, 25))

    def test_the_classes_do_not_interleave(self, day):
        # They ran one after another, so a correct split is three unbroken
        # runs. Interleaving would mean rides landing in a class that had
        # finished before they happened.
        for run in day:
            orders = [r["order"] for r in run.rides]
            assert orders == sorted(orders)
            assert orders[-1] - orders[0] == len(orders) - 1

    def test_the_prize_giving_stays_with_the_class_it_was_for(self, day):
        # Ride 22's card reads "FAIRFAX SADDLES Prix St Georges Freestyle Gold
        # Champion 2024" and matched the *Silver* class by words alone, because
        # the only ones the two classes share are the sponsor's.
        gold = day[-1]
        assert 22 in [r["order"] for r in gold.rides]

    def test_psg_spelled_out_is_still_psg(self, day):
        # Ride 23's caption spells out what the class abbreviates. Without the
        # expansion it was filed under the Silver class, two hours after that
        # class had finished.
        gold = day[-1]
        assert 23 in [r["order"] for r in gold.rides]
