"""Whose jobs the agent is allowed to list.

The editor asks "what's still processing?" without a job id, so the tool has to
find their jobs on its own. Where the uid comes from is the whole point: taking
one from the model would let a guessed uid read another tenant's jobs, so it
comes from the session ADK opened for the signed-in user.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline


class _Context:
    """Stands in for ADK's ToolContext, which exposes user_id as a property."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


@pytest.fixture
def called():
    """Patch the MCP call and hand back the recorded arguments."""
    mock = AsyncMock(return_value={"status": "success", "jobs": [], "count": 0})
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        yield mock


@pytest.mark.asyncio
async def test_owner_comes_from_the_session_not_the_model(called):
    await pipeline.list_jobs(_Context("uid-real"))

    server, tool, args = called.await_args.args
    assert (server, tool) == ("catalog", "list_jobs")
    assert args["owner_uid"] == "uid-real"


@pytest.mark.asyncio
async def test_the_model_cannot_name_an_owner():
    # A uid parameter would be a parameter the model fills in. Its absence from
    # the signature is what stops it being supplied at all.
    import inspect

    parameters = inspect.signature(pipeline.list_jobs).parameters
    assert "owner_uid" not in parameters
    assert set(parameters) == {"tool_context", "status", "limit"}


@pytest.mark.asyncio
async def test_an_anonymous_session_lists_nothing(called):
    result = await pipeline.list_jobs(_Context(""))

    assert result["status"] == "error"
    called.assert_not_awaited(), "no uid means no query, not a query for everyone"


@pytest.mark.asyncio
async def test_status_and_limit_are_passed_through(called):
    await pipeline.list_jobs(_Context("uid-1"), status="running", limit=5)

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
    result = await pipeline.list_jobs(_Context("uid-1"))

    assert result["status"] == "success"
    assert result["jobs"][0]["title"] == "Match A"


@pytest.mark.asyncio
async def test_a_failed_lookup_is_reported_rather_than_read_as_empty(called):
    called.return_value = {"status": "error", "error": "Firestore unavailable"}
    result = await pipeline.list_jobs(_Context("uid-1"))

    # "No jobs" and "could not look" lead the editor to opposite conclusions.
    assert result["status"] == "error"
    assert "jobs" not in result


def test_the_agent_actually_carries_the_tool():
    # The tool list is bound at import time, so a tool that is not in it here is
    # not in the packaged agent either.
    from sprtz_agents import agent

    assert pipeline.list_jobs in agent.root_agent.tools
