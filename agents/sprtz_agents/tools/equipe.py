"""The show timetable, from Equipe itself.

A live URL points at an arena, not at a competition. The camera runs all day
and the classes change under it — the LeMieux recording of 11 September crossed
the 14:00 Prix St Georges Silver Championship, the 15:20 PSG Freestyle Gold
Championship and the Gold prize-giving in one unbroken capture, and the desk
read the lot as a single event with one running order and a rider called
"unknown". A day is not a competition; a class is.

`online.equipe.com` publishes the timetable and it is genuinely fetchable:

    GET /api/v1/meetings                       every show, with dates
    GET /api/v1/meetings/{id}/schedule         that show's classes, with times

That matters because the rest of the grounding cannot do this. `identify_show`
asks Gemini with Google Search, because a show *page* is a JavaScript shell and
`.json` on it answers 406 — so what comes back is Google's index of the
rendered page, one class at a time, as prose. A timetable has to be exact and
complete, and this is the one route that gives both.

Everything that decides anything is pure and takes parsed JSON, so the rules —
which show, which class, where a class begins — are tested against a real
captured timetable rather than against the network.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

BASE = "https://online.equipe.com"
MEETINGS_PATH = "/api/v1/meetings"
SCHEDULE_PATH = "/api/v1/meetings/{show_id}/schedule"

# A show id, from a link an editor pasted. Both spellings reach the same show.
_SHOW_URL = re.compile(r"online\.equipe\.com/(?:[a-z]{2}/)?(?:shows|meetings)/(\d+)", re.I)

# Classes that are not a competition: a timetable attachment, a declarations
# list, a summary of who won. They carry no rides and must not become events.
_NOT_A_CLASS = {"list", "score_summary", "info", "text"}

# How close two names must be to count as the same competition. Names arrive
# from two places — a results site and a caption burnt into a broadcast — and
# the second is abbreviated, upper-cased and often truncated mid-word.
_SAME_NAME = 0.62


@dataclass
class ShowClass:
    """One class of a show: what it is called, and when it was due to start."""

    class_id: int
    name: str
    start_at: datetime.datetime | None
    date: str = ""
    discipline: str = ""
    position: int = 0
    # The class as the organiser numbered it, where it ran, and the sections
    # its results live under — a class is usually one, occasionally several.
    class_no: str = ""
    arena: str = ""
    section_ids: list[int] = field(default_factory=list)
    # The test that was ridden: its name, and the movements it is marked on.
    # Stored because it is what the marks mean — a 7 for a piaffe at
    # coefficient 2 is not a 7 for an entry.
    test_name: str = ""
    judge_positions: list[str] = field(default_factory=list)
    movements: list[dict] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"{BASE}/meeting_classes/{self.class_id}"


@dataclass
class Show:
    """A show: several days, many classes."""

    show_id: int
    name: str
    start_on: str = ""
    end_on: str = ""
    discipline: str = ""
    country: str = ""
    classes: list[ShowClass] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"{BASE}/shows/{self.show_id}"


def show_id_in(urls: list[str]) -> int | None:
    """The show an editor's own link names, if one of them does.

    A pasted link is the strongest signal available: it is the one fact about
    which show this is that nobody had to infer.
    """
    for url in urls or []:
        found = _SHOW_URL.search(str(url))
        if found:
            return int(found.group(1))
    return None


def parse_shows(payload: Any) -> list[Show]:
    """The shows list, as records. Anything unparseable is left out."""
    shows: list[Show] = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        shows.append(Show(
            show_id=int(row["id"]),
            name=str(row.get("display_name") or row.get("name") or ""),
            start_on=str(row.get("start_on") or ""),
            end_on=str(row.get("end_on") or ""),
            discipline=str(row.get("discipline") or ""),
            country=str(row.get("venue_country") or ""),
        ))
    return shows


def parse_schedule(payload: Any) -> Show | None:
    """One show with its classes, from the schedule endpoint."""
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    show = Show(
        show_id=int(payload["id"]),
        name=str(payload.get("display_name") or payload.get("name") or ""),
        start_on=str(payload.get("start_on") or ""),
        end_on=str(payload.get("end_on") or ""),
        discipline=str(payload.get("discipline") or ""),
        country=str(payload.get("venue_country") or ""),
    )
    for row in payload.get("meeting_classes") or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        discipline = str(row.get("discipline") or "")
        if discipline in _NOT_A_CLASS:
            # A timetable PDF and a declarations list are rows on this page and
            # not competitions; an event made from one would hold no rides.
            continue
        sheet = _first_sheet(row.get("score_sheets"))
        show.classes.append(ShowClass(
            class_id=int(row["id"]),
            name=str(row.get("name") or ""),
            start_at=parse_time(row.get("start_at")),
            date=str(row.get("date") or ""),
            discipline=discipline,
            position=int(row.get("position") or 0),
            class_no=str(row.get("class_no") or ""),
            arena=str((row.get("arena") or {}).get("name") or "") if isinstance(row.get("arena"), dict)
            else str(row.get("arena") or ""),
            section_ids=[int(sec["id"]) for sec in row.get("class_sections") or []
                         if isinstance(sec, dict) and sec.get("id")],
            test_name=str(sheet.get("name") or ""),
            judge_positions=[str(p) for p in (sheet.get("judge_by_aliases") or {})],
            movements=[{
                "position": item.get("position"),
                "keyword": item.get("keyword", ""),
                "coefficient": item.get("coefficient"),
                "section": item.get("section", ""),
            } for item in sheet.get("sheet_items") or [] if isinstance(item, dict)],
        ))
    show.classes.sort(key=lambda c: (c.start_at or _FAR_FUTURE, c.position))
    return show


_FAR_FUTURE = datetime.datetime(9999, 1, 1, tzinfo=datetime.UTC)


def _first_sheet(sheets: Any) -> dict:
    """The class's marking sheet. Keyed by sheet id, and a class has one."""
    if isinstance(sheets, dict):
        for sheet in sheets.values():
            if isinstance(sheet, dict):
                return sheet
    return {}


