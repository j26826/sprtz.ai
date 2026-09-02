"""Upload validation.

Everything here runs against probe output rather than the client's claims: a
filename and a Content-Type are both chosen by whoever uploads the file, so the
only evidence that an upload is a video is that a decoder could read it as one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import ffmpeg_ops  # noqa: E402


def good(**over) -> dict:
    base = {
        "duration_sec": 5400.0, "width": 1920, "height": 1080,
        "video_codec": "h264", "audio_codec": "aac", "fps": 25.0,
    }
    base.update(over)
    return base


class TestAccepts:
    def test_a_normal_match_recording(self):
        assert ffmpeg_ops.validate(good()) == []

    def test_video_with_no_audio_track(self):
        assert ffmpeg_ops.validate(good(audio_codec="")) == []

    def test_declared_video_content_type(self):
        assert ffmpeg_ops.validate(good(), declared_content_type="video/mp4") == []


class TestRejects:
    def test_a_file_with_no_video_stream(self):
        """An audio file or a document renamed to .mp4."""
        reasons = ffmpeg_ops.validate(good(video_codec="", width=0, height=0))
        assert any("not a video" in r for r in reasons)

    def test_zero_duration(self):
        reasons = ffmpeg_ops.validate(good(duration_sec=0))
        assert any("duration" in r for r in reasons)

    def test_absurd_duration(self):
        reasons = ffmpeg_ops.validate(good(duration_sec=20 * 3600))
        assert any("exceeds" in r for r in reasons)

    def test_decompression_bomb_dimensions(self):
        """A small file can declare an enormous frame and blow up on decode."""
        reasons = ffmpeg_ops.validate(good(width=100000, height=100000))
        assert any("limit" in r for r in reasons)

    def test_total_pixels_even_when_each_side_is_legal(self):
        reasons = ffmpeg_ops.validate(good(width=7000, height=7000))
        assert any("pixel limit" in r for r in reasons)

    def test_unsupported_video_codec(self):
        reasons = ffmpeg_ops.validate(good(video_codec="rv40"))
        assert any("rv40" in r for r in reasons)

    def test_unsupported_audio_codec(self):
        reasons = ffmpeg_ops.validate(good(audio_codec="ralf"))
        assert any("ralf" in r for r in reasons)

    def test_non_video_declared_content_type(self):
        reasons = ffmpeg_ops.validate(good(), declared_content_type="application/zip")
        assert any("content type" in r for r in reasons)

    def test_every_reason_is_reported_at_once(self):
        """So a bad upload is explained in one go, not one round trip per fault."""
        reasons = ffmpeg_ops.validate(
            good(duration_sec=0, width=0, height=0, video_codec="", audio_codec="ralf")
        )
        assert len(reasons) >= 3


class TestHardening:
    """Verified against the built image, not just asserted here — an http://
    input is refused with "Protocol 'http' not on whitelist" while https://
    still reaches GCS."""

    def _protocols(self) -> set[str]:
        return set(ffmpeg_ops._ALLOWED_PROTOCOLS.split(","))

    def test_plain_http_is_blocked(self):
        """GCP's metadata server is http://169.254.169.254. Allowing plain http
        would let a crafted file steer ffmpeg into fetching this service
        account's access token."""
        assert "http" not in self._protocols()

    def test_https_and_its_transport_are_allowed(self):
        """https is layered on tcp; omitting tcp breaks every GCS read."""
        assert {"https", "tcp", "tls"} <= self._protocols()

    def test_local_files_are_allowed(self):
        """cut, thumbnail and remux all read from scratch."""
        assert "file" in self._protocols()

    def test_ffprobe_does_not_get_nostdin(self):
        """ffprobe has no such option and consumes the next argument as its
        value, failing with "Option not found"."""
        assert "-nostdin" not in ffmpeg_ops._PROBE_HARDENING
        assert "-nostdin" in ffmpeg_ops._FFMPEG_HARDENING

    def test_both_tools_carry_the_protocol_whitelist(self):
        for flags in (ffmpeg_ops._PROBE_HARDENING, ffmpeg_ops._FFMPEG_HARDENING):
            assert "-protocol_whitelist" in flags
