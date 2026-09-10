"""Context links: the editor tells grounding which event this is.

A recording grounded to the right show and the wrong class, and every field
it filled looked plausible. These are the rules that stop that recurring — the
links steer the search and the answer is held to them — and the pins that keep
the new fields from being dropped at the boundaries, which has happened before.
"""

import re
from pathlib import Path

from sprtz_agents.tools import grounding

ROOT = Path(__file__).resolve().parents[3]
STORE = (ROOT / "mcp" / "catalog_server" / "store.py").read_text()
SERVER = (ROOT / "mcp" / "catalog_server" / "server.py").read_text()
API = (ROOT / "api" / "app" / "routers" / "jobs.py").read_text()
PIPELINE = (ROOT / "agents" / "sprtz_agents" / "tools" / "pipeline.py").read_text()


class TestWhatAnEquipeLinkNames:
    def test_every_page_kind_yields_its_id(self):
        urls = [
            "https://online.equipe.com/shows/73895",
            "https://online.equipe.com/meeting_classes/464539/score_sheet",
            "https://online.equipe.com/class_sections/435907",
            "https://online.equipe.com/startlists/1022523",
            "https://online.equipe.com/en/starts/7197151",   # locale prefix
        ]
        assert grounding.equipe_ids(urls) == {
            "shows:73895", "meeting_classes:464539", "class_sections:435907",
            "startlists:1022523", "starts:7197151",
        }

    def test_a_page_that_is_not_equipe_names_nothing(self):
        assert grounding.equipe_ids(["https://example.com/results/73895", "", None]) == set()


class TestTheLinksSteerTheSearch:
    def test_no_links_adds_nothing(self):
        assert grounding.context_lines([]) == ""

    def test_links_are_quoted_and_made_authoritative(self):
        block = grounding.context_lines(["https://online.equipe.com/shows/73895"])
        assert "https://online.equipe.com/shows/73895" in block
        assert "authoritative" in block and "answer only" in block

    def test_capped_at_ten(self):
        block = grounding.context_lines([f"https://x.test/{i}" for i in range(14)])
        assert block.count("https://x.test/") == 10


class TestTheAnswerIsHeldToThem:
    """The fail-closed rule, exercised on its own pieces."""

    def _refused(self, context, cited, answer_url=""):
        wanted = {i for i in grounding.equipe_ids(context) if i.startswith("shows:")}
        if not wanted:
            return False
        got = grounding.equipe_ids([*cited, answer_url])
        return not (wanted & got)

    def test_a_citation_from_the_supplied_show_passes(self):
        assert not self._refused(["https://online.equipe.com/shows/73895"],
                                 ["https://online.equipe.com/shows/73895"])

    def test_a_citation_from_a_different_show_is_refused(self):
        """The exact failure: right championship, wrong year's show."""
        assert self._refused(["https://online.equipe.com/shows/73895"],
                             ["https://online.equipe.com/shows/57906"])

    def test_the_answers_own_url_counts_as_a_citation(self):
        assert not self._refused(["https://online.equipe.com/shows/73895"], [],
                                 answer_url="https://online.equipe.com/shows/73895")

    def test_a_non_show_link_does_not_gate(self):
        """A class link steers; only a show link is checked against, because a
        class page's citations are its show's results pages."""
        assert not self._refused(["https://online.equipe.com/meeting_classes/1"],
                                 ["https://online.equipe.com/shows/99"])


class TestTheApiAcceptsThem:
    def test_both_ingest_models_carry_context_urls(self):
        for cls in ("CreateJobRequest", "RegisterSourceRequest"):
            body = re.search(rf"class {cls}\(BaseModel\):.*?(?=\nclass |\n@router|\ndef )", API, re.S)
            assert body and "context_urls" in body.group(0), f"{cls} has no context_urls"

    def test_every_door_passes_them_to_the_catalog(self):
        """Five doors: the upload, the gs:// registration, the HLS download,
        the live event, and the edit. Counted by site rather than in total, so
        a sixth call that forgot them could not hide behind one that did not."""
        # To the call's closing brace, not the first one: the from-source dict
        # holds an f-string with braces of its own, which cut the earlier match
        # short of the field it was looking for.
        create_dicts = re.findall(r'"create_job",\s*\{(.*?)\n\s*\},\s*\)', API, re.S)
        assert len(create_dicts) == 4, "expected the upload, from-source, from-hls and live calls"
        for body in create_dicts:
            assert '"context_urls": body.context_urls' in body
        patch = re.search(r'"update_job_context",\s*\{(.*?)\}', API, re.S)
        assert patch and '"context_urls": body.context_urls' in patch.group(1)

    def test_they_are_validated_not_trusted(self):
        fn = re.search(r"def _clean_context_urls\(.*?(?=\nclass )", API, re.S).group(0)
        assert "https?://" in fn, "non-http strings would be quoted into a prompt"
        assert "_MAX_CONTEXT_URLS" in fn

    def test_there_is_a_door_to_edit_them(self):
        assert '@router.patch("/{job_id}/context")' in API
        assert '"update_job_context"' in API


class TestTheBoundaryThatDropsFields:
    def test_stored_on_create(self):
        body = re.search(r"def create_job\(.*?(?=\ndef )", STORE, re.S).group(0)
        assert '"contextUrls"' in body

    def test_the_update_exists_and_is_a_tool(self):
        assert "def update_job_context(" in STORE
        assert re.search(r"@mcp\.tool\s*\n\s*def update_job_context\(", SERVER)

    def test_create_tool_accepts_and_forwards_them(self):
        body = re.search(r"def create_job\(.*?(?=\n@mcp|\ndef )", SERVER, re.S).group(0)
        assert "context_urls" in body and "context_urls or []" in body

    def test_the_game_records_what_the_search_was_told_and_asked(self):
        body = re.search(r"def upsert_game\(.*?(?=\ndef )", STORE, re.S).group(0)
        assert '"contextUrls"' in body and '"groundingQueries"' in body


class TestItIsWired:
    def test_the_pipeline_reads_them_off_the_job_and_hands_them_to_grounding(self):
        assert 'context_urls=job.get("contextUrls") or []' in PIPELINE
        assert "context_urls=context_urls or []" in PIPELINE

    def test_a_refusal_is_surfaced_not_swallowed(self):
        assert "answer did not come from" in PIPELINE
