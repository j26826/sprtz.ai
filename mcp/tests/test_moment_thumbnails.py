"""The still cut for each moment.

Two things decide whether the picture is the right one: it is taken at the
moment's peak rather than its start, and it is a keyframe rather than whatever
frame happens to sit on that timestamp. Both are ffmpeg arguments, so both are
checked by reading the command rather than by running it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import ffmpeg_ops  # noqa: E402


@pytest.fixture
def command(tmp_path):
    """Capture the ffmpeg argv, and write a file so the caller sees success."""
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3600):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"\x89PNG fake")
        return ""

    with patch.object(ffmpeg_ops, "_run", fake_run):
        yield calls


class TestTheFrameItPicks:
    def test_it_asks_for_keyframes_only(self, command, tmp_path):
        ffmpeg_ops.keyframe_thumbnail("https://x/v.mp4", tmp_path / "m.png", 2835.0)
        argv = command[0]
        assert "-skip_frame" in argv
        assert argv[argv.index("-skip_frame") + 1] == "nokey"

    def test_the_seek_is_the_peak_and_precedes_the_input(self, command, tmp_path):
        ffmpeg_ops.keyframe_thumbnail("https://x/v.mp4", tmp_path / "m.png", 2835.0)
        argv = command[0]
        # -ss after -i decodes from the start of the file: on a three-hour match
        # that is the whole object rather than a range read.
        assert argv.index("-ss") < argv.index("-i")
        assert argv[argv.index("-ss") + 1] == "2835.000"

    def test_skip_frame_applies_to_the_decoder_not_the_output(self, command, tmp_path):
        # It is an input option: after -i it would be read as an output option
        # and do nothing, silently giving an ordinary frame.
        ffmpeg_ops.keyframe_thumbnail("https://x/v.mp4", tmp_path / "m.png", 10.0)
        argv = command[0]
        assert argv.index("-skip_frame") < argv.index("-i")

    def test_one_frame_at_the_requested_width(self, command, tmp_path):
        ffmpeg_ops.keyframe_thumbnail("https://x/v.mp4", tmp_path / "m.png", 10.0, width=320)
        argv = command[0]
        assert argv[argv.index("-frames:v") + 1] == "1"
        assert argv[argv.index("-vf") + 1] == "scale=320:-2"

    def test_a_png_is_what_lands(self, command, tmp_path):
        dest = tmp_path / "m.png"
        ffmpeg_ops.keyframe_thumbnail("https://x/v.mp4", dest, 10.0)
        assert command[0][-1] == str(dest)


class TestHardening:
    def test_the_protocol_allowlist_is_carried(self, command, tmp_path):
        # Same reasoning as every other ffmpeg call here: a crafted file must
        # not be able to steer this at the metadata server.
        for run in (ffmpeg_ops.keyframe_thumbnail, ffmpeg_ops.still_frame):
            command.clear()
            run("https://x/v.mp4", tmp_path / "m.png", 10.0)
            assert "-protocol_whitelist" in command[0]
            assert "-nostdin" in command[0]

    def test_a_bearer_token_is_sent_for_an_https_source(self, command, tmp_path):
        ffmpeg_ops.keyframe_thumbnail("https://x/v.mp4", tmp_path / "m.png", 10.0,
                                      bearer_token="tok")
        assert any("Authorization: Bearer tok" in a for a in command[0])

    def test_a_local_source_gets_no_http_arguments(self, command, tmp_path):
        ffmpeg_ops.keyframe_thumbnail(tmp_path / "v.mp4", tmp_path / "m.png", 10.0,
                                      bearer_token="tok")
        assert "-reconnect" not in command[0]


class TestWhenThereIsNoKeyframe:
    """A peak inside the file's last GOP has none after it."""

    def test_it_reports_failure_rather_than_raising(self, tmp_path):
        def explode(cmd, timeout=3600):
            raise ffmpeg_ops.FfmpegError("Output file does not contain any stream")

        with patch.object(ffmpeg_ops, "_run", explode):
            assert ffmpeg_ops.keyframe_thumbnail("https://x/v.mp4", tmp_path / "m.png", 1.0) is False

    def test_an_empty_output_is_not_a_thumbnail(self, tmp_path):
        def write_nothing(cmd, timeout=3600):
            Path(cmd[-1]).write_bytes(b"")
            return ""

        with patch.object(ffmpeg_ops, "_run", write_nothing):
            assert ffmpeg_ops.keyframe_thumbnail("https://x/v.mp4", tmp_path / "m.png", 1.0) is False

    def test_the_fallback_reads_the_exact_frame(self, command, tmp_path):
        ffmpeg_ops.still_frame("https://x/v.mp4", tmp_path / "m.png", 2835.0)
        argv = command[0]
        assert "-skip_frame" not in argv
        assert argv[argv.index("-ss") + 1] == "2835.000"
        # -q:v is a JPEG control and means nothing to the png encoder; passing
        # it here would only be cargo from the poster path.
        assert "-q:v" not in argv


