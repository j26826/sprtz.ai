"""Job lifecycle: signed uploads, job creation, playback URLs, and analysis."""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import uuid
from typing import Literal

import google.auth
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from google.auth.transport import requests as google_requests
from google.cloud import storage
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from app.core import cdn, clients
from app.core.auth import CallerIdentity, current_user
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _upstream(result: dict, fallback: str) -> HTTPException:
    """A failure from a service behind this one, said in a sentence.

    What a catalog or media tool returns is a Python exception rendered as
    text — "ValueError: No such job: abc", "DeadlineExceeded: 504" — which is
    true, useless to an editor, and a description of the inside of a system
    they cannot see. The raw text goes to the log, where whoever is debugging
    will look for it; the browser gets the sentence for what failed.
    """
    logger.warning("upstream failure: %s", result.get("error"))
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=fallback)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_ALLOWED_CONTENT_TYPES = {
    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "video/x-msvideo",
}


# Pages the editor knows are about this recording — the Equipe show or class,
# a federation results page, a start list. Handed to grounding as evidence, so
# a search that would otherwise settle on the right show and the wrong class
# is told which class. Bounded because each one is quoted into a prompt.
_MAX_CONTEXT_URLS = 10
_MAX_CONTEXT_URL_LEN = 500


def _clean_context_urls(urls: list[str]) -> list[str]:
    seen: list[str] = []
    for raw in urls or []:
        url = (raw or "").strip()
        if not url:
            continue
        if not re.match(r"^https?://[^\s]+$", url):
            raise ValueError(f"Not an http(s) URL: {url[:80]!r}")
        if len(url) > _MAX_CONTEXT_URL_LEN:
            raise ValueError("A context URL is too long.")
        if url not in seen:
            seen.append(url)
    if len(seen) > _MAX_CONTEXT_URLS:
        raise ValueError(f"At most {_MAX_CONTEXT_URLS} context URLs.")
    return seen


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
    content_type: str = ""
    # ISO 639-1. The language the analysis will be asked to write in, fixed on
    # the job at creation so a match's prose does not claim to change language
    # when a later reader changes theirs.
    metadata_language: str = Field(default="en", max_length=8)
    # "editor" when a person typed the title rather than it being taken off a
    # filename. Defaulting to "derived" keeps an older caller's match named by
    # whatever the analysis reads off the screen, which is what it got before.
    title_source: Literal["editor", "derived"] = "derived"
    # Whose upload prefix the object sits under. Defaults to the caller, and is
    # only ever different when picking up an orphan somebody else left: the
    # path was written with their uid and the bytes are still there under it.
    uploaded_by: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_-]*$")
    context_urls: list[str] = Field(default_factory=list)

    @field_validator("context_urls")
    @classmethod
    def _context_urls(cls, v: list[str]) -> list[str]:
        return _clean_context_urls(v)


def _storage_client() -> storage.Client:
    return storage.Client()


def _signing_token() -> str:
    """Access token used to sign URLs through the IAM Credentials API.

    Cloud Run's metadata credentials carry a token and no private key, so the
    storage library cannot sign locally — it raises "you need a private key to
    sign credentials". Passing an access token alongside the signer's email
    routes signing through IAM's signBlob instead, which needs the service
    account to hold roles/iam.serviceAccountTokenCreator on itself (granted in
    deploy/terraform/iam.tf as api_self_sign).
    """
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google_requests.Request())
    return credentials.token


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
            # Credentials API. Both arguments are required: the email says who
            # signs, the token authorises the signBlob call.
            service_account_email=settings.signer_service_account or None,
            access_token=_signing_token(),
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
    # A uid in the path is a prefix, not a permission: the object must exist,
    # and the pattern on the field keeps it to one path segment so nothing can
    # be traversed out of the uploads prefix.
    owner_prefix = body.uploaded_by or user.uid
    blob_name = f"uploads/{owner_prefix}/{body.job_id}/{safe_name}"
    gcs_uri = f"gs://{settings.uploads_bucket}/{blob_name}"

    # The path is built from the verified uid, so a caller can only ever name an
    # object of their own — but they can still name one that was never uploaded.
    # Checking here keeps a job from existing with nothing behind it, which the
    # pipeline would only discover on its first read.
    if not _storage_client().bucket(settings.uploads_bucket).blob(blob_name).exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No upload found for that job. Upload the file first.",
        )

    result = await clients.call_mcp(
        "catalog",
        "create_job",
        {
            "job_id": body.job_id,
            # Ownership comes from the verified IAP assertion, never from the body.
            "owner_uid": user.uid,
            "title": body.title,
            "title_source": body.title_source,
            "sport": body.sport,
            "gcs_uri": gcs_uri,
            "original_name": body.filename,
            "size_bytes": body.size_bytes,
            "content_type": body.content_type,
            "metadata_language": body.metadata_language,
            "context_urls": body.context_urls,
        },
    )
    if result.get("status") == "error":
        raise _upstream(result, "This match could not be registered. Try again in a moment.")
    return result


