"""The store side of editing a live booking.

The API checks that the event has not started; this checks again, because the
check and the write are otherwise two moments apart and the tick fires every
minute.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store


class _Doc:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


def fake_job(data, updates):
    ref = MagicMock()
    ref.get.return_value = _Doc(data)
    ref.update.side_effect = lambda patch: updates.append(patch)
    return ref


BOOKING = {
    "kind": "live",
    "status": "scheduled",
    "title": "Ring 1",
    "live": {"eventStart": "2027-01-01T09:00:00+00:00", "eventEnd": "2027-01-01T17:00:00+00:00"},
}


class TestEditingABooking:
    def test_it_writes_the_fields_it_was_given(self):
        updates: list[dict] = []
        with patch.object(store, "job_ref", return_value=fake_job(BOOKING, updates)), \
                patch.object(store, "db", MagicMock()):
            out = store.update_live_booking(
                "job-1", event_start="2027-01-01T10:00:00+00:00",
                hls_url="https://s.example/new.m3u8", stall_minutes=9)

        written = updates[-1]
        assert written["live.eventStart"] == "2027-01-01T10:00:00+00:00"
        assert written["hlsUrl"] == "https://s.example/new.m3u8"
        assert written["live.stallMinutes"] == 9.0
        assert "live.eventEnd" not in written
        assert out["changed"]

    def test_a_new_title_is_the_editors_and_is_marked_as_such(self):
        # Same rule as rename_job: a name a person typed beats the composed one.
        updates: list[dict] = []
        game = MagicMock()
        game.get.return_value = _Doc(None)
        db = MagicMock()
        db.return_value.collection.return_value.document.return_value = game
        with patch.object(store, "job_ref", return_value=fake_job(BOOKING, updates)), \
                patch.object(store, "db", db):
            store.update_live_booking("job-1", title="Ring 2")

        assert updates[-1]["title"] == "Ring 2"
        assert updates[-1]["titleSource"] == "editor"

    def test_it_never_touches_updated_at(self):
        # The watchdog reads it; editing a booking is not a sign of life.
        updates: list[dict] = []
        with patch.object(store, "job_ref", return_value=fake_job(BOOKING, updates)), \
                patch.object(store, "db", MagicMock()):
            store.update_live_booking("job-1", event_end="2027-01-01T18:00:00+00:00")

        assert "updatedAt" not in updates[-1]

    def test_nothing_asked_for_is_not_a_write(self):
        updates: list[dict] = []
        with patch.object(store, "job_ref", return_value=fake_job(BOOKING, updates)), \
                patch.object(store, "db", MagicMock()):
            out = store.update_live_booking("job-1")

        assert updates == []
        assert out["changed"] == []


class TestWhatTheStoreRefuses:
    def _refuse(self, job):
        updates: list[dict] = []
        with patch.object(store, "job_ref", return_value=fake_job(job, updates)), \
                patch.object(store, "db", MagicMock()):
            with pytest.raises(ValueError):
                store.update_live_booking("job-1", title="New")
        assert updates == []

    def test_an_event_that_is_recording(self):
        self._refuse({**BOOKING, "status": "recording"})

    def test_an_event_that_has_finished(self):
        self._refuse({**BOOKING, "status": "complete"})

    def test_a_match_that_is_not_live(self):
        self._refuse({**BOOKING, "kind": "upload"})

    def test_a_job_that_is_not_there(self):
        with patch.object(store, "job_ref", return_value=fake_job(None, [])):
            with pytest.raises(KeyError):
                store.update_live_booking("job-1", title="New")