# --- the tool ----------------------------------------------------------------

pytest.importorskip("fastmcp")

from media_server import server  # noqa: E402

# fastmcp wraps a tool in a FunctionTool; the plain function is what we call.
generate = getattr(server.generate_moment_thumbnails, "fn", server.generate_moment_thumbnails)


@pytest.fixture
def media(tmp_path):
    """A media server whose ffmpeg always succeeds and whose GCS records uploads."""
    uploads: list[str] = []

    def fake_keyframe(source, dest, at_sec, width=320, bearer_token=None):
        Path(dest).write_bytes(b"\x89PNG")
        return True

    with (
        patch.object(server, "MEDIA_BUCKET", "media-bucket"),
        patch.object(server.gcs, "bearer_token", lambda: "tok"),
        patch.object(server.gcs, "https_url", lambda uri: "https://x/v.mp4"),
        patch.object(server.gcs, "upload", lambda local, uri, **kw: uploads.append(uri)),
        patch.object(server.ffmpeg_ops, "keyframe_thumbnail", fake_keyframe),
    ):
        yield uploads


class TestTheTool:
    def test_each_moment_is_written_under_the_job(self, media):
        out = generate("gs://uploads/v.mp4", "job1", [
            {"moment_id": "m1", "at_sec": 10.0},
            {"moment_id": "m2", "at_sec": 20.0},
        ])
        assert out["status"] == "success"
        assert media == [
            "gs://media-bucket/jobs/job1/moments/m1.png",
            "gs://media-bucket/jobs/job1/moments/m2.png",
        ]
        assert [t["moment_id"] for t in out["thumbnails"]] == ["m1", "m2"]

    def test_the_peak_is_what_is_read(self, media):
        seen: list[float] = []
        with patch.object(server.ffmpeg_ops, "keyframe_thumbnail",
                          lambda s, d, at, **kw: (Path(d).write_bytes(b"x"), seen.append(at))[0] is None):
            generate("gs://uploads/v.mp4", "job1", [{"moment_id": "m1", "at_sec": 2835.0}])
        assert seen == [2835.0]

    def test_a_moment_id_cannot_escape_the_job_prefix(self, media):
        # The id comes from a model's output by way of the pipeline, and it
        # becomes a path segment here.
        generate("gs://uploads/v.mp4", "job1", [{"moment_id": "../../poster", "at_sec": 1.0}])
        assert media == ["gs://media-bucket/jobs/job1/moments/_poster.png"]

    def test_an_empty_id_is_reported_rather_than_written(self, media):
        out = generate("gs://uploads/v.mp4", "job1", [{"moment_id": "", "at_sec": 1.0}])
        assert media == []
        assert out["failures"][0]["moment_id"] == ""

    def test_one_unreadable_frame_does_not_lose_the_batch(self, media):
        def sometimes(source, dest, at_sec, width=320, bearer_token=None):
            if at_sec == 20.0:
                raise server.ffmpeg_ops.FfmpegError("could not seek")
            Path(dest).write_bytes(b"\x89PNG")
            return True

        with patch.object(server.ffmpeg_ops, "keyframe_thumbnail", sometimes), \
             patch.object(server.ffmpeg_ops, "still_frame", sometimes):
            out = generate("gs://uploads/v.mp4", "job1", [
                {"moment_id": "m1", "at_sec": 10.0},
                {"moment_id": "m2", "at_sec": 20.0},
                {"moment_id": "m3", "at_sec": 30.0},
            ])

        assert [t["moment_id"] for t in out["thumbnails"]] == ["m1", "m3"]
        assert [f["moment_id"] for f in out["failures"]] == ["m2"]
        assert out["status"] == "success", "two of three is a result, not an error"

    def test_no_keyframe_falls_back_to_an_exact_frame(self, media):
        used: list[str] = []

        def no_keyframe(source, dest, at_sec, width=320, bearer_token=None):
            used.append("keyframe")
            return False

        def exact(source, dest, at_sec, width=320, bearer_token=None):
            used.append("exact")
            Path(dest).write_bytes(b"\x89PNG")

        with patch.object(server.ffmpeg_ops, "keyframe_thumbnail", no_keyframe), \
             patch.object(server.ffmpeg_ops, "still_frame", exact):
            out = generate("gs://uploads/v.mp4", "job1", [{"moment_id": "m1", "at_sec": 1.0}])

        assert used == ["keyframe", "exact"]
        assert out["thumbnails"][0]["on_keyframe"] is False
        assert out["count"] == 1
