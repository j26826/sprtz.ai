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


def canonical_identity(readings: list[tuple[str, str]]) -> tuple[str, str]:
    """The spelling a group of readings agrees on.

    Modal rather than first-seen or longest: OCR-ish noise is various, and the
    correct reading is the one that recurs. Rider and horse are voted
    separately, because a segment often catches one line of the graphic and not
    the other.
    """
    riders = Counter(r.strip() for r, _ in readings if r.strip())
    horses = Counter(h.strip() for _, h in readings if h.strip())
    rider = riders.most_common(1)[0][0] if riders else ""
    horse = horses.most_common(1)[0][0] if horses else ""
    return rider, horse


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
# is not the ride that 75 in a straight test is.
_THRESHOLDS = {"freestyle": 80.0, "straight": 75.0}


def high_scoring(rides: list[dict], thresholds: dict[str, float] | None = None) -> list[dict]:
    """The rides that clear the bar for their kind of test.

    A ride whose total failed its own consistency check is not returned. The
    whole point of the check is that an unverified number should not be the
    reason something gets published.
    """
    bars = {**_THRESHOLDS, **(thresholds or {})}
    out = []
    for ride in rides:
        total = ride.get("total_pct")
        if total is None or ride.get("score_check", "").startswith("mismatch"):
            continue
        bar = bars.get(ride.get("test_type") or "straight")
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
        place = hit.get("finalPlace", hit.get("final_place"))
        ride["final_place"] = int(place) if isinstance(place, (int, float)) and place else None
        total = hit.get("totalPct", hit.get("total_pct"))
        grounded_total = float(total) if isinstance(total, (int, float)) else None
        ride["grounded_total_pct"] = grounded_total
        ride["grounded_source"] = source

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