def parse_time(value: Any) -> datetime.datetime | None:
    """Equipe's times, which carry a zone: "2026-09-11 15:20:00 +0100".

    **The offset is kept, not normalised away.** An aware datetime is the same
    instant however it is written, so every comparison and every subtraction
    below is unaffected — but the zone it was written in is the show's own, and
    that is the only thing that can say which *day* a class is on. This used to
    convert to UTC on the way in, and a competition day is local: a class at
    00:30 BST is 23:30 UTC the day before, so a recording that starts then is
    of tomorrow's classes by the timetable and yesterday's by the clock we had
    kept. `show_offset` reads the zone back off these times.

    A time with no offset at all is taken as UTC, which is what our own
    timestamps are.
    """
    if not value:
        return None
    text = str(value).strip()
    for shape in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M %z"):
        try:
            return datetime.datetime.strptime(text, shape)
        except ValueError:
            continue
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


# What a timetable abbreviates and a broadcast spells out, or the other way
# round. "PSG" is not a token that happens to look like another: it *is* Prix
# St Georges, and a class called "PSG FREESTYLE GOLD" shares every word with a
# caption reading "Prix St Georges Freestyle Gold" once that is known. Without
# this, the day of 11 September filed a Gold freestyle ride under the Silver
# class, because the only words the two shared were the sponsor's.
_EXPANSIONS = {
    "psg": "prix st georges",
    "fs": "freestyle",
    "kur": "freestyle",
    "gp": "grand prix",
    "gps": "grand prix special",
    "yr": "young riders",
    "inter": "intermediate",
    "ch": "championship",
    "champ": "championship",
    "nat": "national",
}

# Words that name no class in particular: they are in most of them.
_FILLER = {"the", "and", "of", "class", "fei", "bd", "british"}


def normalise(text: str) -> str:
    """A name as comparable words: lower case, no punctuation, abbreviations out.

    "St." and "St" are the same word; "georges" and "george" are not, and are
    left alone — the difference between two classes is sometimes exactly one
    letter, and stemming them together would lose it.
    """
    plain = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())
    out = []
    for word in plain.split():
        if word in _FILLER:
            continue
        out.append(_EXPANSIONS.get(word, word))
    return " ".join(out).strip()


def same_name(left: str, right: str) -> float:
    """How alike two competition names are, 0 to 1.

    Token overlap as well as sequence similarity, because a caption is usually
    the sponsor and the grade — "FAIRFAX SADDLES PSG FS GOLD" against
    "FAIRFAX SADDLES PSG FREESTYLE GOLD CHAMPIONSHIP - FEI Young Riders" — and
    a straight ratio on those two is low enough to miss.
    """
    if not str(left or "").strip() or not str(right or "").strip():
        return 0.0
    # A scoreboard is several lines — the class on one, the rider and horse on
    # the others — so each line is scored on its own and the best one stands.
    # Scoring the block whole lets a rider's name dilute a class it names
    # outright: "FAIRFAX SADDLES PSG FS GOLD / (118) Danny Morgan" against that
    # class scores 0.5 as a block and 0.8 as its first line.
    best = 0.0
    for line in str(left).splitlines() or [str(left)]:
        best = max(best, _line_match(line, right))
    return max(best, _line_match(left, right))


def _line_match(left: str, right: str) -> float:
    a, b = normalise(left), normalise(right)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # Digits and one-letter tokens are a start number or a stray initial, and
    # they belong to neither name.
    words_a = {w for w in a.split() if len(w) > 1 and not w.isdigit()}
    words_b = {w for w in b.split() if len(w) > 1 and not w.isdigit()}
    if not words_a or not words_b:
        return ratio
    overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
    return max(ratio, overlap)


