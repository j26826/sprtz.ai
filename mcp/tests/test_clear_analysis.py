"""Clearing a job's findings also clears the flag that stopped the last run.

A cancel is a flag the stages read between steps, and it outlived the run it
stopped: a job cancelled at 08:00 answered every later re-run with
"Cancelled before the analysis started" a second after the editor asked for
one, with nothing on the row to say why.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("google.cloud.firestore")

from catalog_server import store  # noqa: E402


@pytest.fixture
def job():
    ref = MagicMock()
    ref.collection.return_value = MagicMock()
    game = MagicMock()
    game.get.return_value.exists = False
    with patch.object(store, "job_ref", return_value=ref), \
         patch.object(store, "game_ref", return_value=game), \
         patch.object(store, "_delete_collection", return_value=0):
        yield ref


class TestClearAnalysis:
    def test_it_clears_the_cancel_flag(self, job):
        store.clear_analysis("j1")
        patch_written = job.update.call_args.args[0]
        assert patch_written["cancelRequested"] is False

    def test_it_still_resets_the_run(self, job):
        store.clear_analysis("j1")
        patch_written = job.update.call_args.args[0]
        assert patch_written["status"] == "uploaded"
        assert patch_written["stage"] == "ingest"
        assert patch_written["progress"] == 0
        assert patch_written["counts"] == {"moments": 0}
        assert patch_written["error"] is None
