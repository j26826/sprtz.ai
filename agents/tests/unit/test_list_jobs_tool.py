"""Listing jobs, which are shared across the desk.

Jobs used to be scoped to whoever uploaded them, and the interesting property
then was that the uid came from the session rather than from the model. That
scoping is gone by decision: accounts are provisioned by hand and everyone with
one is on the same desk, so a match uploaded by a colleague is a match this desk
is working on.

What has to stay true is that the boundary moved rather than vanished — the
route is still behind authentication, and the model still cannot name an owner
to widen or narrow what it sees, because there is no owner to name.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline


@pytest.fixture
def called():
    mock = AsyncMock(return_value={"status": "success", "jobs": [], "count": 0})
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        yield mock


@pytest.mark.asyncio
async def test_it_lists_every_job_not_one_persons(called):
    await pipeline.list_jobs()

    server, tool, args = called.await_args.args
    assert (server, tool) == ("catalog", "list_jobs")
    assert "owner_uid" not in args


def test_the_model_still_cannot_name_an_owner():
    # A uid the model can supply is a uid it can invent. With sharing there is
    # nothing to gain by naming one, and the parameter's absence keeps it that
    # way if scoping ever returns.
    parameters = inspect.signature(pipeline.list_jobs).parameters
    assert "owner_uid" not in parameters
    assert set(parameters) == {"status", "limit"}


def test_find_games_is_unscoped_too():
    parameters = inspect.signature(pipeline.find_games).parameters
    assert "owner_uid" not in parameters
    assert set(parameters) == {"query", "limit"}


@pytest.mark.asyncio
async def test_status_and_limit_are_passed_through(called):
    await pipeline.list_jobs(status="running", limit=5)

    _, _, args = called.await_args.args
    assert args["status"] == "running"
    assert args["limit"] == 5


@pytest.mark.asyncio
async def test_jobs_are_returned_to_the_model(called):
    called.return_value = {
        "status": "success",
        "jobs": [{"job_id": "a", "title": "Match A", "status": "analyzing"}],
        "count": 1,
    }
    result = await pipeline.list_jobs()

    assert result["status"] == "success"
    assert result["jobs"][0]["title"] == "Match A"


@pytest.mark.asyncio
async def test_a_failed_lookup_is_reported_rather_than_read_as_empty(called):
    called.return_value = {"status": "error", "error": "Firestore unavailable"}
    result = await pipeline.list_jobs()

    # "No jobs" and "could not look" lead the editor to opposite conclusions.
    assert result["status"] == "error"
    assert "jobs" not in result


def test_the_agent_actually_carries_the_tool():
    from sprtz_agents import agent

    assert pipeline.list_jobs in agent.root_agent.tools
    assert pipeline.find_games in agent.root_agent.tools