def pick_show(shows: list[Show], *, on: str = "", name_hint: str = "",
              country: str = "", discipline: str = "") -> Show | None:
    """Which show a recording is of, from the shows list.

    The date is the hard filter — a recording happened on a day — and the name
    read off the screen decides between the several shows that ran on it. A
    country or a discipline narrows it further when they are known, but neither
    is required: a show that runs on the right day with the right name is the
    show, whatever else disagrees.
    """
    on_that_day = [s for s in shows if _covers(s, on)] if on else list(shows)
    if not on_that_day:
        return None
    if len(on_that_day) == 1 and not name_hint:
        return on_that_day[0]

    scored = []
    for show in on_that_day:
        score = same_name(name_hint, show.name) if name_hint else 0.0
        if country and show.country and show.country.upper() == country.upper():
            score += 0.15
        if discipline and show.discipline and show.discipline == discipline:
            score += 0.1
        scored.append((score, show))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best, show = scored[0]
    # Without a name there is nothing to choose on, and the wrong show's
    # timetable is worse than no timetable: every ride would be filed under a
    # class it was not in.
    if name_hint and best < _SAME_NAME:
        return None
    return show if best > 0 or not name_hint else None


def _covers(show: Show, day: str) -> bool:
    start = show.start_on or show.end_on
    end = show.end_on or show.start_on
    return bool(start and end and start <= day <= end)


def show_offset(show: Show) -> datetime.timezone:
    """The clock the show keeps, taken from the times it publishes.

    Equipe stamps every class with an explicit offset — "2026-09-12 07:53:00
    +0100" — so the show tells us its own zone and there is no need for a
    timezone database or a guess from the country. A show with no timed class
    at all keeps UTC, which changes nothing for it.

    It matters because a competition day is a *local* day. A class at 00:30 BST
    is 23:30 UTC the day before, and a recording that starts then is of
    tomorrow's classes by the timetable and yesterday's by a UTC clock.
    """
    for c in show.classes:
        if c.start_at and c.start_at.utcoffset() is not None:
            return c.start_at.tzinfo  # type: ignore[return-value]
    return datetime.UTC


def local_day(at: datetime.datetime | None, show: Show) -> str:
    """The show's own date for an instant, as Equipe dates its days."""
    if not at:
        return ""
    return at.astimezone(show_offset(show)).date().isoformat()


def classes_on(show: Show, day: str, arena: str = "") -> list[ShowClass]:
    """The show's classes for one day and one arena, in the order they ran.

    **The arena is not a detail, it is the whole candidate set.** A
    championship runs several rings at once — LeMieux ran three — and a fixed
    camera is pointed at exactly one of them. Handed every class of the day,
    the clock rule below places each ride under whichever class started most
    recently *anywhere on the showground*, which on 12 September filed
    thirty-one of thirty-six rides from the LeMieux Arena under two Vector
    Arena classes. The rule is sound; it was being asked a question about a
    ring it had not been told about.

    An empty arena means every ring, which is right for a one-ring show and for
    a caller that has no idea — but `assign_classes` should be given one ring's
    classes or the clock is meaningless.

    The day is compared in the show's own clock on both sides. `date` comes
    from the API as a local date and `start_at` was converted to UTC on the way
    in, so comparing one against a UTC day and the other against a local one —
    which is what this did — is two different questions joined by `or`.
    """
    picked = list(show.classes)
    if day:
        picked = [c for c in picked
                  if (c.date or "") == day or local_day(c.start_at, show) == day]
    if arena:
        picked = [c for c in picked if same_arena(c.arena, arena)]
    return picked


# An arena name is two or three words and one of them is the ring's own
# ("LeMieux Arena", "Vector Arena"). Someone naming it will type that word and
# not always the rest, so a name that contains the other is the same ring —
# and the fuzzy bar is only there for a typo, not to bridge two real rings.
ARENA_MATCH = 0.82


