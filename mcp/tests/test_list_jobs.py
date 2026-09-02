"""Listing an owner's jobs.

The editor never sees a job id, so this is the only way the agent can answer
"what's still processing?". Two things matter: the owner filter reaches
Firestore rather than being applied afterwards, and a page full of finished
jobs cannot hide the running ones the caller asked for.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store  # noqa: E402


def _snapshot(job_id: str, status: str, **extra):
    snap = MagicMock()
    snap.id = job_id
    snap.to_dict.return_value = {
        "ownerUid": "uid-1",
        "title": f"Match {job_id}",
        "sport": "handball",
        "status": status,
        "stage": "analysis",
        "progress": 40,
        "error": None,
        "counts": {"moments": 3, "clips": 1},
        "createdAt": datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        **extra,
    }
    return snap


class _Query:
    """Records the calls made against it and yields the snapshots given."""

    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.filters: list = []
        self.order_by_args: tuple = ()
        self.limit_value: int | None = None

    def where(self, filter=None):  # noqa: A002 - matches the Firestore signature
        self.filters.append(filter)
        return self

    def order_by(self, field, direction=None):
        self.order_by_args = (field, direction)
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def stream(self):
        return iter(self.snapshots)


@pytest.fixture
def query_with():
    def _make(snapshots):
        query = _Query(snapshots)
        db = MagicMock()
        db.collection.return_value = query
        return query, patch.object(store, "db", return_value=db)
    return _make


class TestScoping:
    """Jobs are shared across the desk; nothing filters them by owner."""

    def test_ordering_goes_to_firestore_and_nothing_is_filtered(self, query_with):
        query, patched = query_with([_snapshot("a", "ready")])
        with patched:
            store.list_jobs(limit=5)

        assert query.order_by_args == ("createdAt", "DESCENDING")
        # Jobs are shared, so there is no owner filter — which is also why this
        # needs only the single-field index on createdAt.
        assert query.filters == []
        assert query.limit_value == 5

    def test_an_owner_argument_is_ignored_rather_than_honoured(self, query_with):
        # A caller still passing one must not be quietly answered with a
        # filtered list it did not ask for.
        query, patched = query_with([_snapshot("a", "ready"), _snapshot("b", "ready")])
        with patched:
            jobs = store.list_jobs("someone-else", limit=5)

        assert query.filters == []
        assert len(jobs) == 2

    def test_unfiltered_listing_returns_every_status(self, query_with):
        query, patched = query_with([
            _snapshot("a", "ready"), _snapshot("b", "failed"), _snapshot("c", "analyzing"),
        ])
        with patched:
            jobs = store.list_jobs("uid-1")

        assert [j["status"] for j in jobs] == ["ready", "failed", "analyzing"]

    def test_limit_is_honoured(self, query_with):
        _, patched = query_with([_snapshot(str(i), "ready") for i in range(10)])
        with patched:
            assert len(store.list_jobs("uid-1", limit=3)) == 3


class TestRunningFilter:
    def test_running_covers_every_unfinished_status(self, query_with):
        _, patched = query_with([_snapshot(s, s) for s in store.RUNNING_STATUSES])
        with patched:
            jobs = store.list_jobs("uid-1", status="running")

        assert {j["status"] for j in jobs} == set(store.RUNNING_STATUSES)

    def test_finished_jobs_do_not_crowd_out_running_ones(self, query_with):
        # The running job is last, behind a full page of finished ones. Reading
        # only `limit` documents would answer "nothing is processing".
        snapshots = [_snapshot(f"done{i}", "ready") for i in range(5)]
        snapshots.append(_snapshot("live", "analyzing"))
        query, patched = query_with(snapshots)
        with patched:
            jobs = store.list_jobs("uid-1", limit=2, status="running")

        assert query.limit_value > 2, "a filtered read must over-fetch"
        assert [j["job_id"] for j in jobs] == ["live"]

    def test_exact_status_is_matched_literally(self, query_with):
        _, patched = query_with([_snapshot("a", "ready"), _snapshot("b", "failed")])
        with patched:
            jobs = store.list_jobs("uid-1", status="failed")

        assert [j["job_id"] for j in jobs] == ["b"]


class TestSummary:
    def test_summary_carries_what_the_agent_needs_to_name_a_job(self, query_with):
        _, patched = query_with([_snapshot("a", "analyzing")])
        with patched:
            job, = store.list_jobs("uid-1")

        assert job["job_id"] == "a"
        assert job["title"] == "Match a"
        assert job["status"] == "analyzing"
        assert job["stage"] == "analysis"
        assert job["counts"] == {"moments": 3, "clips": 1}

    def test_timestamps_are_json_safe(self, query_with):
        _, patched = query_with([_snapshot("a", "ready")])
        with patched:
            job, = store.list_jobs("uid-1")

        assert isinstance(job["created_at"], str)

    def test_title_falls_back_to_the_uploaded_filename(self, query_with):
        _, patched = query_with([
            _snapshot("a", "ready", title="", source={"originalName": "handball1.mp4"}),
        ])
        with patched:
            job, = store.list_jobs("uid-1")

        assert job["title"] == "handball1.mp4"

    def test_embeddings_never_reach_the_summary(self, query_with):
        # Moment vectors are 768 floats each; a listing that carried them would
        # cost more context than the answer is worth.
        _, patched = query_with([_snapshot("a", "ready", embedding=[0.1] * 768)])
        with patched:
            job, = store.list_jobs("uid-1")

        assert "embedding" not in job
