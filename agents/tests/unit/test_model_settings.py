"""Analysis and reranking on their own model and location.

The newer Flash generation is served only through Vertex's `global` location
in this project — every regional endpoint returns 404 for it — so a model and
the location it is called from have to move together, and the engine's own
model, grounding and embeddings have to stay put. These pin every layer of
that, because a layer that silently keeps the old model fails no unit test.
"""

import re
from pathlib import Path

from sprtz_agents.config import Settings

ROOT = Path(__file__).resolve().parents[3]
ANALYSIS = (ROOT / "agents" / "sprtz_agents" / "tools" / "analysis.py").read_text()
PIPELINE = (ROOT / "agents" / "sprtz_agents" / "tools" / "pipeline.py").read_text()
GROUNDING = (ROOT / "agents" / "sprtz_agents" / "tools" / "grounding.py").read_text()
AGENT = (ROOT / "agents" / "sprtz_agents" / "agent.py").read_text()
STAGES = (ROOT / "agents" / "sprtz_agents" / "sub_agents" / "stages.py").read_text()
STORE = (ROOT / "mcp" / "catalog_server" / "store.py").read_text()
BUILD = (ROOT / "deploy" / "cloudbuild.yaml").read_text()
VARS = (ROOT / "deploy" / "terraform" / "variables.tf").read_text()
OUTPUTS = (ROOT / "deploy" / "terraform" / "outputs.tf").read_text()
CLOUD_RUN = (ROOT / "deploy" / "terraform" / "cloud_run.tf").read_text()


def _default(var: str) -> str:
    m = re.search(rf'variable "{var}" \{{.*?default\s*=\s*"([^"]+)"', VARS, re.S)
    assert m, var
    return m.group(1)


class TestTheSettingsFallBackToTheEngine:
    def test_unset_means_the_engine_model_and_region(self, monkeypatch):
        for k in ("SPRTZ_ANALYSIS_MODEL", "SPRTZ_ANALYSIS_LOCATION", "SPRTZ_MODEL", "GOOGLE_CLOUD_LOCATION"):
            monkeypatch.delenv(k, raising=False)
        s = Settings()
        assert (s.analysis_model, s.analysis_location) == (s.model, s.location)

    def test_set_means_independent_of_the_engine(self, monkeypatch):
        monkeypatch.setenv("SPRTZ_MODEL", "gemini-2.5-flash")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        monkeypatch.setenv("SPRTZ_ANALYSIS_MODEL", "gemini-3.6-flash")
        monkeypatch.setenv("SPRTZ_ANALYSIS_LOCATION", "global")
        s = Settings()
        assert (s.analysis_model, s.analysis_location) == ("gemini-3.6-flash", "global")
        assert (s.model, s.location) == ("gemini-2.5-flash", "us-central1"), "the engine did not move"


class TestTheAnalysisCallUsesThem:
    def test_the_client_is_built_on_the_analysis_location(self):
        assert "location=settings.analysis_location" in ANALYSIS
        assert "location=settings.location" not in ANALYSIS

    def test_the_segment_call_names_the_analysis_model(self):
        assert "model=settings.analysis_model" in ANALYSIS
        assert "model=settings.model" not in ANALYSIS

    def test_the_status_message_says_which_model_is_analysing(self):
        assert "with {settings.analysis_model}" in PIPELINE

    def test_grounding_and_judgement_stay_on_the_engine_model(self):
        assert GROUNDING.count("model=settings.model") == 2
        assert "model=get_settings().model" in PIPELINE


class TestDeploymentCarriesThePair:
    def test_the_engine_receives_both(self):
        assert '--env "SPRTZ_ANALYSIS_MODEL=$$(tf analysis_model)"' in BUILD
        assert '--env "SPRTZ_ANALYSIS_LOCATION=$$(tf analysis_location)"' in BUILD

    def test_terraform_outputs_both(self):
        assert 'output "analysis_model"' in OUTPUTS and 'output "analysis_location"' in OUTPUTS

    def test_terraform_defaults(self):
        # Everything the engine asks a model is on 3.8 Flash, which is served
        # only through `global` in this project. The analysis was on 2.5 after
        # a day on 3.6 went badly on the equestrian footage; 3.8 is a different
        # model and is being tried on its own evidence. The embeddings are not
        # here on purpose: the width has to equal the Firestore vector index
        # exactly, and that fails at read time rather than at write.
        assert _default("gemini_model") == "gemini-3.8-flash"
        assert _default("gemini_location") == "global"
        assert _default("analysis_model") == "gemini-3.8-flash"
        assert _default("analysis_location") == "global"
        assert _default("rerank_model") == "gemini-3.8-flash"
        assert _default("rerank_location") == "global"
        assert _default("embedding_model") == "gemini-embedding-001", "embeddings do not move"

    def test_a_global_only_model_is_never_paired_with_a_region(self):
        # Every regional endpoint 404s for the post-2.5 Flash models here;
        # the pair is the guard, and this is the shape it must keep. The
        # engine's own model is in this list now: it had no location of its
        # own, so pointing it at a global-only model would have 404'd the root
        # agent, every stage and the grounding with nothing to set.
        for model_var, location_var in (("gemini_model", "gemini_location"),
                                        ("analysis_model", "analysis_location"),
                                        ("rerank_model", "rerank_location")):
            model, location = _default(model_var), _default(location_var)
            if not model.startswith("gemini-2.5"):
                assert location == "global", f"{model} is only served globally"


