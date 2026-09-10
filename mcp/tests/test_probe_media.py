"""What a prefix of a file says about the whole file.

A faststart MP4's header states the duration, so 32 MiB of it is the whole
answer. A transport stream has no such header: ffprobe reports the duration
of what it was given, and the first 32 MiB of a 6.9 GB recording probed as
149 seconds — the analysis then ran on one window and found nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastmcp")

from media_server import server  # noqa: E402

MP4_HEAD = {"duration_sec": 5400.0, "bytes": 33554433, "container": "mov,mp4,m4a,3gp,3g2,mj2",
            "has_audio": True, "audio_codec": "aac"}
TS_HEAD = {"duration_sec": 149.5, "bytes": 33554433, "container": "mpegts",
           "has_audio": False, "audio_codec": ""}
TS_WHOLE = {"duration_sec": 13498.0, "bytes": 0, "container": "mpegts",
            "has_audio": False, "audio_codec": ""}


class TestTheHeaderIsTrustedOnlyWhenItStatesTheDuration:
    def test_an_mp4_is_answered_from_its_header(self):
        with patch.object(server, "_head_probe", return_value=dict(MP4_HEAD)), \
             patch.object(server.ffmpeg_ops, "probe") as whole, \
             patch.object(server.gcs, "object_size", return_value=6_900_000_000):
            out = server.probe_media("gs://up/a.mp4")
        assert out["duration_sec"] == 5400.0
        whole.assert_not_called()

    def test_a_transport_stream_is_probed_whole(self):
        with patch.object(server, "_head_probe", return_value=dict(TS_HEAD)), \
             patch.object(server.ffmpeg_ops, "probe", return_value=dict(TS_WHOLE)) as whole, \
             patch.object(server.gcs, "https_url", return_value="https://x/a.ts"), \
             patch.object(server.gcs, "bearer_token", return_value="tok"), \
             patch.object(server.gcs, "object_size", return_value=6_896_527_704):
            out = server.probe_media("gs://up/a.ts")
        assert out["duration_sec"] == 13498.0
        whole.assert_called_once()

    def test_the_size_is_the_objects_not_the_reads(self):
        with patch.object(server, "_head_probe", return_value=dict(MP4_HEAD)), \
             patch.object(server.gcs, "object_size", return_value=6_900_000_000):
            out = server.probe_media("gs://up/a.mp4")
        assert out["bytes"] == 6_900_000_000


class TestASilentSourceIsEncodedWithoutAudio:
    def test_the_head_decides(self):
        with patch.object(server, "_head_probe", return_value=dict(TS_HEAD)):
            assert server._source_has_audio("gs://up/a.ts") is False
        with patch.object(server, "_head_probe", return_value=dict(MP4_HEAD)):
            assert server._source_has_audio("gs://up/a.mp4") is True

    def test_unreadable_means_assume_audio(self):
        with patch.object(server, "_head_probe", side_effect=OSError("no")):
            assert server._source_has_audio("gs://up/a.ts") is True

    def test_the_proxy_job_is_told(self):
        from unittest.mock import MagicMock

        create = MagicMock(return_value={"transcoder_job": "t", "output_uri": "o", "analysis_uri": "a"})
        with patch.object(server, "MEDIA_BUCKET", "media"), \
             patch.object(server.gcs, "delete_prefix"), \
             patch.object(server, "_source_has_audio", return_value=False), \
             patch.object(server.transcoder, "create_proxy_job", create):
            server.make_analysis_proxy("gs://up/a.ts", "j1")
        assert create.call_args.kwargs["audio"] is False
