"""`counts.moments` has exactly one writer.

The desk, the agent and the live event's own finish message all quote this
number, and nothing anywhere compares it against the documents it claims to
count. So when the live path incremented it twice — once in `upsert_moments`
writing the moments, once in `finish_live_chunk` marking the chunk analysed —
every live event reported double, and it read as a busy day: 1206 moments
against 603 stored documents, for an event that really found 603.

Uploads were right the whole time, because a VOD run never calls
`finish_live_chunk`. Only live events were wrong, which is the half of the
product where the number is watched most.

A lint rather than a unit test: the fault is one Firestore update too many in
a function whose other effects are all correct, so a mocked client asserts
whatever the code does. What has to hold is that one field has one writer, and
that is a property of the source.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STORE = Path(__file__).resolve().parents[1] / "catalog_server" / "store.py"
LIVE = Path(__file__).resolve().parents[2] / "agents" / "sprtz_agents" / "tools" / "live.py"

_FUNCTION = re.compile(r"^def (\w+)\(", re.M)


def _owner_of(source: str, at: int) -> str:
    """The function a position in the file falls inside."""
    name = "<module>"
    for match in _FUNCTION.finditer(source):
        if match.start() > at:
            break
        name = match.group(1)
    return name


def _writers(field: str) -> list[str]:
    source = STORE.read_text()
    return [_owner_of(source, m.start())
            for m in re.finditer(rf'"{re.escape(field)}":\s*firestore\.Increment', source)]


class TestOneWriter:
    def test_only_the_write_that_stores_moments_counts_them(self):
        writers = _writers("counts.moments")
        assert writers == ["upsert_moments"], (
            "counts.moments must be incremented only where the moments are "
            f"actually written; also incremented in {sorted(set(writers) - {'upsert_moments'})}"
        )

    def test_finishing_a_chunk_still_counts_the_chunk(self):
        # The other counter on that update is the one finish_live_chunk is
        # entitled to: it is the thing that just happened.
        assert _writers("live.chunksAnalysed") == ["finish_live_chunk"]


class TestTheOrderThatMakesItSafe:
    def test_a_live_chunk_stores_its_moments_before_it_is_finished(self):
        # Dropping the second increment is only correct because the moments are
        # already written by the time the chunk is marked analysed. If that
        # order ever inverted, a chunk would be finished with its moments
        # uncounted and the total would be short rather than double.
        source = LIVE.read_text()
        persisted = source.index("saved = await _persist_moments(")
        finished = source.index("await _finish_chunk(\n", persisted)
        assert persisted < finished
