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
TS_HEAD = {"duration_sec": 149.5, "start_sec": 1.4, "bytes": 33554433, "container": "mpegts",
           "has_audio": False, "audio_codec": ""}
TS_TAIL = {"duration_sec": 57.133, "start_sec": 13441.867, "bytes": 33554616, "container": "mpegts"}
TS_WHOLE = {"duration_sec": 13498.0, "bytes": 0, "container": "mpegts",
            "has_audio": False, "audio_codec": ""}
TS_SIZE = 6_896_527_704


class TestTheHeaderIsTrustedOnlyWhenItStatesTheDuration:
    def test_an_mp4_is_answered_from_its_header(self):
        with patch.object(server, "_head_probe", return_value=dict(MP4_HEAD)), \
             patch.object(server.ffmpeg_ops, "probe") as whole, \
             patch.object(server.gcs, "object_size", return_value=6_900_000_000):
            out = server.probe_media("gs://up/a.mp4")
        assert out["duration_sec"] == 5400.0
        whole.assert_not_called()

    def test_a_transport_stream_is_measured_from_its_two_ends(self):
        # The service's ffmpeg 7.1 read a 6.9 GB recording to the end for a
        # question ffmpeg 9 answered with one seek, and the probe timed out.
        slices = []

        def slice_probe(uri, start, end):
            slices.append((start, end))
            return dict(TS_HEAD) if start == 0 else dict(TS_TAIL)

        with patch.object(server, "_slice_probe", side_effect=slice_probe), \
             patch.object(server.ffmpeg_ops, "probe") as whole, \
             patch.object(server.gcs, "object_size", return_value=TS_SIZE):
            out = server.probe_media("gs://up/a.ts")
        assert out["duration_sec"] == pytest.approx(13497.6, abs=0.01)
        assert out["bytes"] == TS_SIZE
        whole.assert_not_called()
        start, end = slices[1]
        assert end == TS_SIZE and start % 188 == 0 and TS_SIZE - start >= 32 * 1024 * 1024

    def test_ends_that_cannot_be_placed_fall_back_to_https(self):
        def slice_probe(uri, start, end):
            return dict(TS_HEAD) if start == 0 else {"duration_sec": 0, "start_sec": 0}

        with patch.object(server, "_slice_probe", side_effect=slice_probe), \
             patch.object(server.ffmpeg_ops, "probe", return_value=dict(TS_WHOLE)) as whole, \
             patch.object(server.gcs, "https_url", return_value="https://x/a.ts"), \
             patch.object(server.gcs, "bearer_token", return_value="tok"), \
             patch.object(server.gcs, "object_size", return_value=TS_SIZE):
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


class TestTheLogCarriesNoToken:
    def test_the_header_value_is_masked(self):
        from media_server import ffmpeg_ops

        cmd = ["ffprobe", "-headers", "Authorization: Bearer ya29.secret\r\n", "-i", "https://x"]
        shown = ffmpeg_ops.redacted(cmd)
        assert "secret" not in " ".join(shown)
        assert shown[2] == "Authorization: Bearer ***"
        assert cmd[2].endswith("secret\r\n"), "the command itself is untouched"
