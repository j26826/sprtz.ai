"""get_event: the agent's view of event -> rides -> moments.

The catalog builds the tree (mcp/catalog_server/event_tree.py); this is what
the producer is handed of it — every ride, the best of its moments, and the
moments outside every ride — cut down so a day of forty rounds fits.
"""

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline


def _moment(mid, start, score, **extra):
    return {"momentId": mid, "label": "Piaffe", "momentType": "piaffe", "startSec": start,
            "summary": f"moment {mid}", "highlightScore": score, "description": "long prose",
            "evidence": ["a", "b"], "executionDetails": "long prose", **extra}


TREE = {
    "status": "success",
    "event": {
        "jobId": "j1", "title": "CDI — Grand Prix Freestyle", "discipline": "Dressage",
        "riders": [
            {"order": 1, "rider": "Anna Berger", "horse": "Lumière", "startNumber": "14",
             "identitySource": "observed", "startSec": 310.0, "endSec": 700.0,
             "testType": "freestyle", "result": {"totalPct": 74.37, "place": 2},
             "momentCount": 3,
             "moments": [_moment("a", 480, 0.5), _moment("b", 566, 0.9), _moment("c", 318, 0.7)]},
            {"order": 2, "rider": "Jonas Keller", "horse": "Falkenstein", "startNumber": "7",
             "identitySource": "schedule", "startSec": 1180.0, "endSec": 1570.0,
             "testType": "freestyle", "result": {"totalPct": 76.02, "place": 1},
             "momentCount": 1, "moments": [_moment("d", 1244, 0.8, requiresHumanReview=True)]},
        ],
        "unassignedMoments": [_moment("prize", 2610, 0.6)],
    },
}


async def _get(**kwargs):
    mock = AsyncMock(return_value=TREE)
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        out = await pipeline.get_event("j1", **kwargs)
    assert mock.await_args.args == ("catalog", "get_event_tree", {"job_id": "j1"})
    return out


@pytest.mark.asyncio
async def test_every_ride_with_its_best_moments_first():
    out = await _get(max_moments_per_ride=2)
    berger, keller = out["event"]["riders"]
    assert [m["momentId"] for m in berger["moments"]] == ["b", "c"]
    assert berger["momentCount"] == 3, "the count still says how many there were"
    assert (berger["start"], berger["end"]) == ("05:10", "11:40")
    assert keller["identitySource"] == "schedule"
    assert keller["moments"][0]["requiresHumanReview"] is True


@pytest.mark.asyncio
async def test_moments_are_cut_to_what_an_answer_needs():
    out = await _get()
    brief = out["event"]["riders"][0]["moments"][0]
    assert set(brief) == {"momentId", "label", "at", "summary", "highlightScore"}
    assert "startSec" not in out["event"]["riders"][0]


@pytest.mark.asyncio
async def test_moments_outside_every_ride_are_reported_not_dropped():
    out = await _get()
    assert out["event"]["unassignedMomentCount"] == 1
    assert out["event"]["unassignedMoments"][0]["momentId"] == "prize"


@pytest.mark.asyncio
async def test_narrowed_to_one_rider_by_either_half_of_the_combination():
    out = await _get(riders="Falkenstein")
    assert [r["rider"] for r in out["event"]["riders"]] == ["Jonas Keller"]
    assert out["event"]["unassignedMoments"] == []


@pytest.mark.asyncio
async def test_a_name_that_matches_nothing_says_so():
    out = await _get(riders="Nobody Atall")
    assert out["event"]["riders"] == []
    assert "No ride matched" in out["note"]


@pytest.mark.asyncio
async def test_a_job_without_rides_says_why():
    mock = AsyncMock(return_value={"status": "success",
                                   "event": {"riders": [], "unassignedMoments": [_moment("x", 5, 0.4)]}})
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        out = await pipeline.get_event("h1")
    assert out["event"]["riders"] == []
    assert "Only equestrian" in out["note"]


@pytest.mark.asyncio
async def test_a_catalog_error_is_passed_through():
    mock = AsyncMock(return_value={"status": "error", "error": "No job 'x'."})
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        out = await pipeline.get_event("x")
    assert out["status"] == "error"


def test_the_agent_can_call_it():
    from sprtz_agents import agent
    names = [getattr(t, "__name__", "") for t in agent._build_tools()]
    assert "get_event" in names
