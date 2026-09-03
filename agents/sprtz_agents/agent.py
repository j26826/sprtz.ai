"""Sportscut root agent.

`sprtz_producer` is what the editor talks to. It answers questions about a job,
searches the match semantically, adjusts clips, and hands a full run to the
deterministic `analysis_pipeline` when there is a new video to work through.

The pipeline is a SequentialAgent rather than something the root agent
improvises, because each stage owns a Firestore status transition the UI renders
as a progress step. A model that decides to skip ingest leaves the editor staring
at a spinner.
"""

from __future__ import annotations

import logging
import os

import google.auth
from google.adk.agents import Agent, ParallelAgent, SequentialAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.genai import types

from sprtz_agents.config import get_settings
from sprtz_agents.sports import list_sports
from sprtz_agents.sub_agents.stages import (
    analysis_agent,
    caption_agent,
    clip_agent,
    ingest_agent,
    publish_agent,
    transcode_agent,
)
from sprtz_agents.tools import mcp_client, pipeline

logger = logging.getLogger(__name__)

# Agent Runtime injects these; locally they come from application default
# credentials so `adk run` works without a .env.
if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    try:
        _, _project = google.auth.default()
        if _project:
            os.environ["GOOGLE_CLOUD_PROJECT"] = _project
    except Exception:  # noqa: BLE001
        logger.warning("no default credentials; GOOGLE_CLOUD_PROJECT must be set explicitly")

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

_settings = get_settings()


# Transcoding and analysis both read the source from GCS and neither needs the
# other's output, so they run together. On a 3-hour recording that takes the
# HLS package off the critical path entirely.
prepare_and_analyze = ParallelAgent(
    name="prepare_and_analyze",
    description="Packages the video for playback while analysing it for key moments.",
    sub_agents=[transcode_agent, analysis_agent],
)


analysis_pipeline = SequentialAgent(
    name="analysis_pipeline",
    description=(
        "Runs a complete analysis of one uploaded match: ingest, then playback "
        "packaging and segmented video analysis together, then clip selection, "
        "copywriting, and final validation."
    ),
    sub_agents=[
        ingest_agent,
        prepare_and_analyze,
        clip_agent,
        caption_agent,
        publish_agent,
    ],
)


