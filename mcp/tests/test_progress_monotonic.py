"""Progress only ever goes forward.

Playback and analysis run concurrently and occupy different bands of the bar —
5-20 and 20-80 — so whichever finishes last writes its number last. An encode
that ended after the analysis had reached 80% pulled the bar back to 20, which
reads as the run having restarted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store  # noqa: E402


@pytest.fixture
def job_at():
    """A job sitting at some progress, and the patch it receives."""
    def _make(current):
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {"progress": current}
        ref = MagicMock()
        ref.get.return_value = snapshot
        return ref, patch.object(store, "job_ref", return_value=ref)
    return _make


def _written(ref):
    return ref.update.call_args.args[0]


class TestForwardOnly:
    def test_a_higher_number_is_written(self, job_at):
        ref, patched = job_at(35)
        with patched:
            store.update_job_status("j", "", progress=60)

        assert _written(ref)["progress"] == 60

    def test_a_lower_number_is_ignored(self, job_at):
        # The transcode stage finishing writes 20 whenever it lands, which is
        # after the analysis has passed it on any match worth analysing.
        ref, patched = job_at(80)
        with patched:
            store.update_job_status("j", "", progress=20)

        assert _written(ref)["progress"] == 80

    def test_the_same_number_is_harmless(self, job_at):
        ref, patched = job_at(45)
        with patched:
            store.update_job_status("j", "", progress=45)

        assert _written(ref)["progress"] == 45

    def test_completion_is_never_held_back(self, job_at):
        ref, patched = job_at(95)
        with patched:
            store.update_job_status("j", "ready", progress=100)

        assert _written(ref)["progress"] == 100


class TestResetting:
    def test_zero_starts_the_bar_over(self, job_at):
        # Re-analysing a finished job must be able to put the bar back, or it
        # would read as complete for the whole of the second run.
        ref, patched = job_at(100)
        with patched:
            store.update_job_status("j", "analyzing", progress=0)

        assert _written(ref)["progress"] == 0

    def test_a_job_with_no_progress_yet_takes_the_first_value(self, job_at):
        ref, patched = job_at(None)
        with patched:
            store.update_job_status("j", "", progress=5)

        assert _written(ref)["progress"] == 5


class TestUnrelatedFields:
    def test_progress_is_left_alone_when_none_is_given(self, job_at):
        ref, patched = job_at(40)
        with patched:
            store.update_job_status("j", "analyzing", stage="analysis")

        assert "progress" not in _written(ref)

    def test_an_empty_status_does_not_blank_the_field(self, job_at):
        # Progress arrives once per segment and has no opinion about status.
        ref, patched = job_at(40)
        with patched:
            store.update_job_status("j", "", progress=50)

        assert "status" not in _written(ref)
