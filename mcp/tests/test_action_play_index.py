"""What gets indexed for semantic search, and what comes back.

The embedding is the whole feature here: a query like "double save by the
keeper" or "who scored from the wing" only matches if the outcome, the
participant and their role are in the vector. Embedding the prose alone answers
neither, because those facts sit in the structured fields beside it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store  # noqa: E402


def _moment(**kwargs) -> dict:
    base = {
        "moment_id": "m1",
        "moment_type": "double_save",
        "category": "defense",
        "label": "Double Save",
        "start_sec": 2832.0,
        "end_sec": 2841.5,
        "peak_sec": 2835.0,
        "confidence": 0.87,
        "excitement": 0.9,
        "highlight_score": 0.8,
        "summary": "#12 blue saves the seven-metre and turns the rebound over.",
        "description": "Keeper stops the seven-metre then turns the rebound over.",
        "action_result": "Save",
        "participant": "#12 blue",
        "participant_role": "Goalkeeper",
        "team1": "SWE",
        "team2": "DEN",
        "score_team1": 24,
        "score_team2": 23,
        "action_team": "SWE",
    }
    base.update(kwargs)
    return base


class TestEmbeddedText:
    def test_the_structured_fields_are_in_the_vector(self):
        text = store.action_play_text(_moment())

        for fragment in ("Double Save", "defense", "Save", "Goalkeeper", "#12 blue"):
            assert fragment in text

    def test_the_description_is_still_in_it(self):
        assert "seven-metre" in store.action_play_text(_moment())

    def test_missing_fields_do_not_leave_empty_joins(self):
        # ". . ." in an embedded string is noise the model has to look past.
        # The description keeps its own full stop; only the joins are at issue.
        text = store.action_play_text(_moment(participant="", participant_role="",
                                              action_result="", action_team="",
                                              summary=""))
        assert ".." not in text
        assert ". ." not in text
        assert text == ("Double Save. defense. "
                        "Keeper stops the seven-metre then turns the rebound over.")


class TestIndexing:
    @pytest.fixture
    def written(self):
        captured = {}

        def fake_embed(texts, task_type="RETRIEVAL_DOCUMENT"):
            captured["texts"] = texts
            return [[0.0] * 768 for _ in texts]

        batch = MagicMock()
        captured["sets"] = []
        batch.set.side_effect = lambda ref, doc: captured["sets"].append(doc)

        fake_db = MagicMock()
        fake_db.batch.return_value = batch

        with patch.object(store, "embed", fake_embed), \
             patch.object(store, "db", return_value=fake_db), \
             patch.object(store, "get_job", return_value={"ownerUid": "uid-1"}), \
             patch("google.cloud.firestore_v1.vector.Vector", lambda v: v):
            yield captured

    def test_the_action_fields_are_persisted(self, written):
        store.upsert_moments("j1", [_moment()])

        doc = written["sets"][0]
        assert doc["actionResult"] == "Save"
        assert doc["participant"] == "#12 blue"
        assert doc["participantRole"] == "Goalkeeper"

    def test_a_caller_supplied_embed_text_still_wins(self, written):
        store.upsert_moments("j1", [_moment(embed_text="explicit text")])

        assert written["texts"] == ["explicit text"]

    def test_without_one_the_fallback_indexes_the_whole_action(self, written):
        store.upsert_moments("j1", [_moment()])

        # The old fallback was label + description, which silently indexed less
        # than the caller's version and made search quality depend on who wrote.
        assert "Goalkeeper" in written["texts"][0]


class TestReadBack:
    def test_the_projection_returns_the_action_fields(self):
        out = store._moment_out({
            "momentId": "m1", "jobId": "j1", "momentType": "double_save",
            "actionResult": "Save", "participant": "#12 blue",
            "participantRole": "Goalkeeper",
        })

        assert out["action_result"] == "Save"
        assert out["participant"] == "#12 blue"
        assert out["participant_role"] == "Goalkeeper"

    def test_documents_written_before_these_fields_existed_still_read(self):
        out = store._moment_out({"momentId": "m1", "jobId": "j1"})

        assert out["action_result"] == ""
        assert out["participant"] == ""

    def test_as_action_play_scales_confidence_to_a_score(self):
        play = store.as_action_play({"confidence": 0.87, "startSec": 0, "endSec": 1})

        assert play["confidenceScore"] == 87
        assert play["type"] == "ActionPlay"


class TestTeamsAreStoredAndSearchable:
    def test_the_team_and_score_fields_are_persisted(self):
        from unittest.mock import MagicMock, patch

        captured = []
        batch = MagicMock()
        batch.set.side_effect = lambda ref, doc: captured.append(doc)
        fake_db = MagicMock()
        fake_db.batch.return_value = batch

        with patch.object(store, "embed", lambda t, task_type=None: [[0.0] * 768 for _ in t]), \
             patch.object(store, "db", return_value=fake_db), \
             patch.object(store, "get_job", return_value={"ownerUid": "u"}), \
             patch("google.cloud.firestore_v1.vector.Vector", lambda v: v):
            store.upsert_moments("j1", [_moment()])

        doc = captured[0]
        assert doc["team1"] == "SWE"
        assert doc["scoreTeam1"] == 24
        assert doc["actionTeam"] == "SWE"

    def test_an_unreadable_score_is_stored_as_null(self):
        from unittest.mock import MagicMock, patch

        captured = []
        batch = MagicMock()
        batch.set.side_effect = lambda ref, doc: captured.append(doc)
        fake_db = MagicMock()
        fake_db.batch.return_value = batch

        with patch.object(store, "embed", lambda t, task_type=None: [[0.0] * 768 for _ in t]), \
             patch.object(store, "db", return_value=fake_db), \
             patch.object(store, "get_job", return_value={"ownerUid": "u"}), \
             patch("google.cloud.firestore_v1.vector.Vector", lambda v: v):
            store.upsert_moments("j1", [_moment(score_team1=None, score_team2=None)])

        # 0 would be a scoreline nobody displayed.
        assert captured[0]["scoreTeam1"] is None

    def test_the_acting_team_is_in_the_vector(self):
        # "Denmark's goals" is a query someone types.
        assert "SWE" in store.action_play_text(_moment())

    def test_the_scoreline_is_not_in_the_vector(self):
        # "24" as text matches nothing anyone would search for, and a bare
        # number dilutes the words that do.
        text = store.action_play_text(_moment())
        assert "24" not in text
        assert "23" not in text

    def test_the_projection_carries_the_teams(self):
        play = store.as_action_play({
            "startSec": 0, "endSec": 1, "confidence": 0.5,
            "team1": "SWE", "team2": "DEN", "scoreTeam1": 24, "scoreTeam2": 23,
            "actionTeam": "SWE",
        })

        assert play["team1"] == "SWE"
        assert play["scoreTeam2"] == 23
        assert play["actionTeam"] == "SWE"

    def test_older_documents_read_back_without_teams(self):
        play = store.as_action_play({"startSec": 0, "endSec": 1, "confidence": 0.5})

        assert play["team1"] == ""
        assert play["scoreTeam1"] is None


class TestSummaryAndTitleAreIndexed:
    def test_the_moment_summary_is_in_the_vector(self):
        # "who saved the seven-metre" is the shape of a real query, and the
        # summary is the field written in those words.
        assert "saves the seven-metre" in store.action_play_text(_moment())

    def test_the_summary_is_persisted_and_read_back(self):
        out = store._moment_out({"momentId": "m", "summary": "A one-line summary."})
        assert out["summary"] == "A one-line summary."

    def test_a_moment_written_before_summaries_existed_still_reads(self):
        assert store._moment_out({"momentId": "m"})["summary"] == ""

    def test_the_game_title_is_projected(self):
        game = store._game_out({"jobId": "j1", "title": "SWE v DEN"})
        assert game["title"] == "SWE v DEN"

    def test_a_game_written_before_titles_existed_still_reads(self):
        assert store._game_out({"jobId": "j1"})["title"] == ""
