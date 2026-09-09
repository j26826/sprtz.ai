"""The dressage clip requirements, and the two boundaries that silently drop fields.

Both Firestore writers in catalog_server/store.py build their payload key by
key rather than dumping the model. A field can therefore be correct in the
schema, correct in the pipeline, and simply absent from the database, with
nothing failing anywhere. That has now nearly happened twice, so it is pinned
here rather than remembered.
"""

import re
from pathlib import Path

import pytest

from sprtz_agents.schemas import Moment
from sprtz_agents.sports import get_profile

STORE = Path(__file__).resolve().parents[3] / "mcp" / "catalog_server" / "store.py"


@pytest.fixture(scope="module")
def dressage():
    return {m.code: m for m in get_profile("equestrian").types_for("dressage")}


@pytest.mark.parametrize("code", [
    # 4.1 Technical Harmony — the movements named as worth clipping when good.
    "pirouette", "half_pass", "tempi_changes",
    # 4.2 Emotional & Narrative.
    "rider_joy", "rider_emotion", "horse_excitement",
    # 4.3 Anomalies & Incidents.
    "outside_boards", "arena_distraction", "tack_failure", "buck",
])
def test_every_named_requirement_has_a_moment_type(dressage, code):
    assert code in dressage, f"nothing in the catalogue can report {code}"


def test_stress_indicators_are_gated_and_celebration_is_not(dressage):
    """The requirement's own distinction, and the one that is easy to get wrong.

    A buck during a test and a buck at prize-giving are the same movement. One
    is a horse in trouble and the other is a horse that is fresh, so only one of
    them is publishable without somebody looking first.
    """
    assert dressage["buck"].requires_human_review
    assert dressage["tack_failure"].requires_human_review
    assert not dressage["horse_excitement"].requires_human_review
    assert not dressage["rider_joy"].requires_human_review


def test_the_gate_is_carried_by_the_moment_not_just_the_type():
    assert "requires_human_review" in Moment.model_fields


def _payload_of(func_name: str) -> str:
    body = re.search(rf"def {func_name}\(.*?(?=\ndef )", STORE.read_text(), re.S)
    assert body, f"{func_name} not found in store.py"
    return body.group(0)


def test_the_review_gate_survives_the_firestore_write():
    assert "requiresHumanReview" in _payload_of("upsert_moments"), (
        "a moment flagged for review would be stored looking like any other"
    )


def test_the_review_gate_survives_the_read_back():
    src = STORE.read_text()
    assert 'requiresHumanReview' in src and 'requires_human_review' in src, (
        "the gate must map back on read or it is lost the moment it is re-read"
    )


def test_not_confirmed_survives_the_game_write():
    assert "not_confirmed" in _payload_of("upsert_game"), (
        "GameDetails.not_confirmed is dropped at the Firestore boundary"
    )
