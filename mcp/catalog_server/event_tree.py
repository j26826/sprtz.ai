"""One event as a tree: the event, the rides in it, the moments in each ride.

A competition day is read by who rode, and what happened while they were in the
arena — not as one flat list of moments with a name printed on each. The
Firestore documents already hold every piece: the game record carries the
rides, and each moment carries the ride it was joined to. This puts them
together, once, so the API, the agents and the editor all see the same
grouping.

A group is one ride: a rider on one horse. A rider who brings two horses to the
same class is two groups, because that is how the class is judged and ranked.
The key is ``riders`` because that is how the editor asks for it; each entry is
the combination, not the person.

Built in code from records that already exist rather than asked of the model.
No single analysis window sees a whole ride — rides cross windows and are
stitched back together afterwards — so a nested answer from the model would be
a nested answer about fragments.

Pure: takes plain dicts, returns plain dicts, touches nothing.
"""

from __future__ import annotations

from typing import Any

# What a moment carries into the tree. The embedding and the ownership fields
# are left behind: the vector is 768 floats nobody reading the tree needs, and
# the ride fields are said once, on the ride, rather than on every moment of it.
_MOMENT_FIELDS = (
    "momentId", "momentType", "label", "category",
    "startSec", "peakSec", "endSec",
    "confidence", "excitement", "highlightScore", "requiresHumanReview",
    "summary", "description", "evidence",
    "executionDetails", "harmonyIndex", "actionResult", "scoreboard",
    "segmentIndexes", "thumbUri",
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _moment_out(m: dict[str, Any]) -> dict[str, Any]:
    return {key: m[key] for key in _MOMENT_FIELDS if key in m}


def _ride_out(ride: dict[str, Any]) -> dict[str, Any]:
    """A stored ride (snake_case, as rides.fuse and grounding write it) for the tree."""
    place = ride.get("final_place")
    return {
        "order": ride.get("order"),
        "startNumber": ride.get("start_number", "") or "",
        "rider": ride.get("rider", "") or "",
        "horse": ride.get("horse", "") or "",
        # "observed" when a graphic named the ride, "schedule" when the start
        # list did. Carried to the group so a heading never presents the second
        # as the first.
        "identitySource": ride.get("identity_source", "") or "",
        "groundedRider": ride.get("grounded_rider", "") or "",
        "groundedHorse": ride.get("grounded_horse", "") or "",
        "startSec": _num(ride.get("start_sec")),
        "endSec": _num(ride.get("end_sec")),
        "testType": ride.get("test_type", "") or "",
        # The analysis windows the ride was seen in. What was looked for and
        # not found is noted per window, so these say which of those notes are
        # about this ride's footage.
        "segments": [int(s) for s in (ride.get("segments") or []) if isinstance(s, int | float)],
        "result": {
            "judgeMarks": list(ride.get("judge_marks") or []),
            "totalPct": ride.get("total_pct"),
            "groundedTotalPct": ride.get("grounded_total_pct"),
            # The published placing when grounding found one, else what the
            # screen showed at the time.
            "place": place if place is not None else ride.get("rank"),
            "scoreSource": ride.get("score_source", "") or "",
            "scoreCheck": ride.get("score_check", "") or "",
            "scoreboard": ride.get("scoreboard", "") or "",
        },
        "moments": [],
    }


def _ride_for(moment: dict[str, Any], rides: list[dict[str, Any]],
              by_order: dict[Any, dict[str, Any]]) -> dict[str, Any] | None:
    """The ride a moment belongs to.

    The order the pipeline stored on the moment first: it is what the tile
    already shows, and grounding may have re-joined it since. Failing that, the
    same rule the pipeline uses — whoever was in the arena at the moment's peak
    (rides.attach_moments) — so a moment stored before rides existed still
    lands somewhere sensible.
    """
    order = moment.get("rideOrder")
    if order is not None and order in by_order:
        return by_order[order]
    at = _num(moment.get("peakSec") or moment.get("startSec"))
    return next((r for r in rides if r["startSec"] <= at <= r["endSec"]), None)


def build_event_tree(job_id: str, job: dict[str, Any], game: dict[str, Any],
                     moments: list[dict[str, Any]]) -> dict[str, Any]:
    """Nest ``moments`` under the rides in ``game``, under the event.

    Args:
        job_id: The job the event is.
        job: The job document, for the title and sport before a game record
            exists.
        game: The game document (``games/{job_id}``), or {} if there is none yet.
        moments: The job's moment documents, any order.

    Returns:
        ``{"event": {...event fields, "riders": [...], "unassignedMoments": [...]}}``.
        Riders are in running order, moments in time order within each. A
        moment outside every ride — the warm-up, the prize-giving, a loose horse
        between rounds — goes to ``unassignedMoments`` rather than vanishing.
        A sport with no rides has ``riders: []`` and every moment unassigned.
    """
    game = game or {}
    job = job or {}

    stored = [r for r in (game.get("rides") or []) if isinstance(r, dict)]
    rides = sorted((_ride_out(r) for r in stored),
                   key=lambda r: (r["order"] is None, r["order"] or 0, r["startSec"]))
    by_order = {r["order"]: r for r in rides if r["order"] is not None}

    unassigned: list[dict[str, Any]] = []
    for m in sorted(moments or [], key=lambda m: _num(m.get("startSec"))):
        ride = _ride_for(m, rides, by_order)
        (ride["moments"] if ride is not None else unassigned).append(_moment_out(m))

    for ride in rides:
        ride["momentCount"] = len(ride["moments"])

    return {
        "event": {
            "jobId": job_id,
            "title": game.get("title") or job.get("title", ""),
            "sport": game.get("sport") or job.get("sport", ""),
            "discipline": game.get("discipline", ""),
            "disciplineConfidence": game.get("disciplineConfidence", 0.0),
            "competition": game.get("competition") or game.get("groundedCompetition", ""),
            "venue": game.get("venue") or game.get("groundedVenue", ""),
            "showTitle": game.get("showTitle", ""),
            "location": game.get("location", ""),
            "date": game.get("matchDate", ""),
            "judges": list(game.get("judges") or []),
            "outcome": game.get("eventOutcome", ""),
            "summary": game.get("summary", ""),
            "notConfirmed": list(game.get("notConfirmed") or []),
            "momentCount": len(moments or []),
            "riderCount": len(rides),
            "riders": rides,
            "unassignedMoments": unassigned,
        },
    }
