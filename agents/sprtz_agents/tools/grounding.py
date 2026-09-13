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


_SHOW_PROMPT = """\
An equestrian {discipline} recording was analysed from video. These are the only \
things actually read off the screen:

{observed}

Use Google Search to identify this show and class. Results, start lists and \
judge panels for this sport are published on online.equipe.com — prefer that \
site, and cite the show, class, start-list or start page you actually used. \
Then reply with a single JSON object, and nothing else:

{{
  "show": "full name of the show as the organiser publishes it, or \"\"",
  "competition": "the series or championship it belongs to, if any, or \"\"",
  "className": "the class or test these rounds were in, or \"\"",
  "venue": "venue name, or \"\"",
  "location": "town and country, or \"\"",
  "date": "YYYY-MM-DD of the class, or \"\"",
  "equipeUrl": "the online.equipe.com page for the show or class, or \"\"",
  "judges": [
    {{"position": "C", "name": "judge's full name", "country": "or \"\""}}
  ],
  "startList": [
    {{
      "startNumber": "the start / head number as printed, as a string",
      "startTime": "HH:MM scheduled start, 24-hour, or \"\"",
      "rider": "rider's full name as published",
      "horse": "horse's full name as published",
      "nation": "rider's nation code, or \"\""
    }}
  ],
  "rides": [
    {{
      "rider": "rider's full name as published",
      "horse": "horse's full name as published",
      "startNumber": "as printed, or \"\"",
      "finalPlace": 3,
      "totalPct": 68.957
    }}
  ],
  "notes": "one sentence on what this class was, or \"\""
}}

`startList` is the whole class in published start order, every combination, \
whether or not the camera saw them — it is what lets a round be placed in the \
video by its scheduled time when no graphic named the rider. `rides` is only \
the combinations whose results plainly correspond to one read off the screen: \
same rider, same horse. Copy published spellings; do not correct on-screen \
ones. Use null for a placing or percentage the page does not show, and "" for \
a start time it does not give. Judge positions are the letters around the \
arena (E, H, C, M, B, and F/K/V/S/R/P where used).

If the search does not identify the show with reasonable confidence, return \
empty strings and empty lists. An empty answer is correct here; a plausible one \
is not, because it will be stored as though somebody had read it, and a rider \
credited with a placing from a different class is worse than no placing at all.\
"""


EQUIPE_HOST = "online.equipe.com"

# The page kinds Equipe publishes, and the id each one carries. A show id is
# the one that matters for "did the answer come from where the editor said":
# class, start-list and start pages all live under a show, and their own ids
# appear in the citations for that show's results.
_EQUIPE_ID = re.compile(
    r"online\.equipe\.com/(?:[a-z]{2}/)?(shows|meeting_classes|class_sections|startlists|starts)/(\d+)")


def equipe_ids(urls: list[str]) -> set[str]:
    """`kind:id` for every Equipe page among the editor's links."""
    found: set[str] = set()
    for url in urls or []:
        m = _EQUIPE_ID.search(url or "")
        if m:
            found.add(f"{m.group(1)}:{m.group(2)}")
    return found


def context_lines(urls: list[str]) -> str:
    """The editor's links, as evidence the search is told to honour."""
    urls = [u for u in (urls or []) if u and u.strip()]
    if not urls:
        return ""
    lines = "\n".join(f"- {u.strip()}" for u in urls[:10])
    return (
        "\nThe editor has said these pages are about this recording:\n"
        f"{lines}\n"
        "Treat them as authoritative about WHICH show and class this is. An "
        "online.equipe.com link names the show or class outright: answer only "
        "from that show, and from that class where the link names one, even if "
        "another class at the same show looks a closer match to the evidence "
        "above. Cite the page you used from among them.\n"
    )


def _client() -> Any:
    from google import genai

    settings = get_settings()
    # The model's own location, not the engine's: grounding runs on the same
    # model as the root agent, and a model served only through `global` is a
    # 404 on the engine's regional endpoint.
    return genai.Client(vertexai=True, project=settings.project_id,
                        location=settings.model_location)


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



def observed_show_lines(
    *, competition: str, venue: str, rides: list[dict], scoreboards: list[str],
) -> str:
    """The evidence for a show search: who rode, and whatever named the event."""
    lines = []
    if competition:
        lines.append(f"- Caption naming a competition: {competition}")
    if venue:
        lines.append(f"- Caption naming a venue: {venue}")
    seen = 0
    for ride in rides:
        rider, horse = (ride.get("rider") or "").strip(), (ride.get("horse") or "").strip()
        if not (rider or horse):
            continue
        marks = f" — displayed total {ride['total_pct']}%" if ride.get("total_pct") is not None else ""
        lines.append(f"- Lower third: {rider or '?'} / {horse or '?'}{marks}")
        seen += 1
        # Enough to pin the class; a search does not need all forty.
        if seen >= 12:
            break
    for raw in scoreboards[:3]:
        if raw and raw.strip():
            lines.append(f"- Raw results graphic: {raw.strip()}")
    return "\n".join(lines) if lines else "- Nothing legible was read from the screen."


