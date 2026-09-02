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
