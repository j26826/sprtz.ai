"""GET /api/jobs/{job_id}/event: the event -> rides -> moments tree.

The API only fronts it — the catalog builds the tree — so what matters here is
that the job is checked first, the catalog is asked the right thing, and a
catalog failure is not dressed up as an empty event.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.auth import CallerIdentity
from app.routers import jobs

USER = CallerIdentity(uid="u1", email="editor@example.com")
TREE = {"jobId": "j1", "riders": [{"order": 1, "rider": "A", "horse": "B", "moments": []}],
        "unassignedMoments": []}


def _calls(job=None, tree=None):
    async def call(server, tool, args):
        if tool == "get_job":
            return job if job is not None else {"job_id": args["job_id"], "status": "analyzed"}
        if tool == "get_event_tree":
            return tree if tree is not None else {"status": "success", "event": TREE}
        raise AssertionError(f"unexpected tool {tool}")
    return AsyncMock(side_effect=call)


def test_returns_the_catalogs_tree():
    mock = _calls()
    with patch.object(jobs.clients, "call_mcp", mock):
        out = asyncio.run(jobs.event_tree("j1", user=USER))
    assert out == {"event": TREE}
    assert mock.await_args_list[-1].args == (
        "catalog", "get_event_tree", {"job_id": "j1", "class_id": ""})


def test_one_class_of_a_day_is_asked_for_by_name():
    # A recording that crossed several classes is several events, and the tree
    # of one of them is that class's rides and only its moments.
    mock = _calls()
    with patch.object(jobs.clients, "call_mcp", mock):
        asyncio.run(jobs.event_tree("j1", class_id="1278771", user=USER))
    assert mock.await_args_list[-1].args[2]["class_id"] == "1278771"


def test_an_unknown_job_is_404_before_the_catalog_is_asked():
    mock = _calls(job={"status": "error", "error": "No job"})
    with patch.object(jobs.clients, "call_mcp", mock), pytest.raises(HTTPException) as err:
        asyncio.run(jobs.event_tree("nope", user=USER))
    assert err.value.status_code == 404
    assert all(c.args[1] != "get_event_tree" for c in mock.await_args_list)


def test_a_catalog_failure_is_an_error_not_an_empty_event():
    mock = _calls(tree={"status": "error", "error": "DeadlineExceeded: Firestore unavailable"})
    with patch.object(jobs.clients, "call_mcp", mock), pytest.raises(HTTPException) as err:
        asyncio.run(jobs.event_tree("j1", user=USER))
    assert err.value.status_code == 502
    # What the catalog said goes to the log, not to the browser: an exception
    # rendered as text is true, useless to an editor, and a description of the
    # inside of a system they cannot see.
    assert "DeadlineExceeded" not in err.value.detail
    assert "could not be read" in err.value.detail
