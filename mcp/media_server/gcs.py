"""GCS helpers shared by the media tools."""

from __future__ import annotations

import concurrent.futures
import logging
import mimetypes
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


def download_range(gcs_uri: str, dest: Path, end_byte: int, start_byte: int = 0) -> Path:
    """Fetch bytes ``start_byte`` to ``end_byte`` of an object into ``dest``.

    ffprobe only needs the header and the moov atom of a faststart MP4, so a
    3 GB match does not have to cross the wire to read its duration; a
    transport stream has no header and is read at both ends instead.
    """
    bucket_name, blob_name = split_uri(gcs_uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = client().bucket(bucket_name).blob(blob_name)
    with dest.open("wb") as handle:
        blob.download_to_file(handle, start=start_byte, end=end_byte)
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


# A match's HLS package is a couple of thousand segments, and deleting them one
# at a time is a round trip each. That is slow enough to be worth parallelising
# on its own, and the delay is also what widens the window in which something
# else can remove an object from under the listing.
_DELETE_WORKERS = 16


def delete_prefix(bucket: str, prefix: str) -> dict[str, int]:
    """Remove every object under a prefix.

    Re-packaging a job writes into a directory that may already hold the
    remains of an earlier attempt. Those remains are not harmless: a playlist
    left by a half-finished run is a playlist the CDN will happily serve, and
    segments from a different packager are named differently so nothing ever
    overwrites them — they just accumulate and are billed for.

    Already-gone counts as deleted. Listing two thousand objects and then
    deleting them is not atomic, so an object can disappear between the two —
    a second delete of the same job, or a re-encode clearing the prefix. The
    caller asked for the prefix to be empty, and a 404 on the way there means
    it is emptier, not that the operation failed. Treating it as an error once
    aborted a whole job deletion partway through and left the job behind.
    """
    from google.api_core import exceptions as gcloud_exceptions

    target = client().bucket(bucket)
    names = [blob.name for blob in client().list_blobs(bucket, prefix=prefix)]
    counts = {"deleted": 0, "already_gone": 0, "failed": 0}
    if not names:
        return counts

    def remove(name: str) -> str:
        try:
            target.blob(name).delete()
            return "deleted"
        except gcloud_exceptions.NotFound:
            return "already_gone"

    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_DELETE_WORKERS) as pool:
        futures = {pool.submit(remove, name): name for name in names}
        for future in concurrent.futures.as_completed(futures):
            try:
                counts[future.result()] += 1
            except Exception as exc:  # noqa: BLE001
                # One object failing for a real reason must not abandon the
                # rest; the caller is told how many, and can ask again.
                counts["failed"] += 1
                errors.append(f"{futures[future]}: {exc}")

    logger.info(
        "cleared gs://%s/%s — %d deleted, %d already gone, %d failed",
        bucket, prefix, counts["deleted"], counts["already_gone"], counts["failed"],
    )
    if errors:
        logger.warning("could not delete %d objects, first: %s", len(errors), errors[0])
    return counts


def delete_object(gcs_uri: str) -> bool:
    """Delete one object. False if it was already gone.

    Already-gone is a success for a delete: a caller retrying after a partial
    failure should not be told the second attempt failed. The absence is caught
    from the delete itself rather than checked first, because a check followed
    by a delete is two calls with a gap in the middle — exactly the race this
    is meant to survive.
    """
    from google.api_core import exceptions as gcloud_exceptions

    bucket_name, blob_name = split_uri(gcs_uri)
    try:
        client().bucket(bucket_name).blob(blob_name).delete()
    except gcloud_exceptions.NotFound:
        logger.info("%s was already gone", gcs_uri)
        return False
    logger.info("deleted %s", gcs_uri)
    return True


# Compose takes at most 32 sources, so more than that is composed in rounds
# into intermediates that are composed again. No bytes pass through here at
# any size — the same reason the live recorder joins its segments this way.
COMPOSE_LIMIT = 32


def compose(sources: list[str], dest_uri: str, content_type: str) -> str:
    """Join objects server-side, in order, however many there are.

    Every source must be in the destination's bucket; GCS compose cannot
    cross one. Returns ``dest_uri``.
    """
    bucket_name, dest_name = split_uri(dest_uri)
    bucket = client().bucket(bucket_name)
    names = []
    for uri in sources:
        source_bucket, name = split_uri(uri)
        if source_bucket != bucket_name:
            raise ValueError(f"compose cannot cross buckets: {uri} into {dest_uri}")
        names.append(name)

    level = list(names)
    scratch: list[str] = []
    round_no = 0
    while len(level) > COMPOSE_LIMIT:
        next_level: list[str] = []
        for i in range(0, len(level), COMPOSE_LIMIT):
            group = level[i:i + COMPOSE_LIMIT]
            inter = f"{dest_name}.part{round_no}-{i // COMPOSE_LIMIT:03d}"
            target = bucket.blob(inter)
            target.content_type = content_type
            target.compose([bucket.blob(n) for n in group])
            next_level.append(inter)
            scratch.append(inter)
        level = next_level
        round_no += 1
    target = bucket.blob(dest_name)
    target.content_type = content_type
    target.compose([bucket.blob(n) for n in level])
    for name in scratch:
        try:
            bucket.blob(name).delete()
        except Exception:  # noqa: BLE001
            logger.warning("could not remove the compose intermediate %s", name)
    return dest_uri


def object_size(gcs_uri: str) -> int:
    """The object's size in bytes, from its metadata — no bytes are read."""
    bucket, name = split_uri(gcs_uri)
    blob = client().bucket(bucket).get_blob(name)
    return int(blob.size or 0) if blob else 0


def object_exists(bucket: str, name: str) -> bool:
    """Whether one object is actually there."""
    return client().bucket(bucket).blob(name).exists()
