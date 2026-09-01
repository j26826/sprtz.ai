"""Reranker safety.

The reranker sits in front of search, so its failure modes must degrade to the
vector order rather than to an empty or wrong result set.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import store  # noqa: E402


def _candidates(n: int = 5) -> list[dict]:
    return [
        {"moment_id": f"m{i}", "label": f"Label {i}", "description": f"Description {i}",
         "similarity": round(0.9 - i * 0.05, 4)}
        for i in range(n)
    ]


def _ranked(pairs: list[tuple[int, float]]) -> store._RerankResult:
    """Build a genuine schema object.

    A MagicMock is rejected by the isinstance guard in _rerank, which is the
    point of that guard — so the test has to produce what the model produces.
    """
    return store._RerankResult(
        ranked=[
            store._RerankedItem(index=i, relevance=score, reason=f"reason {i}")
            for i, score in pairs
        ]
    )


def _response(parsed):
    response = MagicMock()
    response.parsed = parsed
    return response


class TestRerankOrdering:
    def test_results_follow_the_model_order_not_the_vector_order(self):
        candidates = _candidates(4)
        with patch.object(store, "genai_client") as client:
            client.return_value.models.generate_content.return_value = _response(
                _ranked([(3, 0.95), (0, 0.60), (1, 0.30), (2, 0.10)])
            )
            out = store._rerank("q", candidates, limit=4)

        assert [m["moment_id"] for m in out] == ["m3", "m0", "m1", "m2"]
        assert out[0]["rerank_score"] == 0.95
        assert out[0]["rerank_reason"] == "reason 3"

    def test_limit_is_applied_after_reordering(self):
        """Truncating before the rerank would discard the winner."""
        candidates = _candidates(6)
        with patch.object(store, "genai_client") as client:
            client.return_value.models.generate_content.return_value = _response(
                _ranked([(5, 0.99), (4, 0.9), (0, 0.2)])
            )
            out = store._rerank("q", candidates, limit=2)

        assert [m["moment_id"] for m in out] == ["m5", "m4"]

    def test_vector_similarity_is_preserved_alongside_the_new_score(self):
        candidates = _candidates(2)
        with patch.object(store, "genai_client") as client:
            client.return_value.models.generate_content.return_value = _response(
                _ranked([(1, 0.8), (0, 0.4)])
            )
            out = store._rerank("q", candidates, limit=2)

        assert out[0]["similarity"] == candidates[1]["similarity"]
        assert out[0]["rerank_score"] == 0.8


class TestRerankSafety:
    def test_hallucinated_index_is_ignored(self):
        """An out-of-range index would otherwise surface the wrong moment."""
        candidates = _candidates(3)
        with patch.object(store, "genai_client") as client:
            client.return_value.models.generate_content.return_value = _response(
                _ranked([(99, 0.99), (1, 0.8)])
            )
            out = store._rerank("q", candidates, limit=3)

        assert all(m["moment_id"] in {"m0", "m1", "m2"} for m in out)
        assert out[0]["moment_id"] == "m1"

    def test_duplicate_index_is_returned_once(self):
        candidates = _candidates(3)
        with patch.object(store, "genai_client") as client:
            client.return_value.models.generate_content.return_value = _response(
                _ranked([(2, 0.9), (2, 0.8), (0, 0.5)])
            )
            out = store._rerank("q", candidates, limit=3)

        ids = [m["moment_id"] for m in out]
        assert len(ids) == len(set(ids))

    def test_candidates_the_model_omitted_are_still_returned(self):
        """Dropping them would make search silently lose results."""
        candidates = _candidates(4)
        with patch.object(store, "genai_client") as client:
            client.return_value.models.generate_content.return_value = _response(
                _ranked([(1, 0.9)])
            )
            out = store._rerank("q", candidates, limit=4)

        assert len(out) == 4
        assert out[0]["moment_id"] == "m1"
        assert out[1]["rerank_score"] is None, "unranked leftovers keep vector order behind ranked ones"

    def test_model_failure_falls_back_to_vector_order(self):
        candidates = _candidates(4)
        with patch.object(store, "genai_client") as client:
            client.return_value.models.generate_content.side_effect = RuntimeError("503")
            out = store._rerank("q", candidates, limit=3)

        assert [m["moment_id"] for m in out] == ["m0", "m1", "m2"]
        assert all(m["rerank_score"] is None for m in out)

    def test_unparseable_response_falls_back_to_vector_order(self):
        candidates = _candidates(3)
        with patch.object(store, "genai_client") as client:
            client.return_value.models.generate_content.return_value = _response(None)
            out = store._rerank("q", candidates, limit=3)

        assert [m["moment_id"] for m in out] == ["m0", "m1", "m2"]

    def test_empty_input_is_not_sent_to_the_model(self):
        with patch.object(store, "genai_client") as client:
            assert store._rerank("q", [], limit=5) == []
            client.assert_not_called()

    def test_single_candidate_skips_the_model(self):
        """Nothing to reorder, so the call would be pure cost."""
        one = _candidates(1)
        with patch.object(store, "genai_client") as client:
            assert store._rerank("q", one, limit=5) == one
            client.assert_not_called()


class TestOverfetch:
    def test_overfetch_is_capped(self):
        assert min(1000 * store.RERANK_OVERFETCH, store.RERANK_MAX_CANDIDATES) == store.RERANK_MAX_CANDIDATES

    def test_overfetch_exceeds_the_limit_so_reranking_can_change_the_answer(self):
        assert store.RERANK_OVERFETCH > 1
