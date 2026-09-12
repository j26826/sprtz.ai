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


class TestASplitCanBeRunTwice:
    """Splitting writes each class's own name over the recording's competition.

    That is right — the event *is* the class now — but it means a second split
    searched a thousand shows for "D&H INTER I SILVER CHAMPIONSHIP" and found
    none, because that is a class and the list is of shows. So the first split
    worked and every one after it quietly changed nothing, reporting that the
    show could not be found on a timetable whose id the record was carrying.
    """

    def _transport(self, seen):
        def get(url, timeout=20):
            seen.append(url)
            if "/schedule" in url:
                return SCHEDULE
            raise AssertionError(f"the show list should not be needed: {url}")
        return get

    def test_the_stored_show_id_is_used_and_nothing_is_searched(self):
        seen = []
        job = {"live": {"eventStart": "2026-09-12T07:13:00+00:00"}}
        show, classes = equipe.find_classes(
            job=job, context_urls=[], show_id=82348, arena=CAMERA,
            # What a record carries after one split: a class, not a show.
            competition="D&H INTER I SILVER CHAMPIONSHIP - FEI Intermediate I 2009",
            get=self._transport(seen))
        assert show is not None and show.show_id == 82348
        assert [c.class_id for c in classes] == [1278778, 1278779, 1278780]
        assert all("/schedule" in url for url in seen), seen

    def test_a_class_name_finds_no_show_at_all(self):
        # Why the stored id has to win: this is the search the second split was
        # doing, and there is no show by that name because it is not a show.
        shows = equipe.parse_shows([
            {"id": 82348, "name": "LeMieux National Dressage Championships 2026",
             "start_on": "2026-09-10", "end_on": "2026-09-13"},
        ])
        assert equipe.pick_show(
            shows, on="2026-09-12", discipline="Dressage",
            name_hint="D&H INTER I SILVER CHAMPIONSHIP - FEI Intermediate I 2009") is None
        # The show's own name still finds it, which is what a first split has.
        assert equipe.pick_show(
            shows, on="2026-09-12", discipline="Dressage",
            name_hint="LeMieux National Dressage Championships") is not None


class TestTheRecordReachesTheSchema:
    """The catalog hands a game back in the document's shape, not the schema's.

    camelCase against a snake_case model that ignores what it does not know, so
    validating one against the other kept the single-word fields and dropped
    every other. A re-split rebuilt the record without its teams, its final
    score, its grounded values or the id of the show it belongs to — and
    nothing said so, because filling in defaults is what pydantic is for.
    """

    def test_every_camel_case_field_survives(self):
        from sprtz_agents.schemas import GameDetails
        from sprtz_agents.tools.pipeline import _snake_keys

        stored = {
            "jobId": "x", "sport": "equestrian", "showId": 82348,
            "showTitle": "LeMieux National Dressage Championships",
            "homeTeam": "A", "awayTeam": "B", "finalScore": "1-0",
            "disciplineConfidence": 0.9, "groundedVenue": "Somerford Park Farm",
            "classNo": "13", "testName": "Grand Prix", "resultsFinal": True,
        }
        game = GameDetails.model_validate({**_snake_keys(stored), "job_id": "x", "rides": []})
        assert game.show_id == 82348
        assert game.show_title == "LeMieux National Dressage Championships"
        assert (game.home_team, game.away_team, game.final_score) == ("A", "B", "1-0")
        assert game.discipline_confidence == 0.9
        assert game.grounded_venue == "Somerford Park Farm"
        assert (game.class_no, game.test_name, game.results_final) == ("13", "Grand Prix", True)

    def test_a_caller_already_speaking_snake_case_is_unharmed(self):
        from sprtz_agents.tools.pipeline import _snake_keys
        assert _snake_keys({"show_id": 1})["show_id"] == 1
        # The camelCase twin never overwrites a value that is already there.
        assert _snake_keys({"show_id": 1, "showId": 2})["show_id"] == 1