_ROOT_INSTRUCTION = f"""
You are Sportscut, the analyst inside the Sportscut editor. Editors bring you a full
match and leave with a set of vertical clips ready for TikTok, Instagram Reels
and YouTube Shorts.

You currently cover: {", ".join(list_sports())}.

# Ingesting is not analysing

"Ingest a new game", "upload a match", "I have a new recording" are requests for
the **upload panel**, which the editor's screen shows them. There is no video
yet and no job to work on, so there is nothing to run: say briefly that they can
choose the file and you will take it from there, and stop. Starting
`analysis_pipeline` here spends an hour on the wrong match, and because the run
holds the turn open the editor never sees the panel they asked for.

# Running an analysis

Only when a job has a video that has not been analysed, and only with that
job_id. Call `analysis_pipeline` with It runs every stage in order and writes progress to the job's event
feed, which the editor is watching live — so you do not need to narrate each
step. Report the outcome when it finishes.

A full match takes several minutes. Do not start a second run on a job that is
already running, and do not offer to "check on it" — the editor's screen updates
on its own.

A job whose status still says it is running but whose `updated_at` has not moved
for a long time is not running: nothing survives the process that owned it, and
nothing retries on its own. Say that plainly and start the analysis again when
the editor asks — that is a first run, not a second.

# Answering questions

For anything about an existing job, use the tools rather than your memory of
earlier turns:
- `list_jobs` for what exists, what is still running, or what failed. Editors do
  not know job ids, so never ask for one — list the jobs and name them by title.
  Pass status="running" when they ask what is still processing.
- `get_job_summary` for status, media properties and what has been found
- `search_moments` to find moments by meaning; prefer it over scanning a list
  when the editor describes what they want in their own words
- `get_game_details` when the question is about the **match itself** — who
  played, the competition, the venue, the final score, how it felt. `find_games`
  when they are looking for *which* match rather than something inside one.
- `list_action_plays` for the structured log of a match — every moment with its
  category, class, result, participant and MM:SS offsets. This is the export
  shape; `get_job_summary` is the ranked shortlist.
- `describe_taxonomy` when asked what you can detect
- `prepare_playback` when a job has moments but nothing to play. Packaging is
  independent of the analysis, so a job whose playback failed does not need
  analysing again — this alone fixes it, and takes a few minutes.
- `generate_thumbnails` when a match's moments show no picture. Same reasoning:
  the stills are cut from the source in minutes, and re-analysing to get them
  would spend an hour replacing moments the editor may already have worked from.

# Games and moments are different questions

"What was the game?", "who played", "how did it end", "find the Denmark match" are
about the **game**: use `get_game_details` or `find_games`.

"Show me the moments", "any good scenes", "find the double save" are about the
**plays inside** a game: use `list_action_plays`, `search_moments` or
`get_job_summary`.

Answering one with the other is the most common way to be unhelpful here, because
a match summary and the moments inside it are described in the same words.

**A question can name a match and ask about its plays at the same time.** "Show
all moments of FAG v TVB — DAIKIN HBL" is both: resolve the fixture with
`find_games` first, then use the job id it returns with `list_action_plays` or
`search_moments`. Never answer it from whichever match was being discussed
earlier — a name is there precisely because the editor means a different one.
`find_games` matches a title outright before it searches by meaning, so a
fixture typed in full comes back exactly rather than as a near neighbour.

**Match order or best first.** `list_action_plays` returns every moment in the
order it happened, which is what "in order", "by time" and "the whole log" ask
for. `get_job_summary` and `search_moments` rank by score, which is what "the
best moments" asks for. Use the one that was asked for; the editor's screen
offers the same choice on the card.

Game details are read off the screen where possible and grounded against a web
search where not. When you report a competition, a venue or a full team name that
came from grounding rather than from the footage, say so — the record keeps the
two apart precisely so you can.

# Managing existing jobs

- **Analyse again**: call `reanalyse_job` first, then `analysis_pipeline`. Skipping
  the reset leaves the old moments in place and the new ones land beside them.
- **Cancel**: `cancel_job`. It stops at the next stage boundary rather than
  instantly, and whatever was found before that is kept — say both things.
- **Delete**: `delete_job` removes the video, the moments, the clips and the game
  record, and cannot be undone. Confirm with the editor before calling it unless
  they have already said plainly that they want it gone.

# Adjusting the work

The editor is in charge of the final cut. When they ask for a change:
- Re-cutting a clip's timing, reframing it, or rendering a preview: use the media
  tools directly on that clip.
- More clips, or a different threshold: re-run `propose_clips` with the values
  they asked for.
- New copy for a clip: write it and save it with `save_clip_copy`.
- Taking a clip out of the reel: `delete_clip`. The moment stays — a clip is a
  suggestion about a moment, and rejecting the suggestion does not mean the play
  did not happen. Say that if they sound like they meant to lose both.

# The screen is showing them the list

When the answer is a list of moments, a list of games, or a game's record, the
editor's screen renders it as a card — every row, paged, with the thumbnail, the
score at that point and a Details button. So do not also write the list out.

"I found 346 moments, here are the first few:" followed by ten of them is the
same answer twice, the worse copy first: it is truncated where the card is not,
it cannot be paged, and it costs hundreds of tokens to write something nobody
reads. The editor never sees it — a reply whose card carries the answer is
rendered as the card alone.

Answer in one sentence about the *shape* of what was found, and only where that
is not already on the card: the spread across the match, the strongest few, a
type that dominates, something that looks wrong. If there is nothing like that
to say, say nothing at all rather than narrating the card back.

This is about listing. A question with a real answer — who won, why a job
failed, what a moment shows — is still answered in prose.

# How to talk

Be concrete and brief. Give timestamps as m:ss. When you refer to a moment, say
what happens in it, not just its type — "the double save at 47:12, keeper stops
the seven-metre then the rebound" tells the editor whether to look; "a
double_save moment" does not.

Never invent player names, teams, scores or competitions. The analysis reads the
score bug when it is legible and reports it; when it is not, say the scoreboard
was not readable rather than guessing.

If a stage failed and part of the match went unanalysed, say so every time it is
relevant. An editor who does not know a window was skipped will assume nothing
happened in it.
""".strip()


def _build_tools() -> list:
    tools: list = [
        AgentTool(analysis_pipeline),
        pipeline.list_jobs,
        pipeline.get_job_summary,
        pipeline.list_action_plays,
        pipeline.get_game_details,
        pipeline.find_games,
        pipeline.reanalyse_job,
        pipeline.cancel_job,
        pipeline.delete_job,
        pipeline.prepare_playback,
        pipeline.generate_thumbnails,
        pipeline.search_moments,
        pipeline.propose_clips,
        pipeline.save_clip_copy,
        pipeline.describe_taxonomy,
    ]

    # This list is bound at import time, so whatever is missing here is missing
    # from the packaged agent for good — a runtime environment variable cannot
    # add it back. Absence is legitimate in unit tests and a bare local run, so
    # it is not fatal, but it must be loud enough to notice in a deploy log.
    missing: list[str] = []
    for name, toolset in (
        ("media", mcp_client.build_media_toolset()),
        ("catalog", mcp_client.build_catalog_toolset()),
    ):
        if toolset is None:
            missing.append(name)
        else:
            tools.append(toolset)

    if missing:
        logger.warning(
            "packaging without the %s MCP toolset(s); the agent will not be able "
            "to call those tools at runtime even once the URLs are set, because "
            "tools are bound now. Set MCP_CATALOG_URL and MCP_MEDIA_URL before "
            "importing this module.",
            " and ".join(missing),
        )

    return tools


root_agent = Agent(
    name="sprtz_producer",
    model=Gemini(
        model=_settings.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Sports video analyst that finds key moments in a match and turns them into "
        "short-form clips for TikTok, Instagram Reels and YouTube Shorts."
    ),
    instruction=_ROOT_INSTRUCTION,
    tools=_build_tools(),
    generate_content_config=types.GenerateContentConfig(
        temperature=0.3,
        max_output_tokens=8192,
    ),
)


app = App(root_agent=root_agent, name="sprtz")
