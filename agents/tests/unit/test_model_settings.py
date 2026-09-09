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
        assert _default("analysis_model") == "gemini-3.6-flash"
        assert _default("analysis_location") == "global"
        assert _default("rerank_model") == "gemini-3.6-flash"
        assert _default("rerank_location") == "global"
        assert _default("gemini_model") == "gemini-2.5-flash", "the engine's own model is unchanged"

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