class TestTheGameIsUnderGame:
    """`get_game` answers `{"status": …, "game": {…}}`, and the split spread the
    envelope rather than the record.

    So the only key reaching `GameDetails` was the word "game". Every field of
    it has a default except `sport`, so this surfaced as one missing-field
    error — raised before a timetable was ever read, which is why fixing the
    show lookup changed nothing. ADK handed the exception to the model, and the
    model wrote the refusal an editor saw out of the tool's own docstring.

    The test above this one fed `_snake_keys` the record directly, so it passed
    on a shape the tool never sees. This one goes through the tool.
    """

    def _catalog(self, game):
        async def call_tool(server, tool, args):
            if tool == "get_job":
                return {"status": "success", "job_id": args["job_id"], "contextUrls": []}
            if tool == "get_game":
                return {"status": "success", "game": game}
            if tool == "list_game_rides":
                return {"status": "success", "rides": [{"rideOrder": 1, "rider": "A"}]}
            if tool == "list_moments":
                return {"status": "success", "moments": []}
            raise AssertionError(f"unexpected tool: {tool}")
        return call_tool

    async def test_the_record_reaches_the_timetable_intact(self, monkeypatch):
        from sprtz_agents.tools import pipeline

        seen = {}

        def classes_for(job, game, context_urls, arena=""):
            seen["game"] = game
            return []

        monkeypatch.setattr(pipeline.mcp_client, "call_tool", self._catalog({
            "jobId": "j", "sport": "equestrian", "showId": 82348,
            "showTitle": "LeMieux National Dressage Championships",
            "competition": "D&H INTER I SILVER CHAMPIONSHIP - FEI Intermediate I 2009",
        }))
        monkeypatch.setattr(pipeline, "_classes_for", classes_for)

        out = await pipeline.split_event_classes("j")

        # It got as far as asking which classes, rather than raising on `sport`.
        assert "game" in seen, out
        assert seen["game"].sport == "equestrian"
        # And carrying the id that lets a second split find the show at all.
        assert seen["game"].show_id == 82348
        assert seen["game"].job_id == "j"
        assert out["status"] == "idle"

    async def test_a_recording_with_no_record_is_told_so(self, monkeypatch):
        from sprtz_agents.tools import pipeline

        async def call_tool(server, tool, args):
            if tool == "get_job":
                return {"status": "success", "job_id": args["job_id"]}
            if tool == "get_game":
                return {"status": "success"}
            return {"status": "success"}

        monkeypatch.setattr(pipeline.mcp_client, "call_tool", call_tool)
        out = await pipeline.split_event_classes("j")
        assert out["status"] == "error"
        assert "no game record" in out["error"]


