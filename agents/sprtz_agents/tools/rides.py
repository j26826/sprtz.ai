"""Turning a competition day into rides.

An equestrian recording is not one contest. The five real samples are six to
eight and a half hours of a fixed camera on a ring: many combinations in
sequence, promotional films between classes, and long stretches of empty arena.
The unit somebody actually wants — "show me Becky Moody's test", "clip anything
that scored over 75" — sits between the recording and the moments, and nothing
in the pipeline had a name for it.

The rules here are ported from a dressage broadcast pipeline that solved the
same problem with OCR over a decoded frame cache. What is kept is the reasoning;
none of the machinery came across, because it does not need to:

- **Identity is a vote, not a reading.** One lower third misread once should not
  split a ride in two, so fragments are grouped by similarity and the group's
  most common spelling wins.
- **The canon is derived, never declared.** The original carried a dict of the
  eighteen riders in one broadcast, which is a list that is wrong for every
  other recording. Here the canonical spelling is whatever the readings
  themselves agree on most often, so a start list is never needed.
- **Scores follow the graphic, not the clock.** A result revealed after the next
  combination has entered still belongs to the one it was displayed with. That
  is true by construction here, because a segment reports the graphic on the
  ride it was shown with rather than as a free-standing event.
- **Totals are checked, not trusted.** A displayed total that is not the mean of
  its displayed judge marks means something was misread, and the record says so
  rather than reporting a plausible number.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

# How close two readings of the same combination have to be to count as one.
# Tuned for the failure this guards: a dropped or doubled letter in a name, not
# two different riders who happen to share a surname.
_SAME_COMBINATION = 0.86

# Two fragments of one ride, split by a segment boundary, are adjacent in time.
# Generous because the boundary itself is a cut point and a close-up can hide
# the lower third for a while either side of it.
_MERGE_GAP_SEC = 90.0

# Displayed percentages are rounded, so the mean of five of them will not equal
# the displayed total exactly. Wide enough to absorb that, tight enough that a
# single misread digit — which moves a mark by whole percentage points — fails.
_SCORE_TOLERANCE = 0.15


# Letters NFKD will not take apart, because they are letters in their own right
# rather than a base plus a mark. They matter more here than the decomposable
# ones: this sport's results live on Scandinavian platforms, so ø, æ and å are
# ordinary. Without these, "Børk" normalises to "b rk" and stops matching
# "Bork" — a rider split in two by a stroke through a vowel.
_LETTERS = str.maketrans({
    "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
    "ß": "ss", "đ": "d", "Đ": "d", "ð": "d", "Ð": "d",
    "ł": "l", "Ł": "l", "þ": "th", "Þ": "th",
    # Turkish dotless i, by codepoint: ruff flags the literal as visually
    # ambiguous with a plain i, which is exactly why it needs mapping.
    "\u0131": "i",
})


def normalise_name(text: str) -> str:
    """A name reduced to what two readings of it must share.

    Accents go because a lower third, a caption and a results page disagree
    about them more often than they disagree about the person.
    """
    folded = unicodedata.normalize("NFKD", (text or "").translate(_LETTERS))
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", stripped.casefold()).strip()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# What the model writes when it can see there is a competitor and cannot read
# who. The prompt asks for exactly that rather than a guess — an invented name
# is worse than a blank, because an editor publishes it — and "unknown" is the
# answer it gives. Compared after normalise_name, so case and punctuation do
# not matter.
_PLACEHOLDERS = frozenset({
    "unknown", "unk", "n a", "na", "none", "nil", "not visible", "not shown",
    "not legible", "illegible", "unreadable", "unidentified", "unnamed",
    "no name", "tbc", "tba", "rider", "horse",
})


def is_placeholder(name: str) -> bool:
    """Whether a reading says nobody was identified rather than naming anyone.

    "Unknown rider" and "unknown horse" count too: the word is the model's way
    of filling a field it was told not to guess, not a competitor's name.
    """
    key = normalise_name(name)
    return not key or key in _PLACEHOLDERS or key.startswith("unknown ")


def canonical_identity(readings: list[tuple[str, str]]) -> tuple[str, str]:
    """The spelling a group of readings agrees on.

    Modal rather than first-seen or longest: OCR-ish noise is various, and the
    correct reading is the one that recurs. Rider and horse are voted
    separately, because a segment often catches one line of the graphic and not
    the other.

    **A placeholder is not a reading.** The model writes "unknown" where it can
    see a round and cannot read the graphic, and that used to be voted in as a
    name — the LeMieux day's last ride was a rider called "unknown" on a horse
    called "unknown", with three moments credited to them. It is left out of the
    vote, so a round nobody could name is named nobody and shows as a dash.
    Placeholders still group with each other as they did — this decides what a
    ride is called, not which fragments are the same ride.
    """
    riders = Counter(r.strip() for r, _ in readings if not is_placeholder(r))
    horses = Counter(h.strip() for _, h in readings if not is_placeholder(h))
    rider = riders.most_common(1)[0][0] if riders else ""
    horse = horses.most_common(1)[0][0] if horses else ""
    return rider, horse


def untrusted(score_check: str) -> bool:
    """Whether a total's own check says it must not be acted on.

    Two checks can fail, and they read differently: `check_total` writes
    "mismatch: …" when a total does not equal the mean of its own displayed
    judge marks, and `apply_grounding` writes "<source> disagrees: …" when the
    published result contradicts what was on screen. Both mean the number is
    not safe to publish from, and the second was being missed everywhere the
    rule was written out by hand — `high_scoring` and `list_rides` both tested
    `startswith("mismatch")`, so a ride the results page contradicts came back
    as one of the day's best while the editor's own card excluded it.

    One function, because this rule is applied in four places and the web
    carries its own copy of it (`web/src/ridegroups.js`).
    """
    text = str(score_check or "")
    return text.startswith("mismatch") or "disagrees" in text


def check_total(judge_marks: list[float], total_pct: float | None) -> str:
    """Whether a displayed total is consistent with its displayed judge marks.

    This is the invariant that makes model-read numbers safe to keep. A total is
    the mean of the judges' marks, so any single misread digit anywhere in the
    row breaks the equality — which turns "a plausible number" into "a number
    that has been checked", and gives the caller something to escalate on.

    Returns an empty string when there is nothing to check.
    """
    marks = [m for m in (judge_marks or []) if m is not None]
    if total_pct is None or len(marks) < 2:
        return ""
    mean = sum(marks) / len(marks)
    if abs(mean - total_pct) <= _SCORE_TOLERANCE:
        return "ok"
    return f"mismatch: {len(marks)} marks average {mean:.3f}, total shown as {total_pct:.3f}"


def fuse(fragments: list[dict]) -> list[dict]:
    """Stitch per-segment ride fragments into whole rides.

    ``fragments`` are dicts with absolute ``start_sec``/``end_sec`` and whatever
    the segment read. Order in, order out: rides come back sorted by when they
    started, which is the running order.
    """
    ordered = sorted(fragments, key=lambda f: float(f.get("start_sec", 0.0)))
    groups: list[list[dict]] = []

    for fragment in ordered:
        key = normalise_name(
            f"{fragment.get('rider', '')} {fragment.get('horse', '')}")
        placed = False
        for group in groups:
            last = group[-1]
            gap = float(fragment.get("start_sec", 0.0)) - float(last.get("end_sec", 0.0))
            same = _similar(key, normalise_name(
                f"{last.get('rider', '')} {last.get('horse', '')}"))
            # Both tests, not either. A name matching across a two-hour gap is
            # the same rider in a later class and a genuinely separate ride;
            # adjacency alone would weld one competitor onto the next.
            if same >= _SAME_COMBINATION and gap <= _MERGE_GAP_SEC:
                group.append(fragment)
                placed = True
                break
        if not placed:
            groups.append([fragment])

    rides: list[dict] = []
    for order, group in enumerate(groups, start=1):
        rider, horse = canonical_identity(
            [(f.get("rider", ""), f.get("horse", "")) for f in group])

        # The richest scoring reading in the group wins rather than the last:
        # the graphic is shown once, so most fragments of a ride have nothing to
        # say about it and an empty later reading must not erase an earlier one.
        scored = max(
            group,
            key=lambda f: (len(f.get("judge_marks") or []),
                           f.get("total_pct") is not None),
        )
        marks = [float(m) for m in (scored.get("judge_marks") or [])]
        total = scored.get("total_pct")
        total = float(total) if total is not None else None

        rides.append({
            "order": order,
            "rider": rider,
            "horse": horse,
            "start_sec": round(min(float(f["start_sec"]) for f in group), 2),
            "end_sec": round(max(float(f["end_sec"]) for f in group), 2),
            "test_type": next(
                (f.get("test_type", "") for f in group if f.get("test_type")), ""),
            "judge_marks": marks,
            "total_pct": total,
            "rank": next((f.get("rank") for f in group if f.get("rank") is not None), None),
            "scoreboard": next(
                (f.get("scoreboard_text", "") for f in group if f.get("scoreboard_text")), ""),
            "score_source": "observed" if (marks or total is not None) else "",
            "score_check": check_total(marks, total),
            "segments": sorted({int(f.get("segment", -1)) for f in group} - {-1}),
        })
    return rides


# The thresholds the clip requirements name. Freestyle is held higher because
# the marks run higher: an artistic score lifts the total, so 75 in a freestyle
# is not the ride that 75 in a straight test is. These are dressage's bars and
# they are compiled in here rather than living on the sport profile, which is
# where every other sport-specific fact belongs: a jumping round is scored in
# faults and has no percentage to clear at all.
_THRESHOLDS = {"freestyle": 80.0, "straight": 75.0}
_DEFAULT_BAR = "straight"


def high_scoring(rides: list[dict], thresholds: dict[str, float] | None = None) -> list[dict]:
    """The rides that clear the bar for their kind of test.

    A ride whose total failed its own consistency check is not returned. The
    whole point of the check is that an unverified number should not be the
    reason something gets published.

    **An unrecognised test type is measured against the standard bar, not
    dropped.** The lookup used to return None for anything but the two exact
    lowercase strings and the ride fell out of the answer entirely — so a
    "Freestyle" on 84%, or a German "Kür", was silently missing from the day's
    best rides with nothing to say it had been excluded. The model is asked for
    one of two words and usually gives one; what it does when it does not
    should be to lose the higher bar, not the ride.
    """
    bars = {**_THRESHOLDS, **(thresholds or {})}
    out = []
    for ride in rides:
        total = ride.get("total_pct")
        if total is None or untrusted(ride.get("score_check", "")):
            continue
        kind = str(ride.get("test_type") or "").strip().lower()
        bar = bars.get(kind, bars.get(_DEFAULT_BAR))
        if bar is not None and float(total) >= bar:
            out.append(ride)
    return out


def match_watchlist(rides: list[dict], watchlist: list[str]) -> list[dict]:
    """Rides matching a curated list of riders, horses or combinations.

    The list is supplied per job rather than compiled in — it is somebody's
    working note about who is worth watching on the day, and it changes with
    every event. An entry matches on either half of the combination, so "Becky
    Moody" and "James Bond II" both find the same ride.
    """
    wanted = [normalise_name(w) for w in (watchlist or []) if normalise_name(w)]
    if not wanted:
        return []
    hits = []
    for ride in rides:
        rider = normalise_name(ride.get("rider", ""))
        horse = normalise_name(ride.get("horse", ""))
        if any(
            w and (_similar(w, rider) >= _SAME_COMBINATION
                   or _similar(w, horse) >= _SAME_COMBINATION
                   or w in rider or w in horse)
            for w in wanted
        ):
            hits.append(ride)
    return hits


# --- Grounding ----------------------------------------------------------------

# A published total and a displayed one are both rounded to three places, so
# they should agree to the last digit. Anything wider than a rounding wobble is
# one of them being wrong, and the record has to say which it believes.
_GROUND_TOLERANCE = 0.05


def _snake(name: str) -> str:
    """"technicalPct" -> "technical_pct", for callers that send either."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def apply_grounding(rides: list[dict], published: list[dict], *, source: str) -> list[dict]:
    """Attach published results to the rides they belong to.

    Matched by name on both halves of the combination, because a rider with two
    horses in the same class is ordinary and a horse with two riders is not
    unheard of. Nothing observed is ever overwritten: a published value lands in
    its own field beside the one read off the screen, so anyone can see what
    the camera showed and what the search suggested. Where both exist and
    disagree, the disagreement is recorded rather than resolved — that is a
    finding, and it is the reason to keep both.

    A ride that had no displayed total takes the published one as its total,
    labelled by source. Filling an empty field is not overwriting one, and a
    round that was ridden but whose graphic was never on screen still scored
    something.
    """
    out = []
    for ride in rides:
        ride = dict(ride)
        rider = normalise_name(ride.get("rider", ""))
        horse = normalise_name(ride.get("horse", ""))
        hit = None
        for row in published:
            pr = normalise_name(row.get("rider", ""))
            ph = normalise_name(row.get("horse", ""))
            if not (pr or ph):
                continue
            horse_match = bool(horse and ph) and _similar(horse, ph) >= _SAME_COMBINATION
            rider_match = bool(rider and pr) and _similar(rider, pr) >= _SAME_COMBINATION
            # The horse decides. A horse goes once per class under one rider,
            # so its name is as good as a start number — and lower thirds
            # abbreviate riders ("A-M Bork Eppers") in ways a published results
            # page never does, so a rider comparison fails on real data the
            # horse comparison passes. The rider alone still matches when the
            # horse was never read, provided the published horse does not
            # contradict one that was.
            if horse_match or (rider_match and not (horse and ph)):
                hit = row
                break
        if hit is None:
            out.append(ride)
            continue

        ride["grounded_rider"] = (hit.get("rider") or "").strip()
        ride["grounded_horse"] = (hit.get("horse") or "").strip()
        # The head number is printed on the horse and on the start list, and
        # nowhere else on screen — it is the one identifier both sides share.
        number = str(hit.get("startNumber") or hit.get("start_number") or "").strip()
        if number and not ride.get("start_number"):
            ride["start_number"] = number
        nation = (hit.get("nation") or "").strip()
        if nation and not ride.get("nation"):
            ride["nation"] = nation
        place = hit.get("finalPlace", hit.get("final_place"))
        ride["final_place"] = int(place) if isinstance(place, (int, float)) and place else None
        total = hit.get("totalPct", hit.get("total_pct"))
        grounded_total = float(total) if isinstance(total, (int, float)) else None
        ride["grounded_total_pct"] = grounded_total
        ride["grounded_source"] = source
        # The rest of what a published result says, where the source carries
        # it: each judge's own percentage by where they sat, and the technical
        # and artistic halves of a freestyle. Never merged into the observed
        # marks — a broadcast shows five numbers for five seconds and a results
        # page is the record, and telling them apart is the whole arrangement.
        marks = hit.get("judgeMarks", hit.get("judge_marks"))
        if isinstance(marks, dict) and marks:
            ride["grounded_judge_marks"] = {str(k): float(v) for k, v in marks.items()}
        for key, published_key in (("grounded_technical_pct", "technicalPct"),
                                   ("grounded_artistic_pct", "artisticPct")):
            value = hit.get(published_key, hit.get(_snake(published_key)))
            if isinstance(value, (int, float)):
                ride[key] = float(value)

        if grounded_total is not None:
            if ride.get("total_pct") is None:
                ride["total_pct"] = grounded_total
                ride["score_source"] = source
            elif abs(float(ride["total_pct"]) - grounded_total) > _GROUND_TOLERANCE:
                ride["score_check"] = (
                    f"{source} disagrees: shown {float(ride['total_pct']):.3f}, "
                    f"published {grounded_total:.3f}"
                )
            elif not ride.get("score_check") or ride.get("score_check") == "ok":
                ride["score_check"] = f"ok, confirmed by {source}"
        out.append(ride)
    return out


