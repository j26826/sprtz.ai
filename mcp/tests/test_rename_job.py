"""Renaming a match: one name, in both places it is kept.

The desk showed two names for one recording — the job said what the editor had
typed and the game said what the analysis had read off the screen. A rename is
the editor saying which one they want, so it writes both, and marks the job so
the next analysis keeps the chosen name rather than composing over it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store  # noqa: E402


def _rename(title: str, *, game_exists: bool = True):
    job = MagicMock()
    game = MagicMock()
    game.get.return_value = MagicMock(exists=game_exists)
    client = MagicMock()
    client.collection.return_value.document.return_value = game
    with patch.object(store, "job_ref", return_value=job), \
         patch.object(store, "db", return_value=client):
        result = store.rename_job("j1", title)
    return result, job, game


class TestTheNameGoesToBothPlaces:
    def test_the_job_takes_the_name_and_says_whose_it_is(self):
        _, job, _ = _rename("Day 2, Arena 1")
        assert job.update.call_args.args[0] == {
            "title": "Day 2, Arena 1", "titleSource": "editor"}

    def test_the_game_record_takes_it_too(self):
        result, _, game = _rename("Day 2, Arena 1")
        assert game.update.call_args.args[0]["title"] == "Day 2, Arena 1"
        assert result["renamed_game"] is True

    def test_a_match_with_no_record_yet_is_still_renamed(self):
        """The record is written when the analysis has something to say. It
        reads titleSource off the job when it arrives, so it will carry this."""
        result, job, game = _rename("Day 2, Arena 1", game_exists=False)
        assert job.update.called
        assert not game.update.called
        assert result["renamed_game"] is False

    def test_surrounding_space_is_not_part_of_the_name(self):
        result, _, _ = _rename("  Day 2, Arena 1  ")
        assert result["title"] == "Day 2, Arena 1"


class TestWhatARenameIsNot:
    def test_an_empty_name_is_refused(self):
        """A match with no name is worse than one named after its file."""
        with pytest.raises(ValueError):
            _rename("   ")

    def test_renaming_is_not_progress(self):
        """The watchdog reads updatedAt to decide whether a run has died, and
        the editor shows a running job silent for fifteen minutes as stalled.
        Typing a new name must not make a dead run look alive."""
        _, job, _ = _rename("Day 2, Arena 1")
        assert "updatedAt" not in job.update.call_args.args[0]


class TestWhoseNameACreatedJobWears:
    def _created(self, **kwargs):
        with patch.object(store, "job_ref") as job_ref, patch.object(store, "now", return_value=0):
            store.create_job("j1", "u1", "Some name", "equestrian", "gs://b/o",
                             "o.mp4", 10, **kwargs)
        return job_ref.return_value.set.call_args.args[0]

    def test_a_typed_name_is_marked_as_the_editor_s(self):
        assert self._created(title_source="editor")["titleSource"] == "editor"

    def test_a_filename_is_not(self):
        assert self._created()["titleSource"] == "derived"

    def test_anything_else_is_read_as_a_filename(self):
        assert self._created(title_source="nonsense")["titleSource"] == "derived"