def same_arena(left: str, right: str) -> bool:
    """Whether two arena names mean the same ring.

    Lenient about the word "arena" and about which half was typed, strict about
    everything else: filing a day under the wrong ring is the failure this
    whole argument exists to stop, so "Vector" must never answer for "Kudos".
    """
    a, b = normalise(left), normalise(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # Whole-word containment, so "lemieux" finds "lemieux arena" but "vector"
    # cannot find "vectra" by being a prefix of it.
    if f" {a} " in f" {b} " or f" {b} " in f" {a} ":
        return True
    return same_name(left, right) >= ARENA_MATCH


def arenas_on(show: Show, day: str) -> list[str]:
    """The rings this show ran on one day, in the order they first appear.

    What an editor picks from when they say which one the camera is on.
    """
    seen: list[str] = []
    for c in classes_on(show, day):
        if c.arena and c.arena not in seen:
            seen.append(c.arena)
    return seen


# --- Fetching -----------------------------------------------------------------
#
# One call each, both public and unauthenticated. The transport is passed in so
# the rules above can be tested without it, and so a failure here is a warning
# rather than something that can take an analysis down: grounding is the last
# thing to run and the moments stand without it.

def fetch_shows(get=None, timeout: int = 20) -> list[Show]:
    """Every show Equipe currently lists. About half a megabyte of JSON."""
    payload = _get(f"{BASE}{MEETINGS_PATH}", get=get, timeout=timeout)
    return parse_shows(payload) if payload is not None else []


def fetch_schedule(show_id: int, get=None, timeout: int = 20) -> Show | None:
    """One show's classes, with their start times."""
    payload = _get(f"{BASE}{SCHEDULE_PATH.format(show_id=show_id)}", get=get, timeout=timeout)
    return parse_schedule(payload) if payload is not None else None


def _get(url: str, get=None, timeout: int = 20) -> Any:
    try:
        if get is None:
            import httpx

            response = httpx.get(url, timeout=timeout, follow_redirects=True,
                                 headers={"Accept": "application/json"})
            if response.status_code != 200:
                logger.warning("equipe %s answered %s", url, response.status_code)
                return None
            return response.json()
        return get(url)
    except Exception:
        # Equipe being unreachable is not this recording's problem: the rides
        # stand on what was read off the screen, and the day simply stays one
        # event rather than being split into its classes.
        logger.warning("could not read %s", url, exc_info=True)
        return None


# --- Which class a ride was in ------------------------------------------------

# A caption has to look like the class before it is allowed to name it at all.
# Below this it is a partial read of something else.
CAPTION_MATCH = 0.62

# ...and to overrule the clock it has to be this much better than the class the
# clock chose. Two classes of one sponsor share most of their words — "FAIRFAX
# SADDLES PRIX ST.GEORGE SILVER" and "FAIRFAX SADDLES PSG FREESTYLE GOLD" — so
# a card that names neither cleanly scores near-equally against both. The
# prize-giving of 11 September did exactly that, naming the Gold champion in
# words that matched the Silver class by 0.625 to 0.599; on that margin the
# clock is the better answer, and the clock had it right.
CAPTION_MARGIN = 0.08


@dataclass
class ClassRun:
    """One class as it actually ran: its rides, and where they came from."""

    show_class: ShowClass
    rides: list[dict] = field(default_factory=list)
    # "schedule" when the clock placed these rides, "caption" when what was
    # read on screen moved them. Kept because it is the difference between a
    # boundary that was published and one that was observed.
    decided_by: str = "schedule"


def _same_person(left: str, right: str) -> bool:
    """Whether two names are the same rider.

    Normalised equality, plus whole-word prefix either way, because a lower
    third truncates: the arena graphic says "Alexander Harrison" where the
    start list says "Alexander Harrison-West". Prefixes must be whole words and
    at least two of them, so "Sue Carson" cannot answer for "Sue Carson-Smith"
    on one word alone.

    Deliberately no fuzzy bar. A near-miss here does not degrade the answer, it
    moves a ride into a class it was not in — where failing to match simply
    leaves the ride to the clock, which is what decided it before.
    """
    a, b = normalise(left), normalise(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short.split()) >= 2 and long.startswith(f"{short} ")


def entrants_for(show_class: ShowClass, get=None) -> list[str]:
    """Who was down to ride in a class, from its published start list.

    The strongest thing there is about which class a ride belongs to: it is
    the organiser's own record of who was in the ring, against a timetable that
    only says when a class was *due*.
    """
    results = results_for(show_class, get=get)
    return [st.rider for st in (results.starts if results else []) if st.rider]


def _entered_in(ride: dict, classes: list[ShowClass],
                entrants: dict[int, list[str]]) -> list[ShowClass]:
    """The classes whose start list names this ride's rider.

    Narrowing, not deciding — a rider can be down for two classes on one day
    and at this show most of them are, so the start lists rule out far more
    than they rule in. What is left goes to the clock exactly as before.

    A rider nobody can name, or a class whose list could not be read, narrows
    nothing: every class stays a candidate, which is where this started.
    """
    rider = str(ride.get("rider") or "")
    if not rider or not entrants:
        return list(classes)
    named = [c for c in classes
             if any(_same_person(rider, entry) for entry in entrants.get(c.class_id, []))]
    return named or list(classes)


# How far a class may run from its published slot in either direction. A
# timetable slips: a class over-runs, so the ring is not free the moment the
# next one is due, and a class goes in early, so a ride just before its
# published time is still its own. What this rules out is the other order of
# magnitude — a class the day finished with hours ago, or one not due until
# the afternoon. It bounds the two signals that read a rider and a graphic
# and know nothing whatever about when.
SLACK = datetime.timedelta(minutes=30)


def _ended_before(show_class: ShowClass, at: datetime.datetime,
                  classes: list[ShowClass]) -> bool:
    """Whether the ring had moved on from this class by the time of a ride.

    A class ends when the next one in its own ring begins, give or take the
    slack. The last class of the day never ends, because nothing follows it
    to say that it has.
    """
    if show_class.start_at is None:
        return False
    after = [c.start_at for c in classes
             if c.start_at and c.start_at > show_class.start_at]
    return bool(after) and at >= min(after) + SLACK


def _not_due_yet(show_class: ShowClass, at: datetime.datetime) -> bool:
    """Whether the day had not yet reached this class when a ride happened.

    The mirror of the one above, and it had to be written for the same reason.
    The clock alone never picks a class that has not started — there is
    deliberately no allowance forward — but a start list narrowed to a single
    future class leaves the clock nothing else to answer, and its own fallback
    for "before anything was due" then hands the ride to it. That is how two
    rounds of a morning young horses class were filed under an afternoon
    freestyle five hours before it was due in the ring.
    """
    return show_class.start_at is not None and at < show_class.start_at - SLACK


def _not_past(classes: list[ShowClass], at: datetime.datetime | None) -> list[ShowClass]:
    """The classes the ring had not already finished with."""
    if at is None:
        return list(classes)
    return [c for c in classes if not _ended_before(c, at, classes)]


def _reached(classes: list[ShowClass], at: datetime.datetime | None) -> list[ShowClass]:
    """Those of them the day had actually got to."""
    if at is None:
        return list(classes)
    return [c for c in classes if not _not_due_yet(c, at)]


def assign_classes(rides: list[dict], classes: list[ShowClass], *,
                   recorded_from: datetime.datetime | None,
                   entrants: dict[int, list[str]] | None = None) -> list[ClassRun]:
    """Split a day's rides into the classes they belong to.

    Three signals, and they answer different failures. **The start list narrows
    it**: the organiser published who was down to ride in each class, and a
    rider in exactly one of them was in exactly one of them — which is the only
    signal here that is a record rather than an estimate. It narrows rather
    than decides, because a rider can be entered in two classes on one day and
    at this show most are. **The clock places every ride** among what is left:
    a class has a published start and a ride has an absolute time, so each ride
    belongs to the last class that had started. **The caption corrects it**:
    timetables slip, and a class that ran forty minutes late would otherwise
    take its first rides from the class before it. Where a run of rides carries
    a scoreboard naming a class, that naming wins — it was read off the arena,
    which is where the competition actually was.

    The start list is what settles the boundary ride. A class runs over, or
    starts early, and the clock puts the one ride either side of the change on
    the wrong side of it: on 11 September the freestyle's first rider was filed
    under the Intermediate I that was still running, and on the 12th the Grand
    Prix's first was filed under the young horses. One ride each day, both in
    the published list of exactly one class.

    **Both settle a boundary, and neither may cross the day.** A start list
    knows who rode and nothing about when; a caption knows what a graphic said
    and nothing about when either. A rider down for a morning class and an
    afternoon one gets narrowed onto whichever the clock then finds — and the
    clock, asked only about that class, answers it — while a recap card cut in
    between rounds names a class that finished hours ago perfectly clearly. So
    a class the ring has finished with is dropped before either is asked. It is
    finished with once the next class in the ring has started and the slack has
    passed; the last class of the day is never finished with, because nothing
    follows it to say so.

    **The forward bound is the start list's alone.** A caption is read off the
    arena, so it can see a class go in early and report it — that is the
    timetable slipping, which is the thing the caption exists to correct. A
    start list sees nothing. Narrowed to a single class that is not due for
    hours, it leaves the clock with one answer and the clock's own fallback for
    "before anything was due" gives it: two rounds of a morning young horses
    class were filed under an afternoon freestyle five hours early exactly that
    way. So the caption is offered every class the ring has not finished with,
    and the start list only those the day has reached.

    A ride with no absolute time cannot be placed by the clock at all. That is
    an uploaded file rather than a live event, and the answer there is the
    caption alone; with neither, the day stays one event, which is what it was
    before any of this.
    """
    if not classes:
        return []

    runs = [ClassRun(show_class=c) for c in classes]
    by_id = {run.show_class.class_id: run for run in runs}

    for ride in rides or []:
        at = _ride_time(ride, recorded_from)
        # What the ring could still have been running. Neither of the signals
        # below knows the time of day: a start list names who rode, and a
        # caption names what the graphic said. Both were reading a class the
        # day had finished with — three rounds of the 12th's afternoon
        # freestyle were filed under a young horses class that ended at
        # breakfast because their riders were down for it too, and a fourth
        # under a Grand Prix four hours over, off a recap card the broadcast
        # cut to between rounds. The morning capture of the same day did it
        # forwards: two of its rounds went to a freestyle not due for five
        # hours. None of that is a boundary correction; all of it is a jump
        # across the day.
        live = _not_past(classes, at) or list(classes)
        # The two bounds are not the same shape, because the two signals are
        # not. A caption is read off the arena, so it can see a class go in
        # early and say so — that is the timetable slipping, and it is what
        # the caption is for. A start list is a list of names: it cannot see
        # anything, least of all a class that is not due for another five
        # hours. So the caption is offered every class the ring has not
        # finished with, and the start list only those the day has reached.
        reached = _reached(live, at) or list(live)
        pool = _entered_in(ride, reached, entrants or {})
        by_clock = _class_at(at, pool) if at is not None else None
        named, score = _class_named_in(ride, live)
        chosen = None
        if named is not None and by_clock is not None and named.class_id != by_clock.class_id:
            # Both answered and they disagree. The caption only wins by a
            # margin over how well it matches the clock's own class: a card
            # that names neither class cleanly is not evidence of a boundary.
            against_clock = _class_named_in(ride, [by_clock])[1]
            if score - against_clock >= CAPTION_MARGIN:
                chosen = by_id[named.class_id]
                chosen.decided_by = "caption"
            else:
                chosen = by_id[by_clock.class_id]
        elif named is not None:
            chosen = by_id[named.class_id]
            if by_clock is None:
                chosen.decided_by = "caption"
        elif by_clock is not None:
            chosen = by_id[by_clock.class_id]
        if chosen is None:
            # Neither the clock nor a caption could place it. The first class
            # it could have been entered in is the honest default: it is where
            # the recording started, and a ride filed nowhere is a ride nobody
            # can find.
            chosen = by_id[pool[0].class_id] if pool else runs[0]
        # Say so when the start list is what moved it. The clock's own answer
        # over every class is what the record would have said before.
        if len(pool) < len(reached) and at is not None:
            unnarrowed = _class_at(at, reached)
            if unnarrowed is not None and unnarrowed.class_id != chosen.show_class.class_id:
                chosen.decided_by = "start list"
        chosen.rides.append(ride)

    return [run for run in runs if run.rides]


def _ride_time(ride: dict, recorded_from: datetime.datetime | None) -> datetime.datetime | None:
    """When a ride happened, in wall clock, if that can be known."""
    for key in ("start_at", "startAt"):
        parsed = parse_time(ride.get(key))
        if parsed:
            return parsed
    if recorded_from is None:
        return None
    seconds = ride.get("start_sec", ride.get("startSec"))
    if seconds is None:
        return None
    return recorded_from + datetime.timedelta(seconds=float(seconds))


def _class_at(at: datetime.datetime, classes: list[ShowClass]) -> ShowClass | None:
    """The class that had started by this time.

    The last one to have started, with no allowance either way. An allowance
    forward is actively wrong — it lets a class that has not begun claim the
    rides of the one still running — and lateness needs no allowance at all,
    because a class that starts late still starts before its own rides. What is
    left uncovered is a class that runs *over*, whose last rides fall after the
    next class's published start; that is what the caption is for.
    """
    timed = [c for c in classes if c.start_at]
    if not timed:
        return None
    started = [c for c in timed if c.start_at <= at]
    # Before anything was due: the recorder starts ahead of the first class, and
    # those minutes belong to it rather than to the day before.
    return started[-1] if started else timed[0]


def _class_named_in(ride: dict, classes: list[ShowClass]) -> tuple[ShowClass | None, float]:
    """The class a ride's own scoreboard names, and how well it named it.

    **Only what was read off the arena.** This used to include the ride's
    `class_name`, which is not an observation at all: `list_game_rides` stamps
    every ride of a split recording with the title of the class it currently
    sits under, so a second split was handed its own previous answer and
    matched it at 1.00 — beating the clock, the arena and the start list, none
    of which can reach that score. A split therefore confirmed itself, and
    every attempt to correct one reproduced it exactly. `competition` goes with
    it: nothing writes one onto a ride, and an empty key that would behave the
    same way if something ever did is not worth keeping.
    """
    text = "\n".join(str(ride.get(key) or "") for key in
                     ("scoreboard", "scoreboard_text"))
    if not text.strip():
        return None, 0.0
    scored = [(same_name(text, c.name), c) for c in classes if c.name]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best, show_class = scored[0] if scored else (0.0, None)
    return (show_class, best) if best >= CAPTION_MATCH else (None, best)


# --- What a recording turns out to hold ---------------------------------------

def recording_started(job: dict) -> datetime.datetime | None:
    """When the camera started, in wall clock, if the job knows.

    A live event does: the recorder wrote when it began, and every chunk after
    it carries a programme-date-time. An upload does not — a file has offsets
    and no clock — and there the classes are told apart by what was read in the
    arena or not at all.
    """
    live = job.get("live") or {}
    capture = live.get("capture") or {}
    for value in (capture.get("captureStart"), capture.get("startedAt"), live.get("eventStart")):
        parsed = parse_time(value)
        if parsed:
            return parsed
    return None


def day_of(job: dict, started: datetime.datetime | None) -> str:
    """The day a recording is of, as Equipe dates its shows."""
    if started:
        return started.date().isoformat()
    live = (job.get("live") or {}).get("eventStart")
    parsed = parse_time(live) or parse_time(job.get("createdAt"))
    return parsed.date().isoformat() if parsed else ""


def find_classes(*, job: dict, context_urls: list[str], competition: str = "",
                 discipline: str = "", arena: str = "", show_id: int = 0,
                 get=None) -> tuple[Show | None, list[ShowClass]]:
    """The show this recording is of, and the classes it ran that day.

    A show already settled for this recording is the strongest signal there is
    and costs nothing — it is the answer to this question, worked out once and
    written down. Then an editor's own link, which costs one request. Failing
    both, the day and the name read off the screen choose between the shows
    that ran. Any of them can come back with nothing, and nothing is a
    perfectly good answer: the recording then stays one event, as it was
    before any of this.

    Taking the stored id first is what makes a split repeatable. Splitting
    writes each class's own name over `competition` — which is right, that is
    what the event now is — and a second split then searched a thousand shows
    for "D&H INTER I SILVER CHAMPIONSHIP" and found none, because that is a
    class and the list is of shows. So the first split worked, every one after
    it quietly changed nothing, and the message said the show could not be
    found on a timetable the record was carrying the id of.

    `arena` narrows the classes to the one ring the camera is on, and at a
    championship that is not optional — see `classes_on`. Empty means every
    ring, which is right for a one-ring show; the caller is expected to have
    asked `pick_arena` first when nobody named one.
    """
    started = recording_started(job)
    day = day_of(job, started)
    show_id = int(show_id or 0) or show_id_in(context_urls or [])

    if not show_id:
        if not (day and competition):
            # Without a day there is nothing to filter a thousand shows by, and
            # without a name there is nothing to choose between the ones left.
            return None, []
        shows = fetch_shows(get=get)
        picked = pick_show(shows, on=day, name_hint=competition, discipline=discipline)
        if picked is None:
            return None, []
        show_id = picked.show_id

    show = fetch_schedule(show_id, get=get)
    if show is None:
        return None, []
    # Now that the show's own clock is known, ask again which day this is. The
    # day above was UTC, which is all that was available to choose a show by
    # and is good enough for that — a show spans days. A class does not.
    if started:
        day = local_day(started, show) or day
    return show, classes_on(show, day, arena=arena)


def pick_arena(show: Show, day: str, rides: list[dict]) -> str:
    """Which ring a recording is of, read off what its scoreboards said.

    The fallback for a recording nobody named a ring for. Every arena's classes
    are scored against every ride's caption and the best total wins, so one
    card naming one class decides nothing on its own but a day of them does.

    Deliberately all-or-nothing: with no caption anywhere naming any class
    there is no evidence, and the honest answer is no arena — which leaves the
    day unsplit rather than split by a coin toss. A day filed as one event is
    what it was before any of this existed and is recoverable in one tool call;
    a day filed under the wrong ring looks finished and is not.
    """
    totals: dict[str, float] = {}
    for arena in arenas_on(show, day):
        here = classes_on(show, day, arena=arena)
        best = 0.0
        for ride in rides or []:
            named, score = _class_named_in(ride, here)
            if named is not None and score >= CAPTION_MATCH:
                best += score
        totals[arena] = best
    ranked = sorted(totals.items(), key=lambda pair: pair[1], reverse=True)
    if not ranked or ranked[0][1] <= 0:
        return ""
    # One ring has to beat the next by the same margin a caption needs to beat
    # the clock, or two rings running near-identical classes — a Gold and a
    # Silver of one championship, which is exactly what this show runs — would
    # be decided by noise.
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < CAPTION_MARGIN:
        return ""
    return ranked[0][0]


# --- Results: what was actually scored ----------------------------------------
#
# `/api/v1/class_sections/{id}` is the published record of a class once it has
# been ridden: the panel that judged it by name and position, every combination
# in start order with the time it was due in the arena, its placing, its total,
# and each judge's own percentage split into technical and artistic.
#
# All of it is better than what the desk can see. A lower third abbreviates a
# rider and vanishes for whole rounds; a broadcast graphic shows a total for
# five seconds and never shows who sat at M. This is the source of record, and
# it arrives as numbers rather than as prose a model read off a search result.

SECTION_PATH = "/api/v1/class_sections/{section_id}"


@dataclass
class Official:
    """A judge: where they sat, and who they are."""

    position: str
    name: str
    country: str = ""

    def as_dict(self) -> dict:
        return {"position": self.position, "name": self.name, "country": self.country}


@dataclass
class Start:
    """One combination in a class, as published."""

    rider: str
    horse: str
    start_number: str = ""
    start_time: str = ""          # "HH:MM", which is what align_schedule reads
    rank: int | None = None
    total_pct: float | None = None
    technical_pct: float | None = None
    artistic_pct: float | None = None
    nation: str = ""
    judge_marks: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict:
        """The shape `align_schedule` and `apply_grounding` already read."""
        return {
            "rider": self.rider,
            "horse": self.horse,
            "startNumber": self.start_number,
            "startTime": self.start_time,
            "nation": self.nation,
            "finalPlace": self.rank,
            "totalPct": self.total_pct,
            "technicalPct": self.technical_pct,
            "artisticPct": self.artistic_pct,
            "judgeMarks": self.judge_marks,
        }


@dataclass
class ClassResults:
    """One class's published results, or as much of them as exists yet."""

    section_id: int
    state: str = ""
    officials: list[Official] = field(default_factory=list)
    starts: list[Start] = field(default_factory=list)

    @property
    def final(self) -> bool:
        """Whether these are results rather than a start list still running."""
        return self.state == "results"


def parse_section(payload: Any) -> ClassResults | None:
    """A class section: its panel, its start list, and its results."""
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    out = ClassResults(section_id=int(payload["id"]), state=str(payload.get("state") or ""))
    for row in payload.get("officials") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("official_name") or "").strip()
        if not name:
            continue
        out.officials.append(Official(
            position=str(row.get("judge_by") or row.get("judge_by_alias") or "").strip(),
            name=name,
            country=str(row.get("official_country") or "").strip(),
        ))
    for row in payload.get("starts") or []:
        if isinstance(row, dict):
            out.starts.append(_start_of(row))
    # Start order, which is the order the arena saw them in and the order
    # `align_schedule` places against the video.
    out.starts.sort(key=lambda s: (s.start_time or "99:99", s.start_number))
    return out


