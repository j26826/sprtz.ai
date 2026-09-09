"""An absence has to survive to the record, or it means the same as never looking.

These test the path rather than the function. A pure function with no call site
passes its own tests perfectly well — that is how the games-list filter shipped
dead — so what matters is that a negative reported by one segment is still there
when the game record is built.
"""

from sprtz_agents.schemas import EquestrianSegmentAnalysis, GameDetails, NotConfirmed
from sprtz_agents.tools import game_summary
from sprtz_agents.tools.analysis import _not_confirmed_of


class _Plan:
    def __init__(self, index):
        self.index = index


def _segment(index, pairs):
    return (
        _Plan(index),
        EquestrianSegmentAnalysis(
            moments=[], discipline="dressage", discipline_confidence=0.9,
            not_confirmed=[NotConfirmed(moment_type=c, note=n) for c, n in pairs],
        ),
    )


def test_negatives_group_by_type_rather_than_repeating_per_segment():
    """A ride spans overlapping segments, so one rejected candidate arrives twice."""
    merged = _not_confirmed_of([
        _segment(0, [("pirouette", "corner at 03:12, not a turn on the haunches")]),
        _segment(1, [("pirouette", "corner at 04:40, same"), ("piaffe", "trot on the spot? no")]),
    ])
    by_type = {row["momentType"]: row["notes"] for row in merged}
    assert set(by_type) == {"pirouette", "piaffe"}
    assert len(by_type["pirouette"]) == 2, "both notes kept — they name different timecodes"
    assert all(n.startswith("[segment ") for n in by_type["pirouette"]), (
        "a note has to say where it came from or nobody can go and check it"
    )


def test_a_segment_that_checked_nothing_contributes_nothing():
    assert _not_confirmed_of([_segment(0, [])]) == []


def test_a_negative_with_no_note_still_records_the_type():
    merged = _not_confirmed_of([_segment(0, [("buck", "")])])
    assert merged == [{"momentType": "buck", "notes": []}], (
        "'looked for a buck, found none' is the answer the welfare gate needs"
    )


def test_it_reaches_the_game_record():
    game = game_summary.assemble(
        job_id="j1", sport="equestrian", moments=[], segment_summaries=[],
        competitions=[], venues=[], discipline="Dressage",
        not_confirmed=[{"momentType": "pirouette", "notes": ["[segment 0] corner"]}],
        teams_are_constant=False,
    )
    assert isinstance(game, GameDetails)
    assert game.not_confirmed[0]["momentType"] == "pirouette"
