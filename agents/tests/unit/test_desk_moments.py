"""The desk's key moments: every layer, and the answer a running job gives.

Source pins. The failure these guard is a layer that silently does not pass
something on, and one wrong sentence — "no key moments were found" said of a
job that was 60% through finding them.
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
    m = re.search(rf"def {name}\(.*?(?=\n(?:async )?def |\n@|\Z)", src, re.S)
    assert m, name
    return m.group(0)


class TestARunningJobIsNotAnEmptyOne:
    def test_the_summary_says_so_in_words(self):
        body = _fn(PIPELINE, "get_job_summary")
        assert '"note": note' in body
        assert "still running" in body and "does not mean none were found" in body

    def test_the_desk_listing_reports_games_still_analysing(self):
        body = _fn(STORE, "list_top_moments")
        assert "RUNNING_STATUSES" in body and '"running": running' in body

    def test_the_agent_is_told_never_to_say_none_found(self):
        assert 'never "no moments were found"' in AGENT


class TestTheDeskIsWiredEndToEnd:
    def test_the_store_merges_best_first_over_the_union(self):
        body = _fn(STORE, "_merge_top")
        assert 'reverse=True' in body and "out[:limit]" in body

    def test_the_tool_is_exposed(self):
        assert re.search(r"@mcp\.tool\s*\n\s*def list_top_moments\(", SERVER)

    def test_the_route_is_declared_before_any_job_id_route(self):
        assert API.index('@router.post("/top-moments")') < API.index('@router.get("/{job_id}")')

    def test_the_agent_has_the_tool_and_knows_when_to_use_it(self):
        assert "pipeline.list_top_moments," in AGENT
        assert "not named a match" in AGENT
        body = _fn(PIPELINE, "list_top_moments")
        assert '"list_top_moments"' in body and 'job_ids.split(",")' in body
