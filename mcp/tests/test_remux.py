"""Muxing a separate audio rendition into a silent recording."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastmcp")
pytest.importorskip("requests")

from media_server import hls, remux, server  # noqa: E402

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="en",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,AUDIO="a"
video.m3u8
"""
TS_MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nv1.ts\n#EXTINF:4.0,\nv2.ts\n#EXT-X-ENDLIST\n"
CMAF_MEDIA = "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4.0,\nv1.m4s\n#EXT-X-ENDLIST\n"


class TestTheFfmpegCommand:
    def test_it_is_a_stream_copy_of_video_from_the_url_and_audio_from_disk(self):
        cmd = remux.ffmpeg_command("https://x/v.ts", "/tmp/a.ts", "tok")
        assert cmd[cmd.index("-i") + 1] == "https://x/v.ts"
        assert cmd[cmd.index("-i", cmd.index("-i") + 1) + 1] == "/tmp/a.ts"
        assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
        assert cmd[-3:] == ["-f", "mpegts", "pipe:1"]
        assert "-nostdin" in cmd

    def test_the_bearer_token_reaches_only_the_video_input(self):
        cmd = remux.ffmpeg_command("https://x/v.ts", "/tmp/a.ts", "tok")
        assert cmd.index("-headers") < cmd.index("-i")
        assert cmd.count("-headers") == 1

    def test_one_video_and_one_audio_stream_are_mapped(self):
        cmd = remux.ffmpeg_command("https://x/v.ts", "/tmp/a.ts", None)
        maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
        assert maps == ["0:v:0", "1:a:0"]


class TestAudioIsFetchedInOrder:
    def test_segments_are_written_in_playlist_order(self, tmp_path):
        playlist = hls.parse_media(TS_MEDIA, "https://x/")
        fetched = {"https://x/v1.ts": b"AAAA", "https://x/v2.ts": b"BBBB"}
        with patch.object(remux, "_fetch", side_effect=lambda u: fetched[u]):
            total = remux.download_audio(playlist, tmp_path / "a.ts")
        assert total == 8
        assert (tmp_path / "a.ts").read_bytes() == b"AAAABBBB"

    def test_an_init_segment_comes_first(self, tmp_path):
        playlist = hls.parse_media(CMAF_MEDIA, "https://x/")
        fetched = {"https://x/init.mp4": b"INIT", "https://x/v1.m4s": b"SEG"}
        with patch.object(remux, "_fetch", side_effect=lambda u: fetched[u]):
            remux.download_audio(playlist, tmp_path / "a.mp4")
        assert (tmp_path / "a.mp4").read_bytes() == b"INITSEG"


class TestTheDownloadToolNamesTheAudio:
    def _fetch(self, by_url):
        def fake(url):
            for key, text in by_url.items():
                if url.endswith(key):
                    return "", text
            return "The playlist URL answered HTTP 404 Not Found.", ""
        return fake

    def test_a_video_only_ts_variant_gets_its_audio_playlist(self):
        with patch.object(server, "fetch_playlist",
                          side_effect=self._fetch({"master.m3u8": MASTER, "video.m3u8": TS_MEDIA})):
            assert server.separate_audio_for(MASTER, "https://x/master.m3u8") == "https://x/audio.m3u8"

    def test_a_cmaf_variant_is_muxed_by_the_download_tool_itself(self):
        with patch.object(server, "fetch_playlist",
                          side_effect=self._fetch({"master.m3u8": MASTER, "video.m3u8": CMAF_MEDIA})):
            assert server.separate_audio_for(MASTER, "https://x/master.m3u8") == ""

    def test_a_media_playlist_url_has_nothing_to_say(self):
        assert server.separate_audio_for(TS_MEDIA, "https://x/video.m3u8") == ""

    def test_download_hls_reports_it(self):
        with patch.object(server, "HLS2MP4_JOB", "projects/p/locations/l/jobs/h"), \
             patch.object(server, "UPLOADS_BUCKET", "uploads"), \
             patch.object(server, "fetch_playlist",
                          side_effect=self._fetch({"master.m3u8": MASTER, "video.m3u8": TS_MEDIA})), \
             patch.object(server.gcs, "delete_prefix"), \
             patch.object(server.runjobs, "run", return_value="exec-1"):
            out = server.download_hls("j1", "https://x/master.m3u8")
        assert out["status"] == "started"
        assert out["audio_playlist_url"] == "https://x/audio.m3u8"