# gs://bucket/object. Bucket naming is GCS's own rule; the object name is
# deliberately loose, because GCS object names are a flat key space — there are
# no directories to traverse out of, so the only thing worth refusing is a
# control character, which would be a header-splitting attempt rather than a path.
_GCS_URI = re.compile(r"^gs://([a-z0-9][a-z0-9._\-]{1,61}[a-z0-9])/([^\x00-\x1f]{1,1024})$")


class RegisterSourceRequest(BaseModel):
    gcs_uri: str = Field(min_length=6, max_length=1200)
    title: str = Field(min_length=1, max_length=200)
    sport: str = Field(default="handball")
    metadata_language: str = Field(default="en", max_length=8)
    # "editor" when a person typed the title rather than it being taken off a
    # filename. Defaulting to "derived" keeps an older caller's match named by
    # whatever the analysis reads off the screen, which is what it got before.
    title_source: Literal["editor", "derived"] = "derived"
    context_urls: list[str] = Field(default_factory=list)

    @field_validator("context_urls")
    @classmethod
    def _context_urls(cls, v: list[str]) -> list[str]:
        return _clean_context_urls(v)


@router.post("/from-source", status_code=status.HTTP_201_CREATED)
async def create_job_from_source(
    body: RegisterSourceRequest,
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Register a job against a video already in Cloud Storage.

    The browser upload is one non-resumable PUT: a twelve-gigabyte file that
    loses its connection starts again from zero, and these recordings are eight
    hours long. `gcloud storage cp` is resumable, parallel and already
    authenticated, so for a file this size the right answer is to let it do the
    upload and hand the location over afterwards.

    **The bucket is not the caller's to choose.** Reading an object named in a
    request, with this service's credentials, is a confused deputy unless the
    set of readable buckets is decided by the deployment — so it is the uploads
    bucket plus whatever `EXTRA_SOURCE_BUCKETS` names, and nothing else. That is
    the same boundary a browser upload already has; what changes is who does the
    copying.

    Size and name come from the object rather than from the request, because the
    object is the thing that exists.
    """
    match = _GCS_URI.match(body.gcs_uri.strip())
    if not match:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not a Cloud Storage location. Expected gs://bucket/path/to/video.mp4",
        )

    bucket_name, object_name = match.groups()
    if bucket_name not in settings.source_buckets:
        allowed = ", ".join(sorted(settings.source_buckets)) or "none"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Sources may only be read from: {allowed}.",
        )

    try:
        blob = _storage_client().bucket(bucket_name).get_blob(object_name)
    except Exception as exc:
        logger.exception("could not read %s", body.gcs_uri)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Cloud Storage.",
        ) from exc

    if blob is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No object at that location. Check the path and that the copy finished.",
        )
    size_bytes = int(blob.size or 0)
    if size_bytes <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That object is empty.",
        )
    if size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Source exceeds the {settings.max_upload_bytes // 1024**3} GiB limit.",
        )

    # Whether it is a video at all is settled by ffprobe in the ingest stage,
    # the same as for an upload: a name and a content type are both chosen by
    # whoever wrote the object.
    result = await clients.call_mcp(
        "catalog",
        "create_job",
        {
            "job_id": uuid.uuid4().hex[:16],
            "owner_uid": user.uid,
            "title": body.title,
            "title_source": body.title_source,
            "sport": body.sport,
            "gcs_uri": f"gs://{bucket_name}/{object_name}",
            "original_name": object_name.rsplit("/", 1)[-1],
            "size_bytes": size_bytes,
            "content_type": blob.content_type or "",
            "metadata_language": body.metadata_language,
            "context_urls": body.context_urls,
        },
    )
    if result.get("status") == "error":
        raise _upstream(result, "This match could not be registered. Try again in a moment.")
    return result


# An HLS source is an https URL to a playlist. https only: the media job fetches
# whatever is given here from inside the project, and a plain-http URL can be
# steered at the metadata server. Nothing else about it is validated — a token
# in the query string is normal, and whether it is a playlist at all is settled
# by the downloader reading it.
_HLS_URL = re.compile(r"^https://[^\s/?#]+[^\s]{0,1500}$")


def _clean_hls_url(url: str) -> str:
    url = (url or "").strip()
    if not _HLS_URL.match(url) or any(ord(c) < 32 for c in url):
        raise ValueError("Not an HLS URL. Expected https://…/playlist.m3u8")
    return url


class HlsSourceRequest(BaseModel):
    hls_url: str = Field(min_length=10, max_length=1600)
    title: str = Field(min_length=1, max_length=200)
    sport: str = Field(default="handball")
    metadata_language: str = Field(default="en", max_length=8)
    # "editor" when a person typed the title rather than it being taken off a
    # filename. Defaulting to "derived" keeps an older caller's match named by
    # whatever the analysis reads off the screen, which is what it got before.
    title_source: Literal["editor", "derived"] = "derived"
    context_urls: list[str] = Field(default_factory=list)
    # The 1 fps proxy the analysis reads instead of the source. Off only for
    # a source that is already small.
    proxy: bool = True

    @field_validator("hls_url")
    @classmethod
    def _hls(cls, v: str) -> str:
        return _clean_hls_url(v)

    @field_validator("context_urls")
    @classmethod
    def _context_urls(cls, v: list[str]) -> list[str]:
        return _clean_context_urls(v)


@router.post("/from-hls", status_code=status.HTTP_201_CREATED)
async def create_job_from_hls(
    body: HlsSourceRequest,
    user: CallerIdentity = Depends(current_user),
) -> dict:
    """Register a job against an HLS playlist.

    The job has no source object yet: the ingest stage downloads the playlist
    into the uploads bucket with `jobs/hls2mp4` — a progressive MP4 for a
    CMAF stream, a .ts for MPEG-TS — and the 1 fps proxy the analysis reads,
    then carries on as for an upload. So the URL is the only thing checked
    here; the bytes are validated where they land.
    """
    result = await clients.call_mcp(
        "catalog",
        "create_job",
        {
            "job_id": uuid.uuid4().hex[:16],
            "owner_uid": user.uid,
            "title": body.title,
            "title_source": body.title_source,
            "sport": body.sport,
            "gcs_uri": "",
            "original_name": body.hls_url.rsplit("/", 1)[-1].split("?", 1)[0] or "stream.m3u8",
            "size_bytes": 0,
            "content_type": "application/vnd.apple.mpegurl",
            "metadata_language": body.metadata_language,
            "context_urls": body.context_urls,
            "kind": "hls",
            "hls_url": body.hls_url,
        },
    )
    if result.get("status") == "error":
        raise _upstream(result, "This recording could not be registered. Check the playlist URL and try again.")
    return result


# A live event is bounded: the recorder follows the playlist for this long at
# most, and a window longer than a competition day is a typo.
MAX_LIVE_EVENT_HOURS = 12


class LiveEventRequest(BaseModel):
    hls_url: str = Field(min_length=10, max_length=1600)
    title: str = Field(min_length=1, max_length=200)
    sport: str = Field(default="handball")
    event_start: datetime.datetime
    event_end: datetime.datetime
    metadata_language: str = Field(default="en", max_length=8)
    # "editor" when a person typed the title rather than it being taken off a
    # filename. Defaulting to "derived" keeps an older caller's match named by
    # whatever the analysis reads off the screen, which is what it got before.
    title_source: Literal["editor", "derived"] = "derived"
    # Minutes a stream that was flowing may produce nothing before the event is
    # finished. Stalling is ordinary on a live stream — a class ends, the
    # broadcaster stops the encoder — and without an end to it the recorder
    # polled a 404 until the scheduled finish. Bounded both ways: under a
    # minute would end an event on one slow segment, and past four hours the
    # scheduled end is the closer limit anyway.
    stall_minutes: float = Field(default=5, ge=1, le=240)
    context_urls: list[str] = Field(default_factory=list)

    @field_validator("hls_url")
    @classmethod
    def _hls(cls, v: str) -> str:
        return _clean_hls_url(v)

    @field_validator("context_urls")
    @classmethod
    def _context_urls(cls, v: list[str]) -> list[str]:
        return _clean_context_urls(v)

    @field_validator("event_start", "event_end")
    @classmethod
    def _aware(cls, v: datetime.datetime) -> datetime.datetime:
        # A time with no zone is a time in somebody's head. The browser sends
        # UTC; anything else is refused rather than guessed at.
        if v.tzinfo is None:
            raise ValueError("Event times must carry a timezone (send UTC with a Z).")
        return v.astimezone(datetime.UTC)

    @model_validator(mode="after")
    def _window(self) -> LiveEventRequest:
        if self.event_end <= self.event_start:
            raise ValueError("The event must end after it starts.")
        hours = (self.event_end - self.event_start).total_seconds() / 3600
        if hours > MAX_LIVE_EVENT_HOURS:
            raise ValueError(f"A live event is at most {MAX_LIVE_EVENT_HOURS} hours.")
        if self.event_end <= datetime.datetime.now(datetime.UTC):
            raise ValueError("The event has already ended.")
        return self


@router.post("/live", status_code=status.HTTP_201_CREATED)
async def create_live_event(
    body: LiveEventRequest,
    user: CallerIdentity = Depends(current_user),
) -> dict:
    """Schedule a live event.

    Nothing runs now. The job is a document with a URL and a window; the live
    tick — Cloud Scheduler, once a minute, through `/api/live/tick` — finds it
    when its start is five minutes away, starts the capture, and analyses each
    five-minute chunk as the recorder closes it. An event whose start is
    already in the past is picked up on the next tick.
    """
    result = await clients.call_mcp(
        "catalog",
        "create_job",
        {
            "job_id": uuid.uuid4().hex[:16],
            "owner_uid": user.uid,
            "title": body.title,
            "title_source": body.title_source,
            "stall_minutes": body.stall_minutes,
            "sport": body.sport,
            "gcs_uri": "",
            "original_name": "",
            "size_bytes": 0,
            "content_type": "application/vnd.apple.mpegurl",
            "metadata_language": body.metadata_language,
            "context_urls": body.context_urls,
            "kind": "live",
            "hls_url": body.hls_url,
            "event_start": body.event_start.isoformat(),
            "event_end": body.event_end.isoformat(),
        },
    )
    if result.get("status") == "error":
        raise _upstream(result, "This event could not be scheduled. Try again in a moment.")
    return result


@router.post("/search")
async def search_library(
    body: LibrarySearchRequest, user: CallerIdentity = Depends(current_user)
) -> dict:
    """Semantic search over every game's moments, reranked, each result naming its game.

    Declared ahead of the /{job_id} routes on purpose: FastAPI matches in
    declaration order, and a literal "search" would otherwise be captured as a
    job id by the route below it.
    """
    return await clients.call_mcp(
        "catalog",
        "knn_search_moments",
        {
            "query": body.query,
            "job_id": "",
            "limit": body.limit,
            "owner_uid": user.uid,
            "rerank": body.rerank,
            "sport": body.sport,
            "job_ids": body.job_ids,
        },
    )


@router.post("/top-moments")
async def top_moments(
    body: TopMomentsRequest, user: CallerIdentity = Depends(current_user)
) -> dict:
    """The key moments across every game, best first, each naming its game.

    Ahead of the /{job_id} routes for the same reason /search is: a literal
    "top-moments" would otherwise be read as a job id.
    """
    return await clients.call_mcp(
        "catalog", "list_top_moments",
        {"limit": body.limit, "sport": body.sport, "job_ids": body.job_ids},
    )


@router.get("/pending-uploads")
async def list_pending_uploads(
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Files uploaded by this caller that never became a job.

    The browser mints a job id, uploads straight to GCS, then registers the job
    in a second call. If that second call fails — or the tab is closed between
    the two — the bytes are in the bucket and nothing points at them. This lets
    the editor pick one up rather than send a match-length file again.

    Every upload is listed, not just the caller's: matches are shared, and an
    orphan is worth picking up whoever left it. The uid stays in the object path
    as provenance — it is who uploaded the file, not who may see it.
    """
    prefix = "uploads/"
    try:
        blobs = list(
            _storage_client().list_blobs(settings.uploads_bucket, prefix=prefix, max_results=200)
        )
    except Exception as exc:
        logger.exception("could not list uploads for %s", user.uid)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not list earlier uploads.",
        ) from exc

    registered = {
        job.get("job_id")
        for job in (await clients.call_mcp(
            "catalog", "list_jobs", {"limit": 200},
        )).get("jobs", [])
    }

    pending = []
    for blob in blobs:
        # uploads/<uid>/<job_id>/<filename>
        parts = blob.name[len(prefix):].split("/")
        if len(parts) != 3 or parts[1] in registered:
            continue
        pending.append({
            "uploaded_by": parts[0],
            "job_id": parts[1],
            "filename": parts[2],
            "size_bytes": blob.size or 0,
            "content_type": blob.content_type or "",
            "uploaded_at": blob.time_created,
        })

    epoch = datetime.datetime.min.replace(tzinfo=datetime.UTC)
    pending.sort(key=lambda item: item["uploaded_at"] or epoch, reverse=True)
    return {"uploads": pending}