def cites_equipe(sources: list[dict[str, str]]) -> bool:
    """Whether the answer actually came from Equipe, rather than being steered there.

    A prompt can prefer a site; it cannot make the search return it. The
    returned citations say where the answer really came from, and that is what
    decides whether a placing is recorded as Equipe's or as the web's.
    """
    return any(EQUIPE_HOST in (src.get("uri") or "") for src in sources)


async def identify_show(
    *, discipline: str, competition: str, venue: str,
    rides: list[dict], scoreboards: list[str], context_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve an equestrian competition day against online.equipe.com. Never raises.

    Search grounding rather than a page fetch, and that is not a preference.
    Equipe's show and class pages are JavaScript shells — a direct GET returns
    the navigation and the footer and nothing else, and `.json` on them is 406.
    Googlebot renders the JavaScript, so the results exist in the *index*, and
    the index is what search grounding reads. Handing the model the URL would
    have it grounding on an empty page and confabulating around the title.

    One call per show, not per ride. A class results page carries every
    combination at once, so a single grounded answer fills all of them.
    """
    from google.genai import types

    settings = get_settings()
    observed = observed_show_lines(
        competition=competition, venue=venue, rides=rides, scoreboards=scoreboards)
    context = context_lines(context_urls or [])
    # A link the editor gave is evidence too: with nothing read off the screen
    # but a class page in hand, there is still exactly one thing to search for.
    if observed.startswith("- Nothing legible") and not context:
        return {"grounded": False, "reason": "nothing legible to ground on"}
    observed = observed + context

    try:
        response = await _client().aio.models.generate_content(
            model=settings.model,
            contents=_SHOW_PROMPT.format(
                discipline=(discipline or "").replace("_", " ") or "equestrian",
                observed=observed),
            config=types.GenerateContentConfig(
                temperature=0.0,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                http_options=types.HttpOptions(timeout=2 * 60 * 1000),
            ),
        )
    except Exception as exc:
        logger.warning("show grounding failed: %s", exc, exc_info=True)
        return {"grounded": False, "reason": f"{type(exc).__name__}: {exc}"}

    fields = parse_show(getattr(response, "text", "") or "")
    sources = extract_sources(response)
    if not fields.get("show") and not fields.get("rides") and not fields.get("startList"):
        return {"grounded": False, "reason": "search did not identify the show"}

    # Fail closed. The whole reason an editor supplies a link is that the
    # search once settled on the right show and the wrong class, and a record
    # grounded to the wrong class looks fine — three riders who compete in
    # several classes still match. If the editor named an Equipe show and none
    # of the citations are from it, this answer is not the one asked for, and
    # storing it would reproduce the exact failure the link was meant to fix.
    wanted = {i for i in equipe_ids(context_urls or []) if i.startswith("shows:")}
    if wanted:
        cited = equipe_ids([src.get("uri", "") for src in sources]
                           + [fields.get("equipeUrl", "")])
        if not (wanted & cited):
            return {
                "grounded": False,
                "reason": "answer did not come from the show the editor supplied",
                "sources": sources,
                "queries": search_queries(response),
            }

    return {
        "grounded": True,
        "from_equipe": cites_equipe(sources),
        "show": fields.get("show", ""),
        "competition": fields.get("competition", ""),
        "class_name": fields.get("className", ""),
        "venue": fields.get("venue", ""),
        "location": fields.get("location", ""),
        "match_date": fields.get("date", ""),
        "equipe_url": fields.get("equipeUrl", ""),
        "judges": fields.get("judges", []),
        "start_list": fields.get("startList", []),
        "rides": fields.get("rides", []),
        "notes": fields.get("notes", ""),
        "sources": sources,
        "queries": search_queries(response),
    }


def parse_show(text: str) -> dict[str, Any]:
    """The show answer, with its ride list kept as a list rather than flattened."""
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("rides", "judges", "startList"):
            # A list of rows or nothing. A string here would be iterated by
            # the consumers one character at a time.
            if isinstance(value, list):
                out[key] = [r for r in value if isinstance(r, dict)]
        elif isinstance(value, str):
            out[key] = value.strip()
    return out
