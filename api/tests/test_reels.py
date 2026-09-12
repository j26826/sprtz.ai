"""Reels: assembling cuts, and rendering them into one video.

A reel may draw on several matches, which is what most of this is about. The
things worth pinning:

Nothing here takes a caller's word for what a cut is. Every one goes through
the catalog's `plan_cut`, which measures it against the moment on record — the
same rule the single-moment download has always had, now applied to fifty cuts
at once rather than to one.

And a reel that cannot render must say why. Transcoder's edit list emits no
atom for a cut whose source is missing, so a reel referencing a deleted match
would otherwise encode short and look finished; the route refuses instead, and
names the problem.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.auth import CallerIdentity
from app.routers import reels

USER = CallerIdentity(uid="u-1", email="editor@example.com")

REEL = {
    "reelId": "r-1",
    "title": "Best pirouettes",
    "aspect": "16:9",
    "jobIds": ["job-a", "job-b"],
    "cuts": [
        {"cutId": "c000", "order": 0, "jobId": "job-a", "momentId": "m-1",
         "startMs": 600_000, "endMs": 612_000, "label": "Pirouette"},
        {"cutId": "c001", "order": 1, "jobId": "job-b", "momentId": "m-9",
         "startMs": 30_500, "endMs": 41_250, "label": "Passage"},
    ],
    "render": {},
}


def run(coro):
    return asyncio.run(coro)


def arg(mcp, tool):
    """The arguments the first call to `tool` was made with."""
    return next(a for _, t, a in mcp.calls if t == tool)


def called(mcp, tool):
    return any(t == tool for _, t, _ in mcp.calls)


def _mcp(*, reel=REEL, sources=None, render_started=True, state=None):
    """A catalog and a media server that answer plausibly.

    `sources` maps a job id to its source URI; a job absent from it comes back
    with no source, which is how a deleted or unprepared match looks.
    """
    sources = {"job-a": "gs://m/jobs/job-a/s.mp4",
               "job-b": "gs://m/jobs/job-b/s.mp4"} if sources is None else sources
    calls: list[tuple] = []

    async def call(server, tool, args=None):
        calls.append((server, tool, args or {}))
        if tool == "plan_cut":
            # Whatever was asked for, the record says 600-612.
            return {"status": "success", "start_ms": 600_000, "end_ms": 612_000,
                    "start_sec": 600.0, "end_sec": 612.0, "label": "Pirouette"}
        if tool == "get_reel":
            return {"status": "success", "reel": reel} if reel else {"status": "error"}
        if tool == "get_job":
            job_id = args.get("job_id")
            if job_id not in sources:
                return {"status": "error", "error": f"No job {job_id}"}
            return {"status": "success", "source": {"gcsUri": sources[job_id]}}
        if tool == "render_reel":
            return ({"status": "started", "transcoder_job": "tj-1",
                     "reel_uri": "gs://m/reels/r-1/reel.mp4", "duration_ms": 22_750}
                    if render_started else {"status": "error", "error": "nope"})
        if tool == "reel_status":
            return {"status": "success", **(state or {"done": False, "succeeded": False})}
        if tool in ("create_reel", "update_reel", "set_reel_render"):
            return {"status": "success", "reel": reel}
        return {"status": "success"}

    mock = AsyncMock(side_effect=call)
    mock.calls = calls
    return mock


class TestEveryCutIsMeasuredAgainstItsRecord:
    def test_the_times_stored_come_from_plan_cut_not_from_the_request(self):
        mcp = _mcp()
        body = reels.ReelCreate(cuts=[
            reels.CutIn(job_id="job-a", moment_id="m-1", start_sec=0.0, end_sec=99_999.0),
        ])
        with patch.object(reels.clients, "call_mcp", mcp):
            run(reels.create_reel(body, USER))
        created = arg(mcp, "create_reel")
        assert created["cuts"][0]["startMs"] == 600_000
        assert created["cuts"][0]["endMs"] == 612_000

    def test_one_cut_is_planned_per_moment(self):
        mcp = _mcp()
        body = reels.ReelCreate(cuts=[
            reels.CutIn(job_id="job-a", moment_id="m-1"),
            reels.CutIn(job_id="job-b", moment_id="m-9"),
        ])
        with patch.object(reels.clients, "call_mcp", mcp):
            run(reels.create_reel(body, USER))
        assert sum(t == "plan_cut" for _, t, _ in mcp.calls) == 2

    def test_a_moment_that_is_no_longer_there_is_a_404_not_a_reel(self):
        async def call(server, tool, args=None):
            if tool == "plan_cut":
                return {"status": "error", "error": "No moment m-1"}
            return {"status": "success"}
        body = reels.ReelCreate(cuts=[reels.CutIn(job_id="job-a", moment_id="m-1")])
        with patch.object(reels.clients, "call_mcp", AsyncMock(side_effect=call)):
            with pytest.raises(HTTPException) as exc:
                run(reels.create_reel(body, USER))
        assert exc.value.status_code == 404

    def test_a_moment_id_cannot_escape_into_an_object_path(self):
        body = reels.ReelCreate(cuts=[
            reels.CutIn(job_id="job-a", moment_id="../../other/thing"),
        ])
        with patch.object(reels.clients, "call_mcp", _mcp()):
            with pytest.raises(HTTPException) as exc:
                run(reels.create_reel(body, USER))
        assert exc.value.status_code == 404

    def test_editing_the_cuts_re_plans_them(self):
        # An editor dragging a handle is still only making a request.
        mcp = _mcp()
        patch_body = reels.ReelPatch(cuts=[reels.CutIn(job_id="job-a", moment_id="m-1",
                                                       start_sec=0.0, end_sec=99_999.0)])
        with patch.object(reels.clients, "call_mcp", mcp):
            run(reels.update_reel("r-1", patch_body, USER))
        sent = arg(mcp, "update_reel")
        assert sent["patch"]["cuts"][0]["endMs"] == 612_000


class TestWhatAnEditorMayNotSet:
    def test_render_and_publish_are_not_fields_on_the_patch(self):
        # They are records of what happened. The catalog rejects them too; this
        # is the near door refusing to carry them at all.
        assert "render" not in reels.ReelPatch.model_fields
        assert "publish" not in reels.ReelPatch.model_fields

    def test_a_patch_of_nothing_changes_nothing(self):
        mcp = _mcp()
        with patch.object(reels.clients, "call_mcp", mcp):
            run(reels.update_reel("r-1", reels.ReelPatch(), USER))
        assert not called(mcp, "update_reel")


class TestRendering:
    def test_the_encode_gets_a_source_for_every_match_the_cuts_name(self):
        mcp = _mcp()
        with patch.object(reels.clients, "call_mcp", mcp):
            run(reels.render_reel("r-1", USER))
        sent = arg(mcp, "render_reel")
        assert set(sent["sources"]) == {"job-a", "job-b"}
        assert len(sent["cuts"]) == 2

    def test_a_deleted_match_refuses_the_render_rather_than_shortening_it(self):
        # The edit list would silently emit no atom for it.
        mcp = _mcp(sources={"job-a": "gs://m/jobs/job-a/s.mp4"})
        with patch.object(reels.clients, "call_mcp", mcp):
            with pytest.raises(HTTPException) as exc:
                run(reels.render_reel("r-1", USER))
        assert exc.value.status_code == 409
        assert not called(mcp, "render_reel")

    def test_a_match_still_waiting_on_playback_refuses_too(self):
        mcp = _mcp(sources={"job-a": "gs://m/jobs/job-a/s.mp4", "job-b": ""})
        with patch.object(reels.clients, "call_mcp", mcp):
            with pytest.raises(HTTPException) as exc:
                run(reels.render_reel("r-1", USER))
        assert exc.value.status_code == 409

    def test_a_reel_with_no_cuts_cannot_be_rendered(self):
        mcp = _mcp(reel={**REEL, "cuts": [], "jobIds": []})
        with patch.object(reels.clients, "call_mcp", mcp):
            with pytest.raises(HTTPException) as exc:
                run(reels.render_reel("r-1", USER))
        assert exc.value.status_code == 409

    def test_starting_a_render_records_it_on_the_reel(self):
        mcp = _mcp()
        with patch.object(reels.clients, "call_mcp", mcp):
            out = run(reels.render_reel("r-1", USER))
        assert out["render"]["status"] == "running"
        assert out["render"]["transcoderJob"] == "tj-1"
        recorded = arg(mcp, "set_reel_render")
        assert recorded["render"]["transcoderJob"] == "tj-1"

    def test_the_route_returns_rather_than_waiting_for_the_encode(self):
        # A match-length encode behind an open connection is how the one-hour
        # request ceiling gets rediscovered.
        mcp = _mcp()
        with patch.object(reels.clients, "call_mcp", mcp):
            out = run(reels.render_reel("r-1", USER))
        assert out["render"]["status"] == "running"


class TestRenderStatus:
    def _running(self):
        return {**REEL, "render": {"status": "running", "transcoderJob": "tj-1",
                                   "reelUri": "gs://m/reels/r-1/reel.mp4"}}

    def test_a_finished_encode_becomes_ready(self):
        mcp = _mcp(reel=self._running(), state={"done": True, "succeeded": True})
        with patch.object(reels.clients, "call_mcp", mcp):
            out = run(reels.render_status("r-1", USER))
        assert out["render"]["status"] == "ready"

    def test_a_failed_encode_keeps_the_reason(self):
        mcp = _mcp(reel=self._running(),
                   state={"done": True, "succeeded": False, "error": "bad input"})
        with patch.object(reels.clients, "call_mcp", mcp):
            out = run(reels.render_status("r-1", USER))
        assert out["render"]["status"] == "failed"
        assert out["render"]["error"] == "bad input"

    def test_a_settled_render_is_not_asked_about_again(self):
        mcp = _mcp(reel={**REEL, "render": {"status": "ready", "transcoderJob": "tj-1"}})
        with patch.object(reels.clients, "call_mcp", mcp):
            run(reels.render_status("r-1", USER))
        assert not called(mcp, "reel_status")

    def test_a_reel_never_rendered_says_so_without_calling_the_encoder(self):
        mcp = _mcp()
        with patch.object(reels.clients, "call_mcp", mcp):
            out = run(reels.render_status("r-1", USER))
        assert out["render"]["status"] == "none"
        assert not called(mcp, "reel_status")


class TestReadingAndRemoving:
    def test_an_unknown_reel_is_a_404(self):
        mcp = _mcp(reel=None)
        with patch.object(reels.clients, "call_mcp", mcp):
            with pytest.raises(HTTPException) as exc:
                run(reels.get_reel("r-1", USER))
        assert exc.value.status_code == 404

    def test_a_reel_id_cannot_escape_into_an_object_path(self):
        with patch.object(reels.clients, "call_mcp", _mcp()):
            with pytest.raises(HTTPException) as exc:
                run(reels.get_reel("../../jobs/other", USER))
        assert exc.value.status_code == 404

    def test_deleting_a_reel_deletes_no_moments_and_no_matches(self):
        mcp = _mcp()
        with patch.object(reels.clients, "call_mcp", mcp):
            run(reels.delete_reel("r-1", USER))
        touched = {t for _, t, _ in mcp.calls}
        assert "delete_reel" in touched
        assert not touched & {"delete_job", "delete_moment", "clear_analysis"}