async def _load_job(job_id: str, user: CallerIdentity) -> dict:
    """Fetch a job. Any signed-in caller may reach any job.

    Jobs are shared across the desk, so this checks that the job exists rather
    than who uploaded it. Authentication still gates the route — `current_user`
    rejects anyone not signed in — so the boundary moved from "owns it" to
    "signed in", it did not disappear.
    """
    job = await clients.call_mcp("catalog", "get_job", {"job_id": job_id})
    if job.get("status") == "error":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No job {job_id}.")
    return job


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        title = v.strip()
        if not title:
            raise ValueError("A title cannot be empty.")
        return title


@router.patch("/{job_id}/title")
async def rename_job(
    job_id: str, body: RenameRequest, user: CallerIdentity = Depends(current_user),
):
    """Rename a match, and its game record with it.

    The browser cannot write job documents — Firestore rules deny it — so this
    is the one door, as it is for the context links. One name in both places:
    a desk that answers with two different names for one recording is a desk
    nobody trusts.
    """
    result = await clients.call_mcp("catalog", "rename_job", {"job_id": job_id, "title": body.title})
    if result.get("status") == "error":
        raise _upstream(result, "This match could not be renamed. Try again in a moment.")
    return result


class ContextRequest(BaseModel):
    context_urls: list[str] = Field(default_factory=list)

    @field_validator("context_urls")
    @classmethod
    def _context_urls(cls, v: list[str]) -> list[str]:
        return _clean_context_urls(v)


