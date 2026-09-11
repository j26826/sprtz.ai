"""A live event as a stream that can be watched while it is still on.

A live event had nothing to play until someone packaged it, and packaging is an
encode of the whole recording — so every moment found while the event was on
opened on "not packaged for playback yet", eleven times on the LeMieux day. The
recorder already holds each segment as it arrives; writing them where the CDN
serves from, with a playlist beside them, is the event as a stream from its
first analysed chunk, with no encode.

The playlist is pure (`LiveStream`) and checked exactly here, as is what the
recorder writes and the one thing it must never do: lose a segment of the
recording because the playback copy of it failed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import hls, live_capture  # noqa: E402
from media_server.live_capture import LiveStream  # noqa: E402


class TestThePlaylist:
    def test_segments_are_listed_in_order_with_their_own_lengths(self):
        s = LiveStream()
        s.add(100, 6.006)
        s.add(101, 5.994)
        text = s.render()
        assert "#EXT-X-MEDIA-SEQUENCE:100" in text
        assert "#EXTINF:6.006,\n000000100.ts\n#EXTINF:5.994,\n000000101.ts" in text

    def test_it_is_an_event_so_the_whole_day_stays_seekable(self):
        s = LiveStream()
        s.add(1, 6)
        assert "#EXT-X-PLAYLIST-TYPE:EVENT" in s.render()
        assert "#EXT-X-ENDLIST" not in s.render()

    def test_a_finished_recording_is_a_plain_vod(self):
        s = LiveStream()
        s.add(1, 6)
        s.ended = True
        assert s.render().rstrip().endswith("#EXT-X-ENDLIST")

    def test_nothing_recorded_is_no_playlist(self):
        """A playlist with nothing in it is a player that spins for ever."""
        assert LiveStream().render() == ""


class TestTimeOnThePlaylistIsTimeOnTheEvent:
    """Leaving a missing segment out would pull everything after it earlier by
    its length, and every moment after it would open on the wrong few seconds."""

    def test_a_segment_that_would_not_download_keeps_its_place(self):
        s = LiveStream()
        s.add(100, 6)
        s.add_gap(101, 5.5)
        s.add(102, 6)
        assert "#EXT-X-GAP\n#EXTINF:5.500,\n000000101.ts" in s.render()
        assert s.duration() == pytest.approx(17.5)

    def test_segments_that_slid_past_unfetched_are_held_at_the_target_length(self):
        s = LiveStream(target_duration=6)
        s.add(100, 6)
        s.add(103, 6)
        assert [(seq, gap) for seq, _, gap in s.entries] == [
            (100, False), (101, True), (102, True), (103, False)]
        assert s.duration() == pytest.approx(24)

    def test_a_gap_before_the_first_segment_is_not_written(self):
        """The event's clock starts at the first segment that was recorded."""
        s = LiveStream()
        assert s.add_gap(99, 6) is False
        s.add(100, 6)
        assert s.entries[0][0] == 100

    def test_the_playlist_only_moves_forward(self):
        """A restarted recorder takes the whole window it first sees."""
        s = LiveStream()
        s.add(100, 6)
        s.add(101, 6)
        assert s.add(101, 6) is False
        assert s.add(100, 6) is False
        assert [seq for seq, _, _ in s.entries] == [100, 101]


class TestTheTargetDuration:
    def test_it_is_the_longest_segment_rounded_to_nearest(self):
        """RFC 8216: each EXTINF rounded to the nearest integer may not exceed it.
        A ceiling would make 6.006 into 7, and the player reload a second late."""
        s = LiveStream()
        s.add(1, 6.006)
        assert "#EXT-X-TARGETDURATION:6" in s.render()

    def test_half_rounds_up_as_the_rfc_reads(self):
        s = LiveStream()
        s.add(1, 6.5)
        assert "#EXT-X-TARGETDURATION:7" in s.render()