class TestTheEngineModelHasALocationOfItsOwn:
    """`GOOGLE_CLOUD_LOCATION` cannot carry it.

    Agent Runtime injects that itself and refuses a deployment that sets it, so
    the root agent, the stages and the grounding had no way to reach a model
    served only through `global` — ADK builds their client out of the
    environment. The location is given to ADK as a pinned client instead, which
    is what ADK's own documentation prescribes for this.
    """

    def test_the_setting_falls_back_to_the_engine_region(self, monkeypatch):
        monkeypatch.delenv("SPRTZ_MODEL_LOCATION", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        assert Settings().model_location == "us-central1"

    def test_and_wins_when_it_is_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        monkeypatch.setenv("SPRTZ_MODEL_LOCATION", "global")
        s = Settings()
        assert (s.model_location, s.location) == ("global", "us-central1")

    def test_a_differing_location_is_pinned_onto_the_client(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ci")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        monkeypatch.setenv("SPRTZ_MODEL_LOCATION", "global")
        monkeypatch.setenv("SPRTZ_MODEL", "gemini-3.8-flash")
        from sprtz_agents.config import get_settings
        from sprtz_agents.models import PinnedGemini, gemini
        get_settings.cache_clear()
        built = gemini()
        assert isinstance(built, PinnedGemini)
        assert built.api_client._api_client.location == "global"
        get_settings.cache_clear()

    def test_the_same_location_changes_nothing(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ci")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        monkeypatch.delenv("SPRTZ_MODEL_LOCATION", raising=False)
        from sprtz_agents.config import get_settings
        from sprtz_agents.models import PinnedGemini, gemini
        get_settings.cache_clear()
        assert not isinstance(gemini(), PinnedGemini)
        get_settings.cache_clear()

    def test_the_agents_build_their_model_through_it(self):
        # Not `Gemini(model=...)` directly, or the location cannot reach them.
        assert "model=gemini()" in AGENT
        assert "return gemini()" in STAGES

    def test_grounding_asks_from_the_model_location(self):
        assert "location=settings.model_location" in GROUNDING
        assert "location=settings.location)" not in GROUNDING

    def test_the_deployment_carries_it(self):
        assert '--env "SPRTZ_MODEL_LOCATION=$$(tf gemini_location)"' in BUILD
        assert 'output "gemini_location"' in OUTPUTS

    def test_the_catalog_receives_the_rerank_pair(self):
        assert 'value = var.rerank_model' in CLOUD_RUN
        assert '"RERANK_LOCATION"' in CLOUD_RUN and 'value = var.rerank_location' in CLOUD_RUN


class TestRerankingMovesAndEmbeddingsDoNot:
    def test_the_reranker_has_its_own_client_and_location(self):
        assert 'RERANK_LOCATION = os.environ.get("RERANK_LOCATION"' in STORE
        assert "def rerank_client()" in STORE
        body = re.search(r"def _rerank\(.*?(?=\ndef )", STORE, re.S).group(0)
        assert "rerank_client().models.generate_content(" in body
        assert "genai_client()" not in body

    def test_embeddings_keep_the_regional_client(self):
        body = re.search(r"def embed\(.*?(?=\ndef )", STORE, re.S).group(0)
        assert "genai_client()" in body and "rerank_client()" not in body


class TestResponseSchemasAreJsonSchema:
    """Every structured call hands Gemini a JSON Schema, never the Pydantic class.

    Given the class, the SDK converts it to Vertex's own Schema type, and
    gemini-3.6-flash under that constraint writes a float as an unbounded run
    of digits — ``"discipline_confidence": 0.0000…`` for thirty thousand
    characters until the token cap, and the JSON never closes. Nine of sixteen
    segments failed that way on the first run, and the reranker degrades to
    vector order without a word. The same shape as ``response_json_schema``
    answers cleanly in seconds. No unit test can see the difference, so this
    reads the source.
    """

    SITES = {"analysis.py": ANALYSIS, "pipeline.py": PIPELINE, "store.py": STORE}

    def test_no_call_passes_the_class(self):
        for name, src in self.SITES.items():
            code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
            assert "response_schema=" not in code, f"{name} still passes a Pydantic class"

    def test_every_structured_call_passes_json_schema(self):
        for name, src in self.SITES.items():
            assert ".model_json_schema()" in src, f"{name} has no response_json_schema"

    def test_the_answer_is_parsed_from_the_text(self):
        # ``response.parsed`` is only filled in for the class form, so a site
        # that still reads it would return nothing on every call.
        assert "getattr(response, \"parsed\"" not in PIPELINE
        assert "response.parsed" not in STORE
