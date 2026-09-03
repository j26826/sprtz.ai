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

# What the analysis can be asked to write in. The value is the language's own
# name, because naming it in itself is what a model responds to most reliably.
METADATA_LANGUAGES = {
    "en": "English",
    "de": "German (Deutsch)",
    "it": "Italian (Italiano)",
    "fr": "French (Français)",
    "es": "Spanish (Español)",
}

_LANGUAGE_RULE = """\
# Language

Write `description`, `evidence` and `segment_summary` in {language}.

Everything you copy off the screen stays exactly as it appears: team names, \
competition and venue captions, the score bug, shirt numbers. Those are things \
you read, not things you write, and translating them invents a name that was \
never shown. `action_result` and `participant_role` stay in English too — they \
are codes the software matches on, not prose for a reader.\
"""

_SYSTEM = """\
You are a senior {display_name} video analyst working for a highlights desk. You \
watch match footage and mark the moments an editor would cut into a vertical \
short for TikTok, Instagram Reels or YouTube Shorts.

An editor cuts on your timestamps without re-watching the match, so a timestamp \
that is two seconds late costs them the shot.

{context}

{language_rule}

{discipline_step}# Moment catalogue

Report only these. Use each code exactly as written.

{catalogue}

# Never report

{exclusions}

# Describing an action

Alongside the classification, report what the action *was*:

- `action_result` — how it ends, in a word or two: {action_results}. Leave it \
empty when the action does not resolve on camera.
- `participant` — who does it, **only when you can actually read it**: a shirt \
number, or a name shown on screen or spoken by the commentary. Write it as \
`#7 red`, or `unknown`. Never infer a name from the competition, the kit or the \
context — an invented name is worse than an empty field, because an editor will \
publish it.
- `participant_role` — {participant_roles}.
- `summary` — one sentence naming who did what and how it ended, in the order a \
commentator would say it: "#12 blue saves the seven-metre and turns the rebound \
over the bar." This is not a shorter `description`. The description says what the \
picture shows; the summary says what happened, and it is the line an editor \
scans a list by.

# Reading the on-screen graphic

{scoreboard_guidance}

Report `competition` and `venue` for the clip as a whole, and only when a caption, \
a graphic or the commentary actually names one. Leave them empty otherwise — do \
not work the competition out from the teams, or the venue from the home side.\
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


def _entry(m) -> str:
    entry = f"**`{m.code}` — {m.label}.** {m.description}"
    if m.cues:
        entry += " Look for: " + "; ".join(m.cues) + "."
    return entry


def _format_catalogue(profile: SportProfile) -> str:
    """Grouped by category, or by discipline for a sport that has them.

    A dressage test and a showjumping round share nothing an editor cuts on, so
    listing their moments together under "Obstacle" and "Flatwork" would hide
    the one distinction that decides which half of the catalogue applies.
    """
    lines: list[str] = []

    if profile.disciplines:
        for discipline in profile.disciplines:
            types = [m for m in profile.moment_types if m.discipline == discipline.code]
            if not types:
                continue
            lines.append(f"\n## {discipline.label}\n")
            lines += [_entry(m) for m in types]
        shared = [m for m in profile.moment_types if not m.discipline]
        if shared:
            lines.append("\n## Any discipline\n")
            lines += [_entry(m) for m in shared]
        return "\n".join(lines).strip()

    for category in profile.categories:
        lines.append(f"\n## {category.replace('_', ' ').title()}\n")
        lines += [_entry(m) for m in profile.moment_types if m.category == category]
    return "\n".join(lines).strip()


_DISCIPLINE_STEP = """\
# First, identify the discipline

Before you look for anything else, work out which form of this sport the footage \
shows. Judge it on the tack, the rider's attire, the obstacles and the way the \
horse moves — not on the commentary, which often names a competition rather than \
a discipline.

{disciplines}

Report your answer in `discipline`, using the code exactly as written, and how \
sure you are in `discipline_confidence`.

Then use **only the moments listed under that discipline** below. A sliding stop \
does not happen in a dressage test, and reporting one means you have identified \
the wrong discipline rather than found a rare movement.

If the footage genuinely does not settle it, say so with a low \
`discipline_confidence` and report the moments you are confident of. A wrong \
discipline stated confidently is worse than an uncertain one, because everything \
downstream is filtered by it.

"""


def _format_disciplines(profile: SportProfile) -> str:
    return "\n".join(
        f"- **`{d.code}` — {d.label}.** {d.cues}." for d in profile.disciplines
    )


def _format_exclusions(profile: SportProfile) -> str:
    if not profile.exclusions:
        return "Nothing specific."
    return "\n".join(f"- {item}" for item in profile.exclusions)


def build_system_instruction(profile: SportProfile, metadata_language: str = "en") -> str:
    """Stable across every segment of a job, so it caches.

    The language rule lives here rather than in the segment prompt for the same
    reason everything else does: it is identical for every segment, so putting
    it here costs the short per-request prompt nothing and caches after the
    first call. It does mean the cache is per language as well as per sport,
    which is the correct granularity — two jobs in different languages are not
    running the same instruction.
    """
    language = METADATA_LANGUAGES.get(metadata_language, METADATA_LANGUAGES["en"])
    step = (
        _DISCIPLINE_STEP.format(disciplines=_format_disciplines(profile))
        if profile.disciplines else ""
    )
    return _SYSTEM.format(
        display_name=profile.display_name,
        context=profile.context.strip(),
        language_rule=_LANGUAGE_RULE.format(language=language),
        discipline_step=step,
        catalogue=_format_catalogue(profile),
        exclusions=_format_exclusions(profile),
        action_results=", ".join(profile.action_results) or "a word or two of your own",
        participant_roles=(
            ", ".join(profile.participant_roles) or "the role the performer is playing"
        ),
        scoreboard_guidance=profile.scoreboard_guidance.strip(),
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
