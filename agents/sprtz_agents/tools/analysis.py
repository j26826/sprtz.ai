"""Segmented video analysis.

A full match is far too long to hand to one model call, so the video is split
into fixed windows (15 minutes by default), every window is analysed
concurrently by Gemini 2.5 Flash reading the video straight out of GCS, and the
per-window results are merged back into one absolute-timestamped timeline.

Adjacent windows overlap by a short lead so an action straddling a boundary is
seen whole by at least one of them; :func:`merge_segment_results` then collapses
the duplicate.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Awaitable, Callable

from google import genai
from google.genai import types

from sprtz_agents.config import get_settings
from sprtz_agents.schemas import DetectedMoment, Moment, SegmentAnalysis, SegmentPlan
from sprtz_agents.sports import get_profile
from sprtz_agents.sports.prompt import build_segment_prompt, build_system_instruction

logger = logging.getLogger(__name__)

_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".m4v": "video/mp4",
    ".avi": "video/x-msvideo",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
}


def _guess_mime(gcs_uri: str) -> str:
    lowered = gcs_uri.lower()
    for ext, mime in _MIME_BY_EXT.items():
        if lowered.endswith(ext):
            return mime
    return "video/mp4"


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        settings = get_settings()
        _client = genai.Client(
            vertexai=True,
            project=settings.project_id,
            location=settings.location,
        )
    return _client


# --- Segment planning ---------------------------------------------------------


def plan_segments(duration_sec: float) -> list[SegmentPlan]:
    """Split ``duration_sec`` into overlapping analysis windows.

    The last window is merged into its predecessor rather than left as a stub
    when it would be shorter than the overlap — a 40-second tail analysed on its
    own produces nothing but boundary artefacts.
    """
    settings = get_settings()
    window = float(settings.segment_seconds)
    overlap = float(settings.segment_overlap_seconds)

    if duration_sec <= 0:
        return []
    if duration_sec <= window:
        return [SegmentPlan(index=0, start_sec=0.0, end_sec=duration_sec)]

    stride = window - overlap
    if stride <= 0:
        raise ValueError(
            f"segment_overlap_seconds ({overlap}) must be smaller than the "
            f"segment window ({window})."
        )

    count = max(1, math.ceil((duration_sec - overlap) / stride))
    plans: list[SegmentPlan] = []
    for i in range(count):
        start = 0.0 if i == 0 else i * stride
        end = min(duration_sec, start + window)
        plans.append(
            SegmentPlan(
                index=i,
                start_sec=round(start, 3),
                end_sec=round(end, 3),
                overlap_lead_sec=0.0 if i == 0 else overlap,
            )
        )

    # Absorb a final sliver into the previous window.
    if len(plans) > 1 and plans[-1].duration_sec < overlap * 1.5:
        tail = plans.pop()
        prev = plans[-1]
        plans[-1] = prev.model_copy(update={"end_sec": tail.end_sec})

    return plans


# --- Per-segment analysis -----------------------------------------------------


async def _analyse_one(
    gcs_uri: str,
    plan: SegmentPlan,
    total: int,
    sport: str,
    semaphore: asyncio.Semaphore,
    metadata_language: str = "en",
    segment_uri: str = "",
) -> tuple[SegmentPlan, SegmentAnalysis | None, str | None]:
    """Analyse one window.

    ``segment_uri`` points at a file that *is* this window, cut beforehand. It
    is preferred over offsets into the whole match because Gemini fetches the
    entire object to serve a request whatever offsets are given — a 3.22 GiB
    source fails every window with "File content exceeded the size limit.
    max_bytes_fetched: 2146971648". A pre-cut file is also a clip that genuinely
    starts at 00:00, which is what the prompt tells the model its timecodes are
    relative to.
    """
    settings = get_settings()
    profile = get_profile(sport)

    if segment_uri:
        video_part = types.Part(
            file_data=types.FileData(
                file_uri=segment_uri, mime_type=_guess_mime(segment_uri)),
            # No offsets: the file is the window.
            video_metadata=types.VideoMetadata(fps=settings.analysis_fps),
        )
    else:
        video_part = types.Part(
            file_data=types.FileData(file_uri=gcs_uri, mime_type=_guess_mime(gcs_uri)),
            video_metadata=types.VideoMetadata(
                start_offset=f"{plan.start_sec:.3f}s",
                end_offset=f"{plan.end_sec:.3f}s",
                fps=settings.analysis_fps,
            ),
        )

    prompt = build_segment_prompt(
        profile,
        index=plan.index,
        total=total,
        start_sec=plan.start_sec,
        end_sec=plan.end_sec,
        overlap_lead_sec=plan.overlap_lead_sec,
    )

    config = types.GenerateContentConfig(
        system_instruction=build_system_instruction(profile, metadata_language),
        temperature=0.2,
        response_mime_type="application/json",
        response_schema=SegmentAnalysis,
        # A 15-minute window can legitimately contain 30+ moments.
        max_output_tokens=32768,
        # Without a thinking budget the model returns every moment at the same
        # confidence and misses roughly half of them; with one it calibrates and
        # recall roughly doubles on the same footage.
        thinking_config=types.ThinkingConfig(thinking_budget=8192),
        http_options=types.HttpOptions(
            timeout=15 * 60 * 1000,
            # A segment analysis had no retry at all, so one 429 lost fifteen
            # minutes of match outright. Quota is per-minute and thirteen
            # windows go out at once, so exhausting it is ordinary rather than
            # exceptional — and the answer to "try again later" is to try again
            # later, not to drop the segment and report nothing found.
            #
            # The backoff is long because the window it waits for is a minute
            # wide: retrying in two seconds asks the same exhausted quota the
            # same question. Six attempts from 8s, doubling to 120s, covers
            # roughly six minutes.
            retry_options=types.HttpRetryOptions(
                attempts=6,
                initial_delay=8.0,
                max_delay=120.0,
                exp_base=2.0,
                jitter=1.0,
                http_status_codes=[429, 500, 502, 503, 504],
            ),
        ),
    )

    async with semaphore:
        try:
            response = await _get_client().aio.models.generate_content(
                model=settings.model,
                contents=[types.Content(role="user", parts=[video_part, types.Part(text=prompt)])],
                config=config,
            )
        except Exception as exc:
            logger.exception("segment %s analysis failed", plan.index)
            return plan, None, f"{type(exc).__name__}: {exc}"

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, SegmentAnalysis):
        return plan, parsed, None

    text = (getattr(response, "text", "") or "").strip()
    if not text:
        return plan, None, "Model returned an empty response."
    try:
        return plan, SegmentAnalysis.model_validate_json(text), None
    except Exception as exc:  # noqa: BLE001
        return plan, None, f"Unparseable response: {exc}"


async def analyse_segments(
    gcs_uri: str,
    duration_sec: float,
    sport: str = "handball",
    metadata_language: str = "en",
    on_segment_done: Callable[[int, int], Awaitable[None]] | None = None,
    segment_uris: dict[int, str] | None = None,
) -> dict:
    """Analyse every segment of a video concurrently and merge the results.

    ``on_segment_done`` is awaited as each segment lands, with the number
    finished and the total. Segments are the only part of the run long enough to
    need reporting — a full match is an hour here and minutes everywhere else —
    and they finish out of order, so the count is what moves, not the index.

    Returns a plain dict so this is directly usable as an ADK tool result.
    """
    settings = get_settings()
    plans = plan_segments(duration_sec)
    if not plans:
        return {
            "status": "error",
            "error": "Video duration is zero or unknown; cannot plan segments.",
        }

    semaphore = asyncio.Semaphore(settings.max_concurrent_segments)
    done = 0

    async def run(plan: SegmentPlan):
        nonlocal done
        outcome = await _analyse_one(
            gcs_uri, plan, len(plans), sport, semaphore, metadata_language,
            (segment_uris or {}).get(plan.index, ""),
        )
        done += 1
        if on_segment_done is not None:
            try:
                await on_segment_done(done, len(plans))
            except Exception:
                # Progress reporting must never take the analysis down with it.
                logger.warning("progress callback failed", exc_info=True)
        return outcome

    results = await asyncio.gather(*(run(plan) for plan in plans))

    analyses: list[tuple[SegmentPlan, SegmentAnalysis]] = []
    failures: list[dict] = []
    for plan, analysis, error in results:
        if analysis is None:
            failures.append({"segment": plan.index, "error": error})
        else:
            analyses.append((plan, analysis))

    if not analyses:
        return {
            "status": "error",
            "error": "Every segment failed to analyse.",
            "failures": failures,
            "segments_planned": len(plans),
        }

    merged = merge_segment_results(analyses, sport=sport, job_id="")

    return {
        "status": "partial" if failures else "success",
        "segments_planned": len(plans),
        "segments_analysed": len(analyses),
        "failures": failures,
        "moments": [m.model_dump() for m in merged],
        "segment_summaries": [
            {"index": plan.index, "summary": analysis.segment_summary}
            for plan, analysis in analyses
        ],
        # Captions naming the competition or the venue appear in a handful of
        # segments at most, so these come back as every reading rather than one:
        # the caller settles them the same way it settles the team names.
        "competitions": [a.competition for _, a in analyses if a.competition.strip()],
        "venues": [a.venue for _, a in analyses if a.venue.strip()],
    }


# --- Merge --------------------------------------------------------------------


def _iou(a: Moment, b: Moment) -> float:
    """Temporal intersection-over-union of two moments."""
    lo = max(a.start_sec, b.start_sec)
    hi = min(a.end_sec, b.end_sec)
    overlap = max(0.0, hi - lo)
    if overlap <= 0:
        return 0.0
    union = (a.end_sec - a.start_sec) + (b.end_sec - b.start_sec) - overlap
    return overlap / union if union > 0 else 0.0


def _to_absolute(
    detected: DetectedMoment, plan: SegmentPlan, sport: str, job_id: str
) -> Moment | None:
    profile = get_profile(sport)
    spec = profile.by_code(detected.moment_type)
    if spec is None:
        # The model produced a code outside the taxonomy. Dropping it is safer
        # than persisting a type the UI has no filter or scoring prior for.
        logger.warning("dropping unknown moment_type %r", detected.moment_type)
        return None

    resolved = detected.resolve(plan.duration_sec)
    if resolved is None:
        logger.warning(
            "dropping %s in segment %s: timecodes %s/%s/%s fall outside a %.0fs clip",
            detected.moment_type,
            plan.index,
            detected.start_tc,
            detected.peak_tc,
            detected.end_tc,
            plan.duration_sec,
        )
        return None

    rel_start, rel_peak, rel_end = resolved
    start = plan.start_sec + rel_start
    end = plan.start_sec + rel_end
    peak = plan.start_sec + rel_peak

    return Moment(
        moment_id=uuid.uuid4().hex[:16],
        job_id=job_id,
        moment_type=spec.code,
        category=spec.category,
        label=spec.label,
        start_sec=round(start, 2),
        end_sec=round(end, 2),
        peak_sec=round(min(max(peak, start), end), 2),
        confidence=detected.confidence,
        excitement=detected.excitement,
        highlight_score=score_moment(
            base_score=spec.base_score,
            confidence=detected.confidence,
            excitement=detected.excitement,
            is_goal=detected.is_goal,
        ),
        description=detected.description.strip(),
        evidence=list(detected.evidence),
        scoreboard=detected.scoreboard,
        is_goal=detected.is_goal,
        summary=detected.summary.strip(),
        action_result=detected.action_result.strip(),
        participant=detected.participant.strip(),
        participant_role=detected.participant_role.strip(),
        team1=detected.team1.strip(),
        team2=detected.team2.strip(),
        score_team1=detected.score_team1,
        score_team2=detected.score_team2,
        action_team=detected.action_team.strip(),
        segment_indexes=[plan.index],
    )



def resolve_team_names(moments: list[Moment]) -> tuple[str, str]:
    """The match's home and away names, agreed across every segment.

    Team names do not change during a match, but reading them off a score bug
    thirteen times does not give thirteen identical answers — the graphic is
    occluded, abbreviated differently, or absent for a whole segment. Taking the
    most frequent non-empty reading gives one answer for the match instead of
    records that disagree with each other about who is playing.
    """
    from collections import Counter

    home = Counter(m.team1.strip() for m in moments if m.team1.strip())
    away = Counter(m.team2.strip() for m in moments if m.team2.strip())
    return (
        home.most_common(1)[0][0] if home else "",
        away.most_common(1)[0][0] if away else "",
    )


def apply_team_names(moments: list[Moment], home: str, away: str) -> list[Moment]:
    """Give every moment the agreed names, keeping per-moment scores untouched.

    Scores are deliberately not consensused: they change through the match and a
    moment's own reading is the one that belongs beside its timestamp.
    """
    if not home and not away:
        return moments
    return [m.model_copy(update={"team1": home or m.team1, "team2": away or m.team2})
            for m in moments]


def merge_segment_results(
    analyses: list[tuple[SegmentPlan, SegmentAnalysis]],
    *,
    sport: str,
    job_id: str,
    iou_threshold: float = 0.4,
) -> list[Moment]:
    """Convert per-segment detections to one absolute-timestamped timeline.

    Two detections are the same moment when they share a type and their time
    ranges overlap past ``iou_threshold``. That only happens inside a segment
    overlap, which is exactly the case this exists for. The surviving copy keeps
    the wider time range and the higher confidence, and records both segments.
    """
    candidates: list[Moment] = []
    for plan, analysis in analyses:
        for detected in analysis.moments:
            if detected.is_replay:
                continue
            moment = _to_absolute(detected, plan, sport, job_id)
            if moment is not None:
                candidates.append(moment)

    candidates.sort(key=lambda m: (m.start_sec, -m.confidence))

    merged: list[Moment] = []
    for candidate in candidates:
        match = next(
            (
                existing
                for existing in merged
                if existing.moment_type == candidate.moment_type
                and _iou(existing, candidate) >= iou_threshold
            ),
            None,
        )
        if match is None:
            merged.append(candidate)
            continue

        stronger = match if match.confidence >= candidate.confidence else candidate
        index = merged.index(match)
        merged[index] = stronger.model_copy(
            update={
                "start_sec": min(match.start_sec, candidate.start_sec),
                "end_sec": max(match.end_sec, candidate.end_sec),
                "excitement": max(match.excitement, candidate.excitement),
                "highlight_score": max(match.highlight_score, candidate.highlight_score),
                "evidence": list(dict.fromkeys(match.evidence + candidate.evidence)),
                "scoreboard": match.scoreboard or candidate.scoreboard,
                "is_goal": match.is_goal or candidate.is_goal,
                "segment_indexes": sorted(
                    set(match.segment_indexes) | set(candidate.segment_indexes)
                ),
            }
        )

    merged.sort(key=lambda m: m.start_sec)
    return merged


def score_moment(
    *,
    base_score: float,
    confidence: float,
    excitement: float,
    is_goal: bool,
) -> float:
    """Rank a moment for the suggestion grid.

    The type prior carries the most weight because moment type is the strongest
    single predictor of short-form performance, but a low-confidence detection is
    discounted hard: an editor's time is wasted worse by a confident wrong clip
    than by a missing one.
    """
    prior = 0.5 * base_score
    evidence = 0.2 * confidence
    drama = 0.3 * excitement
    raw = prior + evidence + drama
    if is_goal:
        raw += 0.05
    # Confidence gates the whole score, not just its own term.
    gated = raw * (0.55 + 0.45 * confidence)
    return round(min(1.0, max(0.0, gated)), 4)
