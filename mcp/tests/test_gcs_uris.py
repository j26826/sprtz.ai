"""GCS URI handling.

What survived the move to Transcoder API. The segment uploader that used to be
tested here is gone with the in-process packaging it existed to drain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import gcs  # noqa: E402


def test_https_url_and_split():
    assert gcs.https_url("gs://bucket/a/b.mp4") == "https://storage.googleapis.com/bucket/a/b.mp4"
    assert gcs.split_uri("gs://bucket/a/b.mp4") == ("bucket", "a/b.mp4")
    with pytest.raises(ValueError):
        gcs.split_uri("s3://nope/x")


class TestDeletePrefix:
    """Clearing a job's previous package before writing a new one.

    Re-packaging writes into a directory that may hold the remains of an
    earlier attempt, and those remains are not inert: a playlist left by a
    half-finished run is one the CDN will serve, and segments written by a
    different packager are named differently so nothing overwrites them.
    """

    def _client(self, names):
        from unittest.mock import MagicMock

        blobs = []
        for name in names:
            blob = MagicMock()
            blob.name = name
            blobs.append(blob)
        fake = MagicMock()
        fake.list_blobs.return_value = blobs
        return fake, blobs

    def test_every_object_under_the_prefix_is_deleted(self):
        from unittest.mock import patch

        fake, blobs = self._client(["jobs/j/hls/a.ts", "jobs/j/hls/master.m3u8"])
        with patch.object(gcs, "client", return_value=fake):
            removed = gcs.delete_prefix("hls", "jobs/j/hls/")

        assert removed == 2
        assert all(b.delete.called for b in blobs)

    def test_the_prefix_is_passed_to_the_listing(self):
        from unittest.mock import patch

        fake, _ = self._client([])
        with patch.object(gcs, "client", return_value=fake):
            gcs.delete_prefix("hls", "jobs/j/hls/")

        assert fake.list_blobs.call_args.kwargs["prefix"] == "jobs/j/hls/"

    def test_an_empty_prefix_is_not_an_error(self):
        from unittest.mock import patch

        fake, _ = self._client([])
        with patch.object(gcs, "client", return_value=fake):
            assert gcs.delete_prefix("hls", "jobs/new/hls/") == 0
