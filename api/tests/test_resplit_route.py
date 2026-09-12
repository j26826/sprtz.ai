"""Re-splitting a recording is a route, and its outcome is read, not believed.

Asked in the editor's conversation this went wrong three ways in one evening,
none of them in the splitting itself: a crashed tool came back as a considered
refusal, a request naming one recording split whichever match happened to be
open, and a session already told the recording held one class answered the next
request without calling anything.

The job is the route now. What the agent says is not taken as the outcome —
every one of those failures arrived as a sentence saying it had worked — so the
records are read before and after and compared.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import jobs


def run(coro):
    return asyncio.run(coro)


class _Engine:
    """The agent engine, as far as this route is concerned."""

    def __init__(self, reply="done", spy=None):
        self.reply, self.spy = reply, spy if spy is not None else []

    def create_session(self, user_id):
        self.spy.append(("session", user_id))
        return {"id": "s1"}

    def stream_query(self, *, user_id, session_id, message):
        self.spy.append(("message", message))
        yield {"content": {"parts": [{"text": self.reply}]}}


def _catalog(before, after):
    calls = {"n": 0}

    async def call_mcp(server, tool, args):
        if tool == "list_games":
            calls["n"] += 1
            return {"status": "success",
                    "games": before if calls["n"] == 1 else after}
        return {"status": "success"}
    return call_mcp


@pytest.fixture
def route(monkeypatch):
    def go(before, after, reply="done", spy=None):
        engine = _Engine(reply, spy)
        monkeypatch.setattr(jobs, "_agent_engine", lambda settings: engine)
        monkeypatch.setattr(jobs.clients, "call_mcp", _catalog(before, after))
        return engine
    return go


def test_the_job_travels_as_the_route_and_the_arena_as_the_ask(route):
    spy = []
    route([{"classId": "1"}], [{"classId": "2"}], spy=spy)
    run(jobs.split_into_classes(
        "abc123", jobs.SplitRequest(arena="LeMieux Arena"), user=None, settings=None))

    message = next(value for kind, value in spy if kind == "message")
    assert message.startswith("[job_id: abc123]\n")
    assert "Split recording abc123" in message
    assert "LeMieux Arena" in message


def test_a_session_of_its_own_every_time(route):
    # The turn that made no tool call at all had a conversation behind it that
    # had already been told this recording held one class.
    spy = []
    route([], [{"classId": "2"}], spy=spy)
    run(jobs.split_into_classes("abc123", jobs.SplitRequest(), user=None, settings=None))
    assert ("session", jobs.SPLIT_USER) in spy


def test_what_changed_is_read_from_the_records(route):
    route([{"classId": "1278778"}, {"classId": "1278779"}, {"classId": "1278780"}],
          [{"classId": "1278780"}])
    out = run(jobs.split_into_classes(
        "abc123", jobs.SplitRequest(arena="LeMieux Arena"), user=None, settings=None))
    assert out["changed"] is True
    assert out["was"] == ["1278778", "1278779", "1278780"]
    assert out["classes"] == ["1278780"]


def test_a_reply_claiming_success_over_records_that_did_not_move(route):
    # This is the whole reason the route reads them: every failure so far came
    # back as a sentence saying it had worked.
    route([{"classId": "1278780"}], [{"classId": "1278780"}],
          reply="Done — the recording has been split into its classes.")
    out = run(jobs.split_into_classes("abc123", jobs.SplitRequest(), user=None, settings=None))
    assert out["changed"] is False
    assert out["classes"] == ["1278780"]


def test_an_engine_that_throws_is_a_sentence_not_a_traceback(route, monkeypatch):
    route([], [])

    def boom(settings):
        raise RuntimeError("Current state: UPDATING")
    monkeypatch.setattr(jobs, "_agent_engine", boom)

    with pytest.raises(jobs.HTTPException) as caught:
        run(jobs.split_into_classes("abc123", jobs.SplitRequest(), user=None, settings=None))
    assert caught.value.status_code == 502
    assert "UPDATING" not in caught.value.detail
