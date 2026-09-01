"""Cloud CDN signed cookies for HLS playback.

An HLS playlist references its segments relatively, so a player resolving
`v0_00000.ts` against the playlist URL drops any query string the playlist
carried. A signed URL on the playlist alone therefore authorises the playlist
and nothing else, and every segment request arrives unsigned.

So playback is authorised with a **signed cookie**, which the browser attaches
to the playlist, the variant playlists and every one of the several thousand
segments without the player knowing anything about it.

A cookie only reaches the CDN if the CDN host falls under the cookie's domain.
That is satisfied here by construction: the load balancer serves the CDN from
the app's own hostname (``/jobs/*`` routes to the HLS bucket), so the cookie is
same-origin. It is worth knowing why that matters — on separate ``*.run.app``
hostnames it would be impossible, since ``run.app`` is on the Public Suffix
List and no cookie can span two services there.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

# Cloud CDN reads this exact name; it is not configurable.
COOKIE_NAME = "Cloud-CDN-Cookie"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_key(key_value: str) -> bytes:
    """Terraform stores the key base64url-encoded, as the API expects."""
    padding = "=" * (-len(key_value) % 4)
    return base64.urlsafe_b64decode(key_value + padding)


def sign_cookie(
    *,
    url_prefix: str,
    key_name: str,
    key_value: str,
    expires_at: int,
) -> str:
    """Return a Cloud-CDN-Cookie value authorising everything under ``url_prefix``.

    ``url_prefix`` must include the scheme and host, and should end at a path
    boundary — Cloud CDN grants access to every URL that starts with it, so a
    prefix ending mid-segment-name would authorise more than intended.
    """
    if not url_prefix.startswith(("http://", "https://")):
        raise ValueError("url_prefix must include the scheme and host.")

    encoded_prefix = _b64url(url_prefix.encode())
    to_sign = f"URLPrefix={encoded_prefix}:Expires={expires_at}:KeyName={key_name}"
    signature = hmac.new(_decode_key(key_value), to_sign.encode(), hashlib.sha1).digest()
    return f"{to_sign}:Signature={_b64url(signature)}"


def playback(
    *,
    cdn_base_url: str,
    job_id: str,
    key_name: str,
    key_value: str,
    ttl_seconds: int,
    playlist: str = "master.m3u8",
) -> dict:
    """Build the playback URLs and the cookie that authorises them."""
    if not (key_name and key_value and cdn_base_url):
        # Unsigned delivery is not viable: the bucket is private, so handing back
        # a bare URL would surface as an opaque 403 inside the player.
        raise RuntimeError("CDN signing is not configured.")

    base = cdn_base_url.rstrip("/")
    prefix = f"{base}/jobs/{job_id}/"
    expires_at = int(time.time()) + ttl_seconds

    return {
        "hls_url": f"{prefix}hls/{playlist}",
        "poster_url": f"{prefix}poster.jpg",
        "cookie_name": COOKIE_NAME,
        "cookie_value": sign_cookie(
            url_prefix=prefix,
            key_name=key_name,
            key_value=key_value,
            expires_at=expires_at,
        ),
        # Path deliberately matches the signed prefix, so one browser can hold
        # valid cookies for several jobs at once.
        "cookie_path": f"/jobs/{job_id}/",
        "expires_at": expires_at,
        "url_prefix": prefix,
    }

