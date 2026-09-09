"""Search across the desk: every layer carries scope, filters and the game.

Source pins, because the failure this guards is a layer that silently does
not pass something on — a tool that accepts a filter and drops it, a route
that a job-id pattern captures first, a ranker shown moments without their
match. None of those fail a unit test of the layer above or below.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STORE = (ROOT / "mcp" / "catalog_server" / "store.py").read_text()
SERVER = (ROOT / "mcp" / "catalog_server" / "server.py").read_text()
API = (ROOT / "api" / "app" / "routers" / "jobs.py").read_text()
PIPELINE = (ROOT / "agents" / "sprtz_agents" / "tools" / "pipeline.py").read_text()
AGENT = (ROOT / "agents" / "sprtz_agents" / "agent.py").read_text()


def _fn(src: str, name: str) -> str:
    # Ends at the next def, the next decorator, or the end of the file: the
    # per-job search is the last function in its router, and a terminator that
    # could not be end-of-file matched nothing at all.
    m = re.search(rf"def {name}\(.*?(?=\n(?:async )?def |\n@|\Z)", src, re.S)
    assert m, name
    return m.group(0)


class TestTheGameIsJoinedBeforeAnythingRanks:
    def test_join_precedes_rerank_in_the_store(self):
        body = _fn(STORE, "knn_search_moments")
        assert body.index("get_games_by_ids(") < body.index("_rerank(query"), (
            "the ranker must be shown each moment's game, so the join comes first"
        )

    def test_filters_run_on_the_joined_set(self):
        body = _fn(STORE, "knn_search_moments")
        assert body.index("get_games_by_ids(") < body.index("_filter_candidates(")

    def test_the_ranker_uses_the_game_aware_line(self):
        assert "_candidate_line(i, moment)" in _fn(STORE, "_rerank")

    def test_the_prompt_tells_the_ranker_the_game_matters(self):
        assert "which match or class it is in" in STORE


class TestScopeAndFiltersReachTheStore:
    def test_the_tool_accepts_and_forwards_them(self):
        body = _fn(SERVER, "knn_search_moments")
        assert "sport: str" in body and "job_ids: list[str]" in body
        assert "sport=sport, job_ids=job_ids or []" in body

    def test_one_chosen_game_is_a_per_job_query(self):
        body = _fn(STORE, "knn_search_moments")
        assert "len(chosen) == 1" in body and "job_id = chosen[0]" in body

    def test_filtering_over_reads(self):
        assert "fetch = min(fetch * 4" in _fn(STORE, "knn_search_moments")


class TestTheApi:
    def test_the_library_route_is_declared_before_any_job_id_route(self):
        """FastAPI matches in order; a literal 'search' would otherwise be a job id."""
        assert API.index('@router.post("/search")') < API.index('@router.get("/{job_id}")')

    def test_it_searches_everything_and_passes_the_filters(self):
        body = _fn(API, "search_library")
        assert '"job_id": ""' in body
        assert '"sport": body.sport' in body and '"job_ids": body.job_ids' in body
        assert '"rerank": body.rerank' in body, "ranking applies across the desk too"

    def test_the_per_job_route_still_ranks(self):
        assert '"rerank": body.rerank' in _fn(API, "search")


class TestTheAgent:
    def test_the_tool_passes_the_filters(self):
        body = _fn(PIPELINE, "search_moments")
        assert '"sport": sport' in body
        assert 'job_ids.split(",")' in body

    def test_the_instructions_say_what_an_empty_job_id_means(self):
        assert "every match on the desk" in AGENT
        assert "say which\n  match a moment is from" in AGENT or "say which match" in AGENT.replace("\n  ", " ")