@router.patch("/{job_id}/context")
async def update_context(
    job_id: str, body: ContextRequest, user: CallerIdentity = Depends(current_user),
):
    """Replace the job's context links, ahead of analysing it again.

    The browser cannot write job documents — Firestore rules deny it — so this
    is the one door. The whole list is replaced rather than merged, because the
    editor is looking at the whole list when they save it.
    """
    result = await clients.call_mcp(
        "catalog", "update_job_context", {"job_id": job_id, "context_urls": body.context_urls})
    if result.get("status") == "error":
        raise _upstream(result, "Those links could not be saved. Try again in a moment.")
    return result


class LiveBookingRequest(BaseModel):
    """A booking being corrected before it runs.

    Every field optional and merged, because this is an edit of one thing at a
    time — a start pushed back an hour, a playlist URL that came through with a
    stale token — not a re-entry of the whole form.
    """

    hls_url: str | None = Field(default=None, min_length=10, max_length=1600)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    sport: str | None = Field(default=None, max_length=40)
    event_start: datetime.datetime | None = None
    event_end: datetime.datetime | None = None
    metadata_language: str | None = Field(default=None, max_length=8)
    stall_minutes: float | None = Field(default=None, ge=1, le=240)
    context_urls: list[str] | None = None

    @field_validator("hls_url")
    @classmethod
    def _hls(cls, v: str | None) -> str | None:
        return _clean_hls_url(v) if v else v

    @field_validator("context_urls")
    @classmethod
    def _context_urls(cls, v: list[str] | None) -> list[str] | None:
        return _clean_context_urls(v) if v is not None else v

    @field_validator("event_start", "event_end")
    @classmethod
    def _aware(cls, v: datetime.datetime | None) -> datetime.datetime | None:
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError("Event times must carry a timezone (send UTC with a Z).")
        return v.astimezone(datetime.UTC)


