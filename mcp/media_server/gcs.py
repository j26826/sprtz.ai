"""GCS helpers shared by the media tools."""

from __future__ import annotations

import concurrent.futures
import logging
import mimetypes
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from google.cloud import storage

logger = logging.getLogger(__name__)

_client: "storage.Client | None" = None


def client() -> "storage.Client":
    """Lazy on purpose — see the note in catalog_server.store. The Google client
    libraries cost tens of seconds to import on Cloud Run, and the server must
    answer its health check long before that."""
    global _client
    if _client is None:
        from google.cloud import storage

        _client = storage.Client()
    return _client


def bearer_token() -> str | None:
    """Access token for reading GCS objects over plain HTTPS.

    This is how ffmpeg reads a multi-gigabyte source without it ever touching
    local disk — which on Cloud Run is memory. Tokens last an hour; the remux
    reads at I/O speed and finishes in minutes, so expiry mid-job is not a
    concern the way it would be for a full re-encode.
    """
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/devstorage.read_only"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        return credentials.token
    except Exception:  # noqa: BLE001
        logger.warning("no credentials for a bearer token", exc_info=True)
        return None


def https_url(gcs_uri: str) -> str:
    bucket, name = split_uri(gcs_uri)
    return f"https://storage.googleapis.com/{bucket}/{name}"


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


# A copy-remux writes segments as fast as it can read the source, and the
# source is GCS. One upload at a time cannot keep up: each segment is a
# separate round trip, so a serial drain is latency-bound at roughly a tenth of
# what ffmpeg produces. The backlog it leaves behind is not disk — Cloud Run's
# writable filesystem is memory — so falling behind is what killed the
# container 33 seconds into a 3.4 GB match, with 1.9 GB of undrained segments
# resident. Uploading in parallel is what makes the drain outrun the writer.
_SEGMENT_WORKERS = 16
_SEGMENT_POLL_SECONDS = 0.5


class SegmentUploader:
    """Drains finished HLS segments to GCS while ffmpeg is still writing.

    A three-hour remux emits as many gigabytes of segments as the source is
    long, and Cloud Run's writable filesystem is memory. Uploading each segment
    as it completes and deleting it locally keeps residency bounded to whatever
    is in flight rather than to the size of the match. Only .ts files are
    drained mid-run: the playlists are rewritten until ffmpeg exits and are
    uploaded by finish().
    """

    def __init__(self, local_dir: Path, bucket: str, prefix: str,
                 poll_seconds: float = _SEGMENT_POLL_SECONDS,
                 workers: int = _SEGMENT_WORKERS):
        self._dir = local_dir
        self._bucket = client().bucket(bucket)
        self._prefix = prefix.rstrip("/")
        self._poll = poll_seconds
        self._workers = workers
        self._stop = threading.Event()
        self._counters = threading.Lock()
        self._uploaded = 0
        self._bytes = 0
        self._peak_backlog = 0
        self._errors: list[str] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "SegmentUploader":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=60)

    def _ready_segments(self) -> list[Path]:
        # The newest .ts may still be open in ffmpeg; everything older is final
        # because HLS segments are written strictly in sequence.
        segments = sorted(self._dir.glob("*.ts"))
        return segments[:-1]

    def _upload_one(self, path: Path) -> None:
        blob = self._bucket.blob(f"{self._prefix}/{path.name}")
        blob.cache_control = _SEGMENT_CACHE
        size = path.stat().st_size
        blob.upload_from_filename(str(path), content_type=_content_type(path))
        # Only after the bytes are safely in GCS, and only then, is the local
        # copy free to go: an unlink before a failed upload loses the segment.
        path.unlink()
        with self._counters:
            self._uploaded += 1
            self._bytes += size

    def _drain(self, segments: list[Path]) -> None:
        """Upload a batch in parallel, keeping any failure for the next tick."""
        if not segments:
            return
        with self._counters:
            self._peak_backlog = max(self._peak_backlog, len(segments))

        workers = min(self._workers, len(segments))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._upload_one, path) for path in segments]
            for future in concurrent.futures.as_completed(futures):
                exc = future.exception()
                if exc is None:
                    continue
                # The file is still on disk, so the next tick retries it. One
                # segment failing must not stop the others from draining.
                logger.warning("segment upload failed; will retry: %s", exc)
                with self._counters:
                    self._errors.append(str(exc))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._drain(self._ready_segments())
            self._stop.wait(self._poll)

    def finish(self) -> dict:
        """Drain everything left — the last segment and the playlists."""
        self._stop.set()
        # Tolerate a finish() on an uploader that was never entered: the drain
        # below is what matters, and refusing to run it because no background
        # thread exists would strand the segments.
        if self._thread.ident is not None:
            self._thread.join(timeout=120)
        # Everything now, including the segment ffmpeg had open while running.
        self._drain(sorted(self._dir.glob("*.ts")))
        for path in sorted(self._dir.glob("*.m3u8")):
            blob = self._bucket.blob(f"{self._prefix}/{path.name}")
            blob.cache_control = _PLAYLIST_CACHE
            blob.upload_from_filename(str(path), content_type=_content_type(path))
            self._uploaded += 1
            self._bytes += path.stat().st_size
            path.unlink()
        return {
            "files": self._uploaded,
            "bytes": self._bytes,
            "prefix": self._prefix,
            "upload_retries": len(self._errors),
            # How far the drain ever fell behind. This is the number that goes
            # up before the container dies, so it is worth reporting.
            "peak_backlog_segments": self._peak_backlog,
        }
