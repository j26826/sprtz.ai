"""Cloud CDN signed URLs for HLS playback.

Signing a *prefix* rather than a single URL is what makes this usable for HLS:
one signature covers the master playlist, the variant playlists and every
segment under a job, so the player never has to re-sign mid-stream.

Reference: Cloud CDN signed URL prefixes are base64url of the prefix, then an
HMAC-SHA1 of the query string using the key registered on the backend bucket.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sign_url_prefix(
    *,
    url_prefix: str,
    key_name: str,
    key_value: str,
    expires_at: int,
) -> str:
    """Return the signed query string for everything under ``url_prefix``.

    ``url_prefix`` must include the scheme and host and end at a path boundary,
    because Cloud CDN grants access to every URL that starts with it.
    """
    if not url_prefix.startswith(("http://", "https://")):
        raise ValueError("url_prefix must include the scheme and host.")

    # The key is stored base64url-encoded, matching what Terraform registered on
    # the backend bucket.
    padding = "=" * (-len(key_value) % 4)
    secret = base64.urlsafe_b64decode(key_value + padding)

    encoded_prefix = _b64url(url_prefix.encode())
    to_sign = f"URLPrefix={encoded_prefix}&Expires={expires_at}&KeyName={key_name}"
    signature = hmac.new(secret, to_sign.encode(), hashlib.sha1).digest()
    return f"{to_sign}&Signature={_b64url(signature)}"


def playback_url(
    *,
    cdn_base_url: str,
    job_id: str,
    key_name: str,
    key_value: str,
    ttl_seconds: int,
    playlist: str = "master.m3u8",
) -> dict:
    """Build a signed HLS URL for one job.

    Returns the master playlist URL plus the raw query string, because a player
    that fetches segments itself needs to append the same signature to each of
    them when the prefix is not automatically inherited.
    """
    if not (key_name and key_value and cdn_base_url):
        # Unsigned delivery is only viable when the bucket is public, which it is
        # not; surfacing an unsigned URL would produce a silent 403 in the player.
        raise RuntimeError("CDN signing is not configured.")

    prefix = f"{cdn_base_url.rstrip('/')}/jobs/{job_id}/"
    expires_at = int(time.time()) + ttl_seconds
    query = sign_url_prefix(
        url_prefix=prefix, key_name=key_name, key_value=key_value, expires_at=expires_at
    )

    return {
        "hls_url": f"{prefix}hls/{playlist}?{query}",
        "poster_url": f"{prefix}poster.jpg?{query}",
        "signature": query,
        "expires_at": expires_at,
        "url_prefix": prefix,
    }