@router.patch("/{job_id}/live")
async def update_live_booking(
    job_id: str, body: LiveBookingRequest, user: CallerIdentity = Depends(current_user),
) -> dict:
    """Correct a live event that has not started.

    A booking is made hours ahead — a start time, an end time and a playlist
    URL — and all three are the things most likely to be wrong by the time the
    event comes round. Deleting and re-booking was the only way to fix one,
    which loses the title and the context links with it.

    Once the recorder is running this is refused: the window is what the
    recorder was started with and the chunks are numbered against it, so moving
    it mid-event would leave the event's own timeline disagreeing with the
    recording of it. The answer then is to let it finish or delete it.
    """
    job = await _load_job(job_id, user)
    if job.get("kind") != "live":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This match is not a live event.")
    if job.get("status") != "scheduled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This event has already started; it can no longer be rescheduled.",
        )
    # The tick starts a capture `live_lead_seconds` before the start, and a
    # status write lands a moment later — so a booking inside the lead-in is
    # one the recorder may already be starting on.
    live = job.get("live") or {}
    if (live.get("capture") or {}).get("execution"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This event's recorder has already started.")

    start = body.event_start or _parse_iso(live.get("eventStart"))
    end = body.event_end or _parse_iso(live.get("eventEnd"))
    if not (start and end):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This event has no window to edit.")
    if end <= start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="The event must end after it starts.")
    if (end - start).total_seconds() / 3600 > MAX_LIVE_EVENT_HOURS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A live event is at most {MAX_LIVE_EVENT_HOURS} hours.")
    if end <= datetime.datetime.now(datetime.UTC):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="The event has already ended.")

    changes: dict = {"event_start": start.isoformat(), "event_end": end.isoformat()}
    if body.hls_url is not None:
        changes["hls_url"] = body.hls_url
    if body.title is not None:
        changes["title"] = body.title
    if body.sport is not None:
        changes["sport"] = body.sport
    if body.metadata_language is not None:
        changes["metadata_language"] = body.metadata_language
    if body.stall_minutes is not None:
        changes["stall_minutes"] = body.stall_minutes
    if body.context_urls is not None:
        changes["context_urls"] = body.context_urls

    result = await clients.call_mcp(
        "catalog", "update_live_booking", {"job_id": job_id, **changes})
    if result.get("status") == "error":
        raise _upstream(result, "This booking could not be changed. Try again in a moment.")
    return result


