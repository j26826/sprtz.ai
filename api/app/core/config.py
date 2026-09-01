"""Configuration, all injected by Terraform."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    project_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    project_number: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", "")
    )
    location: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_LOCATION", "us-south1"))
    environment: str = field(default_factory=lambda: os.environ.get("ENVIRONMENT", "dev"))

    uploads_bucket: str = field(default_factory=lambda: os.environ.get("UPLOADS_BUCKET", ""))
    media_bucket: str = field(default_factory=lambda: os.environ.get("MEDIA_BUCKET", ""))
    hls_bucket: str = field(default_factory=lambda: os.environ.get("HLS_BUCKET", ""))

    signer_service_account: str = field(
        default_factory=lambda: os.environ.get("SIGNER_SERVICE_ACCOUNT", "")
    )

    mcp_catalog_url: str = field(default_factory=lambda: os.environ.get("MCP_CATALOG_URL", ""))
    mcp_media_url: str = field(default_factory=lambda: os.environ.get("MCP_MEDIA_URL", ""))
    # The engine is created by the deploy script, not Terraform, so the API
    # resolves it by the display name both sides share rather than by an id
    # Terraform never sees. AGENT_ENGINE_RESOURCE still wins when set, which
    # keeps a manual override possible.
    agent_engine_display_name: str = field(
        default_factory=lambda: os.environ.get("AGENT_ENGINE_DISPLAY_NAME", "")
    )
    agent_engine_resource: str = field(
        default_factory=lambda: os.environ.get("AGENT_ENGINE_RESOURCE", "")
    )

    iap_audience: str = field(default_factory=lambda: os.environ.get("IAP_AUDIENCE", ""))

    cdn_base_url: str = field(default_factory=lambda: os.environ.get("CDN_BASE_URL", "").rstrip("/"))
    cdn_signing_key: str = field(default_factory=lambda: os.environ.get("CDN_SIGNING_KEY", ""))
    cdn_signing_key_name: str = field(
        default_factory=lambda: os.environ.get("CDN_SIGNING_KEY_NAME", "")
    )
    cdn_signed_url_ttl: int = field(
        default_factory=lambda: int(os.environ.get("CDN_SIGNED_URL_TTL", "21600"))
    )
    # Domain the Cloud-CDN-Cookie is set on. A browser will only send the cookie
    # to the CDN if the CDN host is covered by it, which means the API and the
    # CDN must share a registrable domain (e.g. api.sprtz.ai + cdn.sprtz.ai,
    # cookie domain .sprtz.ai). Empty means the cookie is returned in the
    # response body but not set — see the note in app/core/cdn.py.
    cdn_cookie_domain: str = field(
        default_factory=lambda: os.environ.get("CDN_COOKIE_DOMAIN", "")
    )

    # Upload cap. A three-hour broadcast at a sane bitrate lands well under this.
    max_upload_bytes: int = field(
        default_factory=lambda: int(os.environ.get("MAX_UPLOAD_BYTES", str(20 * 1024**3)))
    )

    @property
    def is_local(self) -> bool:
        return self.environment == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
