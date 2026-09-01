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


def test_https_url_and_split():
    assert gcs.https_url("gs://bucket/a/b.mp4") == "https://storage.googleapis.com/bucket/a/b.mp4"
    with pytest.raises(ValueError):
        gcs.split_uri("s3://nope/x")