def _parse_iso(value: str | None) -> datetime.datetime | None:
    """A stored ISO time, or None. Accepts the Z the browser sends."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


@router.get("/{job_id}")
async def get_job(job_id: str, user: CallerIdentity = Depends(current_user)) -> dict:
    """Read one job."""
    return await _load_job(job_id, user)


@router.get("/{job_id}/playback")
async def get_playback(
    job_id: str,
    request: Request,
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
    job = await _load_job(job_id, user)
    playback = job.get("playback") or {}
    # A live event plays from the stream its recorder writes as it goes, and
    # prefers it to a package even when one exists: a package made mid-event is
    # a snapshot of the chunks at that moment, and every moment found after it
    # would seek past its end. The stream is always the whole event so far.
    live = _live_stream(job)
    if not (live or playback.get("hlsUrl")):
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
            **({"folder": "live", "playlist": "index.m3u8"} if live else {}),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    # Path-scoped to the job so several jobs can hold valid cookies at once,
    # despite Cloud CDN fixing the cookie's name.
    #
    # Host-only: no Domain attribute. The load balancer serves the CDN from this
    # same hostname, so a Domain widens the cookie for no benefit.
    # cdn_cookie_domain is still read, for a deployment that genuinely serves
    # the CDN from a sibling host.
    #
    # The header is built rather than set through response.set_cookie, which
    # would quote the value — see cdn.cookie_header. That quoting is what made
    # every playlist 403 with the cookie visibly present in the request.
    response.headers.append("set-cookie", cdn.cookie_header(
        name=signed["cookie_name"],
        value=signed["cookie_value"],
        path=signed["cookie_path"],
        max_age=settings.cdn_signed_url_ttl,
        domain=settings.cdn_cookie_domain,
    ))

    # Clear the domain-scoped copy an earlier release left, which the browser
    # sends alongside this one and which Cloud CDN may read instead. Only when
    # this deployment does not want one: otherwise it would delete the cookie
    # just set.
    if not settings.cdn_cookie_domain and request.url.hostname:
        response.headers.append("set-cookie", cdn.expire_cookie_header(
            name=signed["cookie_name"],
            path=signed["cookie_path"],
            domain=request.url.hostname,
        ))

    return {
        "job_id": job_id,
        "hls_url": signed["hls_url"],
        "poster_url": signed["poster_url"],
        "expires_at": signed["expires_at"],
        "renditions": ["source"] if live else playback.get("renditions", []),
        "segment_seconds": playback.get("segmentSeconds", 2),
        "duration_sec": (job.get("media") or {}).get("durationSec", 0.0),
        # "live" while the recorder is still writing the stream, "recorded" once
        # it has finished, "package" for an encode.
        "source": ("live" if live == "recording" else "recorded") if live else "package",
    }


def _live_stream(job: dict) -> str:
    """Whether a live event has a stream to play, and whether it is still growing.

    Returns "recording", "finished", or "" when there is none. Read from what
    the recorder reported rather than from the bucket: it names the playlist
    only once a segment is in it, and a playlist with nothing listed is a
    player that spins for ever. The URL is built from the job id, never from
    the stored path, so nothing written to the job can point a player elsewhere.
    """
    if job.get("kind") != "live":
        return ""
    capture = ((job.get("live") or {}).get("capture") or {})
    if not capture.get("stream"):
        return ""
    return "recording" if capture.get("state") == "recording" else "finished"


@router.get("/{job_id}/moments")
async def list_moments(
    job_id: str,
    limit: int = 200,
    min_score: float = 0.0,
    user: CallerIdentity = Depends(current_user),
) -> dict:
    """List a job's key moments."""
    await _load_job(job_id, user)
    return await clients.call_mcp(
        "catalog", "list_moments", {"job_id": job_id, "limit": limit, "min_score": min_score}
    )


@router.get("/{job_id}/event")
async def event_tree(job_id: str, user: CallerIdentity = Depends(current_user)) -> dict:
    """The event as a tree: the event, each ride in running order, the moments in each.

    A ride is a rider on one horse. Moments outside every ride come back under
    ``unassignedMoments``; a sport without rides has no riders and every moment
    there. Built by the catalog from the stored records — see
    mcp/catalog_server/event_tree.py.
    """
    await _load_job(job_id, user)
    result = await clients.call_mcp("catalog", "get_event_tree", {"job_id": job_id})
    if result.get("status") == "error":
        raise _upstream(result, "The rides for this event could not be read just now.")
    return {"event": result.get("event") or {}}


class ThumbnailRequest(BaseModel):
    # One page of the editor's moments list at a time. Every URL is its own
    # signBlob round trip, so an unbounded request would be a page load waiting
    # on hundreds of them.
    moment_ids: list[str] = Field(min_length=1, max_length=50)


# A moment id is a path segment here, and the object it names sits under a
# prefix this service builds. Anything outside this set is refused rather than
# rewritten: a rewritten id would sign a URL for the wrong object and hand back
# a picture belonging to another moment.
_MOMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")

# Long enough to browse a match's moments without re-signing, short enough that
# a link that escapes the session stops working the same day.
_THUMBNAIL_TTL = datetime.timedelta(hours=6)


