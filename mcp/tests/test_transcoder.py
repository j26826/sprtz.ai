"""The Transcoder job configuration.

A wrong config here does not fail at build time or at call time — the API
accepts the job and the encode fails minutes later, or worse, succeeds and
produces something the player cannot use. These assertions are the parts that
have exactly one correct value.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("google.cloud.video.transcoder_v1")

from media_server import transcoder  # noqa: E402


@pytest.fixture
def config():
    return transcoder.build_preview_config("gs://hls/jobs/j1/hls/")


class TestPreviewRendition:
    def test_it_is_480p(self, config):
        h264 = config.elementary_streams[0].video_stream.h264
        assert h264.height_pixels == 480

    def test_the_width_is_even(self, config):
        # H.264 cannot encode odd dimensions; the encoder rejects the job.
        h264 = config.elementary_streams[0].video_stream.h264
        assert h264.width_pixels % 2 == 0

    def test_there_is_exactly_one_rendition(self, config):
        # A ladder is for public delivery. This stream exists so an editor can
        # judge a moment, and every extra rendition is encode minutes spent on
        # a picture nobody watches.
        video = [s for s in config.elementary_streams if "video" in s.key]
        assert len(video) == 1

    def test_audio_is_included(self, config):
        # Handball reads very differently with crowd noise; a silent preview
        # makes the editor open the source instead.
        audio = [s for s in config.elementary_streams if s.audio_stream.codec]
        assert len(audio) == 1


class TestSeekability:
    def test_a_keyframe_lands_on_every_segment_boundary(self, config):
        # Seeking to a moment's in point is the only thing this stream is for.
        # A GOP longer than a segment makes the player start late, which looks
        # like the timestamps being wrong rather than the encode being wrong.
        h264 = config.elementary_streams[0].video_stream.h264
        segment = config.mux_streams[0].segment_settings.segment_duration
        assert h264.gop_duration == segment


class TestPackaging:
    def test_segments_are_written_individually(self, config):
        # Without this the container is one file and there is no HLS package.
        assert config.mux_streams[0].segment_settings.individual_segments is True

    def test_the_container_is_ts(self, config):
        assert config.mux_streams[0].container == "ts"

    def test_the_manifest_is_hls(self, config):
        from google.cloud.video import transcoder_v1

        assert config.manifests[0].type_ == transcoder_v1.types.Manifest.ManifestType.HLS

    def test_the_manifest_names_the_mux_stream(self, config):
        assert config.manifests[0].mux_streams == [config.mux_streams[0].key]

    def test_the_master_playlist_name_matches_what_the_url_promises(self, config):
        # transcode_hls returns a playback URL before the encode has produced
        # anything, so this name and that URL must agree or playback 404s.
        assert config.manifests[0].file_name == transcoder.MASTER_PLAYLIST


class TestOutputLocation:
    def test_the_output_uri_ends_in_a_slash(self):
        # Transcoder treats the output as a directory prefix. Without the
        # trailing slash it prepends the last path element to every file name.
        assert transcoder.output_uri("hls-bucket", "j1").endswith("/")

    def test_the_prefix_matches_the_cdn_path_for_the_job(self):
        assert transcoder.output_uri("hls-bucket", "j1") == "gs://hls-bucket/jobs/j1/hls/"

    def test_the_config_output_is_where_the_job_writes(self, config):
        assert config.output.uri == "gs://hls/jobs/j1/hls/"


class TestJobLifecycle:
    def _client(self, state_name: str, message: str = ""):
        job = MagicMock()
        job.state.name = state_name
        job.error.message = message
        fake = MagicMock()
        fake.get_job.return_value = job
        return fake

    def test_a_running_job_is_not_done(self):
        with patch.object(transcoder, "client", return_value=self._client("RUNNING")):
            result = transcoder.job_state("projects/p/locations/l/jobs/x")

        assert result["done"] is False
        assert result["succeeded"] is False

    def test_a_succeeded_job_is_done(self):
        with patch.object(transcoder, "client", return_value=self._client("SUCCEEDED")):
            result = transcoder.job_state("projects/p/locations/l/jobs/x")

        assert result["done"] is True
        assert result["succeeded"] is True

    def test_a_failed_job_is_done_but_not_successful(self):
        with patch.object(transcoder, "client", return_value=self._client("FAILED", "bad input")):
            result = transcoder.job_state("projects/p/locations/l/jobs/x")

        assert result["done"] is True
        assert result["succeeded"] is False
        # The reason is what separates a corrupt upload from a bad job config.
        assert result["error"] == "bad input"

    def test_pending_is_treated_as_still_working(self):
        with patch.object(transcoder, "client", return_value=self._client("PENDING")):
            assert transcoder.job_state("projects/p/locations/l/jobs/x")["done"] is False

    def test_creating_a_job_points_at_the_source_and_the_prefix(self):
        fake = MagicMock()
        created = MagicMock()
        created.name = "projects/p/locations/l/jobs/abc"
        fake.create_job.return_value = created

        with patch.object(transcoder, "client", return_value=fake):
            result = transcoder.create_preview_job("gs://up/v.mp4", "hls-bucket", "j1")

        sent = fake.create_job.call_args.kwargs["job"]
        assert sent.input_uri == "gs://up/v.mp4"
        assert sent.output_uri == "gs://hls-bucket/jobs/j1/hls/"
        assert result["transcoder_job"] == "projects/p/locations/l/jobs/abc"
        assert result["master_playlist_uri"] == "gs://hls-bucket/jobs/j1/hls/master.m3u8"
