"""Shared outbound clients: MCP servers and the Agent Runtime."""

from __future__ import annotations

import json
import logging
from typing import Any

import google.auth.transport.requests
import google.oauth2.id_token
import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_MCP_PATH = "/mcp"


def _identity_token(audience: str) -> str:
    return google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience
    )


def _decode(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    for line in stripped.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                return json.loads(payload)
    raise ValueError(f"Undecodable MCP response: {text[:200]!r}")


async def call_mcp(server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke one MCP tool on a private Cloud Run service."""
    settings = get_settings()
    base_url = {
        "catalog": settings.mcp_catalog_url,
        "media": settings.mcp_media_url,
    }.get(server)
    if not base_url:
        raise RuntimeError(f"No URL configured for the {server!r} MCP server.")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        headers["Authorization"] = f"Bearer {_identity_token(base_url)}"
    except Exception:  # noqa: BLE001
        logger.debug("no OIDC token for %s; calling unauthenticated", base_url)

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        response = await client.post(f"{base_url.rstrip('/')}{_MCP_PATH}", json=payload, headers=headers)
        response.raise_for_status()
        body = _decode(response.text)

    if "error" in body:
        raise RuntimeError(f"MCP {server}.{tool}: {body['error']}")

    result = body.get("result", {})
    if "structuredContent" in result:
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError:
                return {"text": block["text"]}
    return result
