"""Sprtz AI API — FastAPI behind IAP."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.routers import agent, jobs

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("sprtz-api")

settings = get_settings()

app = FastAPI(
    title="Sprtz AI API",
    version="0.1.0",
    description="Upload, analysis control and agent conversation for the SPRTZ AI Editor.",
    docs_url="/api/docs" if settings.environment != "prod" else None,
)

# The SPA is served from its own Cloud Run origin, so browser calls are
# cross-origin. IAP has already authenticated everything that reaches here.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.run\.app|http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(jobs.router)
app.include_router(agent.router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "sprtz-api", "env": settings.environment})


@app.get("/api/config")
async def client_config() -> dict:
    """Non-secret settings the SPA needs at runtime."""
    return {
        "environment": settings.environment,
        "project_id": settings.project_id,
        "max_upload_bytes": settings.max_upload_bytes,
        "supported_sports": ["handball"],
    }
