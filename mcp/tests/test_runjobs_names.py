"""An execution is addressed by its full resource name, not its bare id.

A Cloud Run Job sees itself as CLOUD_RUN_EXECUTION, the bare id, and the
recorder reported that over the full name the tick had stored. The cancel
on delete then asked the API about "project sprtz-dev-live-capture-mp5hp"
and was refused, and the recorder ran on for a deleted event.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import runjobs  # noqa: E402

JOB = "projects/p/locations/us-central1/jobs/sprtz-dev-live-capture"


class TestQualify:
    def test_a_bare_id_is_qualified_against_its_job(self):
        assert runjobs.qualify("sprtz-dev-live-capture-mp5hp", JOB) == JOB + "/executions/sprtz-dev-live-capture-mp5hp"

    def test_a_full_name_is_left_alone(self):
        full = JOB + "/executions/x"
        assert runjobs.qualify(full, JOB) == full

    def test_without_a_job_there_is_nothing_to_qualify_against(self):
        assert runjobs.qualify("x", "") == "x"
        assert runjobs.qualify("", JOB) == ""


class TestTheLiveToolsQualify:
    def test_cancel_uses_the_full_name(self):
        pytest.importorskip("fastmcp")
        from media_server import server

        with patch.object(server, "LIVE_CAPTURE_JOB", JOB), \
             patch.object(server.runjobs, "cancel") as cancel:
            out = server.cancel_live_capture("sprtz-dev-live-capture-mp5hp")
        assert out["status"] == "cancelled"
        cancel.assert_called_once_with(JOB + "/executions/sprtz-dev-live-capture-mp5hp")

    def test_status_uses_the_full_name(self):
        pytest.importorskip("fastmcp")
        from media_server import server

        with patch.object(server, "LIVE_CAPTURE_JOB", JOB), \
             patch.object(server.runjobs, "execution_state",
                          return_value={"state": "succeeded", "execution": "x"}) as state:
            server.live_capture_status("sprtz-dev-live-capture-mp5hp")
        state.assert_called_once_with(JOB + "/executions/sprtz-dev-live-capture-mp5hp")
