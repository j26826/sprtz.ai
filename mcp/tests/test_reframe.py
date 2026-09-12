"""Cutting a rendered reel to another shape.

The crop window is the thing worth guarding, and it fails quietly. A window a
few percent wrong is not an error anyone sees at render time — it is a reel
that clips the ball off the side of every shot, found out after it is posted.
So the geometry is pure and checked against the ratios it claims to produce
rather than against a remembered pixel count.

`focus_x` exists because sport is not centred. A wide arena shot cropped to
9:16 down the middle is as likely to frame an empty half as the play, so the
window can be moved — and the two things that must hold are that it never
leaves the picture at either extreme, and that anything a caller passes is
brought back inside rather than becoming a failed encode.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastmcp")

from media_server import ffmpeg_ops  # noqa: E402
from media_server.ffmpeg_ops import ASPECTS, crop_window  # noqa: E402

HD = (1920, 1080)


class TestTheWindowIsTheShapeItClaims:
    @pytest.mark.parametrize("aspect", list(ASPECTS))
    def test_the_window_has_the_target_ratio(self, aspect):
        w, h, _, _ = crop_window(*HD, aspect)
        target = ASPECTS[aspect][0] / ASPECTS[aspect][1]
        # Within a pixel of rounding to even, which H.264 requires.
        assert abs((w / h) - target) < 0.005

    def test_a_wide_source_keeps_its_full_height(self):
        # Every one of these targets is narrower than 16:9, so the only
        # decision is horizontal.
        for aspect in ASPECTS:
            _, h, _, y = crop_window(*HD, aspect)
            assert (h, y) == (1080, 0)

    def test_the_narrower_the_target_the_narrower_the_window(self):
        widths = {a: crop_window(*HD, a)[0] for a in ("9:16", "4:5", "1:1")}
        assert widths["9:16"] < widths["4:5"] < widths["1:1"]

    def test_a_vertical_target_takes_about_a_third_of_a_wide_frame(self):
        # 9:16 out of 16:9 is (9/16)/(16/9) = 31.6% of the width. Worth pinning
        # as a number: it is what the crop guide draws on screen, and the two
        # disagreeing means the preview lies about the output.
        w, _, _, _ = crop_window(*HD, "9:16")
        assert 0.31 < w / HD[0] < 0.32

    def test_dimensions_stay_even_because_h264_requires_it(self):
        for aspect in ASPECTS:
            w, h, _, _ = crop_window(1921, 1081, aspect)
            assert w % 2 == 0 and h % 2 == 0

    def test_a_portrait_source_is_cropped_the_other_way(self):
        # Not the case this feature was built for, but the maths should not
        # quietly produce a window taller than the picture.
        w, h, x, y = crop_window(1080, 1920, "1:1")
        assert (w, h) == (1080, 1080)
        assert x == 0 and y > 0


class TestMovingTheWindow:
    def test_the_default_is_centred(self):
        w, _, x, _ = crop_window(*HD, "9:16")
        assert x == round((HD[0] - w) / 2)

    def test_the_window_never_leaves_the_picture(self):
        for focus in (0.0, 0.25, 0.5, 0.75, 1.0):
            w, _, x, _ = crop_window(*HD, "9:16", focus)
            assert x >= 0
            assert x + w <= HD[0]

    def test_out_of_range_is_brought_back_rather_than_refused(self):
        # It lands in an ffmpeg filter string. A value outside the picture is a
        # failed encode, which is a worse answer than a clamped framing.
        assert crop_window(*HD, "9:16", -5)[2] == 0
        left_edge = crop_window(*HD, "9:16", 0.0)[2]
        right_edge = crop_window(*HD, "9:16", 1.0)[2]
        assert crop_window(*HD, "9:16", 9)[2] == right_edge
        assert left_edge == 0

    def test_moving_right_moves_the_window_right(self):
        xs = [crop_window(*HD, "4:5", f)[2] for f in (0.0, 0.3, 0.6, 1.0)]
        assert xs == sorted(xs)
        assert len(set(xs)) == 4


class TestTheFilterGraph:
    def _cmd(self, monkeypatch, **kw):
        seen = {}
        monkeypatch.setattr(ffmpeg_ops, "_run", lambda cmd, **_: seen.setdefault("cmd", cmd))
        ffmpeg_ops.reframe(Path("/tmp/in.mp4"), Path("/tmp/out.mp4"), **kw)
        return seen["cmd"]

    def test_a_crop_uses_the_window_it_was_given(self, monkeypatch):
        cmd = self._cmd(monkeypatch, aspect="9:16", fill="crop", focus_x=0.75,
                        probe_size=HD)
        graph = cmd[cmd.index("-filter_complex") + 1]
        w, h, x, y = crop_window(*HD, "9:16", 0.75)
        assert f"crop={w}:{h}:{x}:{y}" in graph
        assert "scale=1080:1920" in graph

    def test_blur_keeps_the_whole_frame_rather_than_a_window(self, monkeypatch):
        cmd = self._cmd(monkeypatch, aspect="1:1", fill="blur")
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "boxblur" in graph
        assert "force_original_aspect_ratio=decrease" in graph
        # Nothing is cropped away: the whole frame is scaled to fit over it.
        assert "crop=1080:1080:" not in graph

    def test_without_a_probe_it_centre_crops_in_ffmpegs_own_terms(self, monkeypatch):
        # A probe that failed must not stop the reframe; it only costs the
        # ability to move the window.
        cmd = self._cmd(monkeypatch, aspect="4:5", fill="crop", probe_size=None)
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "force_original_aspect_ratio=increase" in graph
        assert "crop=1080:1350" in graph

    def test_the_protocol_allowlist_is_carried(self, monkeypatch):
        cmd = self._cmd(monkeypatch, aspect="9:16")
        assert "-protocol_whitelist" in cmd
        assert "-nostdin" in cmd

    def test_every_shape_is_faststart_mp4(self, monkeypatch):
        for aspect in ASPECTS:
            cmd = self._cmd(monkeypatch, aspect=aspect)
            assert cmd[cmd.index("-movflags") + 1] == "+faststart"
