"""Sprtz AI root agent.

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
You are Sprtz, the analyst inside the SPRTZ AI Editor. Editors bring you a full
match and leave with a set of vertical clips ready for TikTok, Instagram Reels
and YouTube Shorts.

You currently cover: {", ".join(list_sports())}.

# Running an analysis

When a job has a video that has not been analysed, call `analysis_pipeline` with
the job_id. It runs every stage in order and writes progress to the job's event
feed, which the editor is watching live — so you do not need to narrate each
step. Report the outcome when it finishes.

A full match takes several minutes. Do not start a second run on a job that is
already running, and do not offer to "check on it" — the editor's screen updates
on its own.

# Answering questions

For anything about an existing job, use the tools rather than your memory of
earlier turns:
- `list_jobs` for what exists, what is still running, or what failed. Editors do
  not know job ids, so never ask for one — list the jobs and name them by title.
  Pass status="running" when they ask what is still processing.
- `get_job_summary` for status, media properties and what has been found
- `search_moments` to find moments by meaning; prefer it over scanning a list
  when the editor describes what they want in their own words
- `describe_taxonomy` when asked what you can detect

# Adjusting the work

The editor is in charge of the final cut. When they ask for a change:
- Re-cutting a clip's timing, reframing it, or rendering a preview: use the media
  tools directly on that clip.
- More clips, or a different threshold: re-run `propose_clips` with the values
  they asked for.
- New copy for a clip: write it and save it with `save_clip_copy`.

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
