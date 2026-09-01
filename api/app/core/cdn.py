"""Cloud CDN signed cookies for HLS playback.

An HLS playlist references its segments relatively, so a player resolving
`v0_00000.ts` against the playlist URL drops any query string the playlist
carried. A signed URL on the playlist alone therefore authorises the playlist
and nothing else, and every segment request arrives unsigned.

There are two ways out of that, and which one applies is decided by whether the
deployment has a custom domain:

* **Signed cookies** are cleanest, but a browser only sends a cookie to the CDN
  if the CDN host falls under the cookie's domain — so the API and the CDN must
  share a registrable domain. On the default ``*.run.app`` hostnames this cannot
  work at all: ``run.app`` is on the Public Suffix List, so no cookie can span
  two services there.
* **Rewriting the playlists** works anywhere. The API serves the playlists
  itself, turning every relative reference into an absolute CDN URL carrying the
  prefix signature. The player then requests already-signed segments, and the
  CDN still serves every byte of video — the API only ever touches the few
  kilobytes of playlist text.

Rewriting is the default because it holds on any hostname. Cookie signing is
kept for deployments that do have a custom domain configured.
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


def sign_query(
    *,
    url_prefix: str,
    key_name: str,
    key_value: str,
    expires_at: int,
) -> str:
    """Return the query string authorising everything under ``url_prefix``.

    Cloud CDN validates a signed *prefix*, so the identical query string can be
    appended to the playlist and to every segment beneath it — which is what
    makes playlist rewriting cheap.
    """
    if not url_prefix.startswith(("http://", "https://")):
        raise ValueError("url_prefix must include the scheme and host.")

    encoded_prefix = _b64url(url_prefix.encode())
    to_sign = f"URLPrefix={encoded_prefix}&Expires={expires_at}&KeyName={key_name}"
    signature = hmac.new(_decode_key(key_value), to_sign.encode(), hashlib.sha1).digest()
    return f"{to_sign}&Signature={_b64url(signature)}"


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


def rewrite_playlist(
    body: str,
    *,
    cdn_base_url: str,
    job_id: str,
    signature: str,
    api_playlist_base: str,
) -> str:
    """Rewrite an HLS playlist so nothing in it resolves to an unsigned URL.

    Two kinds of reference need different destinations:

    * A nested **playlist** must come back through the API, because its own
      contents need rewriting in turn.
    * A **segment** goes straight to the CDN with the prefix signature attached,
      so the video bytes never pass through Cloud Run.

    Lines beginning with ``#`` are tags and are passed through untouched, except
    that ``EXT-X-KEY``/``EXT-X-MAP`` carry a ``URI="..."`` which is a real
    reference and must be signed like any other.
    """
    cdn_prefix = f"{cdn_base_url.rstrip('/')}/jobs/{job_id}/hls"
    api_base = api_playlist_base.rstrip("/")

    def destination(ref: str) -> str:
        # Absolute references are already someone else's problem; leave them.
        if ref.startswith(("http://", "https://")):
            return ref
        if ref.endswith(".m3u8"):
            return f"{api_base}/{ref}"
        return f"{cdn_prefix}/{ref}?{signature}"

    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            out.append(raw)
        elif line.startswith("#"):
            if 'URI="' in line:
                head, _, rest = line.partition('URI="')
                ref, _, tail = rest.partition('"')
                out.append(f'{head}URI="{destination(ref)}"{tail}')
            else:
                out.append(raw)
        else:
            out.append(destination(line))
    return "\n".join(out) + "\n"
