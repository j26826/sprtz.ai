"""Runtime configuration.

Every value is injected as an environment variable by Terraform (see
``deploy/terraform/agent_runtime.tf``) so the same package runs locally, in
Cloud Build's integration tests, and on Agent Runtime without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    project_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    location: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))

    # Gemini 2.5 Flash handles the multimodal video pass: frames, on-screen
    # graphics, commentary audio and crowd noise all come from this one model.
    model: str = field(default_factory=lambda: os.environ.get("SPRTZ_MODEL", "gemini-2.5-flash"))

    embedding_model: str = field(
        default_factory=lambda: os.environ.get("SPRTZ_EMBEDDING_MODEL", "gemini-embedding-001")
    )
    embedding_dimensions: int = field(
        default_factory=lambda: _int_env("SPRTZ_EMBEDDING_DIMENSIONS", 768)
    )

    # A full handball match is ~70 minutes of play plus stoppages. Gemini is
    # asked for one segment at a time so a single request never has to hold the
    # whole match, and segments run concurrently.
    segment_minutes: int = field(default_factory=lambda: _int_env("SPRTZ_SEGMENT_MINUTES", 15))
    segment_overlap_seconds: int = field(
        default_factory=lambda: _int_env("SPRTZ_SEGMENT_OVERLAP_SECONDS", 20)
    )
    # How many segment analyses run at once. Six exhausted the per-minute Vertex
    # quota on a thirteen-segment match and every one of those failures cost a
    # whole window. Retries carry the rest; this reduces how often they are
    # needed rather than relying on them.
    max_concurrent_segments: int = field(
        default_factory=lambda: _int_env("SPRTZ_MAX_CONCURRENT_SEGMENTS", 3)
    )

    # Frames per second Gemini samples from the video. 1 fps is deliberate: on
    # real match footage 2 fps roughly doubled the token cost and made the
    # reported timestamps markedly worse, because the longer frame sequence
    # pushed the model into emitting a counter instead of reading clip position.
    analysis_fps: float = field(
        default_factory=lambda: float(os.environ.get("SPRTZ_ANALYSIS_FPS", "1.0"))
    )

    uploads_bucket: str = field(default_factory=lambda: os.environ.get("UPLOADS_BUCKET", ""))
    media_bucket: str = field(default_factory=lambda: os.environ.get("MEDIA_BUCKET", ""))

    mcp_catalog_url: str = field(default_factory=lambda: os.environ.get("MCP_CATALOG_URL", ""))
    mcp_media_url: str = field(default_factory=lambda: os.environ.get("MCP_MEDIA_URL", ""))

    @property
    def segment_seconds(self) -> int:
        return self.segment_minutes * 60


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
