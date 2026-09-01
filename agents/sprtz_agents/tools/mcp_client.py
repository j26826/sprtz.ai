"""Access to the two MCP servers.

The servers run as private Cloud Run services, so every call carries an OIDC
identity token minted for the target service's URL. Two access paths exist and
they are deliberately different:

* :func:`build_media_toolset` / :func:`build_catalog_toolset` expose tools to the
  LLM, for the conversational work an editor drives ("re-cut this five seconds
  earlier", "render a preview").
* :func:`call_tool` is a direct client used *inside* our own coarse-grained
  tools. Bulk data — a few hundred moments with 768-dimension embeddings — must
  never round-trip through the model's context just to reach Firestore.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import google.auth.transport.requests
import google.oauth2.id_token
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from sprtz_agents.config import get_settings

logger = logging.getLogger(__name__)

_MCP_PATH = "/mcp"


def _identity_token(audience: str) -> str:
    """Mint an OIDC token for a private Cloud Run service.

    Runs in a thread because the underlying transport is blocking and this is
    called from async tool code.
    """
    request = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(request, audience)


async def _auth_headers(base_url: str) -> dict[str, str]:
    if not base_url:
        return {}
    try:
        token = await asyncio.to_thread(_identity_token, base_url)
    except Exception:  # noqa: BLE001 - local dev has no metadata server
        logger.debug("no OIDC token available for %s; calling unauthenticated", base_url)
        return {}
    return {"Authorization": f"Bearer {token}"}


def _connection(base_url: str, headers: dict[str, str]) -> StreamableHTTPConnectionParams:
    return StreamableHTTPConnectionParams(
        url=f"{base_url.rstrip('/')}{_MCP_PATH}",
        headers=headers,
        timeout=60,
        sse_read_timeout=15 * 60,
    )


def build_media_toolset(headers: dict[str, str] | None = None) -> McpToolset | None:
    """Media tools the editor can drive conversationally."""
    settings = get_settings()
    if not settings.mcp_media_url:
        logger.warning("MCP_MEDIA_URL unset; media tools unavailable")
        return None
    return McpToolset(
        connection_params=_connection(settings.mcp_media_url, headers or {}),
        tool_filter=["cut_clip", "reframe_vertical", "burn_captions", "render_preview"],
    )


def build_catalog_toolset(headers: dict[str, str] | None = None) -> McpToolset | None:
    """Catalog tools for reading and lightly editing the job's data."""
    settings = get_settings()
    if not settings.mcp_catalog_url:
        logger.warning("MCP_CATALOG_URL unset; catalog tools unavailable")
        return None
    return McpToolset(
        connection_params=_connection(settings.mcp_catalog_url, headers or {}),
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
    raise ValueError(f"Could not decode MCP response: {text[:200]!r}")