class TestTheStartListMayNotCrossTheDay:
    """A start list names who rode, and says nothing whatever about when.

    So narrowing by it and *then* asking the clock asks the clock a question it
    cannot refuse: the last class in the pool to have started, when the pool
    holds one class from the morning, is that morning class however many hours
    ago it ended. The afternoon capture of 12 September came back with three of
    its rounds filed under a young horses class that finished at breakfast and
    a Grand Prix that finished at lunch — every one of their riders down for
    those lists too, as most of a championship's riders are.

    The camera ran 14:00-17:00 and only the 13:50 freestyle was in the ring.
    """

    # The afternoon capture: 13:00 UTC is 14:00 at the show.
    AFTERNOON = datetime.datetime(2026, 9, 12, 13, 0, tzinfo=datetime.UTC)

    def _afternoon(self, rides, entrants=None):
        show = _show()
        return equipe.assign_classes(
            rides, equipe.classes_on(show, "2026-09-12", arena=CAMERA),
            recorded_from=self.AFTERNOON, entrants=entrants)

    def _ride(self, order, minute, rider):
        return {"order": order, "rider": rider,
                "start_sec": minute * 60, "end_sec": minute * 60 + 300}

    def test_a_morning_class_cannot_claim_an_afternoon_ride(self):
        # Riders taken from the two morning start lists, riding in the
        # afternoon. Every one of them was moved by the narrowing before.
        morning = ENTRANTS[1278778] + ENTRANTS[1278779]
        rides = [self._ride(i + 1, 25 + i * 10, morning[i]) for i in range(6)]
        runs = self._afternoon(rides, ENTRANTS)
        assert [r.show_class.class_id for r in runs] == [1278780]
        assert len(runs[0].rides) == 6

    def test_the_clock_alone_would_have_said_the_same(self):
        # The bound restores the answer the clock gives over every class, which
        # is the one the ring supports: nothing else had started by then.
        morning = ENTRANTS[1278778] + ENTRANTS[1278779]
        rides = [self._ride(i + 1, 25 + i * 10, morning[i]) for i in range(6)]
        assert (_spans(self._afternoon(rides, ENTRANTS))
                == _spans(self._afternoon(rides)))

    def test_a_class_is_over_when_the_next_one_starts_and_not_before(self):
        classes = equipe.classes_on(_show(), "2026-09-12", arena=CAMERA)
        young, gold, freestyle = classes
        at = lambda h, m: datetime.datetime(  # noqa: E731
            2026, 9, 12, h, m, tzinfo=equipe.show_offset(_show()))
        # The young horses run to 10:05, and over-run is allowed for.
        assert not equipe._ended_before(young, at(10, 20), classes)
        assert equipe._ended_before(young, at(11, 0), classes)
        # The last class of the day has nothing after it to end it.
        assert not equipe._ended_before(freestyle, at(23, 0), classes)
        assert equipe._ended_before(gold, at(16, 23), classes)

    def test_the_boundary_ride_is_still_settled(self):
        # What the start list is for, and it still does it: a ride four minutes
        # before the freestyle's published start, by a rider down for it alone.
        # The clock says the Grand Prix; the list moves it forward one class.
        rider = ENTRANTS[1278779][0]
        ride = self._ride(1, 46, rider)          # 14:46 at the show
        entrants = {1278780: [rider]}
        runs = self._afternoon([ride], entrants)
        assert [r.show_class.class_id for r in runs] == [1278780]
        # A class that has not started yet is not a class the day is past.
        classes = equipe.classes_on(_show(), "2026-09-12", arena=CAMERA)
        early = datetime.datetime(2026, 9, 12, 9, 0, tzinfo=equipe.show_offset(_show()))
        assert not equipe._ended_before(classes[2], early, classes)

    def test_a_recap_card_does_not_reopen_a_finished_class(self):
        # 14:25, half an hour into the freestyle. The graphic names the Grand
        # Prix that ended at lunch, and names it perfectly clearly — it is a
        # recap the broadcast cut to between rounds, and the published results
        # confirm the rider rode that class four hours earlier. A caption is
        # evidence of what was on the screen, not of what was in the ring.
        ride = self._ride(4, 25, "Charlotte Dujardin")
        ride["scoreboard"] = ("LeMieux Grand Prix Gold (544) Charlotte Dujardin "
                              "Braveheart II T Wilaras Owner Ellie McCarthy Bordeaux")
        runs = self._afternoon([ride], ENTRANTS)
        assert [r.show_class.class_id for r in runs] == [1278780]

    def test_a_caption_still_settles_a_class_the_ring_is_on(self):
        # The bound takes nothing from the caption where the class is live:
        # the freestyle has started and its own card moves a ride the clock
        # would have left with the class before.
        # 13:40, ten minutes before the freestyle was due: the clock still says
        # the Grand Prix, and the arena's own card says otherwise.
        ride = self._ride(1, 10, "Someone Unlisted")
        ride["scoreboard"] = "D&H GOLD CHAMPIONSHIP - FEI Intermediate I Freestyle"
        runs = equipe.assign_classes(
            [ride], equipe.classes_on(_show(), "2026-09-12", arena=CAMERA),
            recorded_from=datetime.datetime(2026, 9, 12, 12, 30, tzinfo=datetime.UTC))
        assert [r.show_class.class_id for r in runs] == [1278780]
        assert runs[0].decided_by == "caption"
