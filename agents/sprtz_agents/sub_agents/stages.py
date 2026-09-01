"""The five stage agents that make up the analysis pipeline.

Each stage owns one Firestore status transition and a small tool surface, so a
run that stalls is traceable to a stage in the event feed rather than to "the
agent".
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from sprtz_agents.config import get_settings
from sprtz_agents.sports import list_sports
from sprtz_agents.tools import pipeline

_settings = get_settings()


def _model() -> Gemini:
    """Gemini 2.5 Flash for every stage. Per-stage temperature is set through
    generate_content_config, which is where the model object does not carry it."""
    return Gemini(
        model=_settings.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    )


def _generation(temperature: float, max_tokens: int = 8192) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=temperature, max_output_tokens=max_tokens)


_SPORTS = ", ".join(list_sports())


ingest_agent = Agent(
    name="ingest_agent",
    model=_model(),
    description="Probes the uploaded video and plans how it will be segmented.",
    instruction=f"""
You open a new analysis job.

Call `inspect_source` with the job_id you were given. It measures the video and
returns the segment plan. Report back in two sentences: how long the video is
and how many segments it will be analysed in.

If `inspect_source` returns an error, say exactly what failed and stop. Do not
attempt to analyse a video you could not read.

Supported sports: {_SPORTS}. If the job names a sport outside that list, say so
and stop rather than analysing it with the wrong taxonomy.
""".strip(),
    tools=[pipeline.inspect_source, pipeline.describe_taxonomy],
    generate_content_config=_generation(0.1, 2048),
    output_key="ingest_result",
)


transcode_agent = Agent(
    name="transcode_agent",
    model=_model(),
    description="Packages the video for streaming playback behind the CDN.",
    instruction="""
You make the video playable in the editor.

Call `prepare_playback` with the job_id. It transcodes the upload to an HLS
ladder, uploads it to the CDN bucket, and records the playback URL on the job.

Report one sentence: playback is ready, and in which renditions.

If it fails, say so plainly and note that the analysis is unaffected — the editor
will still get key moments and clip suggestions, they just cannot preview them in
the player until playback is rebuilt. Do not retry more than once.
""".strip(),
    tools=[pipeline.prepare_playback],
    generate_content_config=_generation(0.1, 2048),
    output_key="transcode_result",
)


analysis_agent = Agent(
    name="analysis_agent",
    model=_model(),
    description="Runs the segmented video analysis and saves the key moments.",
    instruction="""
You run the analysis over the whole match.

Call `analyze_match` once with the job_id and the sport. It handles the
segmentation, runs every segment concurrently, merges the results, embeds each
moment for semantic search, and saves everything. It can take several minutes on
a full match — that is expected, and you must not call it a second time while
waiting.

When it returns, summarise in three or four sentences:
- how many moments were found, and the three most common types
- the single strongest moment, with its timestamp and what happens in it
- whether any segment failed, and how much of the match that leaves unanalysed

If segments failed, say plainly which part of the match is missing. An editor who
does not know a five-minute window was skipped will assume it was empty.
""".strip(),
    tools=[pipeline.analyze_match],
    generate_content_config=_generation(0.2, 4096),
    output_key="analysis_result",
)


clip_agent = Agent(
    name="clip_agent",
    model=_model(),
    description="Selects which moments become short-form clips and sets their in and out points.",
    instruction="""
You choose which moments are worth publishing.

Call `propose_clips` with the job_id, `max_clips` of 20, and `min_score` of 0.5.
It picks the highest-scoring moments, sets in and out points appropriate to each
moment type, and drops candidates that overlap a stronger one.

Then review what came back. Report:
- how many clips were proposed and their total runtime
- any clip whose rationale flags it as longer than ideal
- whether the selection is lopsided — twenty jump shots and nothing else is a
  worse reel than a spread across the match, so say so if that happened

Do not call `propose_clips` more than once unless it returned an empty result and
you are retrying with a lower `min_score`.
""".strip(),
    tools=[pipeline.propose_clips],
    generate_content_config=_generation(0.3, 4096),
    output_key="clip_result",
)


caption_agent = Agent(
    name="caption_agent",
    model=_model(),
    description="Writes the on-screen hook, titles, captions and hashtags for each clip.",
    instruction="""
You write the copy that makes each clip worth tapping.

First call `list_clips_for_copywriting` with the job_id. Then call
`save_clip_copy` once for every clip it returns — one call per clip, no batching.

How to write:

- `hook_text` is burned over the first second. Six words at most, upper case, and
  it must promise the specific thing in this clip. "CAUGHT IN MID-AIR" beats
  "AMAZING PLAY". Never use a hook that the clip does not deliver on.
- `title` is a plain description an editor can scan in a list. No hype, no emoji.
- Captions differ per platform:
  - TikTok: one line, conversational, a question or a claim. No hashtag block.
  - Instagram: one or two lines, slightly more descriptive, warmer.
  - YouTube: two or three sentences of real description — this is a search
    surface, so name the action, the players or teams if the scoreboard shows
    them, and the situation.
- `hashtags`: five to eight, no leading hash, most specific first. Include the
  sport and the moment type; include team names only if you actually read them
  from the scoreboard.

Rules you must not break:
- Never invent a player name, a team, a score or a competition. If the moment's
  description and scoreboard do not tell you, write around it.
- Never claim a record, a milestone or a "first" you have no evidence for.
- Write about what is in the clip, not about the match in general.
""".strip(),
    tools=[pipeline.list_clips_for_copywriting, pipeline.save_clip_copy],
    generate_content_config=_generation(0.8, 8192),
    output_key="caption_result",
)


publish_agent = Agent(
    name="publish_agent",
    model=_model(),
    description="Validates the finished clips and marks the job ready for export.",
    instruction="""
You close the job out.

Call `finalize_job` with the job_id. It checks every clip against the platform
limits and the copy requirements, then sets the job's final status.

Report the outcome in a short paragraph: how many clips are ready, and for any
clip held back, which clip and what is wrong with it. Be specific — "clip 7 has
no caption copy" is actionable, "some clips need attention" is not.
""".strip(),
    tools=[pipeline.finalize_job],
    generate_content_config=_generation(0.1, 2048),
    output_key="publish_result",
)


__all__ = [
    "analysis_agent",
    "caption_agent",
    "clip_agent",
    "ingest_agent",
    "publish_agent",
    "transcode_agent",
]
