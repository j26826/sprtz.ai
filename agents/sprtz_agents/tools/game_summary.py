"""Builds the game-level record from what the analysis actually observed.

This does not re-watch the match. Everything factual — who played, the final
score, the competition, the venue — is already in the segment results, so the
job here is to settle it rather than to discover it, and settling it in code
means it cannot be hallucinated. Gemini is asked for one thing only: the
interpretive fields, sentiment and mood and a short summary, over a digest of
what was seen.

That split matters more than it looks. A model asked for "the game details" in
one go will happily return a coherent-sounding record whose teams never played
each other, in a competition that does not include them, at a venue in the wrong
sport. The facts here are copied; only the judgements are generated.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from pydantic import BaseModel, Field

from sprtz_agents.schemas import GameDetails, Moment


class Judgement(BaseModel):
    """The interpretive half of a game record, and the only generated half."""

    sentiment: str = Field(description="Positive, Neutral or Negative.")
    mood: str = Field(description="One word or short phrase: Intense, End-to-end, Cagey.")
    summary: str = Field(description="Two or three sentences on how the match went.")

logger = logging.getLogger(__name__)

# "SWE 24-23 DEN 58:41" — the scoreline is the pair of numbers around a dash.
# \u2013 is an en dash, which broadcast score bugs do use in place of a hyphen.
_SCORE_RE = re.compile("(\\d{1,3})\\s*[-\u2013:]\\s*(\\d{1,3})")


def most_common_reading(values: list[str]) -> str:
    """The most frequent non-empty reading.

    Non-empty matters: on real footage most segments carry no legible caption at
    all, so a plain majority would answer "nothing" almost every time.
    """
    counter = Counter(v.strip() for v in values if v and v.strip())
    return counter.most_common(1)[0][0] if counter else ""


def final_score_from(moments: list[Moment]) -> tuple[str, int | None, int | None]:
    """The last scoreline anyone could actually read.

    Taken from the latest moment that carried one, not from counting goals: a
    tally of what the analysis happened to detect is not the scoreboard, and
    presenting it as the final score would state a result nobody displayed.
    """
    with_score = [
        m for m in sorted(moments, key=lambda m: m.start_sec)
        if m.score_team1 is not None and m.score_team2 is not None
    ]
    if with_score:
        last = with_score[-1]
        return f"{last.score_team1}-{last.score_team2}", last.score_team1, last.score_team2

    # Fall back to the raw score bug text, which survives even when the parsed
    # fields did not.
    for moment in sorted(moments, key=lambda m: m.start_sec, reverse=True):
        match = _SCORE_RE.search(moment.scoreboard or "")
        if match:
            home, away = int(match.group(1)), int(match.group(2))
            return f"{home}-{away}", home, away
    return "", None, None


def outcome_from(home_team: str, away_team: str, home: int | None, away: int | None) -> str:
    """Who won, or nothing at all.

    An unreadable final score means the winner is unknown. Saying so is the only
    honest answer — a result asserted without a scoreline behind it is the kind
    of thing that gets published.
    """
    if home is None or away is None:
        return ""
    if home == away:
        return "Draw"
    winner = home_team if home > away else away_team
    return f"{winner} win" if winner else "Home win" if home > away else "Away win"


def build_digest(moments: list[Moment], segment_summaries: list[dict]) -> str:
    """What the interpretive pass gets to read. Observations only."""
    by_type = Counter(m.label for m in moments)
    by_result = Counter(m.action_result for m in moments if m.action_result)

    lines = [
        f"{len(moments)} moments detected.",
        "Most common: " + ", ".join(f"{k} x{v}" for k, v in by_type.most_common(8)),
    ]
    if by_result:
        lines.append("Outcomes: " + ", ".join(f"{k} x{v}" for k, v in by_result.most_common(8)))
    lines.append("")
    lines.append("Segment summaries, in order:")
    for entry in segment_summaries:
        summary = (entry.get("summary") or "").strip()
        if summary:
            lines.append(f"- {summary}")
    return "\n".join(lines)


_JUDGEMENT_PROMPT = """\
Below is what a video analysis observed across one {sport} match. Judge the match \
as a whole from it.

