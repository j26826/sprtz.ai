"""The playlist is asked before the download job is started.

A signed CDN link expires. Without this the download job spent a
three-minute cold start to report "the job did not succeed", with the 403
visible only in its own log — and the pipeline then wrote "no moments" over
that. One small request says which status the URL answers, in seconds.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastmcp")
pytest.importorskip("requests")

from media_server import server  # noqa: E402


def _response(status: int, reason: str = "", body: bytes = b"#EXTM3U\n#EXT-X-VERSION:3\n"):
    resp = MagicMock()
    resp.status_code = status
    resp.reason = reason
    resp.iter_content.return_value = iter([body])
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestThePlaylistIsAskedFirst:
    def test_an_expired_signed_link_is_named_as_such(self):
        with patch.object(server.requests, "get", return_value=_response(403, "Forbidden")):
            reason = server.check_playlist("https://cdn.test/m.m3u8?sig=x&exp=1")
        assert "HTTP 403" in reason
        assert "expired" in reason

    def test_a_missing_playlist_is_a_404(self):
        with patch.object(server.requests, "get", return_value=_response(404, "Not Found")):
            assert "HTTP 404" in server.check_playlist("https://cdn.test/m.m3u8")

    def test_a_page_that_is_not_a_playlist_is_refused(self):
        with patch.object(server.requests, "get",
                          return_value=_response(200, "OK", b"<html>login</html>")):
            assert "#EXTM3U" in server.check_playlist("https://cdn.test/m.m3u8")

    def test_a_playlist_that_answers_passes(self):
        with patch.object(server.requests, "get", return_value=_response(200, "OK")):
            assert server.check_playlist("https://cdn.test/m.m3u8") == ""

    def test_a_dead_host_is_reported_not_raised(self):
        with patch.object(server.requests, "get",
                          side_effect=server.requests.ConnectionError("no route")):
            reason = server.check_playlist("https://cdn.test/m.m3u8")
        assert "could not be fetched" in reason

    def test_the_job_is_not_started_when_the_url_does_not_answer(self):
        started = MagicMock()
        with patch.object(server, "HLS2MP4_JOB", "projects/p/locations/l/jobs/hls2mp4"), \
             patch.object(server, "UPLOADS_BUCKET", "uploads"), \
             patch.object(server.requests, "get", return_value=_response(403, "Forbidden")), \
             patch.object(server.runjobs, "run", started):
            out = server.download_hls("j1", "https://cdn.test/m.m3u8?exp=1")
        assert out["status"] == "error"
        assert "HTTP 403" in out["error"]
        started.assert_not_called()


class TestDownloadProgressIsReadFromTheLog:
    """The download streams into one object that appears only when it is done.

    The bucket shows nothing for the whole download, so the bar sat at the
    start of ingest for minutes and was reported as not moving. The job's own
    log has one line per segment, and that is what the poll reads.
    """

    def test_the_segment_line_parses(self):
        from media_server import runjobs

        assert runjobs.parse_segment_progress(
            "[851/3375] Streaming segment to cloud: manifest-video_0-851.ts") == (851, 3375)

    def test_other_lines_do_not(self):
        from media_server import runjobs

        assert runjobs.parse_segment_progress("-> Distributed download: 16 parallel") is None
        assert runjobs.parse_segment_progress("[0/0] Streaming segment") is None
        assert runjobs.parse_segment_progress("") is None

    def test_a_running_download_carries_its_fraction(self):
        with patch.object(server.runjobs, "execution_state",
                          return_value={"state": "running", "execution": "e"}), \
             patch.object(server.runjobs, "execution_progress",
                          return_value={"segments_done": 851, "segments_total": 3375,
                                        "fraction": 851 / 3375}):
            out = server.hls_download_status("projects/p/locations/l/jobs/j/executions/e", "j1")
        assert out["status"] == "running"
        assert out["segments_done"] == 851
        assert 0.25 < out["fraction"] < 0.26

    def test_a_log_that_cannot_be_read_does_not_fail_the_poll(self):
        with patch.object(server.runjobs, "execution_state",
                          return_value={"state": "running", "execution": "e"}), \
             patch.object(server.runjobs, "execution_progress",
                          side_effect=RuntimeError("logging api down")):
            out = server.hls_download_status("projects/p/locations/l/jobs/j/executions/e", "j1")
        assert out["status"] == "running"
        assert "fraction" not in out
