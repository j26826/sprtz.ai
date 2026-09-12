"""Both servers must answer /healthz, because Cloud Run judges them by it.

This is a lint rather than a behaviour test, and it exists because the failure
is silent in every other check. Removing the clip tools from the catalog server
sliced the region to the next `@mcp.tool` and took the health route with it —
which sat between two of them. The image built, the container started, the
server answered `/mcp` correctly, and every startup probe got a 404: twelve
minutes of "Still modifying..." and a failed deploy, with nothing in the
application log that looked like an error.

Reading the source rather than importing the app: importing either server pulls
in fastmcp and the Google client libraries, which is tens of seconds and a set
of credentials this suite does not have.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SERVERS = {
    "catalog": Path(__file__).resolve().parents[1] / "catalog_server" / "server.py",
    "media": Path(__file__).resolve().parents[1] / "media_server" / "server.py",
}


@pytest.mark.parametrize("name", sorted(SERVERS))
class TestEveryServerHasAHealthRoute:
    def test_it_registers_healthz(self, name):
        source = SERVERS[name].read_text()
        assert '@mcp.custom_route("/healthz", methods=["GET"])' in source, (
            f"{name} has no /healthz route; its startup probe would 404 for ever"
        )

    def test_the_handler_is_the_next_thing_after_the_decorator(self, name):
        # A decorator left behind by an edit that took its function is the same
        # failure wearing a different face.
        source = SERVERS[name].read_text()
        after = source.split('@mcp.custom_route("/healthz", methods=["GET"])', 1)[1]
        assert re.match(r"\s*async def healthz\(", after), (
            f"{name}'s /healthz decorator does not decorate a handler"
        )

    def test_it_answers_with_its_own_name(self, name):
        # Two services behind one load balancer, and a probe that cannot say
        # which one answered is a probe nobody can debug.
        source = SERVERS[name].read_text()
        assert f'"service": "mcp-{name}"' in source