class TestTheMuxTools:
    def test_mux_audio_starts_the_job_with_what_it_needs(self):
        run = MagicMock(return_value="projects/p/locations/l/jobs/remux/executions/e1")
        with patch.object(server, "REMUX_JOB", "projects/p/locations/l/jobs/remux"), \
             patch.object(server, "UPLOADS_BUCKET", "uploads"), \
             patch.object(server.gcs, "delete_prefix"), \
             patch.object(server.runjobs, "run", run):
            out = server.mux_audio("j1", "gs://uploads/hls/j1/source/source.ts", "https://x/audio.m3u8")
        assert out["status"] == "started"
        assert out["output_uri"] == "gs://uploads/hls/j1/muxed/source.ts"
        env = run.call_args.args[1]
        assert env["VIDEO_URI"] == "gs://uploads/hls/j1/source/source.ts"
        assert env["AUDIO_PLAYLIST_URL"] == "https://x/audio.m3u8"
        assert env["OUTPUT_URI"] == out["output_uri"]

    def test_mux_status_hands_over_the_muxed_object_and_drops_the_silent_one(self):
        delete = MagicMock()
        with patch.object(server.runjobs, "execution_state", return_value={"state": "succeeded"}), \
             patch.object(server.gcs, "object_size", return_value=7_000_000_000), \
             patch.object(server.gcs, "delete_object", delete):
            out = server.mux_status("e1", "gs://uploads/hls/j1/muxed/source.ts",
                                    "gs://uploads/hls/j1/source/source.ts")
        assert out["status"] == "succeeded"
        assert out["gcs_uri"].endswith("muxed/source.ts")
        assert out["bytes"] == 7_000_000_000
        delete.assert_called_once_with("gs://uploads/hls/j1/source/source.ts")

    def test_a_finished_mux_with_no_object_is_a_failure(self):
        with patch.object(server.runjobs, "execution_state", return_value={"state": "succeeded"}), \
             patch.object(server.gcs, "object_size", return_value=0), \
             patch.object(server.gcs, "delete_object") as delete:
            out = server.mux_status("e1", "gs://u/o.ts", "gs://u/s.ts")
        assert out["status"] == "failed"
        delete.assert_not_called()

    def test_a_running_mux_is_still_running(self):
        with patch.object(server.runjobs, "execution_state", return_value={"state": "running"}):
            assert server.mux_status("e1", "gs://u/o.ts")["status"] == "running"


class TestFetchesRetry:
    """Three thousand fetches from a CDN drop a connection now and then.

    The first real mux lost everything to one "Remote end closed connection
    without response" on an audio segment.
    """

    def _session(self, answers):
        sess = MagicMock()
        sess.get.side_effect = answers
        return sess

    def _ok(self, body=b"SEG"):
        r = MagicMock(); r.status_code = 200; r.content = body; r.raise_for_status = MagicMock()
        return r

    def test_a_dropped_connection_is_retried(self):
        import requests

        sess = self._session([requests.ConnectionError("Remote end closed connection"), self._ok()])
        with patch.object(remux, "_session", MagicMock(s=sess)), patch.object(remux.time, "sleep"):
            assert remux._fetch("https://x/a1.ts") == b"SEG"
        assert sess.get.call_count == 2

    def test_a_server_error_is_retried_but_a_client_error_is_not(self):
        import requests

        bad = MagicMock(); bad.status_code = 503
        sess = self._session([bad, self._ok()])
        with patch.object(remux, "_session", MagicMock(s=sess)), patch.object(remux.time, "sleep"):
            assert remux._fetch("https://x/a1.ts") == b"SEG"

        gone = MagicMock(); gone.status_code = 404
        gone.raise_for_status.side_effect = requests.HTTPError("404", response=gone)
        sess = self._session([gone, self._ok()])
        with patch.object(remux, "_session", MagicMock(s=sess)), patch.object(remux.time, "sleep"):
            with pytest.raises(requests.HTTPError):
                remux._fetch("https://x/a1.ts")
        assert sess.get.call_count == 1

    def test_it_gives_up_with_the_last_reason(self):
        import requests

        sess = self._session([requests.ConnectionError("down")] * remux.FETCH_ATTEMPTS)
        with patch.object(remux, "_session", MagicMock(s=sess)), patch.object(remux.time, "sleep"):
            with pytest.raises(RuntimeError, match="gave up"):
                remux._fetch("https://x/a1.ts")


class TestAFailedMuxSaysSo:
    def test_mux_status_names_the_failure(self):
        with patch.object(server.runjobs, "execution_state",
                          return_value={"state": "failed", "failed_count": 1}):
            out = server.mux_status("e1", "gs://u/o.ts", "gs://u/s.ts")
        assert out["status"] == "failed"
        assert "remux execution failed" in out["error"]
