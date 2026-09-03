"""The upload panel's sport list, against the registry that answers for it.

The API cannot import the sport registry — the agent is packaged separately and
runs on Agent Runtime — so the list the editor sees is a deployment setting.
That makes it a second source of truth, and it failed the way those do:
equestrian was registered, the analysis could run it, and the upload panel still
offered only handball.

Reading the other package's source is a lint rather than a test, and it lives
here because here is where the registry is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sprtz_agents.sports import list_sports

_API_CONFIG = Path(__file__).resolve().parents[3] / "api" / "app" / "core" / "config.py"
_TERRAFORM = Path(__file__).resolve().parents[3] / "deploy" / "terraform" / "variables.tf"


def _api_default() -> list[str]:
    source = _API_CONFIG.read_text()
    match = re.search(r'SUPPORTED_SPORTS",\s*"([^"]*)"', source)
    assert match, "the API no longer reads SUPPORTED_SPORTS with a default"
    return [s.strip() for s in match.group(1).split(",") if s.strip()]


def _terraform_default() -> list[str]:
    source = _TERRAFORM.read_text()
    match = re.search(r'variable "supported_sports"[\s\S]*?default\s*=\s*\[([^\]]*)\]', source)
    assert match, "supported_sports is no longer declared in variables.tf"
    return re.findall(r'"([^"]+)"', match.group(1))


@pytest.mark.skipif(not _API_CONFIG.exists(), reason="api/ is not checked out")
class TestTheEditorCanSelectEverySport:
    def test_the_api_default_matches_the_registry(self):
        # A sport the analysis can run and nobody can select is a sport that
        # does not exist as far as an editor is concerned.
        assert sorted(_api_default()) == sorted(list_sports())

    def test_terraform_sets_the_same_list(self):
        # CI applies Terraform defaults, so this is what a deployment actually
        # gets — the API's own default only applies to a local run.
        assert sorted(_terraform_default()) == sorted(list_sports())

    def test_every_offered_sport_has_a_profile(self):
        from sprtz_agents.sports import get_profile

        for sport in _api_default():
            assert get_profile(sport).moment_types, f"{sport} has an empty taxonomy"