@router.post("/{job_id}/thumbnails")
async def moment_thumbnails(
    job_id: str,
    body: ThumbnailRequest,
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Sign read URLs for a page of moment thumbnails.

    The bucket is private and an `<img>` carries no Authorization header, so the
    picture cannot be fetched the way the rest of the API is. A signed URL is
    the whole credential, which is exactly what an `<img src>` needs.

    Not the CDN's signed cookie, which authorises playback: that one is minted
    by `/playback` and so exists only for a job that has been packaged, while
    moments and their stills exist as soon as the analysis has run. A thumbnail
    that appeared only after an encode would be missing for precisely the job
    someone is waiting on.
    """
    await _load_job(job_id, user)
    if not settings.media_bucket:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Media storage is not configured.",
        )

    wanted = [mid for mid in dict.fromkeys(body.moment_ids) if _MOMENT_ID.match(mid)]
    if not wanted:
        return {"job_id": job_id, "thumbnails": {}, "expires_at": None}

    bucket = _storage_client().bucket(settings.media_bucket)
    token = _signing_token()

    def sign(moment_id: str) -> str:
        return bucket.blob(f"jobs/{job_id}/moments/{moment_id}.png").generate_signed_url(
            version="v4",
            expiration=_THUMBNAIL_TTL,
            method="GET",
            service_account_email=settings.signer_service_account or None,
            access_token=token,
        )

    try:
        # In parallel, and in threads: signing is a blocking round trip to IAM
        # per URL, so a page of ten signed in series is ten round trips of
        # latency on the event loop for one list of pictures.
        urls = await asyncio.gather(
            *(run_in_threadpool(sign, moment_id) for moment_id in wanted)
        )
    except Exception as exc:
        logger.exception("could not sign thumbnail URLs for job %s", job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not sign thumbnail URLs.",
        ) from exc

    return {
        "job_id": job_id,
        "thumbnails": dict(zip(wanted, urls, strict=True)),
        "expires_at": datetime.datetime.now(datetime.UTC) + _THUMBNAIL_TTL,
    }


# How far a trim may run past the moment's own in and out points, and how long
# the result may be. A publish preview exists so an editor can breathe a second
# either side of a play — not so "this moment" can become half the match under
# a moment's name. Both are clamps rather than refusals: a slider dragged to its
# end should stop, not fail.
_TRIM_SLACK_SEC = 120.0
_MAX_CUT_SEC = 600.0
# A cut is rendered on demand and read once; a day is long enough to keep the
# link usable and short enough that a leaked URL stops working.
_DOWNLOAD_TTL = datetime.timedelta(hours=24)


class CutRequest(BaseModel):
    """A trim around one moment, in seconds into the match.

    Absent means the moment's own times. The browser sends what the player is
    showing, so what comes back is what was being watched.
    """

    start_sec: float | None = Field(default=None, ge=0)
    end_sec: float | None = Field(default=None, ge=0)


class PublishYouTubeRequest(CutRequest):
    """What to publish, and how it should read on the channel."""

    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    privacy: Literal["private", "unlisted", "public"] = "private"
    tags: list[str] = Field(default_factory=list, max_length=15)


async def _cut_range(job_id: str, moment_id: str, body: CutRequest) -> tuple[dict, float, float]:
    """Resolve a requested trim against the moment on record.

    Returns the moment and the in and out points to cut, clamped to within
    `_TRIM_SLACK_SEC` of the moment and `_MAX_CUT_SEC` long.
    """
    if not _MOMENT_ID.match(moment_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such moment.")

    found = await clients.call_mcp(
        "catalog", "get_moment", {"job_id": job_id, "moment_id": moment_id}
    )
    moment = found.get("moment") if found.get("status") == "success" else None
    if not moment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such moment.")

    own_start = float(moment.get("start_sec") or 0.0)
    own_end = float(moment.get("end_sec") or 0.0)
    start = own_start if body.start_sec is None else float(body.start_sec)
    end = own_end if body.end_sec is None else float(body.end_sec)

    start = max(0.0, min(start, max(0.0, own_start - _TRIM_SLACK_SEC) + 2 * _TRIM_SLACK_SEC))
    start = max(max(0.0, own_start - _TRIM_SLACK_SEC), min(start, own_end))
    end = min(own_end + _TRIM_SLACK_SEC, max(end, own_start))
    if end <= start:
        end = start + 1.0
    end = min(end, start + _MAX_CUT_SEC)
    return moment, round(start, 3), round(end, 3)


async def _render_cut(job_id: str, moment_id: str, job: dict, start: float, end: float) -> str:
    """Cut the range out of the source and return the object's gs:// URI."""
    source = (job.get("source") or {}).get("gcsUri") or ""
    if not source:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            # A live event has chunks rather than a source until something joins
            # them, and that is a button the editor already has.
            detail="This match has no source video yet. Prepare playback first.",
        )

    cut = await clients.call_mcp("media", "cut_moment", {
        "gcs_uri": source,
        "job_id": job_id,
        "moment_id": moment_id,
        "start_sec": start,
        "end_sec": end,
    })
    if cut.get("status") != "success" or not cut.get("output_uri"):
        logger.error("cut_moment failed for %s/%s: %s", job_id, moment_id, cut.get("error"))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=cut.get("error") or "Could not cut this moment.",
        )
    return cut["output_uri"]


