"""Retry on the analysis calls.

A segment analysis had no retry options at all, so a single 429 lost fifteen
minutes of match — the whole window returned nothing and the job reported no
moments found. Quota is per-minute and every window goes out at once, so
exhausting it is an ordinary event rather than an exceptional one.
"""

from __future__ import annotations

import inspect

from sprtz_agents.config import Settings
from sprtz_agents.tools import analysis


def _retry_options():
    """The HttpRetryOptions literal the request config is built with."""
    source = inspect.getsource(analysis._analyse_one)
    assert "retry_options=types.HttpRetryOptions(" in source, (
        "the segment request carries no retry; one 429 loses the whole window"
    )
    return source[source.index("retry_options=types.HttpRetryOptions("):]


class TestRetry:
    def test_quota_exhaustion_is_retried(self):
        # 429 is the one actually seen: "Resource exhausted. Please try again
        # later." Retrying is precisely what it asks for.
        assert "429" in _retry_options()

    def test_transient_server_errors_are_retried_too(self):
        options = _retry_options()
        for status in ("500", "502", "503", "504"):
            assert status in options, status

    def test_there_is_more_than_one_attempt(self):
        assert "attempts=6" in _retry_options()

    def test_the_backoff_outlasts_a_quota_window(self):
        # Vertex quota is measured per minute, so a two-second retry asks the
        # same exhausted quota the same question. Six attempts from 8s doubling
        # to a 120s ceiling spans about four minutes of waiting.
        options = _retry_options()
        assert "initial_delay=8.0" in options
        assert "max_delay=120.0" in options

    def test_jitter_is_on(self):
        # Thirteen segments retrying in lockstep would rebuild the burst that
        # exhausted the quota in the first place.
        assert "jitter=" in _retry_options()


class TestConcurrency:
    def test_fewer_segments_go_out_at_once_than_a_match_has(self):
        # Thirteen windows at six concurrent exhausted the per-minute quota.
        # Retries carry the rest; this reduces how often they are needed.
        assert Settings().max_concurrent_segments <= 3

    def test_it_is_still_concurrent(self):
        # One at a time would make a three-hour match take hours of wall clock.
        assert Settings().max_concurrent_segments > 1
