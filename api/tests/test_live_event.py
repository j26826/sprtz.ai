"""Scheduling a live event, and who may drive its tick.

The tick is the one route on this API that is not for a signed-in editor: it
is for Cloud Scheduler, and it has to be exactly for Cloud Scheduler.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import auth
from app.routers import live
from app.routers.jobs import LiveEventRequest, _clean_hls_url

# The real clock, not a fixed instant. The window validator refuses an event
# that has already ended, so a hard-coded "now" makes these tests pass until
# that wall-clock time and fail for ever after — which is exactly what
# happened, an hour after the date they named.
NOW = datetime.datetime.now(datetime.UTC)


def _at(minutes: float) -> str:
    return (NOW + datetime.timedelta(minutes=minutes)).isoformat()


class TestTheStreamUrl:
    def test_https_with_a_token_is_fine(self):
        assert _clean_hls_url(" https://cdn.example.com/live/master.m3u8?token=abc ") \
            == "https://cdn.example.com/live/master.m3u8?token=abc"

    def test_plain_http_is_refused(self):
        # The download job fetches this from inside the project; http can be
        # pointed at the metadata server.
        with pytest.raises(ValueError):
            _clean_hls_url("http://169.254.169.254/computeMetadata/v1/")

    def test_control_characters_are_refused(self):
        with pytest.raises(ValueError):
            _clean_hls_url("https://x.test/a\nb.m3u8")


class TestTheWindow:
    def _req(self, **over):
        base = {"hls_url": "https://x.test/live.m3u8", "title": "Final", "sport": "handball",
                "event_start": _at(60), "event_end": _at(120)}
        base.update(over)
        return LiveEventRequest(**base)

    def test_a_future_window_passes_and_is_normalised_to_utc(self):
        # A date far enough ahead that the window cannot fall into the past
        # while anyone is still running this suite.
        req = self._req(event_start="2099-01-01T15:00:00+02:00", event_end="2099-01-01T16:00:00+02:00")
        assert req.event_start.tzinfo == datetime.UTC
        assert req.event_start.hour == 13

    def test_a_naive_time_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError):
            self._req(event_start="2026-09-10T15:00:00", event_end="2026-09-10T16:00:00")

    def test_the_end_must_follow_the_start(self):
        with pytest.raises(ValueError):
            self._req(event_end=_at(30))

    def test_a_day_is_too_long(self):
        with pytest.raises(ValueError):
            self._req(event_end=_at(60 + 13 * 60))

    def test_an_event_already_under_way_is_accepted(self):
        # Its capture starts on the next tick; refusing it would refuse the
        # ordinary case of scheduling after the stream has begun.
        req = self._req(event_start=_at(-10), event_end=_at(50))
        assert req.event_end > req.event_start


class TestWhoIsDue:
    def _job(self, state, start=None, lock=None):
        return {"job_id": "j", "live": {"state": state, "eventStart": start, "tickLockUntil": lock}}

    def test_a_scheduled_event_is_due_from_the_lead_in(self):
        assert live.due_jobs([self._job("scheduled", _at(5))], NOW, 300)
        assert not live.due_jobs([self._job("scheduled", _at(6))], NOW, 300)

    def test_a_running_event_is_always_due(self):
        assert live.due_jobs([self._job("live")], NOW, 300)

    def test_a_locked_event_is_skipped(self):
        assert not live.due_jobs([self._job("live", lock=_at(3))], NOW, 300)
        assert live.due_jobs([self._job("live", lock=_at(-1))], NOW, 300), "an expired lock is no lock"

    def test_a_dead_run_is_not_restarted_every_minute(self):
        recent = {"job_id": "a", "recovery": {"lastAttemptAt": _at(-5)}}
        old = {"job_id": "b", "recovery": {"lastAttemptAt": _at(-45)}}
        never = {"job_id": "c", "recovery": {}}
        assert [j["job_id"] for j in live.stalled_due([recent, old, never], NOW)] == ["b", "c"]


class TestTheSchedulersIdentity:
    def test_the_right_account_for_the_right_audience_is_admitted(self):
        claims = {"email": "sched@p.iam.gserviceaccount.com", "email_verified": True, "aud": "https://app/api/live/tick"}
        with patch.object(auth.id_token, "verify_oauth2_token", return_value=claims):
            assert auth._verify_scheduler("t", "https://app/api/live/tick", "sched@p.iam.gserviceaccount.com") \
                == "sched@p.iam.gserviceaccount.com"

    def test_another_account_is_refused_even_with_a_valid_token(self):
        claims = {"email": "someone@p.iam.gserviceaccount.com", "email_verified": True}
        with patch.object(auth.id_token, "verify_oauth2_token", return_value=claims), \
             pytest.raises(ValueError):
            auth._verify_scheduler("t", "https://app/api/live/tick", "sched@p.iam.gserviceaccount.com")

    def test_the_audience_is_checked_by_the_verifier(self):
        with patch.object(auth.id_token, "verify_oauth2_token") as verify:
            verify.return_value = {"email": "s@x", "email_verified": True}
            auth._verify_scheduler("t", "https://app/api/live/tick", "s@x")
        assert verify.call_args.kwargs["audience"] == "https://app/api/live/tick"
