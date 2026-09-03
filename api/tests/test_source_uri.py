"""Registering a job against a video already in Cloud Storage.

The browser upload is one non-resumable PUT, and the real equestrian recordings
are eight hours and twelve gigabytes — a dropped connection starts the whole
thing again. `gcloud storage cp` is resumable, so for a file that size the right
answer is to let it do the copying and hand the location over afterwards.

That makes this service read an object the caller named, with this service's
credentials, which is a confused deputy unless the set of readable buckets is
decided by the deployment. Most of what is checked below is that boundary.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers.jobs import _GCS_URI


def parse(uri: str):
    match = _GCS_URI.match(uri)
    return match.groups() if match else None


class TestTheUriIsParsedStrictly:
    def test_a_plain_location(self):
        assert parse("gs://sprtz-uploads/equestrian/british.mp4") == (
            "sprtz-uploads", "equestrian/british.mp4")

    def test_a_deep_path_with_spaces_and_unicode(self):
        # GCS object names are a flat key space that allows both.
        bucket, name = parse("gs://sprtz-uploads/2026/Aachen — CSI/round 3.mp4")
        assert bucket == "sprtz-uploads"
        assert name == "2026/Aachen — CSI/round 3.mp4"

    def test_what_is_not_a_location(self):
        for bad in (
            "sprtz-uploads/x.mp4",                 # no scheme
            "https://storage.googleapis.com/b/x",  # the wrong scheme
            "gs://sprtz-uploads",                  # bucket only, no object
            "gs://sprtz-uploads/",                 # empty object name
            "gs:///x.mp4",                         # no bucket
            "gs://UPPER/x.mp4",                    # buckets are lower case
            "",
        ):
            assert parse(bad) is None, bad

    def test_a_control_character_is_refused(self):
        # Nothing to traverse in a flat key space, so the only thing worth
        # refusing is a header-splitting attempt.
        assert parse("gs://sprtz-uploads/a\nb.mp4") is None
        assert parse("gs://sprtz-uploads/a\x00b.mp4") is None

    def test_an_object_name_is_bounded(self):
        assert parse(f"gs://sprtz-uploads/{'a' * 1024}") is not None
        assert parse(f"gs://sprtz-uploads/{'a' * 1025}") is None


class TestTheBucketIsNotTheCallersToChoose:
    """The whole point of the check. Without it, any signed-in user could make
    this service read any object it has permission to reach."""

    def _settings(self, extra=""):
        import os

        from app.core.config import Settings

        os.environ["UPLOADS_BUCKET"] = "sprtz-uploads"
        os.environ["EXTRA_SOURCE_BUCKETS"] = extra
        return Settings()

    def test_the_uploads_bucket_is_allowed(self):
        assert "sprtz-uploads" in self._settings().source_buckets

    def test_nothing_else_is_by_default(self):
        assert self._settings().source_buckets == {"sprtz-uploads"}

    def test_a_deployment_may_name_more(self):
        assert self._settings("partner-media, archive").source_buckets == {
            "sprtz-uploads", "partner-media", "archive",
        }

    def test_empty_entries_are_not_buckets(self):
        # A trailing comma must not admit a bucket named "".
        assert "" not in self._settings("a,,").source_buckets

    @pytest.mark.parametrize("attempt", [
        "sprtz-hls", "sprtz-media", "some-other-project-bucket", "",
    ])
    def test_a_bucket_outside_the_set_is_refused(self, attempt):
        assert attempt not in self._settings().source_buckets


class TestWhatIsTrusted:
    def test_the_object_decides_its_own_size_and_name(self):
        # Both come from the blob rather than the request, because the object
        # is the thing that exists. A request could claim anything.
        source = Path(__file__).resolve().parents[1] / "app" / "routers" / "jobs.py"
        body = re.search(
            r"async def create_job_from_source[\s\S]*?\n@router", source.read_text()
        ).group(0)
        assert '"size_bytes": size_bytes' in body
        assert "blob.size" in body
        assert '"original_name": object_name.rsplit' in body
        # And nothing reads a size or a filename out of the request model.
        assert "body.size_bytes" not in body
        assert "body.filename" not in body

    def test_ownership_comes_from_the_verified_caller(self):
        source = Path(__file__).resolve().parents[1] / "app" / "routers" / "jobs.py"
        body = re.search(
            r"async def create_job_from_source[\s\S]*?\n@router", source.read_text()
        ).group(0)
        assert '"owner_uid": user.uid' in body