{digest}

Return:
- `sentiment`: Positive, Neutral or Negative — the overall tone of the contest.
- `mood`: one word or short phrase for how it felt, such as Intense, End-to-end, \
Cagey or One-sided.
- `summary`: two or three sentences on how the match went.

Judge only from what is above. Do not name players, teams, competitions or venues \
that do not appear in it, and do not state a result — the score is established \
elsewhere and yours would be a guess.\
"""


def judgement_prompt(sport: str, digest: str) -> str:
    return _JUDGEMENT_PROMPT.format(sport=sport, digest=digest)


def compose_title(*, home: str, away: str, competition: str, fallback: str,
                  discipline: str = "") -> str:
    """Name the match from what was actually read.

    Composed here rather than asked of a model, for the same reason the rest of
    the factual half is: a generated title is a sentence that sounds like a
    fixture, and one that names the wrong competition is worse than no title at
    all. When nothing on screen identified the match this falls back to what the
    editor called the upload, which is at least a name they chose themselves.
    """
    if home and away:
        pairing = f"{home} v {away}"
        return f"{pairing} — {competition}" if competition else pairing
    # One team legible is still more use than a filename.
    if home or away:
        known = home or away
        return f"{known} — {competition}" if competition else known
    # An equestrian round often names nobody: the graphic carries a number and a
    # time. "Jumping — CSI Aachen" is a title; the uploaded filename is not.
    if discipline:
        return f"{discipline} — {competition}" if competition else discipline
    return competition or fallback


def assemble(
    *,
    job_id: str,
    sport: str,
    moments: list[Moment],
    segment_summaries: list[dict],
    competitions: list[str],
    venues: list[str],
    judgement: dict | None = None,
    fallback_title: str = "",
    discipline: str = "",
    discipline_confidence: float = 0.0,
) -> GameDetails:
    """Put the record together: facts from the observations, judgements from the model."""
    home_team = most_common_reading([m.team1 for m in moments])
    away_team = most_common_reading([m.team2 for m in moments])
    final_score, home, away = final_score_from(moments)
    judgement = judgement or {}

    competition = most_common_reading(competitions)

    return GameDetails(
        job_id=job_id,
        sport=sport,
        discipline=discipline,
        discipline_confidence=discipline_confidence,
        title=compose_title(
            home=home_team, away=away_team,
            competition=competition, fallback=fallback_title,
            discipline=discipline,
        ),
        home_team=home_team,
        away_team=away_team,
        competition=competition,
        venue=most_common_reading(venues),
        final_score=final_score,
        event_outcome=outcome_from(home_team, away_team, home, away),
        sentiment=(judgement.get("sentiment") or "Neutral").strip(),
        mood=(judgement.get("mood") or "").strip(),
        summary=(judgement.get("summary") or "").strip(),
        moment_count=len(moments),
        highlight_count=sum(1 for m in moments if m.highlight_score >= 0.6),
    )


def embed_text(game: GameDetails) -> str:
    """What makes a game findable.

    "the Sweden Denmark game", "that intense one at the Royal Arena", "handball
    semi-final" — the answer has to carry the teams, the competition, the venue
    and the mood, because those are the words people actually use to mean a
    whole match rather than a moment inside one.
    """
    parts = [
        game.title,
        game.sport,
        # "the dressage test", "that reining round" — for a sport with
        # disciplines, the discipline is the word someone searches by, and it is
        # not in the teams or the venue.
        game.discipline.replace("_", " "),
        game.home_team,
        game.away_team,
        f"{game.home_team} v {game.away_team}" if game.home_team and game.away_team else "",
        # The grounded full names are what someone actually types. A score bug
        # says SWE; nobody searches for "SWE".
        game.grounded_home_team,
        game.grounded_away_team,
        game.competition or game.grounded_competition,
        game.venue or game.grounded_venue,
        game.event_outcome,
        game.mood,
        game.sentiment,
        game.summary,
    ]
    return ". ".join(p.strip() for p in parts if p and p.strip())
