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
        show.classes.append(ShowClass(
            class_id=int(row["id"]),
            name=str(row.get("name") or ""),
            start_at=parse_time(row.get("start_at")),
            date=str(row.get("date") or ""),
            discipline=discipline,
            position=int(row.get("position") or 0),
        ))
    show.classes.sort(key=lambda c: (c.start_at or _FAR_FUTURE, c.position))
    return show


_FAR_FUTURE = datetime.datetime(9999, 1, 1, tzinfo=datetime.UTC)


def parse_time(value: Any) -> datetime.datetime | None:
    """Equipe's times, which carry a zone: "2026-09-11 15:20:00 +0100"."""
    if not value:
        return None
    text = str(value).strip()
    for shape in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M %z"):
        try:
            return datetime.datetime.strptime(text, shape).astimezone(datetime.UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(datetime.UTC) if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


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


def classes_on(show: Show, day: str) -> list[ShowClass]:
    """The show's classes for one day, in the order they were due to run."""
    if not day:
        return list(show.classes)
    return [c for c in show.classes if (c.date or "") == day or _same_day(c.start_at, day)]


def _same_day(at: datetime.datetime | None, day: str) -> bool:
    return bool(at) and at.date().isoformat() == day


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


def assign_classes(rides: list[dict], classes: list[ShowClass], *,
                   recorded_from: datetime.datetime | None) -> list[ClassRun]:
    """Split a day's rides into the classes they belong to.

    Two signals, and they answer different failures. **The clock places every
    ride**: a class has a published start and a ride has an absolute time, so
    each ride belongs to the last class that had started. **The caption
    corrects it**: timetables slip, and a class that ran forty minutes late
    would otherwise take its first rides from the class before it. Where a run
    of rides carries a scoreboard naming a class, that naming wins — it was
    read off the arena, which is where the competition actually was.

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
        by_clock = _class_at(at, classes) if at is not None else None
        named, score = _class_named_in(ride, classes)
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
            # of the day is the honest default: it is where the recording
            # started, and a ride filed nowhere is a ride nobody can find.
            chosen = runs[0]
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
    """The class a ride's own scoreboard names, and how well it named it."""
    text = "\n".join(str(ride.get(key) or "") for key in
                     ("scoreboard", "scoreboard_text", "competition", "class_name"))
    if not text.strip():
        return None, 0.0
    scored = [(same_name(text, c.name), c) for c in classes if c.name]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best, show_class = scored[0] if scored else (0.0, None)
    return (show_class, best) if best >= CAPTION_MATCH else (None, best)