def _start_of(row: dict) -> Start:
    rider = str(row.get("rider_name") or " ".join(
        x for x in (row.get("rider_first_name"), row.get("rider_last_name")) if x)).strip()
    start = Start(
        rider=rider,
        horse=str(row.get("horse_name") or "").strip(),
        start_number=str(row.get("st_nr") or row.get("start_no") or "").strip(),
        start_time=_hhmm(row.get("start_at")),
        rank=int(row["rank"]) if isinstance(row.get("rank"), (int, float)) and row.get("rank") else None,
        nation=str(row.get("rider_country") or row.get("nation") or "").strip(),
    )
    for result in row.get("results") or []:
        if not isinstance(result, dict):
            continue
        percent = _number(result.get("percent"))
        where = str(result.get("judge_by") or "").strip()
        if where:
            # One row per judge, each with their own percentage.
            if percent is not None:
                start.judge_marks[where] = percent
        else:
            # The row with no judge is the combination's own result.
            start.total_pct = percent if percent is not None else start.total_pct
            start.technical_pct = _number(result.get("technical_percent"))
            start.artistic_pct = _number(result.get("artistic_percent"))
    if start.total_pct is None and start.judge_marks:
        # A class still being judged has the marks but not the total yet.
        start.total_pct = round(sum(start.judge_marks.values()) / len(start.judge_marks), 3)
    return start


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hhmm(value: Any) -> str:
    at = parse_time(value)
    if at is None:
        return ""
    # Local time, because a start list is read in the arena's clock and
    # `align_schedule` compares it against a video recorded in the same one.
    text = str(value)
    match = re.search(r"[T ](\d{2}:\d{2})", text)
    return match.group(1) if match else at.strftime("%H:%M")


def fetch_results(section_id: int, get=None, timeout: int = 20) -> ClassResults | None:
    """One class section's published panel, start list and results."""
    payload = _get(f"{BASE}{SECTION_PATH.format(section_id=section_id)}", get=get, timeout=timeout)
    return parse_section(payload) if payload is not None else None


def results_for(show_class: ShowClass, get=None) -> ClassResults | None:
    """The results of a class, across however many sections it was run in.

    A class is usually one section. Where it is several — a split class, a
    consolation — they are one competition to everyone watching, so the starts
    are gathered and the panel is taken from the first section that names one.
    """
    gathered: ClassResults | None = None
    for section_id in show_class.section_ids:
        part = fetch_results(section_id, get=get)
        if part is None:
            continue
        if gathered is None:
            gathered = part
            continue
        gathered.starts.extend(part.starts)
        if not gathered.officials:
            gathered.officials = part.officials
        if part.state == "results":
            gathered.state = part.state
    return gathered
