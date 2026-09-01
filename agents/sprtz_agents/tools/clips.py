"""Turning scored moments into publishable cuts.

Clip boundaries are decided arithmetically rather than by the model: the lead-in
and follow-through a moment type needs is a property of the sport, and an editor
reviewing forty suggestions needs them to be consistent. The model's judgement is
spent on copy, not on subtraction.
"""

from __future__ import annotations

import uuid

from sprtz_agents.schemas import ClipSuggestion, Moment
from sprtz_agents.sports import get_profile

# Short-form platforms all accept far longer, but retention on a sports clip
# falls off a cliff after about 30 seconds and a clip under 5 seconds reads as a
# glitch.
MIN_CLIP_SECONDS = 6.0
MAX_CLIP_SECONDS = 30.0
IDEAL_CLIP_SECONDS = 14.0

# Two suggestions covering the same eight seconds of match are one suggestion and
# one annoyance.
MIN_SEPARATION_SECONDS = 4.0


def _overlaps(a: ClipSuggestion, start: float, end: float) -> bool:
    return start < a.end_sec + MIN_SEPARATION_SECONDS and end > a.start_sec - MIN_SEPARATION_SECONDS


def plan_clip_window(
    moment: Moment,
    *,
    sport: str,
    video_duration_sec: float,
) -> tuple[float, float]:
    """Choose in and out points around a moment.

    Anchored on the peak rather than the reported start, because the peak is the
    timestamp the model is most reliable about and the one the viewer is waiting
    for. A jump shot wants the run-up; a red card wants the foul that earned it.
    """
    profile = get_profile(sport)
    spec = profile.by_code(moment.moment_type)
    lead_in = spec.lead_in_seconds if spec else 2.5
    follow = spec.follow_through_seconds if spec else 3.0

    start = moment.peak_sec - lead_in
    end = moment.peak_sec + follow

    # Never clip away action the analysis actually reported.
    start = min(start, moment.start_sec)
    end = max(end, moment.end_sec)

    # A high-excitement moment earns a longer reaction tail.
    if moment.excitement >= 0.75:
        end += 2.0

    duration = end - start
    if duration < MIN_CLIP_SECONDS:
        shortfall = MIN_CLIP_SECONDS - duration
        start -= shortfall * 0.4
        end += shortfall * 0.6
    elif duration > MAX_CLIP_SECONDS:
        # Trim the lead-in first; the payoff is at the end.
        start = end - MAX_CLIP_SECONDS

    start = max(0.0, start)
    if video_duration_sec > 0:
        end = min(video_duration_sec, end)
        if end - start < MIN_CLIP_SECONDS:
            start = max(0.0, end - MIN_CLIP_SECONDS)

    return round(start, 2), round(end, 2)


def _placeholder_hook(moment: Moment) -> str:
    """A hook that stands in until caption_agent writes the real one.

    Kept factual so a suggestion is reviewable even if the caption stage fails.
    """
    if moment.moment_type == "last_second_free_throw":
        return "AFTER THE BUZZER"
    if moment.moment_type == "kempa_trick":
        return "CAUGHT IN MID-AIR"
    if moment.is_goal:
        return moment.label.upper()
    return moment.label.upper()


def build_clip_suggestions(
    moments: list[Moment],
    *,
    sport: str,
    job_id: str,
    max_clips: int = 20,
    video_duration_sec: float = 0.0,
) -> list[ClipSuggestion]:
    """Pick the best moments and lay a clip over each.

    Moments are taken strongest-first so that when two candidates cover the same
    passage of play the better one wins the slot.
    """
    ranked = sorted(moments, key=lambda m: m.highlight_score, reverse=True)
    suggestions: list[ClipSuggestion] = []

    for moment in ranked:
        if len(suggestions) >= max_clips:
            break

        start, end = plan_clip_window(
            moment, sport=sport, video_duration_sec=video_duration_sec
        )
        if end - start < MIN_CLIP_SECONDS:
            continue
        if any(_overlaps(existing, start, end) for existing in suggestions):
            continue

        duration = round(end - start, 2)
        suggestions.append(
            ClipSuggestion(
                clip_id=uuid.uuid4().hex[:16],
                job_id=job_id,
                moment_id=moment.moment_id,
                start_sec=start,
                end_sec=end,
                duration_sec=duration,
                aspect="9:16",
                hook_text=_placeholder_hook(moment),
                title=moment.label,
                captions={},
                hashtags=[],
                score=moment.highlight_score,
                rationale=_rationale(moment, duration),
            )
        )

    suggestions.sort(key=lambda c: c.start_sec)
    return suggestions


def _rationale(moment: Moment, duration: float) -> str:
    parts = [
        f"{moment.label} at {_clock(moment.peak_sec)}",
        f"scored {moment.highlight_score:.2f}",
        f"cut to {duration:.0f}s",
    ]
    if moment.excitement >= 0.75:
        parts.append("extended tail for the reaction")
    if duration > IDEAL_CLIP_SECONDS + 8:
        parts.append("longer than ideal — trim the lead-in if retention drops")
    return "; ".join(parts) + "."


def _clock(seconds: float) -> str:
    total = round(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
