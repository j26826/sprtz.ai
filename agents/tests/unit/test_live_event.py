"""The live tick: what one minute does to a live event.

The recorder writes chunks; the tick, driven by the scheduler, does the rest.
Every branch here is exercised against a fake catalog, because the real ones
are a Cloud Run Job and a scheduler and neither can be started from a test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sprtz_agents.schemas import Moment
from sprtz_agents.tools import live

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _iso(delta_min: float) -> str:
    return (NOW + timedelta(minutes=delta_min)).isoformat()


def _chunk(index: int, first: int, last: int, **over) -> dict:
    base = {
        "index": index, "status": "captured", "gcsUri": f"gs://media/jobs/j1/live/chunks/chunk_{index:04d}.ts",
        "startSec": index * 300.0, "durationSec": 300.0, "firstSeq": first, "lastSeq": last,
        "firstPdt": _iso(index * 5), "lastPdtEnd": _iso(index * 5 + 5),
        "discontinuities": 0, "gapBefore": None, "gapsInside": [],
    }
    base.update(over)
    return base


class TestContinuity:
    def test_numbers_that_follow_on_are_ok(self):
        out = live.check_continuity(_chunk(1, 51, 100), _chunk(0, 1, 50))
        assert out["status"] == "ok"
        assert out["missingSegments"] == 0

    def test_a_hole_in_the_numbering_is_a_gap_whatever_the_recorder_said(self):
        # The recorder that wrote chunk 1 never saw chunk 0 — a restart — so it
        # noted nothing; the stored numbers still show 10 segments missing.
        out = live.check_continuity(_chunk(1, 61, 110), _chunk(0, 1, 50))
        assert out["status"] == "gap"
        assert out["missingSegments"] == 10
        assert any("missing between chunks 0 and 1" in i for i in out["issues"])

    def test_wall_clock_disagreement_is_a_gap_even_with_consecutive_numbers(self):
        late = _chunk(1, 51, 100, firstPdt=_iso(5 + 2))  # two minutes late
        out = live.check_continuity(late, _chunk(0, 1, 50))
        assert out["status"] == "gap"
        assert out["gapSec"] == pytest.approx(120.0)

    def test_the_recorders_own_notes_count(self):
        out = live.check_continuity(
            _chunk(1, 51, 100, gapsInside=[{"missedSegments": 3, "seconds": 18}]), _chunk(0, 1, 50))
        assert out["status"] == "gap"
        assert "3 segment(s) slid past" in out["issues"][0]

    def test_the_first_chunk_has_nothing_to_follow(self):
        assert live.check_continuity(_chunk(0, 1, 50), None)["status"] == "first"
        # A later chunk with no predecessor on record cannot be judged.
        assert live.check_continuity(_chunk(3, 1, 50), None)["status"] == "unknown"


class TestExpectations:
    def test_expected_chunks_include_the_lead_in(self):
        start = NOW
        end = NOW + timedelta(minutes=55)
        assert live.expected_chunks(start, end, 300, 300) == 12

    def test_progress_stays_inside_its_band(self):
        assert live.live_progress(0, 12) == 5
        assert live.live_progress(6, 12) == 50
        assert live.live_progress(12, 12) == 95
        assert live.live_progress(20, 12) == 95


@pytest.fixture
def catalog():
    """A fake catalog and media server, with the job document it holds."""
    state = {
        "job": {
            "job_id": "j1", "kind": "live", "sport": "handball", "title": "Cup final",
            "hlsUrl": "https://x.test/live.m3u8", "metadataLanguage": "en", "contextUrls": [],
            "counts": {"moments": 0},
            "live": {"state": "scheduled", "eventStart": _iso(10), "eventEnd": _iso(70),
                     "chunkSec": 300, "chunksCaptured": 0, "chunksAnalysed": 0,
                     "capture": {}, "tickLockUntil": None},
        },
        "chunks": [],
        "capture_status": "running",
        "calls": [],
    }

    async def call(server, tool, args=None):
        args = args or {}
        state["calls"].append((tool, args))
        if tool == "get_job":
            return state["job"]
        if tool == "update_live":
            state["job"]["live"].update(args["patch"])
            return {"status": "success"}
        if tool == "start_live_capture":
            return {"status": "started", "execution": "exec-1", "chunk_sec": 300}
        if tool == "live_capture_status":
            return {"status": state["capture_status"]}
        if tool == "list_live_chunks":
            return {"status": "success", "chunks": [dict(c) for c in state["chunks"]]}
        if tool == "mux_chunk":
            if state.get("mux_fails"):
                return {"status": "error", "error": "ffmpeg exited 1"}
            return {"status": "success", "index": args["index"],
                    "gcs_uri": f"gs://media/jobs/j1/live/chunks/chunk_{int(args['index']):04d}_muxed.ts"}
        if tool == "claim_live_chunk":
            for c in state["chunks"]:
                if c["index"] == args["index"] and c["status"] == "captured":
                    c["status"] = "analyzing"
                    return {"claimed": True}
            return {"claimed": False}
        if tool == "reset_live_chunk" and state.get("stale_resets") is not None:
            for c in state["chunks"]:
                if c["index"] == args["index"] and c["status"] == "analyzing" and args.get("stale_after_minutes"):
                    c["status"] = "captured"
                    state["stale_resets"].append(args)
                    return {"reset": True}
            return {"reset": False}
        if tool == "reset_live_chunk":
            for c in state["chunks"]:
                if c["index"] == args["index"] and c["status"] == "failed" and c.get("attempts", 1) < 3:
                    c["status"] = "captured"
                    c["attempts"] = c.get("attempts", 1) + 1
                    return {"reset": True}
            return {"reset": False}
        if tool == "finish_live_chunk":
            for c in state["chunks"]:
                if c["index"] == args["index"]:
                    c["status"] = "failed" if args.get("error") else "analysed"
                    c.update({k: v for k, v in args.items() if k in ("summary", "competition", "venue")})
            return {"status": "success"}
        if tool == "list_moments":
            return {"status": "success", "moments": [dict(m) for m in state.get("stored", [])]}
        if tool == "update_moment_identity":
            return {"status": "success", "updated": len(args.get("identities") or [])}
        if tool == "upsert_moments":
            return {"saved": len(args.get("moments", []))}
        if tool == "cancel_requested":
            return {"cancelling": False}
        return {"status": "success"}

    mock = AsyncMock(side_effect=call)
    with patch.object(live.mcp_client, "call_tool", mock), \
         patch.object(live, "now", lambda: NOW):
        yield state


def _calls(state, tool):
    return [a for t, a in state["calls"] if t == tool]


class TestScheduled:
    @pytest.mark.asyncio
    async def test_not_due_yet_starts_nothing_and_releases_the_lock(self, catalog):
        out = await live.live_tick("j1")
        assert out["status"] == "scheduled"
        assert not _calls(catalog, "start_live_capture")
        assert catalog["job"]["live"]["tickLockUntil"] is None
        assert catalog["job"]["live"]["state"] == "scheduled"

    @pytest.mark.asyncio
    async def test_due_starts_the_capture_five_minutes_before(self, catalog):
        catalog["job"]["live"]["eventStart"] = _iso(5)   # exactly the lead
        out = await live.live_tick("j1")
        assert out["status"] == "started"
        started = _calls(catalog, "start_live_capture")[0]
        assert started["hls_url"] == "https://x.test/live.m3u8"
        assert started["event_end"] == catalog["job"]["live"]["eventEnd"]
        # A first start is not a resume: it clears whatever a previous booking
        # of this job left under its live prefix.
        assert "resume" not in started
        assert catalog["job"]["live"]["state"] == "live"
        assert catalog["job"]["live"]["capture"]["execution"] == "exec-1"

    @pytest.mark.asyncio
    async def test_a_tick_that_holds_the_lock_is_busy(self, catalog):
        catalog["job"]["live"]["tickLockUntil"] = _iso(3)
        assert (await live.live_tick("j1"))["status"] == "busy"
        assert not _calls(catalog, "start_live_capture")

    @pytest.mark.asyncio
    async def test_an_event_that_ended_before_it_started_is_failed(self, catalog):
        catalog["job"]["live"].update({"eventStart": _iso(-120), "eventEnd": _iso(-60)})
        out = await live.live_tick("j1")
        assert out["status"] == "error"
        assert catalog["job"]["live"]["state"] == "failed"


def _live(catalog):
    catalog["job"]["live"].update({
        "state": "live", "eventStart": _iso(-20), "eventEnd": _iso(40),
        "capture": {"execution": "exec-1", "state": "recording", "lastPollAt": _iso(-0.5)},
    })


def _moment() -> Moment:
    return Moment.model_validate({
        "moment_id": "m1", "job_id": "j1", "moment_type": "jump_shot", "category": "shot",
        "label": "Jump shot", "start_sec": 310.0, "end_sec": 316.0, "peak_sec": 313.0,
        "confidence": 0.9, "excitement": 0.8, "highlight_score": 0.9, "description": "d",
        "evidence": [], "is_goal": False,
    })


class TestLive:
    @pytest.mark.asyncio
    async def test_a_captured_chunk_is_claimed_analysed_and_recorded(self, catalog):
        _live(catalog)
        catalog["chunks"] = [_chunk(0, 1, 50, status="analysed"), _chunk(1, 51, 100)]
        analysis = SimpleNamespace(segment_summary="A tight first five minutes.", competition="EHF",
                                   venue="", discipline="", discipline_confidence=0.0)
        seen = {}

        async def fake_analyse(uri, plan, total, sport, sem, language, segment_uri=""):
            seen.update(uri=uri, plan=plan, total=total, sport=sport, segment_uri=segment_uri)
            return plan, analysis, None

        with patch.object(live, "_analyse_one", fake_analyse), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: [_moment()]):
            out = await live.live_tick("j1")

        assert out["status"] == "live"
        assert out["analysed_now"] == 1
        # The chunk is read as its own file at its own offset.
        assert seen["segment_uri"].endswith("chunk_0001.ts")
        assert seen["plan"].start_sec == 300.0 and seen["plan"].end_sec == 600.0
        assert seen["total"] == 13, "55 minutes of event plus the lead-in, in 5-minute chunks"
        finished = _calls(catalog, "finish_live_chunk")[0]
        assert finished["index"] == 1
        assert finished["moments"] == 1
        assert finished["continuity"]["status"] == "ok"
        assert finished["summary"] == "A tight first five minutes."
        # The still is cut from the chunk, at the peak's offset inside it.
        thumbs = _calls(catalog, "generate_moment_thumbnails")[0]
        assert thumbs["gcs_uri"].endswith("chunk_0001.ts")
        assert thumbs["moments"][0]["at_sec"] == pytest.approx(13.0)

    @pytest.mark.asyncio
    async def test_a_chunk_with_separate_audio_is_muxed_before_it_is_analysed(self, catalog):
        _live(catalog)
        catalog["chunks"] = [_chunk(0, 1, 50, audioUri="gs://media/jobs/j1/live/chunks/chunk_0000_audio.ts")]
        analysis = SimpleNamespace(segment_summary="", competition="", venue="", discipline="",
                                   discipline_confidence=0.0)
        seen = {}

        async def fake_analyse(uri, plan, total, sport, sem, language, segment_uri=""):
            seen.update(uri=uri, segment_uri=segment_uri)
            return plan, analysis, None

        with patch.object(live, "_analyse_one", fake_analyse), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: [_moment()]):
            await live.live_tick("j1")

        mux = _calls(catalog, "mux_chunk")[0]
        assert mux["video_uri"].endswith("chunk_0000.ts")
        assert mux["audio_uri"].endswith("chunk_0000_audio.ts")
        assert seen["segment_uri"].endswith("chunk_0000_muxed.ts"), "the analysis reads the muxed file"
        assert _calls(catalog, "generate_moment_thumbnails")[0]["gcs_uri"].endswith("chunk_0000_muxed.ts")
        assert _calls(catalog, "finish_live_chunk")[0]["muxed_uri"].endswith("chunk_0000_muxed.ts")

    @pytest.mark.asyncio
    async def test_a_chunk_already_muxed_is_not_muxed_again(self, catalog):
        _live(catalog)
        catalog["chunks"] = [_chunk(0, 1, 50, audioUri="gs://media/a.ts",
                                    muxedUri="gs://media/jobs/j1/live/chunks/chunk_0000_muxed.ts")]
        analysis = SimpleNamespace(segment_summary="", competition="", venue="", discipline="",
                                   discipline_confidence=0.0)

        async def fake_analyse(uri, plan, total, sport, sem, language, segment_uri=""):
            return plan, analysis, None

        with patch.object(live, "_analyse_one", fake_analyse), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: []):
            await live.live_tick("j1")
        assert not _calls(catalog, "mux_chunk")

    @pytest.mark.asyncio
    async def test_a_mux_that_fails_analyses_the_chunk_silent_and_says_so(self, catalog):
        _live(catalog)
        catalog["chunks"] = [_chunk(0, 1, 50, audioUri="gs://media/a.ts")]
        catalog["mux_fails"] = True
        analysis = SimpleNamespace(segment_summary="", competition="", venue="", discipline="",
                                   discipline_confidence=0.0)
        seen = {}

        async def fake_analyse(uri, plan, total, sport, sem, language, segment_uri=""):
            seen.update(segment_uri=segment_uri)
            return plan, analysis, None

        with patch.object(live, "_analyse_one", fake_analyse), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: []):
            out = await live.live_tick("j1")
        assert out["analysed_now"] == 1
        assert seen["segment_uri"].endswith("chunk_0000.ts")
        warnings = [a for a in _calls(catalog, "emit_event") if a.get("level") == "warning"]
        assert any("without its audio" in w["message"] for w in warnings)

    @pytest.mark.asyncio
    async def test_a_chunk_claimed_by_a_tick_that_died_is_analysed_again(self, catalog):
        _live(catalog)
        catalog["stale_resets"] = []
        catalog["chunks"] = [_chunk(0, 1, 50, status="analyzing", claimedAt=_iso(-40))]
        analysis = SimpleNamespace(segment_summary="", competition="", venue="", discipline="",
                                   discipline_confidence=0.0)

        async def fake_analyse(uri, plan, total, sport, sem, language, segment_uri=""):
            return plan, analysis, None

        with patch.object(live, "_analyse_one", fake_analyse), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: []):
            out = await live.live_tick("j1")
        assert catalog["stale_resets"][0]["stale_after_minutes"] == live.LOCK_MINUTES
        assert out["analysed_now"] == 1

    @pytest.mark.asyncio
    async def test_a_chunk_that_does_not_follow_on_is_said_so(self, catalog):
        _live(catalog)
        catalog["chunks"] = [_chunk(0, 1, 50, status="analysed"), _chunk(1, 61, 110)]
        with patch.object(live, "_analyse_one", AsyncMock(return_value=(None, SimpleNamespace(
                segment_summary="", competition="", venue="", discipline="", discipline_confidence=0), None))), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: []):
            await live.live_tick("j1")
        warnings = [a for a in _calls(catalog, "emit_event") if a.get("level") == "warning"]
        assert any("does not follow straight on" in a["message"] for a in warnings)
        assert _calls(catalog, "finish_live_chunk")[0]["continuity"]["missingSegments"] == 10

    @pytest.mark.asyncio
    async def test_a_failed_chunk_is_recorded_as_failed_not_dropped(self, catalog):
        _live(catalog)
        catalog["chunks"] = [_chunk(0, 1, 50)]
        with patch.object(live, "_analyse_one", AsyncMock(return_value=(None, None, "Unparseable response"))):
            await live.live_tick("j1")
        finished = _calls(catalog, "finish_live_chunk")[0]
        assert finished["error"] == "Unparseable response"

    @pytest.mark.asyncio
    async def test_a_failed_chunk_is_tried_again_next_tick(self, catalog):
        _live(catalog)
        catalog["chunks"] = [_chunk(0, 1, 50, status="failed", attempts=1)]
        with patch.object(live, "_analyse_one", AsyncMock(return_value=(None, SimpleNamespace(
                segment_summary="", competition="", venue="", discipline="", discipline_confidence=0), None))), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: []):
            out = await live.live_tick("j1")
        assert out["analysed_now"] == 1
        assert _calls(catalog, "reset_live_chunk")[0]["index"] == 0

    @pytest.mark.asyncio
    async def test_a_dead_recorder_is_restarted_while_the_event_is_on(self, catalog):
        _live(catalog)
        catalog["capture_status"] = "failed"
        out = await live.live_tick("j1")
        assert out.get("restarted") is True
        assert len(_calls(catalog, "start_live_capture")) == 1
        assert catalog["job"]["live"]["captureRestarts"] == 1
        assert catalog["job"]["live"]["state"] == "live"

    @pytest.mark.asyncio
    async def test_a_restart_keeps_what_was_already_recorded(self, catalog):
        """A first start clears the job's live prefix; a restart must not.

        It used to go through the same call, and so deleted every chunk
        recorded before it — the moments survived in Firestore, but the
        recording the event is played back from is composed out of those files.
        """
        _live(catalog)
        catalog["capture_status"] = "failed"
        await live.live_tick("j1")
        assert _calls(catalog, "start_live_capture")[0]["resume"] is True

    @pytest.mark.asyncio
    async def test_a_restart_carries_the_stall_limit_the_event_was_booked_with(self, catalog):
        _live(catalog)
        catalog["job"]["live"]["stallMinutes"] = 12
        catalog["capture_status"] = "failed"
        await live.live_tick("j1")
        assert _calls(catalog, "start_live_capture")[0]["stall_minutes"] == 12

    @pytest.mark.asyncio
    async def test_a_hung_recorder_is_cancelled_and_restarted(self, catalog):
        _live(catalog)
        catalog["job"]["live"]["capture"]["lastPollAt"] = _iso(-7)
        out = await live.live_tick("j1")
        assert out.get("restarted") is True
        assert _calls(catalog, "cancel_live_capture")[0]["execution"] == "exec-1"

    @pytest.mark.asyncio
    async def test_restarts_are_bounded(self, catalog):
        _live(catalog)
        catalog["capture_status"] = "failed"
        catalog["job"]["live"]["captureRestarts"] = live.MAX_CAPTURE_RESTARTS
        out = await live.live_tick("j1")
        assert out["status"] == "error"
        assert catalog["job"]["live"]["state"] == "failed"

    @pytest.mark.asyncio
    async def test_a_finished_recorder_with_nothing_left_completes_the_event(self, catalog):
        _live(catalog)
        catalog["capture_status"] = "succeeded"
        catalog["chunks"] = [_chunk(0, 1, 50, status="analysed", summary="s", competition="EHF")]
        with patch.object(live, "_record_game_details", AsyncMock(return_value=None)) as game:
            out = await live.live_tick("j1")
        assert out["status"] == "complete"
        assert game.await_args.kwargs["competitions"] == ["EHF"]
        assert catalog["job"]["live"]["state"] == "complete"
        final = [a for a in _calls(catalog, "update_job_status") if a.get("status") == "complete"]
        assert final and final[0]["progress"] == 100

    @pytest.mark.asyncio
    async def test_a_finished_recorder_waits_for_its_last_chunk(self, catalog):
        _live(catalog)
        catalog["capture_status"] = "succeeded"
        # The listing the tick took first is empty; the recorder's final chunk
        # lands between that and the re-check.
        calls = {"n": 0}
        original = live._chunks

        async def chunks(job_id):
            calls["n"] += 1
            if calls["n"] == 2:
                catalog["chunks"] = [_chunk(0, 1, 50)]
            return await original(job_id)

        with patch.object(live, "_chunks", chunks):
            out = await live.live_tick("j1")
        assert out["status"] == "live"
        assert "last chunk" in out["message"]


# --- A day of rounds, live -----------------------------------------------------
#
# An equestrian stream reports who was in the arena and what it looked for and
# did not find. Live mode used to keep neither: the rides were thrown away with
# the model's answer, so no live moment knew whose round it was and the record
# had no rides. These pin that both now survive, across chunks.

from sprtz_agents.schemas import EquestrianSegmentAnalysis, NotConfirmed, ObservedRide  # noqa: E402
from sprtz_agents.tools.analysis import merge_not_confirmed  # noqa: E402


def _dressage(*rides, not_confirmed=()) -> EquestrianSegmentAnalysis:
    return EquestrianSegmentAnalysis(
        segment_summary="Two tests.", discipline="dressage", discipline_confidence=0.9,
        rides=[ObservedRide(rider=r, horse=h, start_tc=s, end_tc=e) for r, h, s, e in rides],
        not_confirmed=[NotConfirmed(moment_type=c, note=n) for c, n in not_confirmed],
    )


def _stored(moment_id: str, peak: float, **over) -> dict:
    base = {"moment_id": moment_id, "moment_type": "piaffe", "category": "flatwork",
            "label": "Piaffe", "start_sec": peak - 3, "end_sec": peak + 5, "peak_sec": peak,
            "confidence": 0.8, "excitement": 0.6, "highlight_score": 0.7, "description": "d",
            "evidence": [], "rider": "", "horse": "", "start_number": "", "ride_order": None,
            "identity_source": ""}
    base.update(over)
    return base


class TestLiveRides:
    @pytest.mark.asyncio
    async def test_a_chunks_rides_and_negatives_are_kept_on_it_in_absolute_time(self, catalog):
        _live(catalog)
        catalog["job"]["sport"] = "equestrian"
        catalog["chunks"] = [_chunk(0, 1, 50, status="analysed"), _chunk(1, 51, 100)]
        analysis = _dressage(("Anna Berger", "Lumière", "00:40", "04:55"),
                             not_confirmed=[("pirouette", "candidate near 03:12 was a corner")])

        async def fake_analyse(uri, plan, total, sport, sem, language, segment_uri=""):
            return plan, analysis, None

        with patch.object(live, "_analyse_one", fake_analyse), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: []):
            out = await live.live_tick("j1")

        finished = _calls(catalog, "finish_live_chunk")[0]
        (fragment,) = finished["ride_fragments"]
        # Chunk 1 starts 300s into the recording.
        assert fragment["start_sec"] == 340.0 and fragment["end_sec"] == 595.0
        assert (fragment["rider"], fragment["horse"]) == ("Anna Berger", "Lumière")
        assert finished["not_confirmed"][0]["momentType"] == "pirouette"
        assert finished["not_confirmed"][0]["notes"][0].startswith("[segment 1]")
        # The agent's own result stays the short summary it was.
        assert "record" not in out["results"][0]

    @pytest.mark.asyncio
    async def test_moments_are_joined_to_rides_and_the_record_carries_them(self, catalog):
        _live(catalog)
        catalog["job"]["sport"] = "equestrian"
        # Chunk 0 was analysed last tick and saw the first half of a ride.
        catalog["chunks"] = [
            _chunk(0, 1, 50, status="analysed", discipline="dressage", disciplineConfidence=0.9,
                   rideFragments=[{"segment": 0, "start_sec": 200.0, "end_sec": 300.0,
                                   "rider": "Anna Berger", "horse": "Lumière"}]),
            _chunk(1, 51, 100),
        ]
        catalog["stored"] = [_stored("early", 250.0), _stored("late", 380.0),
                             _stored("between", 590.0)]
        analysis = _dressage(("Anna Berger", "Lumière", "00:00", "01:40"))

        async def fake_analyse(uri, plan, total, sport, sem, language, segment_uri=""):
            return plan, analysis, None

        facts = AsyncMock()
        with patch.object(live, "_analyse_one", fake_analyse), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: []), \
             patch.object(live, "record_game_facts", facts):
            await live.live_tick("j1")

        # One ride, stitched across the chunk boundary.
        rides = facts.await_args.kwargs["rides"]
        assert len(rides) == 1
        assert (rides[0]["start_sec"], rides[0]["end_sec"]) == (200.0, 400.0)
        # The record holds the discipline's label, as an uploaded match's does.
        assert facts.await_args.kwargs["discipline"] == "Dressage"
        # Both moments inside the ride are named; the one after it is not.
        (patched,) = _calls(catalog, "update_moment_identity")
        named = {i["moment_id"]: i for i in patched["identities"]}
        assert set(named) == {"early", "late"}
        assert named["late"]["rider"] == "Anna Berger"
        assert named["late"]["ride_order"] == 1

    @pytest.mark.asyncio
    async def test_the_finish_hands_the_whole_days_rides_and_negatives_to_the_record(self, catalog):
        _live(catalog)
        catalog["job"]["sport"] = "equestrian"
        catalog["capture_status"] = "succeeded"
        catalog["chunks"] = [
            _chunk(0, 1, 50, status="analysed", discipline="dressage", disciplineConfidence=0.8,
                   rideFragments=[{"segment": 0, "start_sec": 10.0, "end_sec": 280.0,
                                   "rider": "Anna Berger", "horse": "Lumière"}],
                   notConfirmed=[{"momentType": "pirouette", "notes": ["[segment 0] a"]}]),
            _chunk(1, 51, 100, status="analysed",
                   rideFragments=[{"segment": 1, "start_sec": 320.0, "end_sec": 590.0,
                                   "rider": "Jonas Keller", "horse": "Falkenstein"}],
                   notConfirmed=[{"momentType": "pirouette", "notes": ["[segment 1] b"]},
                                 {"momentType": "passage", "notes": []}]),
        ]
        with patch.object(live, "_record_game_details", AsyncMock(return_value=None)) as game:
            out = await live.live_tick("j1")

        assert out["status"] == "complete"
        kwargs = game.await_args.kwargs
        assert [r["rider"] for r in kwargs["rides"]] == ["Anna Berger", "Jonas Keller"]
        assert kwargs["not_confirmed"] == [
            {"momentType": "passage", "notes": []},
            {"momentType": "pirouette", "notes": ["[segment 0] a", "[segment 1] b"]},
        ]
        assert kwargs["discipline"] == "Dressage"

    @pytest.mark.asyncio
    async def test_a_handball_event_has_no_rides_and_joins_nothing(self, catalog):
        _live(catalog)
        catalog["chunks"] = [_chunk(0, 1, 50)]
        catalog["stored"] = [_stored("m1", 30.0)]
        analysis = SimpleNamespace(segment_summary="", competition="", venue="", discipline="",
                                   discipline_confidence=0.0)

        async def fake_analyse(uri, plan, total, sport, sem, language, segment_uri=""):
            return plan, analysis, None

        facts = AsyncMock()
        with patch.object(live, "_analyse_one", fake_analyse), \
             patch.object(live, "merge_segment_results", lambda a, sport, job_id: []), \
             patch.object(live, "record_game_facts", facts):
            await live.live_tick("j1")

        assert _calls(catalog, "finish_live_chunk")[0]["ride_fragments"] == []
        assert facts.await_args.kwargs["rides"] == []
        assert not _calls(catalog, "update_moment_identity")


class TestMergeNotConfirmed:
    def test_one_entry_per_type_with_every_note_once(self):
        out = merge_not_confirmed([
            [{"momentType": "piaffe", "notes": ["x"]}],
            [{"momentType": "piaffe", "notes": ["x", "y"]}, {"momentType": "passage", "notes": []}],
            None,
        ])
        assert out == [{"momentType": "passage", "notes": []},
                       {"momentType": "piaffe", "notes": ["x", "y"]}]



class TestAStreamThatStopped:
    """A stall is a normal ending, and the event says so."""

    @pytest.mark.asyncio
    async def test_the_first_start_carries_the_stall_limit(self, catalog):
        catalog["job"]["live"]["eventStart"] = _iso(5)
        catalog["job"]["live"]["stallMinutes"] = 5
        await live.live_tick("j1")
        assert _calls(catalog, "start_live_capture")[0]["stall_minutes"] == 5

    @pytest.mark.asyncio
    async def test_an_event_booked_before_the_setting_waits_for_its_end(self, catalog):
        catalog["job"]["live"]["eventStart"] = _iso(5)
        catalog["job"]["live"].pop("stallMinutes", None)
        await live.live_tick("j1")
        assert _calls(catalog, "start_live_capture")[0]["stall_minutes"] == 0

    @pytest.mark.asyncio
    async def test_an_event_the_stream_ended_early_says_so_and_is_not_a_warning(self, catalog):
        """Scheduled until 21:30 and complete at 17:06 reads as a fault unless
        the event says what happened — which is that the broadcast stopped."""
        _live(catalog)
        catalog["capture_status"] = "succeeded"
        catalog["job"]["live"]["capture"].update({"endedBy": "stalled", "stallMinutes": 5})
        catalog["chunks"] = [_chunk(0, 1, 50, status="analysed", summary="s", competition="EHF")]
        with patch.object(live, "_record_game_details", AsyncMock(return_value=None)):
            out = await live.live_tick("j1")
        assert out["status"] == "complete"
        done = [a for a in _calls(catalog, "emit_event") if "complete" in a.get("message", "")][-1]
        assert "stream stopped" in done["message"] and "5 minutes" in done["message"]
        assert done["level"] == "info"
