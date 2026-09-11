"""list_rides: the agent's view of a competition day's running order.

It read the rides through get_game, whose output leaves them out, so it
answered "no rides recorded" for every event that had them — while the
editor's own rides card, reading the raw record, showed them. These pin that
the rides are read from where they are, and that the filters still work.
"""

from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.tools import pipeline

RIDES = [
    {"order": 1, "rider": "Anna Berger", "horse": "Lumière", "start_sec": 310.0, "end_sec": 700.0,
     "test_type": "freestyle", "judge_marks": [74.2, 75.1, 73.8], "total_pct": 74.37,
     "rank": 2, "score_check": "ok", "score_source": "observed"},
    {"order": 2, "rider": "Jonas Keller", "horse": "Falkenstein", "start_sec": 1180.0, "end_sec": 1570.0,
     "test_type": "freestyle", "judge_marks": [76.0, 76.1], "total_pct": 76.02,
     "rank": 1, "score_check": "ok", "score_source": "observed"},
    {"order": 3, "rider": "Marie Duval", "horse": "Cassiopeia", "start_sec": 2050.0, "end_sec": 2440.0,
     "test_type": "freestyle", "judge_marks": [71.5, 71.9, 71.7], "total_pct": 71.85,
     "rank": 3, "score_check": "mismatch: 3 marks average 71.700, total shown as 71.850",
     "score_source": "observed"},
]


async def _rides(**kwargs):
    mock = AsyncMock(return_value={"status": "success", "job_id": "j1", "rides": RIDES})
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        out = await pipeline.list_rides("j1", **kwargs)
    return out, mock


@pytest.mark.asyncio
async def test_the_rides_come_from_the_record_not_the_game_summary():
    out, mock = await _rides()
    assert mock.await_args.args == ("catalog", "list_game_rides", {"job_id": "j1"})
    assert [r["rider"] for r in out["rides"]] == ["Anna Berger", "Jonas Keller", "Marie Duval"]
    assert out["count"] == 3 and out["total_rides"] == 3
    assert "note" not in out


@pytest.mark.asyncio
async def test_a_score_bar_leaves_out_a_total_that_fails_its_own_check():
    out, _ = await _rides(min_score=70)
    assert [r["rider"] for r in out["rides"]] == ["Anna Berger", "Jonas Keller"]


@pytest.mark.asyncio
async def test_a_watchlist_finds_a_ride_by_its_horse():
    out, _ = await _rides(watchlist="Falkenstein")
    assert [r["rider"] for r in out["rides"]] == ["Jonas Keller"]


@pytest.mark.asyncio
async def test_a_job_without_rides_says_why():
    mock = AsyncMock(return_value={"status": "success", "job_id": "h1", "rides": []})
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        out = await pipeline.list_rides("h1")
    assert out["rides"] == [] and "Only equestrian" in out["note"]


@pytest.mark.asyncio
async def test_no_game_record_is_passed_through_as_the_error_it_is():
    mock = AsyncMock(return_value={"status": "error", "error": "No game record for job 'x'."})
    with patch.object(pipeline.mcp_client, "call_tool", mock):
        out = await pipeline.list_rides("x")
    assert out["status"] == "error"
