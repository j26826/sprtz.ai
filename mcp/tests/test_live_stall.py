"""A live stream that stops ends the event; it is not waited on until the end time.

Stalling is ordinary on a live stream — a class ends, the broadcaster stops the
encoder, the origin starts answering 404. Castr did exactly that at 17:01 on the
LeMieux day, and the recorder polled the 404 every three seconds for four and a
half hours until the scheduled finish, holding the event open with its bar at
44%. After `stall_sec` with nothing new the recorder now ends the way it ends at
EXT-X-ENDLIST: the partial chunk is closed, the capture is marked finished, and
the tick finishes the event normally.

The loop is driven here on a fake clock — time only moves when the recorder
sleeps — so four hours of polling is a few thousand iterations of nothing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import live_capture  # noqa: E402

T0 = datetime(2026, 9, 11, 13, 25, tzinfo=timezone.utc)


class _Playlist:
    def __init__(self, fresh: int = 0, endlist: bool = False):
        self.segments = [object()] * fresh
        self.endlist = endlist
        self.target_duration = 6.0


class _Clock:
    """Monotonic seconds, advanced only by sleep, with wall time kept in step."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return T0 + timedelta(seconds=self.t)


def _record(*, flows_for: float, stall_min: float, event_min: float = 240,
            then_404: bool = False, resumes_at: float | None = None,
            first_segment_at: float = 0.0):
    """Run a recorder over a stream that produces segments for `flows_for`
    seconds (starting at `first_segment_at`), then produces nothing — or
    answers 404 — optionally resuming at `resumes_at`."""
    clock = _Clock()
    recorder = live_capture.Recorder(
        "j1", "https://x/master.m3u8", T0 + timedelta(minutes=event_min),
        300, MagicMock(), stall_sec=stall_min * 60)

    def flowing():
        at = clock.t
        return (first_segment_at <= at < first_segment_at + flows_for
                or (resumes_at is not None and at >= resumes_at))

    def poll():
        if then_404 and not flowing() and clock.t >= first_segment_at + flows_for:
            raise RuntimeError("404 Client Error: Not Found")
        return _Playlist(fresh=1 if flowing() else 0)

    with patch.object(live_capture.time, "monotonic", clock.monotonic), \
         patch.object(live_capture.time, "sleep", clock.sleep), \
         patch.object(live_capture, "now", clock.now), \
         patch.object(recorder, "resolve", return_value=_Playlist(fresh=1 if flowing() else 0)), \
         patch.object(recorder, "poll", side_effect=poll), \
         patch.object(recorder, "take", side_effect=lambda p: len(p.segments)), \
         patch.object(recorder, "close_chunk") as close_chunk, \
         patch.object(recorder, "report"):
        code = recorder.run()
    return recorder, code, clock.t / 60, close_chunk


class TestAStreamThatStops:
    def test_it_ends_the_event_after_the_stall_limit(self):
        recorder, code, minutes, _ = _record(flows_for=30 * 60, stall_min=5)
        assert recorder.ended_by == "stalled"
        assert code == 0
        # Thirty minutes of stream, then five of nothing — not four hours.
        assert 34.9 <= minutes <= 35.2

    def test_a_stream_answering_404_is_a_stream_that_stopped(self):
        recorder, _, minutes, _ = _record(flows_for=30 * 60, stall_min=5, then_404=True)
        assert recorder.ended_by == "stalled"
        assert minutes < 36

    def test_it_ends_as_a_finished_capture_not_a_failed_one(self):
        """The tick finishes an event whose recorder exited cleanly; this is
        that path, with the partial chunk closed so its minutes are analysed."""
        recorder, code, _, close_chunk = _record(flows_for=30 * 60, stall_min=5)
        assert recorder.state == "finished"
        assert code == 0
        close_chunk.assert_called_once()

    def test_the_capture_record_says_why_it_ended(self):
        recorder, _, _, _ = _record(flows_for=30 * 60, stall_min=5)
        capture = recorder._capture()
        assert capture["endedBy"] == "stalled"
        assert capture["stallMinutes"] == 5


class TestWhatIsNotAStall:
    def test_a_stream_that_has_not_begun_is_the_lead_in(self):
        """The recorder starts five minutes early, and a broadcaster who goes
        live on the minute has produced nothing for exactly that long."""
        recorder, _, minutes, _ = _record(
            flows_for=60 * 60, stall_min=5, first_segment_at=12 * 60, event_min=120)
        # Waited twelve minutes for the first segment, recorded the hour, then stalled.
        assert recorder.ended_by == "stalled"
        assert 76.9 <= minutes <= 77.2

    def test_a_pause_shorter_than_the_limit_is_ridden_through(self):
        recorder, _, minutes, _ = _record(
            flows_for=30 * 60, stall_min=5, resumes_at=33 * 60, event_min=60)
        assert recorder.ended_by == "end"
        assert minutes >= 60

    def test_no_limit_waits_for_the_end_time_as_before(self):
        """An execution started before the setting existed carries none."""
        recorder, _, minutes, _ = _record(flows_for=30 * 60, stall_min=0, event_min=90)
        assert recorder.ended_by == "end"
        assert minutes >= 90