# --- The schedule -------------------------------------------------------------

# How far a round may run from its scheduled time and still be the round the
# schedule says. Dressage days slip, but they slip in minutes, not in classes:
# ten minutes either side is a late start, twenty is somebody else's slot.
_SCHEDULE_WINDOW_SEC = 600.0


def _time_of_day(value: str) -> float | None:
    """'HH:MM' or 'HH:MM:SS' as seconds past midnight, else None."""
    parts = (value or "").strip().split(":")
    if len(parts) not in (2, 3) or not all(x.isdigit() for x in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    sec = int(parts[2]) if len(parts) == 3 else 0
    if h > 23 or m > 59 or sec > 59:
        return None
    return h * 3600 + m * 60 + sec


def _row_matches(ride: dict, row: dict) -> bool:
    """Same combination, by the rule apply_grounding uses: the horse decides."""
    rider, horse = normalise_name(ride.get("rider", "")), normalise_name(ride.get("horse", ""))
    pr, ph = normalise_name(row.get("rider", "")), normalise_name(row.get("horse", ""))
    horse_match = bool(horse and ph) and _similar(horse, ph) >= _SAME_COMBINATION
    rider_match = bool(rider and pr) and _similar(rider, pr) >= _SAME_COMBINATION
    return horse_match or (rider_match and not (horse and ph))


def align_schedule(rides: list[dict], start_list: list[dict]) -> tuple[list[dict], dict]:
    """Place the published start list against the video, and name what it can.

    A lower third names the rider for some rounds and not others — the camera
    was on a close-up when the graphic ran, or the producer never keyed it. The
    start list knows who rode at 14:32; what it does not know is where 14:32 is
    in an eight-hour file. So the rounds that *were* named are used as anchors:
    each one gives the difference between its scheduled clock time and where
    it actually starts in the video, and the median of those is the offset.
    Every unnamed round is then looked up by its own predicted clock time.

    The median rather than the first anchor, because one misattributed round
    would otherwise shift the whole day. And a round is only named this way
    when exactly one scheduled start falls inside the window around it: two
    candidates is not an identification, it is a guess with a coin.

    Anything named here says so. `identity_source` is "observed" for a name
    read off the screen and "schedule" for one inferred from the timetable,
    and the two are never confused, because a caption that names the wrong
    rider is the failure this whole record is built to avoid.
    """
    rows = []
    for row in start_list or []:
        at = _time_of_day(str(row.get("startTime") or row.get("start_time") or ""))
        if at is None:
            continue
        rows.append({**row, "_at": at})

    out = [dict(r) for r in rides]
    for ride in out:
        ride.setdefault("identity_source", "observed" if (ride.get("rider") or ride.get("horse")) else "")

    if not rows:
        return out, {"anchors": 0, "offset_sec": None, "named": 0}

    # Anchors: named rounds the start list also has.
    deltas = []
    for ride in out:
        if not (ride.get("rider") or ride.get("horse")):
            continue
        for row in rows:
            if _row_matches(ride, row):
                deltas.append(row["_at"] - float(ride.get("start_sec", 0.0)))
                if not ride.get("start_number"):
                    number = str(row.get("startNumber") or row.get("start_number") or "").strip()
                    if number:
                        ride["start_number"] = number
                break
    if not deltas:
        return out, {"anchors": 0, "offset_sec": None, "named": 0}

    deltas.sort()
    offset = deltas[len(deltas) // 2]
    named = 0
    for ride in out:
        if ride.get("rider") or ride.get("horse"):
            continue
        predicted = float(ride.get("start_sec", 0.0)) + offset
        # Nearest against runner-up, not a count of what falls in the window.
        # Starts are six to eight minutes apart, so a window wide enough to
        # absorb a late start always holds the neighbours too, and counting
        # them would name nothing all day. A round predicted six seconds from
        # one slot and eight minutes from the next is that slot; one predicted
        # midway between two is a guess with a coin, and stays blank.
        ranked = sorted(rows, key=lambda row: abs(row["_at"] - predicted))
        nearest = abs(ranked[0]["_at"] - predicted)
        if nearest > _SCHEDULE_WINDOW_SEC:
            continue
        if len(ranked) > 1 and abs(ranked[1]["_at"] - predicted) < 2 * nearest:
            continue
        row = ranked[0]
        ride["rider"] = (row.get("rider") or "").strip()
        ride["horse"] = (row.get("horse") or "").strip()
        number = str(row.get("startNumber") or row.get("start_number") or "").strip()
        if number:
            ride["start_number"] = number
        if row.get("nation"):
            ride["nation"] = str(row["nation"]).strip()
        ride["identity_source"] = "schedule"
        named += 1

    return out, {"anchors": len(deltas), "offset_sec": round(offset, 1), "named": named}


def attach_moments(moments: list[dict], rides: list[dict]) -> list[dict]:
    """Give every moment the ride it happened in.

    By time, in code: a moment at 1:28:34 belongs to whoever was in the arena
    at 1:28:34, which the ride windows already say. Asking the model to name
    the rider on each moment would ask it forty times for a fact it was asked
    once, and give it forty chances to answer differently.
    """
    out = []
    for m in moments:
        m = dict(m)
        at = float(m.get("peak_sec") or m.get("start_sec") or 0.0)
        ride = next(
            (r for r in rides
             if float(r.get("start_sec", 0.0)) <= at <= float(r.get("end_sec", 0.0))),
            None,
        )
        if ride is not None:
            m["rider"] = ride.get("rider", "") or ""
            m["horse"] = ride.get("horse", "") or ""
            m["start_number"] = ride.get("start_number", "") or ""
            m["ride_order"] = ride.get("order")
            m["identity_source"] = ride.get("identity_source", "") or ""
        out.append(m)
    return out