@router.post("/{job_id}/moments/{moment_id}/download")
async def download_moment(
    job_id: str,
    moment_id: str,
    body: CutRequest,
    user: CallerIdentity = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Render one moment as an MP4 and hand back a signed URL for it.

    Signed rather than served through this API: the file is tens of megabytes
    and proxying it would hold a request open for the whole transfer, on a
    service whose other job is streaming an agent's reply.
    """
    job = await _load_job(job_id, user)
    if not settings.media_bucket:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Media storage is not configured.",
        )

    moment, start, end = await _cut_range(job_id, moment_id, body)
    await _render_cut(job_id, moment_id, job, start, end)

    name = f"jobs/{job_id}/downloads/{moment_id}.mp4"
    blob = _storage_client().bucket(settings.media_bucket).blob(name)
    token = _signing_token()

    def sign() -> str:
        return blob.generate_signed_url(
            version="v4",
            expiration=_DOWNLOAD_TTL,
            method="GET",
            service_account_email=settings.signer_service_account or None,
            access_token=token,
            # Without this the browser opens the MP4 in a tab: the object's own
            # content type says video, and a link that plays is not a download.
            response_disposition=f'attachment; filename="{_download_name(job, moment)}"',
        )

    try:
        url = await run_in_threadpool(sign)
    except Exception as exc:
        logger.exception("could not sign a download URL for %s/%s", job_id, moment_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not sign the download URL.",
        ) from exc

    return {
        "job_id": job_id,
        "moment_id": moment_id,
        "url": url,
        "filename": _download_name(job, moment),
        "start_sec": start,
        "end_sec": end,
        "expires_at": datetime.datetime.now(datetime.UTC) + _DOWNLOAD_TTL,
    }


def _download_name(job: dict, moment: dict) -> str:
    """A filename someone can find again in their downloads folder.

    The match, the moment and its timecode — not the moment id, which is a hex
    string that says nothing once the file is on a desktop.
    """
    parts = [job.get("title") or "match", moment.get("label") or moment.get("moment_type") or "moment"]
    at = int(float(moment.get("start_sec") or 0))
    parts.append(f"{at // 60:02d}m{at % 60:02d}s")
    safe = re.sub(r"[^A-Za-z0-9]+", "-", " ".join(str(p) for p in parts)).strip("-")
    return f"{safe[:120] or 'moment'}.mp4"


@router.post("/{job_id}/moments/{moment_id}/publish/youtube")
async def publish_moment_to_youtube(
    job_id: str,
    moment_id: str,
    body: PublishYouTubeRequest,
    user: CallerIdentity = Depends(current_user),
) -> dict:
    """Cut one moment and upload it to the configured YouTube channel."""
    job = await _load_job(job_id, user)
    _, start, end = await _cut_range(job_id, moment_id, body)
    output = await _render_cut(job_id, moment_id, job, start, end)

    result = await clients.call_mcp("media", "publish_youtube", {
        "clip_uri": output,
        "title": body.title,
        "description": body.description,
        "privacy": body.privacy,
        "tags": body.tags,
    })
    if result.get("status") != "success":
        # The reason is the editor's to act on — a revoked refresh token, a
        # channel over its daily quota — so it travels rather than being
        # flattened into "upload failed".
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.get("error") or "YouTube would not accept the upload.",
        )
    return {
        "job_id": job_id,
        "moment_id": moment_id,
        "video_id": result.get("video_id"),
        "url": result.get("url"),
        "privacy": result.get("privacy"),
        "start_sec": start,
        "end_sec": end,
    }


class LibrarySearchRequest(BaseModel):
    """A search across every game on the desk, optionally narrowed."""
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    rerank: bool = True
    # Narrowing. A sport keeps only games of that sport; job_ids keeps only
    # those games. One job id is answered by the per-job index exactly.
    sport: str = Field(default="", max_length=40, pattern=r"^[a-z_]*$")
    job_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("job_ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        clean = [x.strip() for x in v if x and x.strip()]
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", x) for x in clean):
            raise ValueError("job_ids must be job identifiers")
        return list(dict.fromkeys(clean))


class TopMomentsRequest(BaseModel):
    """The key moments across the desk, optionally narrowed."""
    limit: int = Field(default=20, ge=1, le=50)
    sport: str = Field(default="", max_length=40, pattern=r"^[a-z_]*$")
    job_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("job_ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        clean = [x.strip() for x in v if x and x.strip()]
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", x) for x in clean):
            raise ValueError("job_ids must be job identifiers")
        return list(dict.fromkeys(clean))


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    rerank: bool = True


@router.post("/{job_id}/search")
async def search(
    job_id: str, body: SearchRequest, user: CallerIdentity = Depends(current_user)
) -> dict:
    """Semantic search over a job's moments, reranked by relevance."""
    await _load_job(job_id, user)
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
