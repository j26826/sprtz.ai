"""Firestore query shapes that would need an index nobody created.

These faults do not fail at import, at call time, or in any unit test that
mocks the client. They fail on a live read, against real data, with a message
containing a link to go and create the index — which is a bad way to find out
and exactly how "I cannot retrieve all the moments" reached an editor.

So this reads the source rather than running the queries. It is a lint, and it
belongs with the tests because that is when it needs to run.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STORE = Path(__file__).resolve().parents[1] / "catalog_server" / "store.py"

_FUNCTION = re.compile(r"^def (\w+)\(", re.M)
_FILTER = re.compile(r'FieldFilter\(\s*"(\w+)",\s*"([^"]+)"')
_ORDER = re.compile(r'\.order_by\(\s*"(\w+)"')

# Anything that is not an equality constrains a range, and Firestore requires
# the first ordering to be on that same field.
_EQUALITY = {"==", "in", "array_contains", "array_contains_any"}


def _queries() -> list[tuple[str, list[tuple[str, str]], list[str]]]:
    """Every function in the store, with the filters and ordering it applies."""
    source = STORE.read_text()
    starts = [(m.group(1), m.start()) for m in _FUNCTION.finditer(source)]
    found = []
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(source)
        body = source[start:end]
        found.append((name, _FILTER.findall(body), _ORDER.findall(body)))
    return found


def test_no_query_orders_by_a_field_other_than_the_one_it_range_filters():
    """The exact fault that broke list_action_plays.

    "highlightScore >= x ordered by startSec" is two fields, so Firestore wants
    a composite index for it and refuses the read until one exists.
    """
    offenders = []
    for name, filters, orders in _queries():
        ranges = [f for f, op in filters if op not in _EQUALITY]
        if ranges and orders and orders[0] != ranges[0]:
            offenders.append(f"{name}: range on {ranges[0]!r} but ordered by {orders[0]!r}")

    assert not offenders, (
        "these need a composite index that may not exist, and fail on a live "
        "read rather than here: " + "; ".join(offenders)
    )


def test_every_equality_plus_ordering_pair_has_a_declared_index():
    """An equality filter plus an ordering on another field is also composite.

    These do have indexes — jobs_by_owner_recent and the two KNN ones — so this
    checks the declaration still exists rather than assuming it does.
    """
    terraform = (Path(__file__).resolve().parents[2]
                 / "deploy" / "terraform" / "firestore.tf").read_text()

    for name, filters, orders in _queries():
        equalities = [f for f, op in filters if op in _EQUALITY]
        if not equalities or not orders:
            continue
        for field in equalities:
            assert f'field_path = "{field}"' in terraform, (
                f"{name} filters on {field!r} and orders by {orders[0]!r}, which "
                f"is a composite index, but no index in firestore.tf declares "
                f"{field!r}"
            )


def test_the_action_play_listing_needs_only_a_single_field_index():
    name_to_query = {n: (f, o) for n, f, o in _queries()}
    filters, orders = name_to_query["list_action_plays"]

    # Ordering by one field with no range filter is served by the single-field
    # index every collection has automatically.
    assert orders == ["startSec"]
    assert not [f for f, op in filters if op not in _EQUALITY]
