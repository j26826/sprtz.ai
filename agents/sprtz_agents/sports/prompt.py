"""Builds the Gemini video-analysis prompt for one segment of a match.

Split deliberately in two:

* The **system instruction** carries the role, the court context and the whole
  moment catalogue. It is byte-identical for every segment of every job in a
  sport, so it is a context-cache hit after the first call.
* The **segment prompt** is short and carries only what changes: where this
  window sits in the match, and how to report time.

That split is not just an optimisation. An earlier version inlined the full
catalogue into every per-segment prompt, and on real footage the model stopped
reporting observed timestamps and started emitting a sequential counter
(1.1s, 1.5s, 2.3s...) — thirty "moments" inside a three-second span. Keeping the
per-request prompt short fixed it. If you lengthen the segment prompt, re-check
that reported timestamps still spread across the window.
"""

from __future__ import annotations

from sprtz_agents.sports.registry import SportProfile

_SYSTEM = """\
You are a senior {display_name} video analyst working for a highlights desk. You \
watch match footage and mark the moments an editor would cut into a vertical \
short for TikTok, Instagram Reels or YouTube Shorts.

An editor cuts on your timestamps without re-watching the match, so a timestamp \
that is two seconds late costs them the shot.

{context}

# Moment catalogue

Report only these. Use each code exactly as written.

{catalogue}

# Never report

{exclusions}\
"""


_TASK = """\
This clip is segment {index} of {total}, covering {start_clock}-{end_clock} of the match.

# Timestamps

Give `start_tc`, `peak_tc` and `end_tc` as MM:SS elapsed **within this clip**. The \
first frame of the clip is 00:00 and the last is {duration_clock}.

Judge each timecode from where the action actually sits in the clip. Do not number \
moments sequentially, do not reuse a timecode for two different moments, and never \
report a timecode past {duration_clock}. If action runs throughout the clip, your \
last moment should sit near the end of it.

- `start_tc`: first frame of the build-up. For a fast break that is the turnover; \
for a card it is the foul, not the card.
- `peak_tc`: the decisive frame — ball leaving the hand, the save, the card raised.
- `end_tc`: where it resolves, including the immediate reaction.

# Judgement

- `confidence` is about the classification. Use 1.0 only when it is beyond doubt, \
0.5-0.8 when probable, below 0.5 when unsure but still worth flagging. Vary it — a \
response where every moment scores the same confidence is wrong.
- `excitement` compares this instance against a typical one of the same type. Weigh \
how close the score is, how much time remains, the crowd and commentary reaction, \
and how hard the action was.
- `evidence`: what you actually saw or heard, not a restatement of the definition.
- Read the score bug whenever legible and put its raw text in `scoreboard`.

{boundary_guidance}

If nothing in this clip is worth reporting, return an empty `moments` list. An empty \
answer is correct and useful; a padded one is not.\
"""

_FIRST_BOUNDARY = """\
This is the first segment, so an action already underway in the opening frames has \
no visible build-up. Report it from 00:00 and say so in `description`.\
"""

_OVERLAP_BOUNDARY = """\
This clip overlaps the previous one by {overlap:.0f} seconds. Report anything you \
see in that opening window anyway — a moment straddling the boundary is better \
caught twice and merged than missed.\
"""


def _clock(seconds: float) -> str:
    total = round(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _mmss(seconds: float) -> str:
    total = round(seconds)
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def _format_catalogue(profile: SportProfile) -> str:
    lines: list[str] = []
    for category in profile.categories:
        lines.append(f"\n## {category.replace('_', ' ').title()}\n")
        for m in (t for t in profile.moment_types if t.category == category):
            entry = f"**`{m.code}` — {m.label}.** {m.description}"
            if m.cues:
                entry += " Look for: " + "; ".join(m.cues) + "."
            lines.append(entry)
    return "\n".join(lines).strip()


def _format_exclusions(profile: SportProfile) -> str:
    if not profile.exclusions:
        return "Nothing specific."
    return "\n".join(f"- {item}" for item in profile.exclusions)


def build_system_instruction(profile: SportProfile) -> str:
    """Stable across every segment, so it caches."""
    return _SYSTEM.format(
        display_name=profile.display_name,
        context=profile.context.strip(),
        catalogue=_format_catalogue(profile),
        exclusions=_format_exclusions(profile),
    )


def build_segment_prompt(
    profile: SportProfile,
    *,
    index: int,
    total: int,
    start_sec: float,
    end_sec: float,
    overlap_lead_sec: float = 0.0,
) -> str:
    """Short, per-segment task prompt. Keep it short — see the module docstring."""
    boundary = (
        _FIRST_BOUNDARY
        if index == 0 or overlap_lead_sec <= 0
        else _OVERLAP_BOUNDARY.format(overlap=overlap_lead_sec)
    )
    return _TASK.format(
        index=index + 1,
        total=total,
        start_clock=_clock(start_sec),
        end_clock=_clock(end_sec),
        duration_clock=_mmss(end_sec - start_sec),
        boundary_guidance=boundary,
    )