class TestFragmentedMp4:
    def test_the_initialisation_segment_is_mapped_and_the_version_raised(self):
        s = LiveStream(container="mp4")
        s.init_name = "init.mp4"
        s.add(7, 4)
        text = s.render()
        assert '#EXT-X-MAP:URI="init.mp4"' in text
        assert "#EXT-X-VERSION:6" in text
        assert "000000007.m4s" in text


class TestARestartCarriesTheStreamOn:
    def test_the_playlist_reads_back_exactly(self):
        s = LiveStream()
        s.add(100, 6.006)
        s.add_gap(101, 5.5)
        s.add(104, 6)
        assert LiveStream.parse(s.render()).entries == s.entries

    def test_a_resumed_recording_is_not_finished(self):
        """An ENDLIST left by the execution that died would tell every player
        the event was over."""
        s = LiveStream()
        s.add(1, 6)
        s.ended = True
        assert LiveStream.parse(s.render()).ended is False

    def test_the_map_and_container_survive(self):
        s = LiveStream(container="mp4")
        s.init_name = "init.mp4"
        s.add(7, 4)
        back = LiveStream.parse(s.render())
        assert back.init_name == "init.mp4" and back.container == "mp4"

    def test_nothing_to_read_is_an_empty_stream(self):
        assert LiveStream.parse("").entries == []


# --- what the recorder writes -------------------------------------------------


def _segment(seq: int, duration: float = 6.0) -> hls.Segment:
    return hls.Segment(seq=seq, url=f"https://x/s{seq}.ts", duration=duration,
                       pdt=None, discontinuity=False)


class _Playlist:
    def __init__(self, segments):
        self.segments = segments
        self.endlist = False
        self.target_duration = 6.0
        self.init_url = ""


def _recorder(stream_bucket: bool = True):
    store = MagicMock()
    store.prefix = "jobs/j1/live"
    store.stream_prefix = "jobs/j1/live"
    store.stream_bucket = MagicMock() if stream_bucket else None
    recorder = live_capture.Recorder("j1", "https://x/m.m3u8",
                                     live_capture.now(), 300, store)
    return recorder, store


def _stream_writes(store):
    return [c.args[0] for c in store.put_stream.call_args_list]


class TestWhatTheRecorderWrites:
    def test_each_segment_goes_to_the_stream_as_well_as_the_recording(self):
        recorder, store = _recorder()
        with patch.object(recorder, "_download", return_value=b"x" * 10):
            recorder.take(_Playlist([_segment(100), _segment(101)]))
        assert store.put.call_count == 2                     # the recording
        assert _stream_writes(store) == ["000000100.ts", "000000101.ts", "index.m3u8"]

    def test_the_segment_is_written_before_the_playlist_that_names_it(self):
        recorder, store = _recorder()
        with patch.object(recorder, "_download", return_value=b"x"):
            recorder.take(_Playlist([_segment(100)]))
        assert _stream_writes(store).index("000000100.ts") < _stream_writes(store).index("index.m3u8")

    def test_the_playlist_is_uncacheable_and_the_segments_are_not(self):
        recorder, store = _recorder()
        with patch.object(recorder, "_download", return_value=b"x"):
            recorder.take(_Playlist([_segment(100)]))
        caches = {c.args[0]: c.args[3] for c in store.put_stream.call_args_list}
        assert "no-store" in caches["index.m3u8"]
        assert "max-age=86400" in caches["000000100.ts"]

    def test_a_segment_that_would_not_download_is_a_gap_in_the_stream(self):
        recorder, store = _recorder()
        data = {100: b"x", 101: None, 102: b"x"}
        with patch.object(recorder, "_download", side_effect=lambda seg: data[seg.seq]):
            recorder.take(_Playlist([_segment(100), _segment(101), _segment(102)]))
        assert [(seq, gap) for seq, _, gap in recorder.stream.entries] == [
            (100, False), (101, True), (102, False)]

    def test_nothing_new_writes_nothing(self):
        recorder, store = _recorder()
        with patch.object(recorder, "_download", return_value=b"x"):
            recorder.take(_Playlist([_segment(100)]))
            store.put_stream.reset_mock()
            recorder.take(_Playlist([_segment(100)]))      # the same window again
        assert store.put_stream.call_count == 0


