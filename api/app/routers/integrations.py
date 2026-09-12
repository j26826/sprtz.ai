"""Connecting the desk to a YouTube channel.

Publishing a video is not something a service account can do: a video belongs
to a channel, and a channel belongs to a person. So the desk holds an OAuth
client — a deployment fact, passed in by Terraform — and a refresh token for
one channel, which someone grants here by approving the app once.

The refresh token never reaches the browser. `GET /api/integrations/youtube`
says whether each part is present and what the channel is called; the token
itself goes from Google to this service to Firestore, and out only to the media
service that uploads with it.
"""

from __future__ import annotations

import logging
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core import clients
from app.core.auth import CallerIdentity, current_user
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/integrations", tags=["integrations"])

CONFIG_NAME = "youtube"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"
# Uploading is all this asks for. `youtube.readonly` comes with it only so the
# connected channel can be named back to the editor — a dialog that says
# "connected" without saying to what is a dialog nobody can check.
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)


class YouTubeSettings(BaseModel):
    """What the settings panel can write.

    Every field is optional because this is a merge: pasting a refresh token
    must not clear the client it belongs to. An empty string is not "unset" —
    it is how a field is cleared, which is what Disconnect does.
    """

    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=200)
    refresh_token: str | None = Field(default=None, max_length=500)
    privacy: str | None = Field(default=None, pattern="^(private|unlisted|public)$")


async def _config() -> dict:
    found = await clients.call_mcp("catalog", "get_config", {"name": CONFIG_NAME})
    if found.get("status") != "success":
        logger.warning("could not read the youtube config: %s", found.get("error"))
        return {}
    return found.get("config") or {}


def _client(stored: dict, settings: Settings) -> tuple[str, str]:
    """The OAuth client in force: what was typed in, else the deployment's."""
    return (
        stored.get("clientId") or settings.youtube_client_id,
        stored.get("clientSecret") or settings.youtube_client_secret,
    )


@router.get("/youtube")
async def youtube_status(
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """What is configured, without saying what any of it is."""
    stored = await _config()
    client_id, client_secret = _client(stored, settings)
    return {
        "client_configured": bool(client_id and client_secret),
        # True when the deployment supplied the client, so the panel can say
        # "from the deployment" rather than showing two empty boxes.
        "client_from_deployment": bool(
            settings.youtube_client_id and not stored.get("clientId")
        ),
        "connected": bool(stored.get("refreshToken")),
        "channel_title": stored.get("channelTitle", ""),
        "privacy": stored.get("privacy", "private"),
        "redirect_uri": settings.youtube_redirect_uri,
        # A connect flow needs somewhere for Google to send the editor back to,
        # and that somewhere has to be registered on the client by hand.
        "can_connect": bool(client_id and client_secret and settings.youtube_redirect_uri),
    }


@router.put("/youtube")
async def save_youtube_settings(
    body: YouTubeSettings,
    user: CallerIdentity = Depends(current_user),
) -> dict:
    """Write the parts of the YouTube configuration someone typed in."""
    values = {
        key: value
        for key, value in (
            ("clientId", body.client_id),
            ("clientSecret", body.client_secret),
            ("refreshToken", body.refresh_token),
            ("privacy", body.privacy),
        )
        if value is not None
    }
    if not values:
        return {"status": "success", "saved": []}
    # A refresh token pasted by hand belongs to whatever channel granted it,
    # and this cannot know which. Clearing the remembered name is better than
    # showing the previous channel's beside a new channel's token.
    if "refreshToken" in values:
        values["channelTitle"] = ""
    saved = await clients.call_mcp("catalog", "set_config",
                                   {"name": CONFIG_NAME, "values": values})
    if saved.get("status") != "success":
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=saved.get("error") or "Could not save the settings.")
    return {"status": "success", "saved": sorted(values)}


