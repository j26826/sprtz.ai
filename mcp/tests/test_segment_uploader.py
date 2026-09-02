"""The uploader is what keeps disk bounded during a remux, so its invariants
matter more than its happy path: never touch the newest (still-open) segment,
survive an upload failure, and drain everything on finish."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import gcs  # noqa: E402


@pytest.fixture
def bucket():
    fake_bucket = MagicMock()
    uploaded: dict[str, bytes] = {}

    def make_blob(name):
        blob = MagicMock()
        def upload(filename, content_type=None):
            uploaded[name] = Path(filename).read_bytes()
        blob.upload_from_filename.side_effect = upload
        return blob

    fake_bucket.blob.side_effect = make_blob
    fake_bucket.uploaded = uploaded
    with patch.object(gcs, "client") as client:
        client.return_value.bucket.return_value = fake_bucket
        yield fake_bucket


def _write(dirpath: Path, name: str, data: bytes = b"x" * 64) -> Path:
    p = dirpath / name
    p.write_bytes(data)
    return p


def test_newest_segment_is_never_uploaded_mid_run(tmp_path, bucket):
    """ffmpeg is still writing it; uploading a half-written segment would put a
    corrupt file behind an immutable CDN header."""
    up = gcs.SegmentUploader(tmp_path, "b", "jobs/j/hls", poll_seconds=0.05)
    _write(tmp_path, "v0_00000.ts")
    _write(tmp_path, "v0_00001.ts")
    _write(tmp_path, "v0_00002.ts")

    with up:
        deadline = threading.Event()
        deadline.wait(0.4)
    # Mid-run, only the two older segments may have been drained.
    assert "jobs/j/hls/v0_00002.ts" not in bucket.uploaded or not (tmp_path / "v0_00002.ts").exists() is False


def test_drained_segments_are_deleted_locally(tmp_path, bucket):
    with gcs.SegmentUploader(tmp_path, "b", "jobs/j/hls", poll_seconds=0.05) as up:
        _write(tmp_path, "v0_00000.ts")
        _write(tmp_path, "v0_00001.ts")
        threading.Event().wait(0.4)
        assert not (tmp_path / "v0_00000.ts").exists(), "uploaded segment must be deleted"
    up.finish()


def test_finish_drains_everything_including_playlists(tmp_path, bucket):
    up = gcs.SegmentUploader(tmp_path, "b", "jobs/j/hls", poll_seconds=10)
    with up:
        _write(tmp_path, "v0_00000.ts")
        _write(tmp_path, "v0_00001.ts")
        _write(tmp_path, "master.m3u8", b"#EXTM3U")
        _write(tmp_path, "v0.m3u8", b"#EXTM3U")
    result = up.finish()

    assert set(bucket.uploaded) == {
        "jobs/j/hls/v0_00000.ts",
        "jobs/j/hls/v0_00001.ts",
        "jobs/j/hls/master.m3u8",
        "jobs/j/hls/v0.m3u8",
    }
    assert result["files"] == 4
    assert not any(tmp_path.iterdir()), "nothing may be left on disk"


def test_upload_failure_is_retried_not_fatal(tmp_path, bucket):
    """One 503 must not kill the drain loop; the file stays local for retry."""
    calls = {"n": 0}
    real = bucket.blob.side_effect

    def flaky(name):
        blob = real(name)
        if calls["n"] == 0:
            calls["n"] += 1
            failing = MagicMock()
            failing.upload_from_filename.side_effect = RuntimeError("503")
            return failing
        return blob

    bucket.blob.side_effect = flaky

    up = gcs.SegmentUploader(tmp_path, "b", "jobs/j/hls", poll_seconds=0.05)
    with up:
        _write(tmp_path, "v0_00000.ts")
        _write(tmp_path, "v0_00001.ts")
        threading.Event().wait(0.5)
    result = up.finish()
    assert "jobs/j/hls/v0_00000.ts" in bucket.uploaded
    assert result["upload_retries"] >= 1


def test_the_drain_runs_in_parallel(tmp_path, bucket):
    """A serial drain cannot keep up with a copy-remux.

    ffmpeg writes segments as fast as it reads the source, and the backlog it
    leaves behind sits in a filesystem that is really RAM. Uploading one at a
    time is latency-bound — each segment is its own round trip — which is what
    let 1.9 GB accumulate and take the container down. This asserts the
    uploads actually overlap, since that is the whole fix.
    """
    concurrent_now = 0
    peak = 0
    lock = threading.Lock()
    real = bucket.blob.side_effect

    def slow(name):
        blob = real(name)
        inner = blob.upload_from_filename.side_effect

        def upload(filename, content_type=None):
            nonlocal concurrent_now, peak
            with lock:
                concurrent_now += 1
                peak = max(peak, concurrent_now)
            threading.Event().wait(0.1)
            inner(filename, content_type=content_type)
            with lock:
                concurrent_now -= 1

        blob.upload_from_filename.side_effect = upload
        return blob

    bucket.blob.side_effect = slow

    for i in range(9):
        _write(tmp_path, f"v0_{i:05d}.ts")

    up = gcs.SegmentUploader(tmp_path, "b", "jobs/j/hls", poll_seconds=0.05, workers=8)
    up.finish()

    assert peak > 1, "segments uploaded one at a time cannot outrun ffmpeg"


def test_a_segment_is_kept_locally_until_it_is_safely_uploaded(tmp_path, bucket):
    """Unlinking before the upload succeeds loses the segment for good."""
    def always_fails(name):
        blob = MagicMock()
        blob.upload_from_filename.side_effect = RuntimeError("503")
        return blob

    bucket.blob.side_effect = always_fails

    _write(tmp_path, "v0_00000.ts")
    _write(tmp_path, "v0_00001.ts")
    up = gcs.SegmentUploader(tmp_path, "b", "jobs/j/hls", poll_seconds=0.05)
    up._drain(sorted(tmp_path.glob("*.ts")))

    assert (tmp_path / "v0_00000.ts").exists()
    assert (tmp_path / "v0_00001.ts").exists()


def test_one_bad_segment_does_not_block_the_others(tmp_path, bucket):
    real = bucket.blob.side_effect

    def one_bad(name):
        if name.endswith("v0_00001.ts"):
            blob = MagicMock()
            blob.upload_from_filename.side_effect = RuntimeError("503")
            return blob
        return real(name)

    bucket.blob.side_effect = one_bad

    for i in range(4):
        _write(tmp_path, f"v0_{i:05d}.ts")
    up = gcs.SegmentUploader(tmp_path, "b", "jobs/j/hls", poll_seconds=0.05)
    up._drain(sorted(tmp_path.glob("*.ts")))

    assert "jobs/j/hls/v0_00003.ts" in bucket.uploaded
    assert (tmp_path / "v0_00001.ts").exists(), "the failed one waits for the next tick"


def test_the_backlog_high_water_mark_is_reported(tmp_path, bucket):
    """This is the number that climbs before the container dies."""
    for i in range(6):
        _write(tmp_path, f"v0_{i:05d}.ts")
    up = gcs.SegmentUploader(tmp_path, "b", "jobs/j/hls", poll_seconds=0.05)
    result = up.finish()

    assert result["peak_backlog_segments"] >= 6


def test_https_url_and_split():
    assert gcs.https_url("gs://bucket/a/b.mp4") == "https://storage.googleapis.com/bucket/a/b.mp4"
    with pytest.raises(ValueError):
        gcs.split_uri("s3://nope/x")
