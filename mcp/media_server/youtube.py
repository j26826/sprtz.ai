"""Publishing one moment to YouTube.

The upload is a resumable one against the Data API: an MP4 of a single moment
is tens of megabytes, well inside one request, but a resumable session is what
the API documents for a video of any size and it fails in a way that names the
step rather than returning a 400 on the whole multipart body.

Credentials are a channel's, not a service's. There is no service-account path
to YouTube — an upload belongs to a human's channel — so this holds an OAuth
client and a refresh token for that channel, entered in the editor's settings
and kept in Firestore where only these services can read it. The refresh token
is the long-lived secret; access tokens are minted per upload and never stored.

Every network call takes its transport as an argument so the whole flow can be
tested against fakes: the failure modes worth covering here are an expired
refresh token and an upload that stops halfway, and neither is reachable
against the real API from a test.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("mcp-media.youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
WATCH_URL = "https://www.youtube.com/watch?v="

# The document the editor's settings panel writes, through the API and the
# catalog. One per deployment rather than one per user: the desk is shared, and
# a channel is the desk's, not a person's.
CONFIG_COLLECTION = "config"
CONFIG_DOC = "youtube"

# What YouTube accepts. "private" is the default deliberately: publishing to the
# world is not something to do by accident from a button in a review dialog.
PRIVACY = ("private", "unlisted", "public")

# 24 = "Entertainment" in YouTube's category list, and the closest thing to
# "sport highlights" that is valid in every region. 17 is Sports, which is what
# this is, but it is rejected in a handful of regions and a failed upload is
# worse than a coarser category.
DEFAULT_CATEGORY_ID = "17"

# One chunk. A moment is seconds long, so the file is small; this is the cap
# above which this refuses rather than trying to stream a whole match out.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


class YouTubeError(RuntimeError):
    """Something the editor has to act on: bad credentials, a rejected upload."""


def credentials(db: Any = None) -> dict[str, str]:
    """The channel's credentials: the deployment's client, the desk's channel.

    The OAuth client is a deployment fact — Terraform hands it to this service
    as environment — while the refresh token is the channel someone connected,
    which changes without a deploy and so lives in Firestore. Either can be
    written in Settings, and what is stored wins: a deployment default is a
    starting point, not a ceiling.
    """
    stored = stored_credentials(db if db is not None else firestore_client())
    return {
        "clientId": stored.get("clientId") or os.environ.get("YOUTUBE_CLIENT_ID", ""),
        "clientSecret": stored.get("clientSecret") or os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
        "refreshToken": stored.get("refreshToken", ""),
    }


def stored_credentials(db: Any) -> dict[str, str]:
    """Read the stored YouTube document, or an empty dict when there is none."""
    doc = db.collection(CONFIG_COLLECTION).document(CONFIG_DOC).get()
    data = doc.to_dict() if doc.exists else None
    return data or {}


def missing_fields(creds: dict[str, str]) -> list[str]:
    """Which of the three required fields are absent, in the order asked for."""
    return [f for f in ("clientId", "clientSecret", "refreshToken") if not creds.get(f)]


def access_token(creds: dict[str, str], *, post=requests.post, timeout: int = 30) -> str:
    """Exchange the stored refresh token for an access token.

    A refresh token is revoked by changing the channel's password, by the owner
    withdrawing the app, or by six months of disuse — all of which arrive here
    as `invalid_grant`, which says nothing about which. So the error says what
    to do about it instead of repeating the code.
    """
    absent = missing_fields(creds)
    if absent:
        raise YouTubeError(f"YouTube is not configured: {', '.join(absent)} missing.")

    response = post(TOKEN_URL, data={
        "client_id": creds["clientId"],
        "client_secret": creds["clientSecret"],
        "refresh_token": creds["refreshToken"],
        "grant_type": "refresh_token",
    }, timeout=timeout)

    if response.status_code != 200:
        detail = _error_detail(response)
        if "invalid_grant" in detail:
            raise YouTubeError(
                "YouTube refused the stored refresh token. It has been revoked or "
                "has expired; reconnect the channel in Settings."
            )
        raise YouTubeError(f"YouTube would not issue an access token: {detail}")

    token = (response.json() or {}).get("access_token")
    if not token:
        raise YouTubeError("YouTube returned no access token.")
    return token


def upload(path: Path, *, token: str, title: str, description: str = "",
           privacy: str = "private", tags: list[str] | None = None,
           category_id: str = DEFAULT_CATEGORY_ID,
           post=requests.post, put=requests.put, timeout: int = 600) -> dict[str, str]:
    """Upload one MP4 and return its video id and watch URL."""
    if privacy not in PRIVACY:
        raise YouTubeError(f"privacy must be one of {', '.join(PRIVACY)}.")
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise YouTubeError(f"{size} bytes is past this service's upload ceiling.")
    if not title.strip():
        raise YouTubeError("A video needs a title.")

    body = {
        "snippet": {
            # YouTube rejects a title over 100 characters outright, and a
            # moment's summary is a sentence — so it is cut here rather than
            # losing the upload at the end of it.
            "title": title.strip()[:100],
            "description": description[:5000],
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }

    started = post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
        data=json.dumps(body),
        timeout=60,
    )
    if started.status_code not in (200, 201):
        raise YouTubeError(f"YouTube would not start the upload: {_error_detail(started)}")

    session_url = (started.headers or {}).get("Location") or (started.headers or {}).get("location")
    if not session_url:
        raise YouTubeError("YouTube started the upload without giving a session URL.")

    with path.open("rb") as handle:
        finished = put(
            session_url,
            headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
            data=handle,
            timeout=timeout,
        )
    if finished.status_code not in (200, 201):
        raise YouTubeError(f"YouTube rejected the upload: {_error_detail(finished)}")

    video = finished.json() or {}
    video_id = video.get("id")
    if not video_id:
        raise YouTubeError("YouTube accepted the upload without returning a video id.")
    return {"video_id": video_id, "url": WATCH_URL + video_id, "privacy": privacy}


def _error_detail(response: Any) -> str:
    """The API's own message where there is one, the status where there is not.

    Never the whole body: it can carry the request back, and the request that
    failed is the one with an access token in its headers.
    """
    try:
        payload = response.json() or {}
    except Exception:  # noqa: BLE001
        return f"HTTP {getattr(response, 'status_code', '?')}"
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or ""
        reasons = [e.get("reason", "") for e in error.get("errors", []) if isinstance(e, dict)]
        return " ".join(x for x in [message, *reasons] if x) or f"HTTP {response.status_code}"
    if isinstance(error, str):
        description = payload.get("error_description") or ""
        return f"{error} {description}".strip()
    return f"HTTP {getattr(response, 'status_code', '?')}"


def firestore_client():
    """The client this module reads credentials with.

    Imported lazily for the same reason everything Google is here: the import
    itself costs tens of seconds of cold start.
    """
    from google.cloud import firestore

    return firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
