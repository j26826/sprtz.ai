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

    Listing two thousand objects and then deleting them is not atomic, which is
    the other half of what these cover.
    """

    def _client(self, names, missing=(), broken=()):
        from unittest.mock import MagicMock

        from google.api_core import exceptions as gcloud_exceptions

        deleted = []

        def make_blob(name):
            blob = MagicMock()
            blob.name = name
            if name in missing:
                blob.delete.side_effect = gcloud_exceptions.NotFound(name)
            elif name in broken:
                blob.delete.side_effect = RuntimeError("503")
            else:
                blob.delete.side_effect = lambda: deleted.append(name)
            return blob

        bucket = MagicMock()
        bucket.blob.side_effect = make_blob
        fake = MagicMock()
        fake.bucket.return_value = bucket
        fake.list_blobs.return_value = [make_blob(n) for n in names]
        fake.deleted = deleted
        return fake

    def test_every_object_under_the_prefix_is_deleted(self):
        from unittest.mock import patch

        fake = self._client(["jobs/j/hls/a.ts", "jobs/j/hls/master.m3u8"])
        with patch.object(gcs, "client", return_value=fake):
            counts = gcs.delete_prefix("hls", "jobs/j/hls/")

        assert counts["deleted"] == 2
        assert sorted(fake.deleted) == ["jobs/j/hls/a.ts", "jobs/j/hls/master.m3u8"]

    def test_the_prefix_is_passed_to_the_listing(self):
        from unittest.mock import patch

        fake = self._client(["jobs/j/hls/a.ts"])
        with patch.object(gcs, "client", return_value=fake):
            gcs.delete_prefix("hls", "jobs/j/hls/")

        assert fake.list_blobs.call_args.kwargs["prefix"] == "jobs/j/hls/"

    def test_an_empty_prefix_is_not_an_error(self):
        from unittest.mock import patch

        fake = self._client([])
        with patch.object(gcs, "client", return_value=fake):
            counts = gcs.delete_prefix("hls", "jobs/new/hls/")

        assert counts == {"deleted": 0, "already_gone": 0, "failed": 0}

    def test_an_object_that_vanished_is_not_a_failure(self):
        from unittest.mock import patch

        # Listing then deleting is not atomic. A second delete of the same job,
        # or a re-encode clearing the prefix, removes objects underneath us —
        # and the caller wanted them gone, so a 404 is the desired end state.
        names = [f"jobs/j/hls/{i}.ts" for i in range(5)]
        fake = self._client(names, missing=names[:2])
        with patch.object(gcs, "client", return_value=fake):
            counts = gcs.delete_prefix("hls", "jobs/j/hls/")

        assert counts["already_gone"] == 2
        assert counts["deleted"] == 3
        assert counts["failed"] == 0

    def test_one_vanished_object_does_not_abandon_the_rest(self):
        from unittest.mock import patch

        # This is what broke a real job deletion: a 404 on segment 1400 of
        # about 2000 aborted the loop, so the remaining objects stayed and the
        # job could not be removed at all.
        names = [f"jobs/j/hls/{i}.ts" for i in range(10)]
        fake = self._client(names, missing=[names[4]])
        with patch.object(gcs, "client", return_value=fake):
            counts = gcs.delete_prefix("hls", "jobs/j/hls/")

        assert counts["deleted"] == 9
        assert len(fake.deleted) == 9

    def test_a_real_failure_is_counted_rather_than_swallowed(self):
        from unittest.mock import patch

        # A 503 is not a 404. The prefix is still dirty, and the caller has to
        # know that before it drops the job document that points at it.
        names = [f"jobs/j/hls/{i}.ts" for i in range(4)]
        fake = self._client(names, broken=[names[1]])
        with patch.object(gcs, "client", return_value=fake):
            counts = gcs.delete_prefix("hls", "jobs/j/hls/")

        assert counts["failed"] == 1
        assert counts["deleted"] == 3


class TestDeleteObject:
    def test_a_missing_object_reports_false_rather_than_raising(self):
        from unittest.mock import MagicMock, patch

        from google.api_core import exceptions as gcloud_exceptions

        blob = MagicMock()
        blob.delete.side_effect = gcloud_exceptions.NotFound("gone")
        bucket = MagicMock()
        bucket.blob.return_value = blob
        fake = MagicMock()
        fake.bucket.return_value = bucket

        with patch.object(gcs, "client", return_value=fake):
            assert gcs.delete_object("gs://b/missing.mp4") is False

    def test_absence_is_caught_from_the_delete_not_checked_first(self):
        from unittest.mock import MagicMock, patch

        blob = MagicMock()
        bucket = MagicMock()
        bucket.blob.return_value = blob
        fake = MagicMock()
        fake.bucket.return_value = bucket

        with patch.object(gcs, "client", return_value=fake):
            assert gcs.delete_object("gs://b/there.mp4") is True

        # exists() then delete() is two calls with a gap in the middle, which is
        # the race this is meant to survive rather than re-create.
        blob.exists.assert_not_called()
        blob.delete.assert_called_once()
