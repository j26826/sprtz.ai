"""How the live recorder groups segments into chunks, and what it says about them.

The assembler is pure — the recorder feeds it segments and asks it whether
the open chunk is due — so every rule about chunk length and continuity is
checked here without a stream, a bucket or Firestore.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import hls, live_capture  # noqa: E402

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _seg(seq: int, duration: float = 6.0, pdt: datetime | None = None, disc: bool = False) -> hls.Segment:
    return hls.Segment(seq=seq, url=f"https://x/s{seq}.ts", duration=duration,
                       pdt=pdt if pdt is not None else T0 + timedelta(seconds=(seq - 1) * 6),
                       discontinuity=disc)


def _feed(assembler: live_capture.ChunkAssembler, segments) -> list[live_capture.Chunk]:
    closed = []
    for s in segments:
        if assembler.should_close(s.duration):
            closed.append(assembler.close())
        assembler.add(s, f"parts/{s.seq:09d}.ts")
    return closed


class TestChunkLength:
    def test_a_chunk_closes_before_the_segment_that_would_overshoot(self):
        a = live_capture.ChunkAssembler(300)
        closed = _feed(a, [_seg(i) for i in range(1, 60)])   # 59 × 6s = 354s
        assert len(closed) == 1
        assert closed[0].duration == 300.0
        assert closed[0].segments == 50
        assert (closed[0].first_seq, closed[0].last_seq) == (1, 50)
        assert a.open.first_seq == 51, "the next chunk starts with the segment that would not fit"

    def test_no_segment_is_ever_split(self):
        a = live_capture.ChunkAssembler(300)
        closed = _feed(a, [_seg(i, duration=7.0) for i in range(1, 50)])
        assert closed[0].duration == 294.0  # 42 × 7, not 300
        assert closed[0].segments == 42

    def test_chunks_are_numbered_in_order(self):
        a = live_capture.ChunkAssembler(60)
        closed = _feed(a, [_seg(i) for i in range(1, 35)])
        assert [c.index for c in closed] == [0, 1, 2]


class TestContinuityNotes:
    def test_consecutive_segments_carry_no_gap(self):
        a = live_capture.ChunkAssembler(60)
        closed = _feed(a, [_seg(i) for i in range(1, 25)])
        assert closed[0].gap_before is None
        assert closed[1].gap_before is None
        assert closed[1].gaps_inside == []

    def test_a_hole_between_chunks_is_noted_on_the_later_one(self):
        a = live_capture.ChunkAssembler(60)
        _feed(a, [_seg(i) for i in range(1, 11)])          # chunk 0: seq 1-10, 60s
        # seq 11-13 never arrived; the playlist slid past them. Seven more
        # segments is not enough to close the chunk that opened after the hole.
        closed = _feed(a, [_seg(i) for i in range(14, 21)])
        assert closed[0].last_seq == 10
        assert a.open.first_seq == 14
        assert a.open.gap_before == {"missedSegments": 3, "seconds": 18.0}

    def test_a_hole_inside_a_chunk_is_noted_inside_it(self):
        a = live_capture.ChunkAssembler(120)
        _feed(a, [_seg(i) for i in range(1, 6)] + [_seg(i) for i in range(8, 12)])
        assert a.open.gaps_inside == [{"missedSegments": 2, "seconds": 12.0}]

    def test_a_gap_the_recorder_saw_lands_on_the_next_chunk(self):
        a = live_capture.ChunkAssembler(60)
        _feed(a, [_seg(i) for i in range(1, 11)])
        a.close()
        a.note_gap(4, 24.0)                                 # noticed before anything new arrived
        _feed(a, [_seg(i) for i in range(15, 20)])
        assert a.open.gap_before == {"missedSegments": 4, "seconds": 24.0}

    def test_discontinuity_markers_are_counted(self):
        a = live_capture.ChunkAssembler(60)
        _feed(a, [_seg(1), _seg(2), _seg(3, disc=True), _seg(4)])
        assert a.open.discontinuities == 1


class TestChunkRecord:
    def test_the_offset_comes_from_the_clock_when_there_is_one(self):
        a = live_capture.ChunkAssembler(60)
        _feed(a, [_seg(i) for i in range(1, 11)])
        chunk = a.close()
        record = live_capture.chunk_record(chunk, "gs://m/c0.ts", "ts", T0 - timedelta(seconds=90), 0.0)
        assert record["startSec"] == 90.0
        assert record["durationSec"] == 60.0
        assert record["status"] == "captured"
        assert record["firstPdt"].startswith("2026-09-10T12:00:00")

    def test_and_from_the_running_total_when_there_is_not(self):
        a = live_capture.ChunkAssembler(60)
        _feed(a, [hls.Segment(seq=i, url="u", duration=6.0) for i in range(1, 11)])
        record = live_capture.chunk_record(a.close(), "gs://m/c3.ts", "ts", None, 180.0)
        assert record["startSec"] == 180.0
        assert record["firstPdt"] is None


class TestAudioParts:
    """A separate audio rendition is paired with the video by segment number."""

    def test_the_audio_for_a_chunk_is_the_parts_in_its_range(self):
        a = live_capture.AudioParts()
        for seq in range(1, 60):
            a.add(seq, f"audio/{seq:09d}.ts")
        names, missing, stale = a.take(1, 50)
        assert len(names) == 50 and names[0].endswith("000000001.ts") and names[-1].endswith("000000050.ts")
        assert missing == 0 and stale == []
        assert sorted(a.parts) == list(range(51, 60)), "the rest wait for the next chunk"

    def test_missing_audio_is_counted_not_invented(self):
        a = live_capture.AudioParts()
        for seq in [1, 2, 4, 5]:
            a.add(seq, f"audio/{seq}.ts")
        names, missing, _ = a.take(1, 5)
        assert len(names) == 4 and missing == 1

    def test_audio_older_than_the_chunk_is_stale(self):
        a = live_capture.AudioParts()
        for seq in [3, 4, 10, 11]:
            a.add(seq, f"audio/{seq}.ts")
        names, missing, stale = a.take(10, 11)
        assert names == ["audio/10.ts", "audio/11.ts"]
        assert sorted(stale) == ["audio/3.ts", "audio/4.ts"]
        assert a.parts == {}

    def test_the_chunk_record_carries_its_audio(self):
        a = live_capture.ChunkAssembler(60)
        closed = _feed(a, [_seg(i) for i in range(1, 12)])
        rec = live_capture.chunk_record(closed[0], "gs://m/jobs/j/live/chunks/chunk_0000.ts", "ts", T0, 0.0,
                                        audio={"uri": "gs://m/jobs/j/live/chunks/chunk_0000_audio.ts",
                                               "container": "ts", "segments": 10, "missing": 0})
        assert rec["audioUri"].endswith("chunk_0000_audio.ts")
        assert rec["audioSegments"] == 10 and rec["audioMissing"] == 0
        silent = live_capture.chunk_record(closed[0], "gs://m/v.ts", "ts", T0, 0.0)
        assert silent["audioUri"] is None
