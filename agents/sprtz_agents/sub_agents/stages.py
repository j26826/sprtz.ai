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
from sprtz_agents.tools import live, pipeline

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
returns the segment plan. Report back in two sentences: what sport the job is
for, how long the video is, and how many segments it will be analysed in.

Name the sport even though nobody asked: your reply is the context every later
stage sees, and leaving it out is what makes them start guessing.

If `inspect_source` returns an error, say exactly what failed and stop. Do not
attempt to analyse a video you could not read.

Supported sports: {_SPORTS}. If the job names a sport outside that list, say so
and stop rather than analysing it with the wrong taxonomy. Do not ask which one
it is — it is on the job, and there is nobody here to answer.
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
will still get key moments, they just cannot preview them in
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

Call `analyze_match` once with the job_id you were given, and nothing else.

**Never ask which sport it is.** The sport was chosen at upload and is recorded
on the job; the tool reads it from there. Nobody is reading your reply while
this runs, so a question is not a pause — it is the end of the analysis, and
every stage after it then completes on zero moments and reports the job
finished. If you find yourself without a fact you think you need, call the tool
anyway: it has the job in front of it and you do not.

It handles the
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


finalize_agent = Agent(
    name="finalize_agent",
    model=_model(),
    description="Closes the run out on what the analysis found.",
    instruction="""
You close the job out.

Call `finalize_job` with the job_id. It reads how many moments the analysis
saved and sets the job's final status: ready when there are moments, failed
when there are none, because a run that found nothing needs someone to look at
it rather than to read as a quiet match.

Report the outcome in one or two sentences: how many moments the match holds,
or — if there are none — that the analysis produced nothing and needs re-running.
""".strip(),
    tools=[pipeline.finalize_job],
    generate_content_config=_generation(0.1, 2048),
    output_key="finalize_result",
)


__all__ = [
    "analysis_agent",
    "finalize_agent",
    "ingest_agent",
    "transcode_agent",
]


live_event_agent = Agent(
    name="live_event_agent",
    model=_model(),
    description=(
        "Drives a live event: starts its capture on time, analyses each chunk as "
        "the recorder closes it, and finishes the event when the recorder does."
    ),
    instruction="""
You look after a live event. You are woken once a minute by the scheduler, not
by an editor, and each wake-up is one step.

Call `live_tick` exactly once with the job_id you were given, then report its
result in one sentence and stop. Never call it twice in one turn, and never
call anything else unless the editor asked a question — for "how is the live
event going?" call `live_status` and answer from it.

What the result means:
- `scheduled`: not due yet; say when the capture is due.
- `started`: the capture has begun; say so.
- `live`: say how many chunks were analysed this time and how many are done
  of the expected total. If a chunk failed or did not follow on from the one
  before it, say which — an editor who does not know five minutes are missing
  will assume nothing happened in them.
- `complete`: say how many chunks and key moments the event ended with.
- `busy`: another tick holds the event; say so and stop.
- `error`: say exactly what failed.

Never start `analysis_pipeline` on a live event. It has no source file; its
chunks are analysed as they arrive, by this tick.
""".strip(),
    tools=[live.live_tick, live.live_status],
    generate_content_config=_generation(0.1, 2048),
    output_key="live_result",
)