class TestTheRecordingComesFirst:
    """The stream is how an editor watches; the recording is what the analysis
    reads and what the event is. A playback copy that fails is a warning."""

    def test_a_failed_stream_write_does_not_lose_the_segment(self):
        recorder, store = _recorder()
        store.put_stream.side_effect = RuntimeError("503 from the HLS bucket")
        with patch.object(recorder, "_download", return_value=b"x"):
            taken = recorder.take(_Playlist([_segment(100), _segment(101)]))
        assert taken == 2
        assert store.put.call_count == 2
        # Both are in the open chunk, so the analysis will read them.
        assert len(recorder.assembler.open.parts) == 2

    def test_a_recorder_with_no_stream_bucket_records_as_before(self):
        recorder, store = _recorder(stream_bucket=False)
        with patch.object(recorder, "_download", return_value=b"x"):
            assert recorder.take(_Playlist([_segment(100)])) == 1
        assert not store.put_stream.called
        assert recorder._capture()["stream"] == ""


class TestWhereTheStreamIsReported:
    def test_named_only_once_something_is_in_it(self):
        recorder, _ = _recorder()
        assert recorder._capture()["stream"] == ""
        with patch.object(recorder, "_download", return_value=b"x"):
            recorder.take(_Playlist([_segment(100)]))
        assert recorder._capture()["stream"] == "jobs/j1/live/index.m3u8"


class TestTheFinish:
    def _finish(self, *, entries: bool = True):
        recorder, store = _recorder()
        if entries:
            recorder.stream.add(100, 6)
        # An event already over: the loop ends on its first check.
        recorder.event_end = live_capture.now()
        with patch.object(recorder, "resolve", return_value=_Playlist([])), \
             patch.object(recorder, "close_chunk"), patch.object(recorder, "report"):
            code = recorder.run()
        return recorder, store, code

    def test_a_clean_finish_closes_the_stream_as_a_vod(self):
        recorder, store, code = self._finish()
        assert code == 0
        last = store.put_stream.call_args_list[-1]
        assert last.args[0] == "index.m3u8"
        assert last.args[1].decode().rstrip().endswith("#EXT-X-ENDLIST")

    def test_an_empty_stream_is_not_written_at_the_finish(self):
        _, store, _ = self._finish(entries=False)
        assert not store.put_stream.called


class TestStartingTheRecorder:
    """start_live_capture: where the stream goes, and what a first start clears."""

    def _start(self, **kwargs):
        from media_server import server
        with patch.object(server, "LIVE_CAPTURE_JOB", "projects/p/locations/l/jobs/lc"), \
             patch.object(server, "MEDIA_BUCKET", "media-b"), \
             patch.object(server, "HLS_BUCKET", "hls-b"), \
             patch.object(server.gcs, "delete_prefix") as delete, \
             patch.object(server.runjobs, "run", return_value="exec-1") as run:
            fn = getattr(server.start_live_capture, "fn", server.start_live_capture)
            fn("j1", "https://x/m.m3u8", "2026-09-11T21:30:00Z", 300, **kwargs)
        return delete, run.call_args.args[1]

    def test_the_recorder_is_told_where_the_stream_goes(self):
        _, env = self._start()
        assert env["HLS_BUCKET"] == "hls-b"

    def test_a_first_start_clears_a_previous_booking_s_stream(self):
        delete, _ = self._start()
        assert ("hls-b", "jobs/j1/live/") in [c.args for c in delete.call_args_list]

    def test_a_restart_keeps_both_the_recording_and_the_stream(self):
        delete, _ = self._start(resume=True)
        assert not delete.called
