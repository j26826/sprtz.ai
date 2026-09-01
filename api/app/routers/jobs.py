"""Job lifecycle: signed uploads, job creation, playback URLs, and analysis."""

from __future__ import annotations

import datetime
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from google.cloud import storage
from pydantic import BaseModel, Field

from app.core import cdn, clients
from app.core.auth import CallerIdentity, current_user
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_ALLOWED_CONTENT_TYPES = {
    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "video/x-msvideo",
}


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="video/mp4")
    size_bytes: int = Field(gt=0)


class UploadResponse(BaseModel):
    job_id: str
    upload_url: str
    gcs_uri: str
    expires_at: datetime.datetime


class CreateJobRequest(BaseModel):
    job_id: str
    title: str = Field(min_length=1, max_length=200)
    sport: str = Field(default="handball")
    filename: str
    size_bytes: int = Field(gt=0)


def _storage_client() -> storage.Client:
    return storage.Client()


@router.post("/upload-url", response_model=UploadResponse)
async def create_upload_url(
    body: UploadRequest,
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> UploadResponse:
    """Mint a V4 signed URL so the browser uploads straight to GCS.

    The video never passes through this service — a three-hour match would tie up
    a Cloud Run instance for the whole upload and cap out its request size.
    """
    if body.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content type {body.content_type!r}.",
        )
    if body.size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds the {settings.max_upload_bytes // 1024**3} GiB limit.",
        )

    job_id = uuid.uuid4().hex[:16]
    safe_name = _SAFE_NAME.sub("_", body.filename)[:120]
    # Namespaced by uid so one tenant's object path can never collide with another's.
    blob_name = f"uploads/{user.uid}/{job_id}/{safe_name}"

    blob = _storage_client().bucket(settings.uploads_bucket).blob(blob_name)
    expiration = datetime.timedelta(hours=6)

    try:
        upload_url = blob.generate_signed_url(
            version="v4",
            expiration=expiration,
            method="PUT",
            content_type=body.content_type,
            # Cloud Run has no private key, so signing is delegated to the IAM
            # Credentials API using the service's own identity.
            service_account_email=settings.signer_service_account or None,
            access_token=None,
        )
    except Exception as exc:
        logger.exception("could not sign an upload URL")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create an upload URL.",
        ) from exc

    return UploadResponse(
        job_id=job_id,
        upload_url=upload_url,
        gcs_uri=f"gs://{settings.uploads_bucket}/{blob_name}",
        expires_at=datetime.datetime.now(datetime.UTC) + expiration,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_job(
    body: CreateJobRequest,
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Register an uploaded video as a job, ready to analyse."""
    safe_name = _SAFE_NAME.sub("_", body.filename)[:120]
    gcs_uri = f"gs://{settings.uploads_bucket}/uploads/{user.uid}/{body.job_id}/{safe_name}"

    result = await clients.call_mcp(
        "catalog",
        "create_job",
        {
            "job_id": body.job_id,
            # Ownership comes from the verified IAP assertion, never from the body.
            "owner_uid": user.uid,
            "title": body.title,
            "sport": body.sport,
            "gcs_uri": gcs_uri,
            "original_name": body.filename,
            "size_bytes": body.size_bytes,
        },
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.get("error"))
    return result


async def _load_owned_job(job_id: str, user: CallerIdentity) -> dict:
    """Fetch a job and confirm the caller owns it."""
    job = await clients.call_mcp("catalog", "get_job", {"job_id": job_id})
    if job.get("status") == "error":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No job {job_id}.")
    if job.get("ownerUid") != user.uid:
        # Deliberately a 404: confirming existence would leak the id space.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No job {job_id}.")
    return job


@router.get("/{job_id}")
async def get_job(job_id: str, user: CallerIdentity = Depends(current_user)) -> dict:
    """Read one job."""
    return await _load_owned_job(job_id, user)


@router.get("/{job_id}/playback")
async def get_playback(
    job_id: str,
    response: Response,
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Return the HLS URL and set the Cloud CDN cookie that authorises it.

    A cookie rather than a signed URL, because an HLS playlist references its
    segments relatively: a query-string signature is dropped when the player
    resolves them, so it would authorise the playlist and none of its several
    thousand segments. The browser attaches a cookie to all of them.

    This works because the CDN is served from the app's own hostname through the
    load balancer. On separate hosts it could not be — the cookie would have to
    span two domains, which is impossible on *.run.app.
    """
    job = await _load_owned_job(job_id, user)
    playback = job.get("playback") or {}
    if not playback.get("hlsUrl"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Playback is still being prepared for this job.",
        )

    try:
        signed = cdn.playback(
            cdn_base_url=settings.cdn_base_url,
            job_id=job_id,
            key_name=settings.cdn_signing_key_name,
            key_value=settings.cdn_signing_key,
            ttl_seconds=settings.cdn_signed_url_ttl,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    # Path-scoped to the job so several jobs can hold valid cookies at once,
    # despite Cloud CDN fixing the cookie's name.
    response.set_cookie(
        key=signed["cookie_name"],
        value=signed["cookie_value"],
        domain=settings.cdn_cookie_domain or None,
        path=signed["cookie_path"],
        max_age=settings.cdn_signed_url_ttl,
        secure=True,
        httponly=True,
        samesite="lax",
    )

    return {
        "job_id": job_id,
        "hls_url": signed["hls_url"],
        "poster_url": signed["poster_url"],
        "expires_at": signed["expires_at"],
        "renditions": playback.get("renditions", []),
        "segment_seconds": playback.get("segmentSeconds", 2),
        "duration_sec": (job.get("media") or {}).get("durationSec", 0.0),
    }


@router.get("/{job_id}/moments")
async def list_moments(
    job_id: str,
    limit: int = 200,
    min_score: float = 0.0,
    user: CallerIdentity = Depends(current_user),
) -> dict:
    """List a job's key moments."""
    await _load_owned_job(job_id, user)
    return await clients.call_mcp(
        "catalog", "list_moments", {"job_id": job_id, "limit": limit, "min_score": min_score}
    )


@router.get("/{job_id}/clips")
async def list_clips(
    job_id: str, limit: int = 100, user: CallerIdentity = Depends(current_user)
) -> dict:
    """List a job's suggested clips."""
    await _load_owned_job(job_id, user)
    return await clients.call_mcp("catalog", "list_clips", {"job_id": job_id, "limit": limit})


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    rerank: bool = True


@router.post("/{job_id}/search")
async def search(
    job_id: str, body: SearchRequest, user: CallerIdentity = Depends(current_user)
) -> dict:
    """Semantic search over a job's moments, reranked by relevance."""
    await _load_owned_job(job_id, user)
    return await clients.call_mcp(
        "catalog",
        "knn_search_moments",
        {
            "query": body.query,
            "job_id": job_id,
            "limit": body.limit,
            "owner_uid": user.uid,
            "rerank": body.rerank,
        },
    )
