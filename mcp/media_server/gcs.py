"""GCS helpers shared by the media tools."""

from __future__ import annotations

import concurrent.futures
import logging
import mimetypes
from pathlib import Path

from google.cloud import storage

logger = logging.getLogger(__name__)

_client: storage.Client | None = None


def client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def split_uri(gcs_uri: str) -> tuple[str, str]:
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Not a GCS URI: {gcs_uri!r}")
    bucket, _, name = gcs_uri[5:].partition("/")
    if not bucket or not name:
        raise ValueError(f"GCS URI must include an object path: {gcs_uri!r}")
    return bucket, name


def download(gcs_uri: str, dest: Path) -> Path:
    bucket_name, blob_name = split_uri(gcs_uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = client().bucket(bucket_name).blob(blob_name)
    blob.download_to_filename(str(dest))
    return dest


def download_range(gcs_uri: str, dest: Path, end_byte: int) -> Path:
    """Fetch only the first ``end_byte`` bytes.

    ffprobe only needs the header and the moov atom, so a 3 GB match does not
    have to cross the wire to read its duration — as long as the file is
    faststart. The caller falls back to a full download when it is not.
    """
    bucket_name, blob_name = split_uri(gcs_uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = client().bucket(bucket_name).blob(blob_name)
    with dest.open("wb") as handle:
        blob.download_to_file(handle, start=0, end=end_byte)
    return dest


def upload(local: Path, gcs_uri: str, content_type: str | None = None, cache_control: str | None = None) -> str:
    bucket_name, blob_name = split_uri(gcs_uri)
    blob = client().bucket(bucket_name).blob(blob_name)
    if cache_control:
        blob.cache_control = cache_control
    blob.upload_from_filename(
        str(local), content_type=content_type or _content_type(local)
    )
    return gcs_uri


def _content_type(path: Path) -> str:
    # mimetypes does not know the HLS types, and getting them wrong makes the
    # CDN serve a playlist as a download instead of playing it.
    match path.suffix.lower():
        case ".m3u8":
            return "application/vnd.apple.mpegurl"
        case ".ts":
            return "video/mp2t"
        case ".m4s":
            return "video/iso.segment"
        case ".mp4":
            return "video/mp4"
        case ".jpg" | ".jpeg":
            return "image/jpeg"
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


# Playlists must be revalidated so a re-transcode is picked up; segments are
# immutable once written, so they get a long CDN and browser TTL.
_PLAYLIST_CACHE = "public, max-age=60"
_SEGMENT_CACHE = "public, max-age=31536000, immutable"


def upload_directory(local_dir: Path, bucket: str, prefix: str, workers: int = 16) -> dict:
    """Upload an HLS package. Parallel, because a long match is thousands of segments."""
    files = sorted(p for p in local_dir.rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"Nothing to upload from {local_dir}")

    target_bucket = client().bucket(bucket)

    def _one(path: Path) -> int:
        rel = path.relative_to(local_dir).as_posix()
        blob = target_bucket.blob(f"{prefix.rstrip('/')}/{rel}")
        blob.cache_control = _PLAYLIST_CACHE if path.suffix == ".m3u8" else _SEGMENT_CACHE
        blob.upload_from_filename(str(path), content_type=_content_type(path))
        return path.stat().st_size

    total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for size in pool.map(_one, files):
            total += size

    return {"files": len(files), "bytes": total, "prefix": prefix.rstrip("/")}
