"""Google Search grounding for what the footage cannot tell you.

The analysis reads the picture, and the picture only ever shows so much: a score
bug says `SWE 24-23 DEN`, not which competition this is, which round, or that
the venue caption two segments earlier said Royal Arena. Grounding resolves that
kind of thing against the web and, crucially, says where each answer came from.

Two rules shape how it is used here.

**It runs once per match, not once per moment.** A per-moment search would be
hundreds of queries for one job, and would answer questions the frame already
answers. One call establishes the fixture; everything else is derived from
observation.

**Grounded values never overwrite observed ones.** They land in their own fields
alongside their sources, so a caller can always tell what a camera showed from
what a search suggested. That distinction is the whole reason this is safe to
add: the failure mode being guarded against is a confident record whose teams
never played each other, and silently merging the two would reintroduce it in a
form nobody could audit.

Search grounding and structured output are requested separately for a practical
reason: asking for both a search tool and a strict response schema in one call
is fragile across model versions, so this asks for prose with citations and
parses it, which works either way.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sprtz_agents.config import get_settings

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

_FIXTURE_PROMPT = """\
A {sport} match was analysed from video. These are the only things actually read \
off the screen or heard in the commentary:

{observed}

Use Google Search to identify this fixture. Then reply with a single JSON object, \
and nothing else:

{{
  "competition": "full name of the competition or league, or \\"\\"",
  "homeTeamFullName": "canonical full name of the first team, or \\"\\"",
  "awayTeamFullName": "canonical full name of the second team, or \\"\\"",
  "venue": "venue name, or \\"\\"",
  "matchDate": "YYYY-MM-DD, or \\"\\"",
  "notes": "one sentence on what this fixture was, or \\"\\""
}}

If the search does not identify the fixture with reasonable confidence, return \
empty strings. An empty field is a correct answer here; a plausible guess is not, \
because it will be stored as though someone had read it. Never fill a field from \
what sounds likely for these initials — abbreviations collide across sports and \
leagues.\
"""


def _client() -> Any:
    from google import genai

    settings = get_settings()
    return genai.Client(vertexai=True, project=settings.project_id, location=settings.location)


def observed_lines(
    *, home_team: str, away_team: str, final_score: str,
    competition: str, venue: str, scoreboards: list[str],
) -> str:
    """The evidence handed to the search, so it grounds on facts not on vibes."""
    lines = []
    if home_team or away_team:
        lines.append(f"- Score bug names: {home_team or '?'} v {away_team or '?'}")
    if final_score:
        lines.append(f"- Last legible score: {final_score}")
    if competition:
        lines.append(f"- Caption naming a competition: {competition}")
    if venue:
        lines.append(f"- Caption naming a venue: {venue}")
    for raw in scoreboards[:3]:
        if raw and raw.strip():
            lines.append(f"- Raw score bug text: {raw.strip()}")
    return "\n".join(lines) if lines else "- Nothing legible was read from the screen."


def parse_fixture(text: str) -> dict[str, str]:
    """Pull the JSON object out of a grounded reply.

    A grounded response is prose plus citations rather than clean JSON, so the
    object is extracted rather than assumed to be the whole body.
    """
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: (v.strip() if isinstance(v, str) else "") for k, v in data.items()}


def extract_sources(response: Any) -> list[dict[str, str]]:
    """Where the answer came from, so a person can check it."""
    sources: list[dict[str, str]] = []
    for candidate in getattr(response, "candidates", None) or []:
        metadata = getattr(candidate, "grounding_metadata", None)
        for chunk in getattr(metadata, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            if web is None or not getattr(web, "uri", ""):
                continue
            source = {"title": getattr(web, "title", "") or "", "uri": web.uri}
            if source not in sources:
                sources.append(source)
    return sources


def search_queries(response: Any) -> list[str]:
    queries: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        metadata = getattr(candidate, "grounding_metadata", None)
        for query in getattr(metadata, "web_search_queries", None) or []:
            if query not in queries:
                queries.append(query)
    return queries


async def identify_fixture(
    *, sport: str, home_team: str, away_team: str, final_score: str,
    competition: str, venue: str, scoreboards: list[str],
) -> dict[str, Any]:
    """Resolve the match against Google Search. Never raises.

    Grounding is an enrichment: a match that cannot be identified is still a
    perfectly good job, so every failure here degrades to "not grounded" rather
    than failing the stage.
    """
    from google.genai import types

    settings = get_settings()
    observed = observed_lines(
        home_team=home_team, away_team=away_team, final_score=final_score,
        competition=competition, venue=venue, scoreboards=scoreboards,
    )
    if observed.startswith("- Nothing legible"):
        # With nothing read off the screen there is nothing to ground on, and a
        # search would be answering from the sport alone.
        return {"grounded": False, "reason": "nothing legible to ground on"}

    try:
        response = await _client().aio.models.generate_content(
            model=settings.model,
            contents=_FIXTURE_PROMPT.format(sport=sport, observed=observed),
            config=types.GenerateContentConfig(
                temperature=0.0,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                http_options=types.HttpOptions(timeout=2 * 60 * 1000),
            ),
        )
    except Exception as exc:
        logger.warning("fixture grounding failed: %s", exc, exc_info=True)
        return {"grounded": False, "reason": f"{type(exc).__name__}: {exc}"}

    fields = parse_fixture(getattr(response, "text", "") or "")
    sources = extract_sources(response)
    if not any(fields.values()):
        return {"grounded": False, "reason": "search did not identify the fixture"}

    return {
        "grounded": True,
        "competition": fields.get("competition", ""),
        "home_team_full_name": fields.get("homeTeamFullName", ""),
        "away_team_full_name": fields.get("awayTeamFullName", ""),
        "venue": fields.get("venue", ""),
        "match_date": fields.get("matchDate", ""),
        "notes": fields.get("notes", ""),
        "sources": sources,
        "queries": search_queries(response),
    }
