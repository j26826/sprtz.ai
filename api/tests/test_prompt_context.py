"""What the agent is told before the editor's words.

The session's scope goes first, then the open job, then the message — and a
missing scope or job leaves no empty bracket behind.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers.agent import build_prompt


def test_scope_then_job_then_message():
    out = build_prompt("show the best moments", "job-1", "sport=equestrian; disciplines=Dressage; job_ids=a,b")
    assert out.splitlines() == [
        "[scope: sport=equestrian; disciplines=Dressage; job_ids=a,b]",
        "[job_id: job-1]",
        "show the best moments",
    ]


def test_nothing_chosen_is_nothing_said():
    assert build_prompt("hello", None, None) == "hello"
    assert build_prompt("hello", None, "") == "hello"
    assert build_prompt("hello", "job-1", None) == "[job_id: job-1]\nhello"
