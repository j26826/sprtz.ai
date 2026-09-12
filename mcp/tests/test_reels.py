"""What a reel is allowed to be, and what a cut is allowed to mean.

Two things here are worth guarding and neither fails loudly on its own.

The first is the bound on a cut. It lived only in the API route while the
editor's Download button was the only door to it; a reel renders many cuts at
once and an agent can be asked to publish one, and neither goes through that
route. `clamp_cut` is that rule moved to where the record is, and the tests
below are the boundary conditions the route's own suite covers, restated
against the moved code — if it ever stops agreeing with the route, "publish
this moment" can quietly become an hour of the match under a moment's name.

The second is the edit list. A reel drawing on several matches is one
Transcoder job with an input per match and an atom per cut, and the failure
mode is silent: a cut whose source is missing simply produces no atom, so a
reel referencing a deleted match would render short and look finished. The
config tests pin the mapping, and `render_reel` refuses rather than skips.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastmcp")
pytest.importorskip("google.cloud.video.transcoder_v1")

from catalog_server import store  # noqa: E402
from media_server import transcoder  # noqa: E402

SLACK = store.TRIM_SLACK_SEC
MAX_CUT = store.MAX_CUT_SEC


class TestACutIsBoundedByItsRecord:
    def test_no_request_at_all_is_the_moment_itself(self):
        assert store.clamp_cut(100.0, 110.0) == (100.0, 110.0)

    def test_a_request_for_everything_is_clamped_to_the_slack_either_side(self):
        # Not a refusal. A slider dragged to its end should stop.
        assert store.clamp_cut(100.0, 110.0, 0.0, 1e9) == (0.0, 110.0 + SLACK)

    def test_neither_end_may_stray_further_than_the_slack(self):
        start, end = store.clamp_cut(1000.0, 1010.0, 0.0, 99999.0)
        assert start == 1000.0 - SLACK
        assert end == 1010.0 + SLACK

    def test_the_in_point_cannot_go_below_zero_on_an_early_moment(self):
        # A moment 5s into the match has less than the slack available.
        start, _ = store.clamp_cut(5.0, 12.0, -500.0, None)
        assert start == 0.0

    def test_an_inverted_request_still_yields_a_second_of_video(self):
        start, end = store.clamp_cut(100.0, 110.0, 108.0, 20.0)
        assert end > start
        assert end == start + 1.0

    def test_no_cut_may_exceed_the_ceiling(self):
        # A long moment plus slack at both ends would otherwise beat MAX_CUT.
        start, end = store.clamp_cut(0.0, 5000.0, 0.0, 5000.0)
        assert end - start == MAX_CUT

    def test_a_zero_length_record_is_survivable(self):
        start, end = store.clamp_cut(0.0, 0.0)
        assert end > start


class TestCutsAreOrderedAndBounded:
    def _cuts(self, n):
        return [{"jobId": "j", "momentId": f"m{i}", "startMs": i * 1000,
                 "endMs": i * 1000 + 800} for i in range(n)]

    def test_cuts_are_renumbered_in_the_order_given(self):
        ordered, _ = store.normalise_cuts(self._cuts(3))
        assert [c["order"] for c in ordered] == [0, 1, 2]
        assert [c["cutId"] for c in ordered] == ["c000", "c001", "c002"]

    def test_the_total_is_the_sum_of_the_cuts_not_the_span_they_cover(self):
        # Three 800ms cuts spread over three seconds is 2.4s of video, not 2.8s.
        _, total = store.normalise_cuts(self._cuts(3))
        assert total == 2400

    def test_more_cuts_than_the_ceiling_are_dropped_rather_than_accepted(self):
        ordered, _ = store.normalise_cuts(self._cuts(store.MAX_REEL_CUTS + 20))
        assert len(ordered) == store.MAX_REEL_CUTS

    def test_a_negative_or_inverted_cut_is_coerced_not_stored(self):
        ordered, _ = store.normalise_cuts([{"jobId": "j", "startMs": -50, "endMs": -900}])
        assert ordered[0]["startMs"] == 0
        assert ordered[0]["endMs"] > ordered[0]["startMs"]

    def test_a_cut_keeps_its_own_millisecond(self):
        # The point of storing integers: a decision made by dragging a handle
        # should not walk every time the reel is saved.
        ordered, _ = store.normalise_cuts([{"jobId": "j", "startMs": 10_001, "endMs": 22_749}])
        assert (ordered[0]["startMs"], ordered[0]["endMs"]) == (10_001, 22_749)


class TestTheReelProjection:
    def test_an_empty_document_still_has_the_safe_defaults(self):
        out = store._reel_out({})
        assert out["privacy"] == "private"
        assert out["aspect"] == "16:9"
        assert out["cutCount"] == 0

    def test_nothing_is_ever_public_by_default(self):
        # Publishing cannot be undone and the reel has not been watched yet.
        assert store._reel_out({})["privacy"] == "private"


class TestTheEditListIsTheConcatenation:
    CUTS = [
        {"jobId": "jobA", "startMs": 125_500, "endMs": 139_250},
        {"jobId": "jobB", "startMs": 10_001, "endMs": 22_750},
        {"jobId": "jobA", "startMs": 400_000, "endMs": 409_500},
    ]
    SOURCES = {"jobA": "gs://media/jobs/jobA/source.mp4",
               "jobB": "gs://media/jobs/jobB/source.mp4"}

    def _config(self, **kw):
        return transcoder.build_reel_config("gs://media/reels/r1/", self.CUTS, self.SOURCES, **kw)

    def test_each_distinct_match_becomes_one_input(self):
        cfg = self._config()
        assert [i.key for i in cfg.inputs] == ["in0", "in1"]
        assert [i.uri for i in cfg.inputs] == [self.SOURCES["jobA"], self.SOURCES["jobB"]]

    def test_cuts_may_interleave_the_matches_they_came_from(self):
        # This is what makes a cross-event reel one encode rather than three.
        assert [list(a.inputs)[0] for a in self._config().edit_list] == ["in0", "in1", "in0"]

    def test_the_atoms_are_in_reel_order(self):
        assert [a.key for a in self._config().edit_list] == ["atom0", "atom1", "atom2"]

    def test_a_millisecond_survives_the_trip_to_the_encoder(self):
        # Duration carries nanoseconds, so nothing is rounded to the second.
        first = self._config().edit_list[1]
        assert round(first.start_time_offset.total_seconds() * 1000) == 10_001
        assert round(first.end_time_offset.total_seconds() * 1000) == 22_750

    def test_a_silent_source_drops_the_audio_stream_rather_than_failing_late(self):
        # Transcoder asked for an AAC stream it cannot fill fails minutes in.
        keys = [s.key for s in self._config(audio=False).elementary_streams]
        assert keys == ["video-reel"]

    def test_the_three_fields_that_must_agree_still_do(self):
        # fillContentGaps needs DROP_DUPLICATE needs optimization DISABLED, and
        # the API refuses the job at creation if they disagree.
        # Compared by name: proto-plus coerces the string we set into an enum
        # member on the way back out.
        h264 = self._config().elementary_streams[0].video_stream.h264
        assert h264.frame_rate_conversion_strategy.name == transcoder.FRAME_RATE_CONVERSION
        assert transcoder.FILL_CONTENT_GAPS is True
        assert transcoder.OPTIMIZATION == "DISABLED"

    def test_the_object_name_is_known_before_the_encode_starts(self):
        assert transcoder.reel_output_uri("media", "r1") == "gs://media/reels/r1/"
        assert transcoder.REEL_FILE_NAME == "reel.mp4"
