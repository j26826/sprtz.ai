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

from app.core import cdn

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



class TestSetCookieHeader:
    """The header is built by hand because the standard library breaks it.

    `http.cookies` excludes "=" from the characters legal in a cookie value, so
    it quotes anything containing one — and every Cloud CDN cookie contains
    four. Cloud CDN does not strip those quotes, so the browser stores a valid
    cookie, sends it on every request, and gets 403 for the playlist and every
    segment. That reads as a signing or a permissions problem, which is where
    the time goes.
    """

    def _header(self, **overrides):
        kwargs = dict(name=cdn.COOKIE_NAME, value=cdn.sign_cookie(
            url_prefix="https://app.example/jobs/j1/", key_name="k1",
            key_value=KEY, expires_at=1900000000,
        ), path="/jobs/j1/", max_age=3600)
        kwargs.update(overrides)
        return cdn.cookie_header(**kwargs)

    def test_the_value_is_not_quoted(self):
        header = self._header()
        assert '="URLPrefix' not in header, header
        assert '"' not in header, header
        assert header.startswith(f"{cdn.COOKIE_NAME}=URLPrefix="), header

    def test_starlette_would_have_quoted_it(self):
        # The check that gives the assertion above its meaning: without it the
        # test passes against a helper nobody needed.
        import http.cookies

        jar = http.cookies.SimpleCookie()
        jar[cdn.COOKIE_NAME] = cdn.sign_cookie(
            url_prefix="https://app.example/jobs/j1/", key_name="k1",
            key_value=KEY, expires_at=1900000000,
        )
        assert '="URLPrefix' in jar.output(header="").strip()

    def test_the_signature_survives_intact(self):
        value = cdn.sign_cookie(url_prefix="https://app.example/jobs/j1/", key_name="k1",
                                key_value=KEY, expires_at=1900000000)
        sent = self._header(value=value).split("; ")[0].split("=", 1)[1]
        assert sent == value

    def test_it_is_scoped_and_hardened(self):
        header = self._header()
        assert "Path=/jobs/j1/" in header
        assert "Max-Age=3600" in header
        assert "Secure" in header and "HttpOnly" in header and "SameSite=Lax" in header

    def test_no_domain_by_default(self):
        assert "Domain=" not in self._header()

    def test_a_configured_domain_is_carried(self):
        assert "Domain=cdn.example" in self._header(domain="cdn.example")

    def test_expiring_the_domain_copy_targets_the_domain_one(self):
        # Deleting a cookie means matching its name, path and domain. A
        # host-only cookie and a Domain= cookie of the same name are two
        # cookies, and the browser sends both.
        header = cdn.expire_cookie_header(
            name=cdn.COOKIE_NAME, path="/jobs/j1/", domain="app.example")
        assert header.startswith(f"{cdn.COOKIE_NAME}=;")
        assert "Domain=app.example" in header
        assert "Path=/jobs/j1/" in header
        assert "Max-Age=0" in header
