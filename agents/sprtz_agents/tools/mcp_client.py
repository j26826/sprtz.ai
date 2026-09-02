"""Access to the two MCP servers.

The servers run as private Cloud Run services, so every call carries an OIDC
identity token minted for the target service's URL. Two access paths exist and
they are deliberately different:

* :func:`build_media_toolset` / :func:`build_catalog_toolset` expose tools to the
  LLM, for the conversational work an editor drives ("re-cut this five seconds
  earlier", "render a preview"). These are built once, at import time, so their
  credentials cannot be baked in — they come from ``header_provider``, which ADK
  calls before every listing and every tool call.
* :func:`call_tool` is a direct client used *inside* our own coarse-grained
  tools. Bulk data — a few hundred moments with 768-dimension embeddings — must
  never round-trip through the model's context just to reach Firestore.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import google.auth.transport.requests
import google.oauth2.id_token
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from sprtz_agents.config import get_settings

logger = logging.getLogger(__name__)

_MCP_PATH = "/mcp"


def _identity_token(audience: str) -> str:
    """Mint an OIDC token for a private Cloud Run service."""
    request = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(request, audience)


# Tokens are good for an hour. Minting one per call would put a blocking
# metadata round trip in front of every tool the model uses, so they are held
# for well under their lifetime and re-minted on expiry.
_TOKEN_TTL_SECONDS = 45 * 60
_token_cache: dict[str, tuple[str, float]] = {}


def _cached_identity_token(audience: str) -> str:
    cached = _token_cache.get(audience)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]
    token = _identity_token(audience)
    _token_cache[audience] = (token, now + _TOKEN_TTL_SECONDS)
    return token


def _bearer(base_url: str) -> dict[str, str]:
    """Authorization header for a private service, or nothing outside GCP.

    An empty result is a real, visible failure mode rather than a quiet
    fallback: Cloud Run answers a request with no Authorization header with a
    403 whose body never reaches the caller, so an unauthenticated call looks
    from here like a server that has stopped responding.
    """
    if not base_url:
        return {}
    try:
        return {"Authorization": f"Bearer {_cached_identity_token(base_url)}"}
    except Exception:  # a developer machine has no metadata server
        logger.warning(
            "no OIDC token for %s; calling it unauthenticated, which a private "
            "Cloud Run service will reject with 403", base_url, exc_info=True,
        )
        return {}


async def _auth_headers(base_url: str) -> dict[str, str]:
    return await asyncio.to_thread(_bearer, base_url)


def _header_provider(base_url: str) -> Callable[[Any], dict[str, str]]:
    """Per-request headers for a toolset.

    The toolsets are built once at import time, so a token baked in here would
    be a token that expires an hour into the deployment. ADK calls this before
    each listing and each tool call instead, which is also what lets the token
    be refreshed at all.
    """
    def provide(_readonly_context: Any) -> dict[str, str]:
        return _bearer(base_url)

    return provide


def _connection(base_url: str) -> StreamableHTTPConnectionParams:
    return StreamableHTTPConnectionParams(
        url=f"{base_url.rstrip('/')}{_MCP_PATH}",
        timeout=60,
        sse_read_timeout=15 * 60,
    )


def build_media_toolset() -> McpToolset | None:
    """Media tools the editor can drive conversationally."""
    settings = get_settings()
    if not settings.mcp_media_url:
        logger.warning("MCP_MEDIA_URL unset; media tools unavailable")
        return None
    return McpToolset(
        connection_params=_connection(settings.mcp_media_url),
        header_provider=_header_provider(settings.mcp_media_url),
        tool_filter=["cut_clip", "reframe_vertical", "burn_captions", "render_preview"],
    )


def build_catalog_toolset() -> McpToolset | None:
    """Catalog tools for reading and lightly editing the job's data."""
    settings = get_settings()
    if not settings.mcp_catalog_url:
        logger.warning("MCP_CATALOG_URL unset; catalog tools unavailable")
        return None
    return McpToolset(
        connection_params=_connection(settings.mcp_catalog_url),
        header_provider=_header_provider(settings.mcp_catalog_url),
        tool_filter=[
            "get_job",
            "list_moments",
            "list_clips",
            "update_clip",
            "knn_search_moments",
            "emit_event",
        ],
    )


# --- Direct client ------------------------------------------------------------


async def call_tool(server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke one MCP tool directly and return its decoded JSON result.

    ``server`` is ``"media"`` or ``"catalog"``.
    """
    import httpx

    settings = get_settings()
    base_url = {"media": settings.mcp_media_url, "catalog": settings.mcp_catalog_url}.get(server)
    if not base_url:
        raise RuntimeError(f"No URL configured for the {server!r} MCP server.")

    headers = await _auth_headers(base_url)
    headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
    )

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(15 * 60)) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}{_MCP_PATH}", json=payload, headers=headers
        )
        response.raise_for_status()
        body = _decode(response.text)

    if "error" in body:
        raise RuntimeError(f"MCP {server}.{tool} failed: {body['error']}")

    result = body.get("result", {})
    if result.get("isError"):
        raise RuntimeError(f"MCP {server}.{tool} reported an error: {result}")

    # structuredContent is the typed result; content is the text fallback.
    if "structuredContent" in result:
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError:
                return {"text": block["text"]}
    return result


def _decode(text: str) -> dict[str, Any]:
    """Decode a JSON-RPC reply that may arrive as an SSE stream."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    for line in stripped.splitlines():
        if line.startswith("data:"):
            candidate = line[len("data:") :].strip()
            if candidate:
                return json.loads(candidate)

    # Keep-alive comments and nothing else means the server was alive, working,
    # and then stopped before it answered — a container killed mid-response
    # rather than anything wrong with the encoding. Saying "could not decode"
    # and quoting the pings sends the reader after the wrong thing.
    if stripped.startswith(":"):
        pings = sum(1 for line in stripped.splitlines() if line.startswith(":"))
        raise RuntimeError(
            f"The MCP server closed the stream after {pings} keep-alive(s) "
            "without returning a result. It was still working when it stopped, "
            "so suspect the container going down mid-request — on Cloud Run, "
            "check the platform log for a memory limit."
        )
    raise ValueError(f"Could not decode MCP response: {text[:200]!r}")