@router.delete("/youtube")
async def disconnect_youtube(user: CallerIdentity = Depends(current_user)) -> dict:
    """Forget the channel, keeping the client it was connected with."""
    cleared = await clients.call_mcp("catalog", "clear_config", {
        "name": CONFIG_NAME, "fields": ["refreshToken", "channelTitle"],
    })
    if cleared.get("status") != "success":
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=cleared.get("error") or "Could not disconnect.")
    return {"status": "success", "connected": False}


@router.get("/youtube/auth-url")
async def youtube_auth_url(
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Where to send the editor to approve the channel.

    `access_type=offline` with `prompt=consent` is what makes Google return a
    refresh token: without the prompt it returns one only the first time a
    given client is approved, so reconnecting after a disconnect would hand
    back an access token that expires in an hour and nothing that outlives it.
    """
    stored = await _config()
    client_id, client_secret = _client(stored, settings)
    if not (client_id and client_secret):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No OAuth client is configured for YouTube.",
        )
    if not settings.youtube_redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No redirect URI is configured for YouTube.",
        )

    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": settings.youtube_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    })
    return {"url": f"{AUTH_URL}?{query}"}


@router.get("/youtube/callback", include_in_schema=False)
async def youtube_callback(
    code: str = "",
    error: str = "",
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Where Google sends the editor back to, and where the token is kept.

    Not behind `current_user`: this is a top-level browser navigation from
    Google, which carries no Authorization header and cannot be given one. What
    stands in for it is the code — single-use, minted for this deployment's own
    client, and worthless without the client secret held here.
    """
    if error or not code:
        return _page("YouTube was not connected", error or "No authorisation code came back.")

    stored = await _config()
    client_id, client_secret = _client(stored, settings)
    if not (client_id and client_secret and settings.youtube_redirect_uri):
        return _page("YouTube was not connected", "This deployment has no OAuth client.")

    async with httpx.AsyncClient(timeout=30) as http:
        exchanged = await http.post(TOKEN_URL, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": settings.youtube_redirect_uri,
            "grant_type": "authorization_code",
        })
        if exchanged.status_code != 200:
            logger.warning("youtube code exchange failed: %s", exchanged.status_code)
            return _page("YouTube was not connected",
                         "Google would not exchange the authorisation code.")
        payload = exchanged.json()
        refresh = payload.get("refresh_token", "")
        if not refresh:
            # Google returns one only with consent freshly granted. Approving a
            # client that is already approved comes back without it, and a
            # connection with no refresh token lasts an hour.
            return _page("YouTube was not connected",
                         "Google returned no refresh token. Remove this app's access in your "
                         "Google account and connect again.")
        title = await _channel_title(http, payload.get("access_token", ""))

    saved = await clients.call_mcp("catalog", "set_config", {
        "name": CONFIG_NAME,
        "values": {"refreshToken": refresh, "channelTitle": title},
    })
    if saved.get("status") != "success":
        return _page("YouTube was not connected", "The channel could not be saved.")
    return _page("YouTube connected", f"Publishing to {title or 'your channel'}. "
                                      "You can close this tab.")


async def _channel_title(http: httpx.AsyncClient, token: str) -> str:
    """The connected channel's name, best effort: it is a label, not a fact."""
    if not token:
        return ""
    try:
        response = await http.get(CHANNEL_URL, params={"part": "snippet", "mine": "true"},
                                  headers={"Authorization": f"Bearer {token}"})
        items = (response.json() or {}).get("items") or []
        return (items[0].get("snippet") or {}).get("title", "") if items else ""
    except Exception:
        logger.warning("could not read the connected channel's name", exc_info=True)
        return ""


def _page(heading: str, detail: str) -> HTMLResponse:
    """A plain page, because this tab is Google's and not the editor's app."""
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>Arenos</title>"
        "<body style=\"font:14px/1.6 system-ui;margin:15vh auto;max-width:32rem;padding:0 1rem\">"
        f"<h1 style=\"font-size:18px\">{_escape(heading)}</h1>"
        f"<p>{_escape(detail)}</p>"
    )


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
