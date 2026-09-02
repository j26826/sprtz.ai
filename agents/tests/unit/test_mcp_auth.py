"""Authorization on MCP calls.

Both MCP servers are private Cloud Run services. A call that arrives without an
Authorization header is refused with a 403 whose body never reaches us, so the
symptom of forgetting one is not an auth error — it is an agent whose tools all
appear to be broken, and a job that stops without saying why.

That is what happened: the toolsets were built with a static empty header dict,
so every model-facing media and catalog tool failed while the pipeline's own
direct calls kept working, because only the latter minted a token.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest

from sprtz_agents.tools import mcp_client


@pytest.fixture(autouse=True)
def _clear_cache():
    mcp_client._token_cache.clear()
    yield
    mcp_client._token_cache.clear()


@pytest.fixture
def minted():
    with patch.object(mcp_client, "_identity_token", return_value="tok-1") as mock:
        yield mock


class TestHeaderProvider:
    def test_a_toolset_call_carries_a_bearer_token(self, minted):
        provide = mcp_client._header_provider("https://media.example")

        assert provide(None) == {"Authorization": "Bearer tok-1"}

    def test_the_token_is_minted_for_the_target_service(self, minted):
        mcp_client._header_provider("https://media.example")(None)

        # Cloud Run checks the audience, so a token for the other service is as
        # useless as no token at all.
        minted.assert_called_once_with("https://media.example")

    def test_headers_are_produced_per_call_not_once(self, minted):
        # The toolsets are built at import time. A token captured then would
        # expire an hour into the deployment and never be refreshed.
        provide = mcp_client._header_provider("https://media.example")
        provide(None)
        mcp_client._token_cache.clear()
        provide(None)

        assert minted.call_count == 2

    def test_a_cached_token_is_reused_within_its_life(self, minted):
        provide = mcp_client._header_provider("https://media.example")
        provide(None)
        provide(None)
        provide(None)

        assert minted.call_count == 1, "one metadata round trip per tool call is a tax"

    def test_each_service_gets_its_own_token(self, minted):
        mcp_client._header_provider("https://media.example")(None)
        mcp_client._header_provider("https://catalog.example")(None)

        assert minted.call_count == 2
        assert set(mcp_client._token_cache) == {
            "https://media.example", "https://catalog.example",
        }

    def test_an_unmintable_token_is_reported_loudly(self, caplog):
        with patch.object(mcp_client, "_identity_token", side_effect=RuntimeError("no metadata")):
            headers = mcp_client._header_provider("https://media.example")(None)

        assert headers == {}
        # Silence here is what made this cost a debugging cycle: the call still
        # goes out, and Cloud Run's 403 looks like an unresponsive server.
        assert any(record.levelname == "WARNING" for record in caplog.records)


class TestToolsetsAreWired:
    def _settings(self, **kwargs):
        replaced = dataclasses.replace(mcp_client.get_settings(), **kwargs)
        return patch.object(mcp_client, "get_settings", return_value=replaced)

    def test_the_media_toolset_is_given_a_header_provider(self, minted):
        with self._settings(mcp_media_url="https://media.example"):
            toolset = mcp_client.build_media_toolset()

        assert toolset is not None
        assert toolset._header_provider is not None
        assert toolset._header_provider(None) == {"Authorization": "Bearer tok-1"}

    def test_the_catalog_toolset_is_given_a_header_provider(self, minted):
        with self._settings(mcp_catalog_url="https://catalog.example"):
            toolset = mcp_client.build_catalog_toolset()

        assert toolset is not None
        assert toolset._header_provider(None) == {"Authorization": "Bearer tok-1"}

    def test_no_static_headers_are_baked_into_the_connection(self, minted):
        with self._settings(mcp_media_url="https://media.example"):
            toolset = mcp_client.build_media_toolset()

        # A header dict fixed at build time is the bug this file exists for.
        assert not toolset._connection_params.headers


class TestDirectClient:
    @pytest.mark.asyncio
    async def test_the_direct_client_authenticates_too(self, minted):
        headers = await mcp_client._auth_headers("https://catalog.example")

        assert headers == {"Authorization": "Bearer tok-1"}

    @pytest.mark.asyncio
    async def test_no_url_means_no_header_rather_than_a_crash(self):
        assert await mcp_client._auth_headers("") == {}
