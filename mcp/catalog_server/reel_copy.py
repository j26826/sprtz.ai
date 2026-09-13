"""The copy a reel goes out with.

Same split as the game record, and for the same reason: **facts are assembled
in code and only judgement is generated.** A model asked for "the metadata"
returns a coherent-sounding post whose rider never rode, in a class that was
not held, at a venue in the wrong sport — and unlike a game summary, this one
is published to a channel under someone's name.

So the title is composed from what was read on screen, the keywords come from
the sport's own taxonomy and the names the analysis recorded, and the only
generated things are the description and the hashtags. Even those are given
nothing but observations and told not to go beyond them.

The digest builder and the composers are pure, which is most of this file, and
tested. The one model call is at the bottom and falls back to the composed
copy on any failure: a reel that publishes with a plain description is a much
better outcome than one that cannot be published at all.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Long enough to be worth reading, short enough that YouTube shows it before
# the fold. The hard ceiling is 5000; this is an editorial one.
MAX_DESCRIPTION = 900

# YouTube counts tags against a 500-character budget, so a long tail of them
# costs the useful ones. Eight is what fits comfortably.
MAX_TAGS = 12
MAX_HASHTAGS = 8


class ReelCopy(BaseModel):
    """The interpretive half, and the only generated half."""

    description: str = Field(
        description="Two or three sentences for the channel, in the language "
                    "of the digest. What happens, and why it is worth watching.")
    hashtags: list[str] = Field(
        default_factory=list,
        description="Five to eight hashtags, without the # sign, drawn only "
                    "from names and words that appear in the digest.")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _tag(text: str) -> str:
    """A word or name as a keyword: trimmed, collapsed, never empty."""
    return _clean(text).strip("#").strip()


def _hashtag(text: str) -> str:
    """CamelCase, because a hashtag cannot hold a space."""
    parts = re.findall(r"[A-Za-z0-9]+", str(text or ""))
    return "".join(p[:1].upper() + p[1:] for p in parts)


def compose_title(reel: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """Name the reel from what was actually read.

    Composed, never generated: a model-written title is a sentence that sounds
    like a fixture, and one naming the wrong competition is worse than a dull
    one. An editor's own name always wins — if they typed it, it is the name.
    """
    typed = _clean(reel.get("title"))
    if typed and typed.lower() not in ("highlights", "untitled reel"):
        return typed[:100]

    names = [_clean(e.get("competition") or e.get("title")) for e in events]
    names = [n for n in names if n]
    if len(names) == 1:
        return f"{names[0]} — Highlights"[:100]
    if len(names) > 1:
        sports = {_clean(e.get("discipline") or e.get("sport")) for e in events}
        sports.discard("")
        if len(sports) == 1:
            return f"{next(iter(sports))} — Highlights"[:100]
    return typed[:100] or "Highlights"


def collect_keywords(reel: dict[str, Any], moments: list[dict[str, Any]],
                     events: list[dict[str, Any]]) -> list[str]:
    """Keywords from the record, not from a model.

    The vocabulary is the sport's own — the moment labels the taxonomy defines
    — plus the names the analysis read off the screen. Nothing here is
    invented, which is the whole point: a keyword nobody competed under is a
    channel claiming something that did not happen.

    Ordered by what a search would most plausibly be for: the sport and the
    competition, then who was in it, then what they did.
    """
    ordered: list[str] = []

    def add(value: str) -> None:
        tag = _tag(value)
        if tag and tag.lower() not in {x.lower() for x in ordered}:
            ordered.append(tag)

    for event in events:
        add(event.get("discipline") or event.get("sport"))
        add(event.get("competition"))
        add(event.get("venue"))
    for moment in moments:
        add(moment.get("rider"))
        add(moment.get("horse"))
    for moment in moments:
        add(moment.get("label") or moment.get("moment_type"))
    return ordered[:MAX_TAGS]


def build_digest(reel: dict[str, Any], moments: list[dict[str, Any]],
                 events: list[dict[str, Any]]) -> str:
    """What the generated half gets to read. Observations only.

    Deliberately not the whole records: the model is writing two sentences,
    and everything in here it could get wrong is something it might repeat.
    """
    seconds = round((reel.get("durationMs") or 0) / 1000)
    lines = [
        f"A highlights reel of {len(reel.get('cuts') or [])} cuts, {seconds} seconds long.",
    ]
    if events:
        lines.append("From:")
        for event in events:
            bits = [_clean(event.get("title")), _clean(event.get("discipline") or event.get("sport")),
                    _clean(event.get("competition")), _clean(event.get("venue"))]
            lines.append("- " + " · ".join(b for b in bits if b))
    if moments:
        lines.append("")
        lines.append("What is in it, in the order it plays:")
        for moment in moments:
            who = " / ".join(b for b in [_clean(moment.get("rider")),
                                         _clean(moment.get("horse"))] if b)
            label = _clean(moment.get("label") or moment.get("moment_type"))
            summary = _clean(moment.get("summary"))
            bits = [b for b in [label, who, summary] if b]
            if bits:
                lines.append("- " + " — ".join(bits))
    return "\n".join(lines)


_PROMPT = """\
Below is everything a video analysis observed about one highlights reel. Write \
the copy it goes out with.

{digest}

Return:
- `description`: two or three sentences for the channel. Say what is in the \
reel and why it is worth watching. Write it for someone deciding whether to \
press play, not as a list of what is above.
- `hashtags`: five to eight, without the # sign.

Everything you write must be supported by what is above. Do not name a rider, \
horse, team, competition or venue that does not appear in it; do not state a \
score, a placing or a record; and do not say a moment was the best, the \
fastest or the first unless it says so. If the digest is thin, write less.\
"""


def prompt_for(digest: str) -> str:
    return _PROMPT.format(digest=digest)


def fallback_description(reel: dict[str, Any], moments: list[dict[str, Any]],
                         events: list[dict[str, Any]]) -> str:
    """Plain copy, composed, for when the model is not there.

    Not an apology: it is accurate, and a reel that publishes with a plain
    description is a much better outcome than one that cannot be published.
    """
    labels: list[str] = []
    for moment in moments:
        label = _clean(moment.get("label") or moment.get("moment_type"))
        if label and label not in labels:
            labels.append(label)
    where = " · ".join(_clean(e.get("competition") or e.get("title")) for e in events if e)
    parts = []
    if labels:
        parts.append(f"In this reel: {', '.join(labels[:6])}.")
    if where:
        parts.append(f"From {where}.")
    return " ".join(parts)


def compose(reel: dict[str, Any], moments: list[dict[str, Any]],
            events: list[dict[str, Any]], generated: ReelCopy | None = None) -> dict[str, Any]:
    """The finished copy: composed facts, plus whatever the model added."""
    keywords = collect_keywords(reel, moments, events)
    # Whether the model wrote what is actually being used, not merely whether
    # it replied. A whitespace-only answer falls back, and saying it was
    # generated would be the flag describing the attempt rather than the copy.
    written = _clean(getattr(generated, "description", ""))
    described = written or fallback_description(reel, moments, events)

    tags = [_hashtag(h) for h in (getattr(generated, "hashtags", None) or [])]
    if not tags:
        tags = [_hashtag(k) for k in keywords]
    hashtags: list[str] = []
    for tag in tags:
        if tag and tag.lower() not in {h.lower() for h in hashtags}:
            hashtags.append(tag)

    return {
        "title": compose_title(reel, events),
        "description": described[:MAX_DESCRIPTION],
        "tags": keywords,
        "hashtags": hashtags[:MAX_HASHTAGS],
        "generated": bool(written),
    }