class TestTheStallSetting:
    def test_it_is_read_from_the_execution_environment(self):
        with patch.dict(live_capture.os.environ, {
                "JOB_ID": "j1", "HLS_URL": "https://x/m.m3u8",
                "EVENT_END": "2026-09-11T21:30:00Z", "STALL_MINUTES": "7"}), \
             patch.object(live_capture, "MEDIA_BUCKET", "b"), \
             patch.object(live_capture, "Store") as store, \
             patch.object(live_capture, "Recorder") as recorder:
            store.return_value.resume_point.return_value = {
                "next_index": 0, "cumulative_sec": 0.0, "capture_start": None}
            recorder.return_value.run.return_value = 0
            live_capture.main()
        assert recorder.call_args.kwargs["stall_sec"] == 7 * 60

    def test_a_bad_value_means_no_limit_rather_than_a_crash(self):
        with patch.dict(live_capture.os.environ, {
                "JOB_ID": "j1", "HLS_URL": "https://x/m.m3u8",
                "EVENT_END": "2026-09-11T21:30:00Z", "STALL_MINUTES": "five"}), \
             patch.object(live_capture, "MEDIA_BUCKET", "b"), \
             patch.object(live_capture, "Store") as store, \
             patch.object(live_capture, "Recorder") as recorder:
            store.return_value.resume_point.return_value = {
                "next_index": 0, "cumulative_sec": 0.0, "capture_start": None}
            recorder.return_value.run.return_value = 0
            live_capture.main()
        assert recorder.call_args.kwargs["stall_sec"] == 0


class TestARestartOntoAStreamThatHasGone:
    """A resumed recording's stream is not late, it was flowing and has gone.

    It used to be treated like a stream that had not come up yet: ten minutes
    of waiting for the playlist, then "the stream never answered", exit 1 — and
    the tick restarted it three times and then failed the whole event, over
    hours of good chunks.
    """

    def _resolve_dead(self, *, resumed: bool, stall_min: float):
        clock = _Clock()
        recorder = live_capture.Recorder(
            "j1", "https://x/master.m3u8", T0 + timedelta(hours=4), 300, MagicMock(),
            stall_sec=stall_min * 60)
        recorder.resumed = resumed
        recorder.store.stream_bucket = None
        with patch.object(live_capture.time, "monotonic", clock.monotonic), \
             patch.object(live_capture.time, "sleep", clock.sleep), \
             patch.object(live_capture, "now", clock.now), \
             patch.object(live_capture, "_fetch", side_effect=RuntimeError("404 Not Found")), \
             patch.object(recorder, "report"):
            code = recorder.run()
        return recorder, code, clock.t / 60

    def test_it_ends_the_event_cleanly_within_the_stall_limit(self):
        recorder, code, minutes = self._resolve_dead(resumed=True, stall_min=5)
        assert (recorder.ended_by, recorder.state, code) == ("stalled", "finished", 0)
        assert minutes <= 5.5                     # not the ten-minute grace

    def test_a_first_start_still_gives_a_late_producer_the_grace(self):
        """A stream that has not come up yet is the lead-in, not an ending."""
        recorder, code, minutes = self._resolve_dead(resumed=False, stall_min=5)
        assert (recorder.state, code) == ("failed", 1)
        assert minutes >= 10

    def test_without_a_limit_a_restart_fails_as_it_did(self):
        recorder, code, _ = self._resolve_dead(resumed=True, stall_min=0)
        assert (recorder.state, code) == ("failed", 1)


class TestARestartOntoAStreamThatAnswersButIsStill:
    def test_the_stall_clock_runs_from_the_restart(self):
        """No first segment is coming, so waiting for one would wait for ever."""
        clock = _Clock()
        recorder = live_capture.Recorder(
            "j1", "https://x/master.m3u8", T0 + timedelta(hours=4), 300, MagicMock(),
            stall_sec=5 * 60)
        recorder.resumed = True
        with patch.object(live_capture.time, "monotonic", clock.monotonic), \
             patch.object(live_capture.time, "sleep", clock.sleep), \
             patch.object(live_capture, "now", clock.now), \
             patch.object(recorder, "resolve", return_value=_Playlist(fresh=0)), \
             patch.object(recorder, "poll", return_value=_Playlist(fresh=0)), \
             patch.object(recorder, "take", return_value=0), \
             patch.object(recorder, "close_chunk"), patch.object(recorder, "report"):
            code = recorder.run()
        assert (recorder.ended_by, code) == ("stalled", 0)
        assert 4.9 <= clock.t / 60 <= 5.2


class TestMainMarksAResume:
    def test_a_recording_with_chunks_on_record_is_a_resume(self):
        with patch.dict(live_capture.os.environ, {
                "JOB_ID": "j1", "HLS_URL": "https://x/m.m3u8",
                "EVENT_END": "2026-09-11T21:30:00Z", "STALL_MINUTES": "5"}), \
             patch.object(live_capture, "MEDIA_BUCKET", "b"), \
             patch.object(live_capture, "Store") as store, \
             patch.object(live_capture.Recorder, "run", return_value=0):
            store.return_value.resume_point.return_value = {
                "next_index": 43, "cumulative_sec": 12878.4, "capture_start": None}
            store.return_value.read_stream.return_value = ""
            recorders = []
            original = live_capture.Recorder.__init__

            def capture(self, *a, **k):
                original(self, *a, **k)
                recorders.append(self)

            with patch.object(live_capture.Recorder, "__init__", capture):
                live_capture.main()
        assert recorders[0].resumed is True
        assert recorders[0].chunks_captured == 43
