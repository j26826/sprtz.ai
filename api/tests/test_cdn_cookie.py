"""Cloud CDN signed cookie format and scoping."""
import base64
import hashlib
import hmac
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import cdn  # noqa: E402

KEY = base64.urlsafe_b64encode(b"0123456789abcdef").decode().rstrip("=")

def test_format_matches_cloud_cdn_spec():
    v = cdn.sign_cookie(url_prefix="https://cdn.x.com/jobs/j1/", key_name="k1",
                        key_value=KEY, expires_at=1900000000)
    assert re.fullmatch(r"URLPrefix=[\w-]+:Expires=\d+:KeyName=k1:Signature=[\w-]+", v), v

def test_signature_is_hmac_sha1_over_the_signed_fields():
    prefix, expires, name = "https://cdn.x.com/jobs/j1/", 1900000000, "k1"
    v = cdn.sign_cookie(url_prefix=prefix, key_name=name, key_value=KEY, expires_at=expires)
    body, _, sig = v.rpartition(":Signature=")
    expected = base64.urlsafe_b64encode(
        hmac.new(b"0123456789abcdef", body.encode(), hashlib.sha1).digest()
    ).decode().rstrip("=")
    assert sig == expected, f"{sig} != {expected}"

def test_url_prefix_is_base64url_of_the_real_prefix():
    prefix = "https://cdn.x.com/jobs/j1/"
    v = cdn.sign_cookie(url_prefix=prefix, key_name="k1", key_value=KEY, expires_at=1)
    enc = re.search(r"URLPrefix=([\w-]+)", v).group(1)
    dec = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode()
    assert dec == prefix, dec

def test_prefix_must_be_absolute():
    for bad in ("/jobs/j1/", "cdn.x.com/jobs/", ""):
        with pytest.raises(ValueError):
            cdn.sign_cookie(url_prefix=bad, key_name="k", key_value=KEY, expires_at=1)

def test_playback_scopes_cookie_to_the_job():
    a = cdn.playback(cdn_base_url="https://cdn.x.com", job_id="jobA", key_name="k",
                     key_value=KEY, ttl_seconds=600)
    b = cdn.playback(cdn_base_url="https://cdn.x.com", job_id="jobB", key_name="k",
                     key_value=KEY, ttl_seconds=600)
    assert a["cookie_path"] == "/jobs/jobA/" and b["cookie_path"] == "/jobs/jobB/"
    assert a["cookie_value"] != b["cookie_value"], "different jobs must not share a signature"
    assert a["hls_url"] == "https://cdn.x.com/jobs/jobA/hls/master.m3u8"
    # No query string: the signature lives in the cookie, which is the whole point.
    assert "?" not in a["hls_url"], a["hls_url"]

def test_expiry_is_in_the_future_and_reported():
    r = cdn.playback(cdn_base_url="https://cdn.x.com", job_id="j", key_name="k",
                     key_value=KEY, ttl_seconds=600)
    assert 590 <= r["expires_at"] - int(time.time()) <= 601

def test_unconfigured_signing_raises_rather_than_returning_a_dead_url():
    for kwargs in ({"key_name": ""}, {"key_value": ""}, {"cdn_base_url": ""}):
        base = dict(cdn_base_url="https://cdn.x.com", job_id="j", key_name="k",
                    key_value=KEY, ttl_seconds=60)
        base.update(kwargs)
        with pytest.raises(RuntimeError):
            cdn.playback(**base)


class TestSignedQueryAndRewrite:
    """Playback on the default *.run.app hostnames, where cookies cannot work."""

    def _sig(self):
        return cdn.sign_query(
            url_prefix="https://cdn.x/jobs/j1/", key_name="k",
            key_value=KEY, expires_at=1900000000,
        )

    def test_query_signature_verifies_independently(self):
        sig = self._sig()
        body, _, signature = sig.rpartition("&Signature=")
        expected = base64.urlsafe_b64encode(
            hmac.new(b"0123456789abcdef", body.encode(), hashlib.sha1).digest()
        ).decode().rstrip("=")
        assert signature == expected

    def test_segments_go_to_the_cdn_signed(self):
        out = cdn.rewrite_playlist(
            "#EXTM3U\n#EXTINF:2.0,\nv0_00000.ts\n",
            cdn_base_url="https://cdn.x", job_id="j1",
            signature=self._sig(), api_playlist_base="https://api.x/api/jobs/j1/hls",
        )
        seg = [line for line in out.splitlines() if line.endswith(".ts") or ".ts?" in line][0]
        assert seg.startswith("https://cdn.x/jobs/j1/hls/v0_00000.ts?")
        assert "Signature=" in seg, "segment must be signed"

    def test_nested_playlists_come_back_through_the_api(self):
        """They need rewriting in turn, so they cannot go straight to the CDN."""
        out = cdn.rewrite_playlist(
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv0.m3u8\n",
            cdn_base_url="https://cdn.x", job_id="j1",
            signature=self._sig(), api_playlist_base="https://api.x/api/jobs/j1/hls",
        )
        assert "https://api.x/api/jobs/j1/hls/v0.m3u8" in out
        assert "cdn.x/jobs/j1/hls/v0.m3u8" not in out

    def test_uri_attributes_in_tags_are_signed(self):
        """EXT-X-MAP / EXT-X-KEY carry real references inside a comment line."""
        out = cdn.rewrite_playlist(
            '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n',
            cdn_base_url="https://cdn.x", job_id="j1",
            signature=self._sig(), api_playlist_base="https://api.x/api/jobs/j1/hls",
        )
        assert 'URI="https://cdn.x/jobs/j1/hls/init.mp4?' in out
        assert "Signature=" in out

    def test_tags_and_blank_lines_survive_untouched(self):
        src = "#EXTM3U\n#EXT-X-VERSION:3\n\n#EXT-X-ENDLIST\n"
        out = cdn.rewrite_playlist(
            src, cdn_base_url="https://cdn.x", job_id="j1",
            signature=self._sig(), api_playlist_base="https://api.x/api/jobs/j1/hls",
        )
        for tag in ("#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-ENDLIST"):
            assert tag in out

    def test_absolute_references_are_left_alone(self):
        out = cdn.rewrite_playlist(
            "#EXTM3U\nhttps://elsewhere.example/x.ts\n",
            cdn_base_url="https://cdn.x", job_id="j1",
            signature=self._sig(), api_playlist_base="https://api.x/api/jobs/j1/hls",
        )
        assert "https://elsewhere.example/x.ts" in out
        assert "elsewhere.example/x.ts?URLPrefix" not in out

    def test_relative_prefix_still_rejected(self):
        with pytest.raises(ValueError):
            cdn.sign_query(url_prefix="/jobs/", key_name="k", key_value=KEY, expires_at=1)
