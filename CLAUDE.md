# Arenos — working notes

An agentic SaaS that watches a full sports match and finds the moments worth
publishing, which an editor then downloads or puts on a channel. Clip
generation — a reel of proposed cuts with copy written for each — was withdrawn
in September 2026 to be rebuilt, so what is here now is the finding and the two
ways out of it. Handball and equestrian today;
the sport taxonomy is pluggable. Rebranded from Sportscut onto the Arenos
design system — see the Brand and UI sections below.

Read `docs/ARCHITECTURE.md` for *why* the pieces fit together. This file is for
working *in* the repo: conventions, commands, and the things that have already
cost a debugging cycle.

---

## Layout

```
agents/      ADK agents on Vertex AI Agent Runtime (the product's brain)
  sprtz_agents/agent.py          sprtz_producer + analysis_pipeline
  sprtz_agents/sub_agents/       the four pipeline stages
  sprtz_agents/sports/           moment taxonomies + Gemini prompt  ← add sports here
  sprtz_agents/tools/            segmented analysis, rides, MCP access
mcp/         MCP tool servers (private Cloud Run)
  media_server/                  Transcoder API for HLS; ffmpeg: probe, cut, stills; YouTube upload
  catalog_server/                Firestore, embeddings, KNN + Gemini rerank
api/         FastAPI behind IAP: signed uploads, signed CDN URLs, agent SSE proxy
web/         Arenos editor SPA (chat-first, Arenos design system)
jobs/        Cloud Run Jobs: hls2mp4 (vendored Rust HLS downloader + its core crate)
deploy/      cloudbuild.yaml, terraform/, scripts/{preflight,bootstrap}.sh
```

## Commands

```bash
# Agents
cd agents && uv sync --all-groups
GOOGLE_CLOUD_PROJECT=ci uv run pytest tests/unit -q     # 45 tests
uv run ruff check sprtz_agents                          # must be clean

# MCP servers
cd mcp && uv run --with pytest --with fastmcp --with google-cloud-firestore \
  --with google-genai --with google-cloud-storage --with google-auth \
  --with pydantic --with requests pytest tests -q       # 145 tests

# API
cd api && ENVIRONMENT=local uvicorn app.main:app --reload   # bypasses IAP

# Terraform (needs the binary; not installed by default here)
cd deploy/terraform && terraform fmt -recursive && terraform validate
```

**A test must not pin "now" to a literal instant.** `test_live_event.py` fixed
`NOW` at 2026-09-10 12:00 UTC, and the window validator refuses an event that
has already ended — so the suite passed until that wall-clock time and failed
every build after it, an hour later the same day, naming a validation error
rather than a clock. Relative windows come from `datetime.now(UTC)`; a test
that needs a literal date picks one far enough ahead to stay ahead.

`ENVIRONMENT=local` is the only thing that bypasses IAP verification, and
Terraform sets `ENVIRONMENT` in every deployed environment, so that branch is
unreachable in the cloud.

## Git workflow

Branch → commit → push → **PR → merge → delete the branch**, every time. Never
commit to `main` directly. Delete merged branches locally and on the remote.

Commit identity for this repo is `j26826 <j26826@pm.me>`, set repo-locally.
Never use any other name.

Merging to `main` fires the Cloud Build trigger, which runs `terraform apply`
against the live project. Treat a merge as a deploy.

---

## Decisions that are load-bearing

### Models and analysis

- **Gemini 2.5 Flash for video analysis, in `us-central1`; Gemini 3.6 Flash
  for search reranking, through Vertex's `global` location.** 3.6 Flash ran
  the analysis for a day and its moments were judged less accurate on the
  equestrian footage, so the analysis went back to 2.5; the lesson below about
  structured output stays, because the reranker is still on 3.6 and any later
  model may behave the same. In this project every regional endpoint returns
  404 for the post-2.5 Flash models, so a model that is only global is not a
  model in `us-central1`. Model and location therefore travel as a pair:
  `analysis_model`/`analysis_location` and `rerank_model`/`rerank_location` in
  Terraform, reaching the engine as `SPRTZ_ANALYSIS_MODEL`/`_LOCATION` and the
  catalog as `RERANK_MODEL`/`RERANK_LOCATION`. Each falls back to the engine's
  own model and region when unset. The **root agent, the game judgement and
  grounding are also on Gemini 2.5 Flash** in `us-central1` (`gemini_model`), and
  so does **gemini-embedding-001** (768-dim)
  for semantic search. The embedding width must equal the Firestore vector
  index dimension exactly or queries fail at read time, not write time.
- **Structured output is requested as `response_json_schema`, never
  `response_schema=<Pydantic class>`.** Handed the class, the SDK converts it
  to Vertex's own Schema type, and gemini-3.6-flash under that constraint
  writes a float as an unbounded run of digits — `"discipline_confidence":
  0.0000…` for thirty thousand characters until the token cap, so the JSON
  never closes. Nine of sixteen segments were "Unparseable response" on the
  first 3.6 run, and the reranker degraded to vector order without a word.
  The same shape passed as `cls.model_json_schema()` answers in seconds; the
  thinking configuration made no difference either way. `response.parsed` is
  only filled for the class form, so every site parses `response.text` itself.
  `TestResponseSchemasAreJsonSchema` reads the three sources and fails on a
  regression, because no unit test can see the difference.
- **The merge is near-linear, and it had to become so.** An engine worker was
  killed between the last window and "Found N key moments" on a 3.75-hour
  recording: `merge_segment_results` scanned everything merged so far for
  every candidate and then scanned again to find the match's index, which is
  cubic in the worst case — and a model that over-produces on a competition
  day is exactly that worst case. Candidates are sorted by start, so the only
  entries one can overlap are the recent ones; the scan walks back from the
  end and stops once even the longest moment seen could no longer reach the
  candidate's start. It also logs how many detections it was given, because
  that number is the first thing anyone will want when this happens again.
- A match is split into **15-minute segments overlapping by 20s**, analysed
  concurrently, then merged with temporal IoU per moment type. A 3-hour
  recording is 13 segments.
- **The segments are real files, not offsets.** Gemini fetches the *whole*
  object to serve a request whatever `video_metadata` offsets it is given, so a
  3.22 GiB source failed every window with `File content exceeded the size
  limit. max_bytes_fetched: 2146971648` — 2.0 GiB. Slicing by time never made
  the bytes smaller. `split_for_analysis` stream-copies one file per window into
  the media bucket, and the request points at that instead. Offsets remain the
  fallback for a source small enough to fetch.
- The cut is a copy, not an encode: `-ss` on an HTTPS source is a range read, so
  each window pulls roughly its own share. Segments are written, uploaded and
  deleted one at a time — the writable filesystem is memory, and holding
  thirteen at once is how this container died before. They are removed once the
  analysis has read them, and a job delete clears any a dead run left behind.
- A pre-cut file is also a clip that genuinely starts at 00:00, which is what
  the prompt claims its timecodes are relative to. Reading offsets into a long
  file, the model has been seen reporting match-absolute times instead — 54
  detections in one run, every one dropped as out of window.
- **A segment analysis retries; it did not.** One 429 lost a whole fifteen-minute
  window, and the job reported no moments found rather than a quota problem.
  Vertex quota is per-minute and every window goes out at once, so exhausting it
  is ordinary. Six attempts from 8s doubling to a 120s ceiling, with jitter so
  thirteen segments do not retry in lockstep and rebuild the burst.
  `max_concurrent_segments` is 3 rather than 6 for the same reason — retries
  carry the rest, this reduces how often they are needed.
- **1 fps, not 2.** Doubling the sample rate doubled cost *and* made timestamps
  worse.
- The model reports **`MM:SS` timecodes within the clip**, not float seconds.
  Out-of-window values are rejected, never clamped — a clamp puts a moment at a
  timestamp nobody observed.


### HLS sources, live events, and the watchdog

Three kinds of job, told apart by `kind`: `upload` (a file in the bucket),
`hls` (a recorded playlist URL), `live` (a playlist URL and a time window).

**An HLS source is downloaded by a Cloud Run Job, not by the media service.**
`jobs/hls2mp4` is the `vod-hls2mp4` tool vendored from ais-media-services
with its `ais-media-core` sibling; its Dockerfile's runtime takes ffmpeg from
Debian rather than from upstream's shared base image, which takes an hour to
build and this pipeline does not want. The build context is `jobs/hls2mp4`,
not the crate — the Dockerfile COPYs the two crates as siblings. It is the
longest build step by far, so it starts first and reuses the previous image's
dependency layer through `--cache-from`. The media service starts an
execution with per-run env (`download_hls`) and the ingest stage polls it
(`hls_download_status`), the same shape as a Transcoder encode. The download
lands under `hls/<job>/source/` in the uploads bucket as `.mp4` (CMAF) or
`.ts` (MPEG-TS) — which is not known until the playlist is read, so the status
call lists the prefix rather than assuming a name — and `set_source` puts it
on the job. **Then the 1 fps 480p proxy is made from it on Transcoder**
(`make_analysis_proxy`, polled with `transcode_status` like the package),
stored as `source.analysisUri`: the analysis reads that, because it is the
same picture Gemini samples anyway at a fraction of the bytes. The download
tool can make the proxy itself (`--proxy-1fps`) and the first release let it —
that is one core decoding the whole recording after the download, an hour on
a 3.75-hour match, where Transcoder spreads it and reads the bucket directly.
The download job pulls sixteen segments at once (`MAX_PARALLEL`, streamed
straight into GCS, so the bound is the instance's network); eight managed 28
MB/s on a 6.9 GB recording. **A segment the origin no longer has is skipped, not fatal.** A live playlist
with a DVR window (`vbegin=`) expires segments from its far edge while a
download is still walking it, and one 404 at segment 1447 of 3228 ended the
whole recording — `fetch_bytes` retried a permanent status three times and
then aborted. `fetch_bytes_optional` tells "not there" (404/410) from "could
not answer", the downloader counts the misses and carries on, and its audio
is dropped with it so the tracks cannot drift. More than 5% missing, or ten,
fails the job: publishing the remains of a playlist that has outrun its
window would be a false success.

**The playlist is asked before the execution is
started** (`check_playlist`): a signed CDN link expires — JW Player's carries
`exp=` — and without the check the job spent a three-minute cold start to say
"the job did not succeed", with the 403 only in its own log.
The object goes on the job *before* the proxy is attempted, and a proxy that
fails is a warning: the analysis then cuts the source into windows as it does
for an upload. The Transcoder service agent needs write on the media bucket
for it (`transcoder_media_write`), the same minutes-in failure as the
package's grants. Thumbnails and downloads still read `source.gcsUri`; a still
from a 480p proxy is not a still.

**A prefix of an MPEG-TS is a shorter file.** `probe_media` reads the first
32 MiB and trusts the result only for an MP4-family container, whose header
states the duration; a transport stream has no such header, ffprobe reports
the length of what it was given, and 32 MiB of a 6.9 GB recording probed as a
149-second video — the analysis ran on one window and found nothing, twice.
A container with no header is measured from its two ends instead: the
last packet's time minus the first's, from two 32 MiB range reads
(`_ends_probe`, tail aligned to the 188-byte packet). Not over HTTPS: the
service's ffmpeg 7.1 read the whole 6.9 GB to answer that, where ffmpeg 9 on
a laptop answered with one seek in a second, and the probe timed out at ten
minutes. HTTPS remains the path for a non-faststart MP4. `bytes` always comes
from the object. **The ffmpeg log line masks `-headers`** (`redacted`): it
carried the bucket bearer token into Cloud Logging once per probe. **A source with no audio track is encoded without one**: Transcoder
asked for an AAC stream from a silent file fails minutes in with "does not
have any inputs with an audio track", so `transcode_hls` and
`make_analysis_proxy` read the head first (`_source_has_audio`). **An HLS
recording whose audio is a separate rendition — JW Player's are — arrives
from the download tool as video-only MPEG-TS**, because it muxes a separate
audio rendition only into CMAF. `download_hls` reads the master it already
fetched for the check, and when the chosen variant names an `EXT-X-MEDIA`
audio group and its media playlist is TS, it reports the audio playlist;
the ingest stage then runs `mux_audio` — a Cloud Run Job on the media image
(`media_server.remux`) that fetches the audio segments to the memory-backed
disk and has one ffmpeg stream-copy video from the bucket and audio from
disk into a new transport stream, piped straight back to the bucket — and
polls `mux_status`, which hands over the muxed object and deletes the silent
one. A mux that fails is a warning and the recording stays silent; the
analysis, the preview and the stills all still run. The segment fetches retry
from two seconds doubling with jitter, on a keep-alive session per worker:
the first real mux lost all of a three-thousand-segment fetch to one dropped
CDN connection. A failed ffmpeg removes the empty object its upload had
already created, because a zero-byte object under the muxed name reads as a
finished mux.

**A live event is a recorder plus a tick, never one long process.** A live
playlist is a sliding window of a few segments — 20 to 30 seconds — so the
capture cannot be something that looks once a minute, and nothing on the
engine survives a deploy, so it cannot be an agent run either. The recorder
is `media_server.live_capture`, run as a Cloud Run Job execution on the
mcp-media image (`command` override): it follows the playlist until the end
time or `EXT-X-ENDLIST`, writes each media segment to the media bucket, and
joins them **server-side with GCS compose** into five-minute chunks
(`live_chunk_seconds`) — no bytes pass through the container. Each closed
chunk is one document under `jobs/{job}/chunks/{index}` carrying its first and
last media-sequence numbers, its programme-date-time span, and what the
recorder saw slide past. That is the media service's second Firestore writer,
on purpose: a recorder that has to wait on the catalog's request path is a
recorder that drops segments while it waits.

The tick is **Cloud Scheduler → `POST /api/live/tick` → `live_event_agent`**,
once a minute. The route is the one on the API not for an editor: it admits a
Google-signed ID token for exactly that audience from exactly the scheduler's
service account (`scheduler_caller`), and 404s when neither is configured.
The API lists live jobs, wakes the agent only for the ones due — a scheduled
event from `live_lead_seconds` before its start, a running one always — and
`live_tick` does one step: start the capture, or analyse every chunk the
recorder has closed, or finish. A chunk is analysed **through `_analyse_one`,
the same call an uploaded match's windows go through**, as a file that starts
at 00:00 with the plan's `start_sec` set to the chunk's offset, so the merged
timestamps are absolute and the event reads as one timeline. Claiming a chunk
is a Firestore transaction and the tick holds a lock on the job, because the
scheduler fires on the minute whatever the last tick is still doing and five
minutes analysed twice is every moment in them saved twice.

**A separate audio rendition is recorded beside the video and muxed per
chunk.** JW Live and Unified Streaming keep the audio in an `EXT-X-MEDIA`
group, so a recorder that follows the variant alone records a silent event.
The recorder resolves the group's default rendition (`separate_audio_url`),
polls its playlist after the video's on every loop, stores its segments as
`audio_parts/` and, when a video chunk closes, composes the audio parts
whose sequence numbers fall in the chunk's range into `chunk_NNNN_audio`
(`AudioParts`, pure) — the origins that publish separate audio number both
in step. The chunk record carries `audioUri`/`audioSegments`/`audioMissing`;
the tick asks `mux_chunk` for one file (seconds of ffmpeg in-request — a
chunk is small, unlike a whole recording) before `_analyse_one`, reads the
thumbnails from it, and records `muxedUri` so a retried chunk is not muxed
twice. A mux that fails is a warning and the chunk is analysed silent.

**A live event is a stream while it is on.** It had nothing to play until
someone packaged it, and packaging is an encode of the whole recording — so
every moment found while the event was on opened on "not packaged for playback
yet", eleven times on the LeMieux day with nobody pressing the button. The
recorder already holds each segment as it arrives, so it also writes it into
the **HLS bucket** under `jobs/{job}/live/`, with an `index.m3u8` beside them
(`LiveStream`, pure). The CDN serves that prefix and the signed cookie is
already scoped to `/jobs/{job}/`, so `/playback` points the player at it with
no encode and no new grant — the media service account already held
`objectAdmin` on that bucket. It sits beside `hls/` rather than in it because an
encode clears `hls/` before writing. Four details carry it:

- **Time on the playlist is time on the event.** A segment that could not be
  fetched keeps its place as `EXT-X-GAP`, and segments that slid past unfetched
  are held at the target duration; leaving them out would pull everything after
  earlier and every later moment would open on the wrong seconds. A gap before
  the first segment is not written, since the event's clock starts there.
- **The playlist is uncacheable** (`no-cache, no-store`). The CDN caches purely
  by origin headers, so that header is the whole arrangement; segments are
  immutable and cache for a day.
- **`EVENT`, not a sliding window**, so the whole day stays seekable; the finish
  adds `EXT-X-ENDLIST` and it becomes a plain VOD. A restarted recorder reads
  the playlist back and carries it on — never ended, or a player would be told
  an event that is still on had finished.
- **The recording comes first.** A failed stream write is a warning and nothing
  more: a recorder that dropped a segment because its playback copy failed would
  be the worst trade available.

`/playback` prefers the stream to a package for a live event: a package made
mid-event is a snapshot of the chunks at that moment, and every moment found
after it would seek past its end. It reports `source` as `live`, `recorded` or
`package`, and builds the URL from the job id rather than the stored path. The
segments are written twice — once for the analysis, once for the player — so a
live event costs about twice its size in storage. A separate audio rendition is
not in the stream: JW-style events play video-only there, and the package and
the per-chunk mux still carry their audio.

**A live event's chunks also become one recording**, for what comes after. It has
no source video — it has a row of five-minute chunks — so `prepare_playback`
refused it with "has no source video" and every moment the analysis had
found opened on "This match has not been packaged for playback yet".
`_compose_live_source` joins the chunks with GCS compose in the bucket they
are already in (`compose_live_source`, `gcs.compose`, the same rounds-of-32
the recorder uses), so no bytes pass through the container and a twelve-hour
event costs a few API calls. It runs on demand, which is what makes a long
broadcast watchable before it ends, and again when the event finishes so
downloads and source-quality stills have a file to read. The muxed chunk wins
over the silent one where there is both.

**A stream that stops ends the event.** Stalling is ordinary on a live stream
— a class ends, the broadcaster stops the encoder, the origin starts answering
404 — and the recorder used to wait on it until the scheduled end. Castr went
404 at 17:01 on the LeMieux day and the recorder polled it every three seconds
for four and a half hours, holding the event open with its bar at 44% and
logging a full traceback on every poll. Now a stream that has produced nothing
new for `stallMinutes` ends the way `EXT-X-ENDLIST` does: the partial chunk is
closed and analysed, the capture is marked `finished` with `endedBy: stalled`,
and the tick finishes the event normally and says why it ended early. **The
stall clock starts at the first segment, not at the start of the recording** —
the recorder begins five minutes early, and a broadcaster who goes live on the
minute has produced nothing for exactly that long; waiting on a stream that has
not begun is the lead-in. The limit is an editor setting (default 5 minutes,
1-240), copied onto the event when it is booked like the metadata language, so
it reaches the recorder as `STALL_MINUTES` on its execution. **An event booked
without one gets the default** (`SPRTZ_LIVE_STALL_MINUTES`, 5): the first cut
let it wait for its end time, and the LeMieux day — booked an hour before the
setting existed — polled a stream gone since 17:01 to 21:30 on exactly that. **A
restart onto a dead stream ends, it does not fail.** A resumed recording's
stream is not late, it was flowing and has gone, so its stall clock runs from
the restart and `resolve` waits only the stall limit rather than the ten-minute
grace a late producer gets; before, it gave up as "failed", was restarted three
times, and the tick failed the whole event over hours of good chunks. And a
**running execution keeps the image it started with**: a fix to the recorder
reaches the next recording, never the one in progress. The cost
of a short limit is real and worth knowing: a lunch break the broadcaster cuts
the feed for is, to the recorder, a stream that stopped.

**A restart resumes; it does not start again.** `start_live_capture` clears the
job's live prefix on a first start, and the restart path went through the same
call — so restarting a dead recorder mid-event deleted every chunk recorded
before it. The moments survived in Firestore, but the recording the event is
played back from is composed out of those files. The restart passes `resume`,
which keeps them.

**Continuity is checked two-sided.** The recorder notes what it saw; the tick
checks each chunk against the previous one *as stored* — sequence numbers that
do not follow on, wall clock that does not agree — because a gap between two
recorder executions is exactly the gap the recorder cannot see. A gap is a
warning event naming the chunk, never a silent join.

**The same clock is the watchdog.** Nothing on the engine retries a run that
dies with its process, and the job then reads as running for ever — so the
tick also asks the catalog for running jobs nothing has written to in fifteen
minutes and hands each to `recover_job`, which confirms the silence from the
job's own `updatedAt`, counts the restart on the job (`recovery.attempts`,
capped at two — a job that dies every time is saying something), clears the
previous findings, and tells the root agent to run `analysis_pipeline` again.
The live tick does the equivalent for its own parts: a failed chunk is put
back to `captured` up to three times, and so is a chunk still `analyzing`
from a claim older than the tick lock — the tick that claimed it died with
its process, which is what a deploy over a live event does to the chunk in
flight, and nothing else would ever ask for it again; and a recorder execution that has died
or stopped reporting for five minutes while the event is still on is
restarted, up to three times; a restarted recorder **resumes its chunk
numbering** from what is on record, or the second execution's chunk 0 would
sit on top of the first's. **A stage that waits on something else has to
keep the clock moving**: the watchdog reads the job's `updatedAt`, and an event
in the feed does not touch it. The first HLS download on the desk was restarted
fifteen minutes in by a tick that could not tell "Still downloading" from a
dead run, so the download poll and every wait on Transcoder re-report their
last progress fraction every five minutes — the bar does not move, the clock
does. Inside a VOD analysis, segments that failed get a
second pass on their own once the burst is over — what the HTTP retry cannot
cover is a response that came back unparseable, and a window asked again with
the quota free usually answers.

Four figures are mirrored and must move together: the stall default
(`liveStallMinutes` in `web/src/settings.js`, `stall_minutes` on the API's
`LiveEventRequest`, `SPRTZ_LIVE_STALL_MINUTES` on the engine), the lead-in
(`SPRTZ_LIVE_LEAD_SECONDS`, `LIVE_LEAD_SECONDS` on the API, `LIVE_LEAD_SEC` in
`web/src/live.js`) and the chunk length (`live_chunk_seconds` in Terraform,
reaching the recorder, the media service and the engine) — the web's copies
only decide what a row says before the job document carries its own.

The scheduler lives in `scheduler_region` (default `us-central1`): Cloud
Scheduler serves fewer regions than Cloud Run and the job can target any URL.

### The prompt is the product

Keep the **per-segment prompt short**. The 18-type moment catalogue lives in the
*system instruction*, where it is byte-stable and caches.

An earlier version inlined the catalogue into every segment prompt. On real
footage the model stopped reporting observed timestamps and emitted a
sequential counter instead — thirty "moments" inside three seconds, all at
identical confidence. `test_segment_prompt_stays_short` guards this. If you
lengthen the segment prompt, re-check that timestamps still spread across the
window.

### Firestore query shapes

An inequality filter forces the **first `order_by` to be that same field**.
`list_action_plays` filtered `highlightScore >= x` and ordered by `startSec`,
which Firestore will not run without a composite index — and it says so on a
live read, against real data, with a link to go and create one. That is how "I
cannot retrieve all the moments" reached an editor: nothing failed at import, at
call time, or in any test that mocks the client.

It now orders by `startSec` alone, which the automatic single-field index
serves, and applies the threshold in Python — over-reading first, or a page of
low-scoring early moments would return almost nothing. Same reasoning as the
status filter in `list_jobs`.

`test_query_shapes.py` reads the store's source and fails on this shape
wherever it appears, and checks that every equality-plus-ordering pair still has
an index declared in `firestore.tf`. It is a lint rather than a test, and it
lives with the tests because that is when it needs to run.

### The ActionPlay record

Every detected moment is also an **ActionPlay**: `actionCategory` (one of the
sport's five groupings), `actionClass`, `actionResult`, `participant`,
`participantRole`, `description`, MM:SS `timeOffsetStart`/`End` **into the
match**, and a 0-100 `confidenceScore`. `list_action_plays` returns them in
match order — the structured log, where `list_moments` is the ranked shortlist.

Each record also carries a `summary`: one sentence naming who did what and how
it ended, in the order a commentator would say it. It is **not** a shorter
`description` — the description says what the picture shows, the summary says
what happened, and it is the line an editor scans a list by. Two fields that
read as the same request get the same answer twice, and one of them then costs
tokens in every prompt and every vector for nothing.

`GameDetails` carries a `title`, composed in code from the strongest facts
available — `SWE v DEN — EHF Euro`, falling back to one legible team, then the
competition, then whatever the editor called the upload. Not generated: a
model-written title is a sentence that sounds like a fixture, and one naming the
wrong competition is worse than no title.

**A name a person gave is not a fallback, it is the name.** The desk showed two
for one recording: the job said what had been typed into the ingest panel and
the game said `dressage — LeMieux National Dressage Championships`, composed
from the screen. `titleSource` on the job says which it is wearing — `editor`
for a title someone typed or renamed, `derived` for one taken off a filename or
a URL, and absent means derived so nothing already on the desk changes its name.
Only `editor` beats the composed title (`compose_title(chosen=…)`, fed by
`pipeline.chosen_title`); a filename still loses to what was read on screen,
which is the whole reason composing exists.

**A match can be renamed where its name is read** — the job row, the live row
and the game card, all three inline. `PATCH /api/jobs/{id}/title` is the one
door, as it is for the context links, and `store.rename_job` writes the job and
its game record together so the two cannot disagree afterwards; a job with no
record yet is still renamed, and the record reads `titleSource` when it arrives.
It deliberately does **not** touch `updatedAt`: the watchdog reads that to
decide whether a run has died and the editor shows fifteen minutes of silence
as stalled, so typing a new name must not make a dead run look alive.

It also carries `team1`/`team2` (home and away as printed on the score bug),
`scoreTeam1`/`scoreTeam2` at that moment, and `actionTeam` — the side the action
belongs to, named to match `team1` or `team2` so the two join.

Confidence is a 0-1 probability everywhere inside and 0-100 only in this shape,
scaled at the projection rather than stored twice and allowed to disagree.

**An unreadable score is `null`, never `0`.** Nil-nil is a real scoreline; not
being able to read the bug is not a scoreline at all, and 0 invents one that was
never displayed. The model is also told not to carry a score forward or to
derive one from goals it has counted — a calculated score is one nobody showed.

**Team names are consensused across the match, scores are not.** Who is playing
does not change, but reading it off a score bug once per segment does not give
one answer — the graphic is occluded, abbreviated differently, or absent for a
whole segment. `resolve_team_names` takes the most frequent non-empty reading and
`apply_team_names` gives it to every moment, so records cannot disagree about who
is playing; the result is also stored on the job by `record_teams`. Scores are
left per-moment on purpose: the scoreline changes through the match, so a
moment's own reading is the one that belongs beside its timestamp.

`participant` is **observed or empty**, never inferred. The model is told to
write a shirt number or `unknown` rather than guess a name, because an invented
name is worse than a blank field: an editor publishes it. Same rule as the
scoreboard.

The three new fields are described in the *system instruction*, not the segment
prompt — they describe the response shape, which is identical for every segment,
so they cache and cost the short prompt nothing. The segment prompt had 305
characters of headroom against `test_segment_prompt_stays_short`.

### One still per moment

Every moment gets a PNG, cut at its **peak** — not its in point, which is
deliberately a second or two of run-up and shows the play about to happen rather
than the play.

The frame is the **first I-frame at or after the peak** (`-ss` before `-i`, then
`-skip_frame nokey`). A keyframe is a whole picture the encoder already chose as
a reference, and it costs one decode instead of a GOP of them, which is the
difference that matters when this runs a couple of hundred times per match. At
or *after* is deliberate too: input `-ss` alone lands on the keyframe *before*
the timestamp, which can be a GOP earlier — several seconds of handball, and a
different play. A peak inside the file's last GOP has no keyframe after it, and
there the answer is an exact frame rather than no picture, so `keyframe_thumbnail`
returns False instead of raising and the caller falls back to `still_frame`.

`generate_thumbnails` is a root-agent tool as well as part of the analysis, for
the same reason `prepare_playback` is: a match analysed before the stills
existed needs minutes of range reads, not an hour of Gemini that would also
replace moments the editor has already worked from. It cuts only what is
missing, does not clear the prefix — the surviving files are the ones being
kept — and reports no progress, because setting a finished job's stage back to
`analysis` would make the strip say it is analysing.

They are cut ten at a time from the pipeline rather than all at once, the same
reasoning as the analysis windows: a single request for two hundred would run
for minutes, report nothing while it did, and name no particular moment when it
failed.

**Served by signed URL, not through the CDN.** The media bucket is private and
an `<img>` carries no Authorization header, so the picture needs a URL that is
its own credential. The CDN's signed cookie would have been cheaper — one cookie
for all of them — but it is minted by `/playback`, which only exists for a job
that has been packaged, and moments exist as soon as the analysis has run. A
thumbnail that appeared only after an encode would be missing for precisely the
job someone is waiting on. Signing is a round trip to IAM per URL, so the editor
asks for the page it is showing rather than for the match, and `POST
/api/jobs/{id}/thumbnails` signs them in parallel.

### Game details, and grounding

Each job also gets a **GameDetails** record: sport, home and away team,
competition, venue, final score, event outcome, sentiment, mood and a summary.
It lives in its own top-level `games` collection with its own vector index.

The separation is the point. "Find the Sweden Denmark match" and "find the
double save" are different questions over different units, and one index holding
both answers each with the other — a match summary and the moments inside it
share most of their vocabulary. `find_games`/`knn_search_games` answer the first,
`search_moments`/`knn_search_moments` the second, and the root agent is told
which is which because it is the easiest thing here to get wrong.

**Facts are assembled in code; only judgements are generated.** Teams, final
score, competition and venue are settled from what the segments observed —
consensus for the constant ones, the latest legible reading for the score. Only
sentiment, mood and the summary come from a model, over a digest of
observations. A model asked for "the game details" in one call returns a
coherent-sounding record whose teams never played each other, in a competition
that does not include them, at a venue in the wrong sport.

The final score is the **last legible scoreline**, never a count of detected
goals: a tally of what the analysis happened to find is not the scoreboard. No
legible score means no `final_score` and no `event_outcome` — the winner is
genuinely unknown, and `0-0` is a real result rather than a way of saying
"unreadable".

**The record is written while the match is still being analysed.** It used
to be written once, at the end of a run: a live event that runs for twelve
hours therefore showed "No games yet" on the desk beside four hundred
moments already found, and a run that died after saving its moments left the
match invisible for good. `record_game_facts` writes what
`game_summary.assemble` can settle from the observations alone — no model,
no search — and the live tick refreshes it after every tick that analysed a
chunk; the finish overwrites it with the complete record. `summarise_match`
is the root-agent tool for the other case: it rebuilds a record from the
moments already stored, without re-analysing anything.

**Google Search grounding runs once per match, not once per moment.** It takes
what was read off the screen and resolves the fixture: full team names, the
competition, the venue, the date, with sources. Grounded values are stored in
their own fields beside the observed ones and never overwrite them, so a caption
and a search result stay distinguishable — merging them would reintroduce the
invented-fixture failure in a form nobody could audit. Grounding is requested as
prose-plus-citations and parsed, rather than with a response schema, because
asking for a search tool and a strict schema in one call is fragile across model
versions.

### Progress

`progress` existed on the job, the UI read it, and **nothing ever wrote it** —
which is why the bar never moved. Stages now report through `_progress`, and
`STAGE_SPANS` gives each stage a share of the bar weighted by how long it
actually takes: analysis is 20-80 because it is an hour of Gemini calls against
minutes for everything else, and equal slices would park the bar mid-way for
most of a run. The web `STAGES` table mirrors it; change one and change both.
The last band, 80-100, belonged to the clip and caption stages and now belongs
to `finalize`, which is a read and a status write — the bar has to arrive at
100 somewhere, and giving that span to the analysis would claim time it does
not spend.

**The ingest panel is idle once the job exists, not once the turn ends.**
Every registration path handed the job to the agent and then awaited `ask()`,
which stays open for the whole analysis, with the panel's status left on
`analyzing` — so every ingest button was disabled for an hour, including
Schedule Live on the other tab, and an engine that never answered left them
disabled for good. The panel's work finishes when the match is registered;
the run's progress is the stage strip's business.

**Ingesting is not analysing.** "Ingest a new game" asks for the upload panel:
there is no video yet and nothing to run. The agent used to answer it by
starting `analysis_pipeline`, which spends an hour on the wrong match and holds
the turn open — so the panel the editor asked for never appeared either, because
a card chosen from the finished reply cannot arrive while the reply is still
running.

Cards are normally chosen from the finished reply, which is useless for
anything long: an analysis would have shown its progress widget an hour after
the progress was worth watching. `ask()` takes the cards to attach up front for
that reason, and the upload, retry, re-analyse and cancel paths all pass the
jobs card so the stage strip is on screen from the moment the run starts. The
Firestore listener re-renders on every job write, so it follows by itself
afterwards.

**An HLS download reports its own count.** It streams the whole playlist into
one object through one resumable upload, so the bucket shows nothing until it
is done and the bar sat at the start of ingest for minutes — which was
reported as "not progressing". The job logs one `[n/N] Streaming segment` line
per segment; `hls_download_status` reads the newest through Cloud Logging
(`execution_progress`, best effort, `roles/logging.viewer` on the media
service) and the ingest stage reports it across the download's half of the
band, with a note at each quarter. Ingest is 0-10 rather than 0-5 for the
same reason: a download plus a proxy encode is a quarter of an hour.

Cutting gets the first quarter of the analysis band. Thirteen windows take a
minute or two and the first Gemini call several more, so with the whole band
given to segment completions the bar sat at the stage's start for five minutes
with nothing to say — which reads as a dead run, and was reported as one. The
cut is a countable operation, so it reports as it goes.

Windows are cut one request at a time rather than all in one call: it is what
lets progress be reported between them, keeps any single request well short of
the client's timeout on a long match, and makes a failure name the window it
happened in instead of ending the batch.

Segment completion is what moves the bar during analysis, counted rather than
indexed because segments finish out of order.

Thumbnails take the last twelve percent of the same band, for the same reason
the cut takes the first quarter: three countable things happen inside one stage,
and a stage that reports nothing while it works reads as a dead run. The three
slices tile the band exactly, which is what `TestTheAnalysisBandIsShared`
checks — a gap makes the bar jump and an overlap makes it go backwards.

**Progress only goes forward.** Playback and analysis run concurrently and own
different bands — 5-20 and 20-80 — so whichever finishes last writes last, and
an encode ending after the analysis had reached 80% pulled the bar back to 20.
`update_job_status` takes the higher of the stored and the new value. Zero is
the exception, because that is how a re-run says it is starting over rather than
how a stage reports being early.

An empty `status` passed to `update_job_status` leaves the status alone.
Progress updates arrive once per segment and have no opinion about status, so
without that they would blank the field the whole UI reads.

### Cancelling and deleting

**Cancel is a flag, not a kill.** The run is a sequence of calls on Agent Runtime
with no handle to interrupt, so stages check `cancel_requested` between steps and
stop at the next boundary. **Inside the analysis that boundary is the window,
not the whole recording**: the check used to sit only before and after
`analyse_segments`, so a run cancelled one second in carried on through every
window of a three-hour match — and because the editor had meanwhile pressed
Analyse again, two full analyses ran in one engine worker. That worker was
killed with no traceback, twice, the second time after it had already found
117 moments. `analyse_segments` takes a `should_stop` and asks it before each
window. Moments already found are saved rather than discarded
— cancelling should not also destroy the hour that was already paid for.

**An execution is addressed by its full resource name.** A Cloud Run Job
sees itself as `CLOUD_RUN_EXECUTION`, the bare id, and the recorder reported
that over the full name the tick had stored; the API reads a bare id as a
project ("Permission denied on resource project sprtz-dev-live-capture-…"),
so the cancel on delete was refused and the status probe with it — and a
recorder whose state cannot be read is one the event can never see finish.
The recorder now qualifies its id against `LIVE_CAPTURE_JOB`, set on the job's
template, and the media tools qualify any bare id they are handed
(`runjobs.qualify`).

**Deleting a live event stops its recorder first.** The recorder is a Cloud
Run Job execution that knows the job only by id; with the document gone it
would record chunks for nothing until the event's end. `delete_job` cancels
the execution named in `live.capture` unless it is already finished, failed
or cancelled.

**Delete removes media first, then Firestore.** A failure after the media is gone
leaves a job pointing at a missing video, which is recoverable; the other order
leaves orphaned gigabytes nothing refers to. Firestore does not cascade, so
moments and events are deleted explicitly, and the `games` record is a
separate top-level document that an imagined cascade would miss entirely.

**Re-analysing clears first.** Without `clear_analysis` the previous run's
moments stay put and the new ones land beside them: the same play twice, with a
count that grows on every retry.

**And it clears the cancel flag.** `cancelRequested` is read by the stages
between steps, and it outlived the run it stopped: a job cancelled in the
morning answered every re-run after it with "Cancelled before the analysis
started" a second after the editor pressed Analyse again, and the row simply
went back to `cancelled` with nothing to say why. Starting again is the one
moment the flag certainly no longer applies.

### Search

Retrieval and ranking answer different questions. `knn_search_moments`
over-fetches 4x and has Gemini rerank for relevance. Reranker failure degrades
to vector order — it must never empty the result set.

**What is embedded is the whole ActionPlay**, not the description: class,
category, result, participant role, participant, acting team, then the prose.
The scoreline is deliberately left out — "24-23" as text matches nothing anyone
would type, and a bare number dilutes the words that do. "Double save by
the keeper" and "who scored from the wing" are answerable only if the outcome
and the role are in the vector, because they live in the structured fields
rather than inside the sentence.

### Media

The worker never holds a match locally: **Cloud Run's writable filesystem is
memory.** Sources stream over HTTPS with a bearer token — `-ss` becomes a range
seek, so a 60s cut reads 16 MB, not 3.2 GB.

**Playback packaging runs on Transcoder API, not here.** It reads the source
from GCS and writes the HLS package to GCS itself, so no video byte passes
through this container. That is not a preference: in-process packaging wrote as
many gigabytes of segments as the source was long, through a filesystem that is
really RAM, and no amount of draining made it survivable — the container was
killed at 2103 MiB with concurrency 4, then again at 2078 MiB with concurrency
1. Moving the job out removed the ceiling rather than raising it.

The preview is **one 480p rendition**, because an editor is judging whether a
moment is worth cutting, not watching the match. A ladder is what public
delivery needs, and Transcoder can produce one by adding mux streams — the
reason not to is encode minutes, no longer CPU we do not have.

Transcoder is asynchronous and the pipeline treats it that way: `transcode_hls`
starts a job and returns, `transcode_status` polls it, and `prepare_playback`
waits with a widening interval while reporting each state change. Blocking a
request until a match-length encode finished would only move the one-hour
ceiling onto an idle connection.

**A button that asks the agent to act on a match names it.** The player's
Prepare playback said "this match", leaving the agent to work out which one
from the conversation — and when it could not, it answered without calling
anything, so the player went on saying the match was not packaged and
nothing had been asked to package it. The button carries the job id and the
request quotes it; the root instruction says to use that id rather than ask
which match is meant.

**Packaging a finished match gives it its status back.** `prepare_playback` is
a stage of a run *and* a button pressed on a match that finished days ago, and
on the second path the job's status is not the run's to change: it set
`transcoding` at the start and never set anything else, so the LeMieux event
sat overnight reading as a run in progress — complete, playable, and stalled to
every reader, including the editor's own fifteen-minute rule. It now remembers
what the job was, and restores it on every way out: the encode finishing, the
encode failing, and Transcoder refusing to start one. Inside a pipeline run the
status is the run's, so nothing is restored there.

Packaging is independent of the analysis, so a job can hold moments and have
nothing to play. `prepare_playback` is a root-agent tool as well as a pipeline
stage for that reason — re-running a whole analysis to fix playback would be an
hour spent on the wrong thing — and the player offers it when `/playback`
returns 409. Each encode clears the job's HLS prefix first: Transcoder names
segments differently from the ffmpeg packager that preceded it, so nothing is
ever overwritten, and a playlist left by a half-finished run is one the CDN will
serve.

**A gap in a stream is filled, not refused.** A real recording can be missing
frames in the middle — a LeMieux upload has two minutes with no audio at
01:32:00 — and Transcoder rejects the whole encode for it: "Failed to
generate output for elementary stream audio-aac. Media frames are missing
starting at time 5520s and ending at time 5640s." Both jobs set
`fill_content_gaps`, because no preview and no analysis proxy at all is a
worse answer to two silent minutes than a filled gap. It needs a second
field with it or the API refuses the job at creation:
"frameRateConversionStrategy is DOWNSAMPLE, must be set to DROP_DUPLICATE
when fillContentGaps is enabled" — and then a third, refused in turn:
"frameRateConversionStrategy is DROP_DUPLICATE, optimization should be
DISABLED". Three fields, each discovered only by being rejected at creation
by the one before it.

Two grants decide whether an encode works, and both fail *minutes in* rather
than at job creation: the **Transcoder service agent** — not the media service
account — needs read on the uploads bucket and write on the HLS bucket.

### Playback

One HLS stream per job behind Cloud CDN, authorised by a **signed cookie**.

HLS playlists reference segments relatively, so a query-string signature is
dropped when the player resolves them — sign only the playlist and every one of
the thousands of segments 403s. A cookie is attached by the browser to all of
them.

That works because the **CDN is served from the app's own hostname**: the load
balancer routes `/jobs/*` to the HLS bucket, so the cookie is same-origin. On
separate `*.run.app` hostnames it would be impossible — `run.app` is on the
Public Suffix List, so no cookie can span two services.

**The `Set-Cookie` header is built by hand, and must stay that way.**
`response.set_cookie` puts the value through `http.cookies`, whose legal
character set excludes `=`; a value containing one is wrapped in double quotes.
A Cloud CDN cookie is four `=`-separated fields, so it was quoted every time,
and Cloud CDN does not strip the quotes. The browser stored a valid cookie, sent
it on every request, and got 403 for the playlist and each of the thousands of
segments — while DevTools showed the cookie present, sent and unexpired.

That is why the search went to signing keys, bucket IAM, the certificate and the
Public Suffix List: a hand-signed `curl` returned 200 and so cleared everything
except the one thing that was wrong, because `curl` sent the value unquoted.
`test_starlette_would_have_quoted_it` pins the standard library's behaviour
beside the assertion, so the check is not just "the helper does what it does".

A cookie set with `Domain=` and a host-only cookie of the same name are two
cookies; the browser sends both, and the older sorts first. A release that
changes the scoping therefore leaves a copy that can keep answering for the
correct one until it expires, so `/playback` expires the domain-scoped one.

Reviewing a moment is a seek to its in point with a stop at its out point — that
is what replaces a timeline. **The player shows three seconds either side**
(`web/src/player.js`, `playRange`, tested): the in point is a second or two
of run-up by design and the out point is the end of the play, and the editor
is judging the play in its context. The record keeps the moment's own times;
only the playback is wider, clamped at zero and at the match's length. Every
way into the player — the row's thumbnail and Details — goes through
`openDetails`, so the pad lives in one place.

### Sessions

An Identity Platform ID token lasts an hour and the SDK renews it only when
something asks, so a tab left open sends a stale one and gets a 401 that reads
as "logged out". Three things address that, and the second is the one that
matters: persistence is set explicitly, a timer refreshes at 45 minutes, and
**`api()` retries once on a 401 with a force-refreshed token**. The retry is
what turns an expiry into a pause nobody notices; once only, because a second
401 is a real authentication failure.

**A session that has really ended signs out.** A 401 that survives the forced
refresh, or a scheduled refresh Firebase refuses with an `auth/*` code, means
the credential itself is gone — revoked, disabled, or the password changed on
another device. The desk used to stay on screen under it with every card
failing and nothing saying why; now it signs out, which paints the sign-in card
and is the only way back. Once per expiry, however many requests fail together:
a page mid-analysis has several in flight and each of them signing out would be
several sign-outs and several re-renders. Only `auth/*` — a network blip is not
an expiry, and the SDK recovers from one on its own. Signing out also closes
the popups and clears the moments, the games and the open job: behind the
sign-in card it is another person's desk, and a details popup left open would
still be playing their match.

Restoring a stored session is asynchronous and `onAuthStateChanged` fires `null`
first, so the page paints nothing until auth resolves — otherwise every reload
flashes the sign-in card, which is itself indistinguishable from being logged
out.

### Languages

Six locales in `web/src/i18n.js`: en-GB, en-US, de, it, fr, es. **en-GB is the
base**, because the product's own voice is British — the agent says "analyse" —
so en-US is a small override of what actually differs rather than a full second
copy nobody edits.

A missing key falls back to en-GB, never to the key itself: a half-translated
locale should read as slightly English, not print `header.signOut` mid-sentence.
Static chrome carries `data-i18n`; the chat and its cards call `t()` as they
render, so switching language re-renders rather than reloads. Messages already
on screen keep their text — rewriting something the editor has already read
would be worse than leaving it in the previous language.

**There is no greeting, and there are no suggestion chips.** A new session
used to open with an agent card ("Upload a match and I will watch all of it…")
and three action buttons, with the same three prompts repeated as chips under
the composer. Both are gone at the editor's request: the opener is the only
thing a new session says, and it says it once.

**A session always exists.** With nothing in localStorage the app used to
paint an empty transcript and wait for someone to find the + in the rail — an
empty screen is not a starting point, and the opener only exists inside a
session. Signing in opens the most recent stored session, or starts one.

**Only the current turn is on screen.** The transcript used to grow for the
whole session, which on a competition day is a screenful of cards above the
one being read — and every one of them re-renders on every Firestore write
while an analysis runs. `currentTurn` (`web/src/transcript.js`, tested) takes
the last question and everything answering it. **It carries each message's own
index**, because every card and every click handler addresses `state.msgs[i]`:
a slice that renumbered them would wire each button to the wrong message.
Nothing is deleted — the session still stores the transcript, so switching away
and back is still switching back.

`STAGES` in `app.js` mirrors `STAGE_SPANS` in the agent's pipeline. Change one
and change both.

### Settings

Three options, and they are not the same kind of thing.

**App language** and **theme** are per-device display preferences in
localStorage. An empty locale means "follow the browser" and is stored as a real
choice, so a later change of browser language still takes effect rather than
being pinned to whatever it was the day the user first looked.

**Metadata language belongs to the job, not the browser.** It is copied onto the
job document at registration and read from there at analysis time. A match's
descriptions were generated in one language and stay in it, so a reader
switching their UI to German must not make stored English prose claim to be
German. Changing the setting affects matches analysed from then on, and the UI
says so.

The metadata list is deliberately shorter than the UI's: en-GB and en-US are one
"English" here, because asking a model for British rather than American prose
about a handball match is a distinction it cannot hold reliably, while a button
label plainly differs.

**Themes are a token overlay, not a stylesheet.** `metro-light` is the Modernist
palette exactly as shipped — an empty override — so adding a theme means listing
the tokens that differ rather than copying a file that then drifts.
`applyTheme` clears every known theme's tokens before applying, or switching
from a theme that sets a token to one that does not would leave the old value
behind.

`skyline-dark` is the second: near-black cool ground, a violet accent, IBM
Plex, 14px cards and 8px pills on hairlines rather than 2px rules.

**Structure is a theme's business as much as its palette is.** The radii come
from `ds/styles.css`, which ships them at 0; `app.css` names them everywhere it
draws a box, so setting them does something instead of being ignored. Rule
weight had no token at all, so `--rule` and `--rule-hair` are defined in
`app.css`'s own `:root` at today's 2px and 1px — the design system has no
concept of a border width, and putting them in the vendored file would be a
local edit that drifts from upstream. Modernist keeps its square corners and
its 2px rules by leaving all of them alone, which is what an empty override
means.

Two things deliberately do **not** follow the theme. The focus ring stays 2px:
it is drawn outside the box rather than being part of it, and a 1px focus ring
is worse than a 2px one. And a container that rounds also clips — a row's own
background or bottom rule is square, so without `overflow: hidden` it draws
over the corner it is meant to sit inside, which shows up as a notch on exactly
one row of a list.

**The ramps are read by step, not by lightness.** Each rung of the neutral and
accent scales has a fixed job in `app.css`, and a theme answers the job rather
than preserving the order: neutral 100 is a surface, 300-500 are rules, 600-800
are text, and 900 is the ground a picture sits on — so 900 is *dark* in a dark
theme while 800 is nearly white. Sorting them into a monotonic ramp would put a
white background behind every video. Same for the accent: 100-300 are hover
tints, 700-900 are the accent as text.

A theme also carries **the wordmark and the fonts its type tokens name**. The
logo carries its own colour rather than taking the accent, so a dark theme
cannot tint it — it swaps the file, and `web/check.mjs` fails if a theme names
one that is not in `assets/`. `--font-body: 'IBM Plex Sans'` with nothing
fetching IBM Plex is a token that quietly means `system-ui`: the theme looks
applied, reads wrong, and nothing says why, so `applyTheme` maintains one
`<link>` whose href it rewrites.

**Four literals in `app.css`, and they are one exception.** White on the video
ground and white on the accent: both of those surfaces are dark under every
theme, so their foreground does not follow the page. `--color-bg` was written
there and worked only while the page happened to be light — it inverted with
it, which is how a dark theme finds them. The letterbox matte is the same
argument at full strength.

**Failure is not the accent.** They are both red in the Modernist palette and
nothing depended on the difference until a theme moved the accent to violet, at
which point a failed job printed in it reads as a highlight. The `failed` tones
and the two error panels use the `accent-2` ramp, which is near-identical to
`accent` in the light theme and red in the dark one.

### The language rule in the prompt

Prose is translated; observations are not. `description`, `evidence` and
`segment_summary` are written in the chosen language, while team names,
captions, the score bug and shirt numbers stay exactly as they appear — those
are read, not written, and translating one invents a name nobody displayed.
`action_result` and `participant_role` stay English because they are matched on
as codes: a German "Tor" and an English "Goal" in one corpus is two categories
for one thing.

The rule lives in the system instruction, so the cache is now per sport *and*
per language — the correct granularity, since two jobs in different languages
are not running the same instruction.

### A session has a scope, and the opener asks for it

Every new session opens with the same question — *what would you like to work
on in this session?* — answered by the two things this desk does: **add a new
video** and **find moments**. The first's links open a panel; the second
carries four ways of saying *which* matches: all of the catalogue, the recent
event, one or more events, or across a sport and its disciplines. There was a
third option, generate clips, over the same four scopes; it went with clip
generation, because an opener offering a way into something that no longer runs
is worse than an opener with two options.

Adding a video carries a third link: **Check status**, which swaps the stage
strip in. It is local — the strip renders from the jobs listener, so asking the agent
what is running would be a round trip for something the client already holds,
and on a desk mid-analysis the answer would arrive after the bar had moved. All
of them swap a card into the message that offered them and carry one Back, which
is offered only when the opener put the card there: a strip the editor asked
for in words has no opener behind it, and a Back that restored a card nobody
had seen would be a trapdoor rather than a way out.

**The scope is the second half of that answer, not a separate interrogation.**
It used to be its own card, asking which games before anything had said why —
a step nobody could connect to what they had come to do. Now "Find moments ·
Across a sport" both names the session and narrows every card in it, and when
the scope settles `applyScope` asks the agent for what the session was opened
to get. The intent rides on the message (`msg.intent`) because a discipline
picker takes two or three renders to answer. Adding a video is the exception
and deliberately so: a video that is not on the desk yet has no scope to pick.

The answer is the session's `scope` (`web/src/scope.js`, tested). It does four
things: **names the session** in the sidebar
(`All games`, `Equestrian · Dressage`, the game's headline, `3 games`);
**narrows the cards** — the games list, the desk shortlist's `sport`/`job_ids`,
and the search panel's presets; is **sent to the agent on every message** as a
`[scope: …]` line (`context` on `POST /api/agent/messages`, composed by
`build_prompt`), which the root instruction reads and passes on to
`search_moments`/`list_top_moments`; and chooses which job is open. Job ids
travel with the names because a name is what a vector search is worst at.

Disciplines are whatever the desk has actually seen for that sport — a sport
with none (handball) skips the question. Registering a match in a session
scopes the session to that match. **Switching sessions restores the
transcript and the agent's own session id** as well as the scope (`msgs`,
`agentSessionId` on the session, last eighty turns, big search results
dropped), so switching back is switching back rather than starting again; a
session with no scope yet — one from before this existed — is asked on open.


Moments, events and the game record are all read through listeners
`selectJob` opens, so with no job selected every one of those cards is empty
however much has been analysed — and an empty card reads as "the analysis found
nothing" rather than "no match is open". Decoupling sessions from jobs removed
the line that used to select the newest job, and took that context with it.

`ensureJobContext` selects the most recent match when nothing else has, and
never overrides a session that names its own.

### Adding a video, and booking one

**Two panels, not two tabs of one.** A file is here now and a live event is a
reservation; they share a sport and the context links, and nothing else. Tabbing between them put a datetime picker one click from a drop
zone and made the panel read as a single form with half its fields hidden.
Which panel is on the message (`ingestKind`), so an ingest panel scrolled back
to is the panel that was opened, and Back returns it to the opener that
offered it.

**One source, chosen.** A file, a path into the bucket and a recorded playlist
are three answers to one question, and the panel used to ask it three times —
three inputs and three buttons where exactly one was ever going to be used.
`state.upload.src` picks one and the footer carries the single button that
acts on it, beside a line saying whether the form is answerable yet: a
disabled button on its own says no and not why.

**A match can be named.** Every registration route already took a `title` and
the web derived one from the filename or the URL. The panel now has a box for
it; empty still means "take it from the source", and it is cleared once the
match is registered so the next one does not inherit it.

### The cards

Six, each reachable from `attachCards` and each with an empty state:
`ingestCard`, `jobsCard` (the stage strip), `gameCard`, `gamesCard`,
`momentsCard`, `activityCard`. `showActivity` was never set by anything, so
that card could not appear at all until this was audited — a renderer nobody
routes to is dead code that looks alive. `reelCard` and `publishCard` went with
clip generation, and their routes in `cards.js` went with them: a question
about cutting or posting now falls through to the moments, which is the honest
answer while there is nothing that cuts.

**No card returns an empty string.** Rendering nothing is indistinguishable from
a card that failed to render, and the two have very different answers: "No
moments yet" is information, a blank space is a bug report. `emptyCard` says
which.

**A question that names a match is about that match.** "Show all moments of FAG
v TVB — DAIKIN HBL" is answered by selecting that job first, not by listing
whichever match happened to be open — a name is in the question precisely
because the editor means a different one. The browser resolves it against
`state.games`, which it already holds, and the agent resolves it with
`find_games`, which matches the title text before it searches by meaning.

**A name is what a vector search is worst at.** "FAG v TVB — DAIKIN HBL" is two
abbreviations and a sponsor: its embedding sits beside every other fixture in
the same league, so `knn_search_games` answers with a plausible neighbour rather
than the match asked for. `match_games_by_title` compares the text instead —
letters and digits only, since titles are composed with an em dash and typed
back with a hyphen — and answers exactly or not at all, which is the right
failure for a name. Longest title wins, so a fixture whose title is a prefix of
another's cannot answer for it, and a one-word name is not a match at all. It
reads the collection through a field mask: this scans every game, and the
768-float vectors are almost all of the bytes.

**Both lists filter on what was asked for.** It used to answer every
question with every moment, so "show all goals" and "show me the best moments"
produced the same 346 rows — which reads as a filter that ran and matched
everything rather than one that never existed. `src/moments.js` narrows the
list by kind and by half of the match.

The vocabulary is **the taxonomy's, not a list kept here**: the words matched
against are the moment's own class, category, result, participant role and
summary, so "penalties" finds `7-Metre Penalty` because that is what the sport
profile calls it. Every term must match, with any-term as a fallback — "wing
shot" is one kind of moment, and matching either word made it mean "shot",
which is how asking for wing shots returned jump shots; "penalties and
suspensions" is the case that needs the fallback, since no moment is both.

The games list works the same way and against the same kind of words: "show all
handball games" matches the sport the analysis recorded, "show all dressage
games" the discipline it read off the footage. Neither is a list kept in the UI,
so a sport added tomorrow is searchable the day it is added.

**Words that say *how* to show a list are not words about its contents.**
"details", "full", "summary" and the like are noise, and so are the names of the
axes — `sport`, `discipline`, `type` — because a record holds `handball`, not
the word "sport". Without that, "show all equestrian game details" searches for
"details", matches nothing, and shows everything.

**A word the vocabulary does not use shows everything and says so.** An empty
card would claim the analysis found nothing when the records are right there
and it is the word that is wrong. The head row distinguishes the three states:
narrowed (with a count and a Show all), nothing matched, or no filter at all.

**Halves are the match, not the clock.** The upload carries whatever was
recorded before throw-off — the first goal of one of these matches is at
29:47 — so a fixed 30:00 would put the entire first half into the second. The
split is the midpoint of the span the analysis actually found. "First" only
means a half when the word `half` follows it, or a taxonomy containing a
`First Wave` could never be searched.

**Moments are ordered by score or by time**, and the card says which. Score
answers "the best moments"; match order answers "in order" and is how the log
reads. The order lives on the message rather than in the stored ids, so the
toggle re-sorts an answer given ten turns ago, and changing it returns to page
one — page four of a ranked list is not page four of the same moments in match
order.

**A card's answer is not written out again in prose.** "Show me the best
moments" came back as a card of all 346 and a paragraph re-typing the first ten,
timecodes and all: the same answer twice, the truncated copy first. The message
text is hidden when its card carries the answer, and the agent is told not to
write it — *only* when the card has content, because an empty card says "no
moments yet", which is not the same as "I could not read them". The
missing-index failure arrived as prose beside an empty card, and hiding it
unconditionally would have made that unreadable.

**Which card answers a question lives in `src/cards.js`, and is tested.** It
was a chain of literal phrases, and literal phrases are brittle in the exact
place it matters: `all games` matched "show all games" and missed "show all
**the** games", which fell through to moments — the wrong data entirely, with
the prose that would have explained it hidden because a card claimed the
answer. It now reads the question as words: the plural noun, or the singular
with a scope word like all/every/list, chooses the games list, and a question
naming plays is about the plays whatever else it mentions ("show all moments of
the FAG v TVB **match**").

**Asking for details opens them in place.** "show all game details" is a
request for the records; leaving each behind its own Details button answers it
with an index. The expanded rows come from `GAME_DETAIL_ROWS`, the same list
the popup uses, because a second list is a second thing to keep current.

**Lists page rather than truncate.** The moments list used to stop at six, which
looks like an analysis that found six; showing all two hundred would bury the
conversation. Ten a page, with the page held on the message so scrolling back to
an earlier answer finds it where it was left. `pageOf` clamps out-of-range pages
rather than rendering blank, and takes a page size — three when a game's record
is open, because ten of those is a dozen rows each and a page nobody can see the
end of is not a page. Jobs and the activity feed page too: fifty jobs and
eighty events in one message bury the conversation as surely as two hundred
moments did.

### The two detail widgets

A moment row and a game row are the same shape on purpose: a headline worth
reading, the facts that qualify it underneath, and everything else in a shared
popup.

**Every moment tile is marked Beta**, as a ribbon across its top-left corner.
The classification is untuned — see Known gaps — and a result that looks like a
finished product invites an editor to publish it without checking. A ribbon
rather than a pill because a pill either covers the picture, where it hides a
play, or joins the text, where it pushes a line down on every tile of a page;
the corner is the one place a badge costs nothing. It carries the tile's own
radius, since a container that rounds also clips, and it is `aria-hidden`: a
screen reader saying "beta" before each of two hundred moments is noise.

**A moment tile has no buttons.** The frame carries a play button and the
whole frame opens the moment in the player, where its record, its ride and the
two ways out of it are; the foot shows the moment's type and its confidence (a
mono percentage and a bar). Details and Add used to sit at the foot of every
tile, and down a page of moments they were most of what the page said — and
neither downloading nor publishing is a decision anyone makes without watching
the thing first, which is why both live in the player instead.

**The moment plays inside its own popup and nowhere else**, in the wider left
column, autoplaying from the in point and stopping at the out point. The row's
thumbnail opens it. The row used to hold a player
slot of its own, which meant two elements carrying the same `data-slot` and the
wrong one winning on document order; it also meant playing a moment from
anywhere but its own row did nothing at all. **The popup is the whole
viewport** — no backdrop margin, no card floating in the middle, `100dvh` —
because a moment is judged by watching it and every pixel given back to the
page behind was a pixel off the picture. The card is a column: a head that
stays, and a split that scrolls inside its two halves rather than scrolling the
card. Both the split and each column carry `min-height: 0`, or a grid child
refuses to shrink below its content and the dialog quietly grows past the
viewport. In that layout the video takes the height it is given rather than an
aspect ratio (`object-fit: contain`, letterboxed); stacked under 760px it goes
back to 16:9 and the column scrolls as one. The video takes
the larger share of the split: the record beside it is a two-column table of
short values that reads fine narrow, while a 16:9 frame squeezed to half a
dialog is the thing someone opened the popup to look at. Opening the details of a play is the point
at which someone wants to see it, and the facts are what they are checking it
against — reading "double save" and watching the save are the same act. The two
share one dialog, so opening a game after a moment clears the player column and
collapses the grid; without that a game record would inherit a video it has
nothing to do with, still playing. `mountPlayer` prefers the popup's slot,
because the transcript's carries the same moment id and comes first in document
order. What differs is that a moment has
a thumbnail to play and a game does not, so they have separate grids — reusing
`.moment-row` for a game squeezes the headline into the 72px thumb column.

### Taking a moment off the desk

Two things can be done with a moment once it has been watched: **Download** it
as an MP4, or **Publish** it to a channel. Both sit in the popup's head, and
both cut *the range that is playing* — the padded moment by default, or
whatever Widen and the trim have made of it. What comes out is what was on
screen, which is the only version of this that needs no explaining.

**The publish preview is the player, not a picture of one.** Publish swaps the
record column for a panel — trim, title, description, visibility — while the
video beside it keeps playing; moving either end re-aims that same video. A
separate confirmation dialog with its own small preview would be a second
player to build, and a worse one than the one already running.

**The record bounds the trim.** Either end may move up to `_TRIM_SLACK_SEC`
(120s) from the moment's own in and out points, and no cut may exceed
`_MAX_CUT_SEC` (600s). Both are clamps, not refusals: a control held at its
limit should stop rather than start failing. **The figures are mirrored** in
`web/src/player.js` (`TRIM_SLACK_SEC`, `MAX_CUT_SEC`, `trim`, tested) and in
`api/app/routers/jobs.py`, and only the API's decide — the browser's copy
exists so a button never offers what the server would refuse. Change one and
change both. The API reads the moment itself (`catalog.get_moment`) rather than
trusting the times it was sent, so "this moment" cannot become an hour of the
match under a moment's name.

**A download is a signed URL, not a proxied file.** `POST
/api/jobs/{id}/moments/{mid}/download` cuts the range (`media.cut_moment`, into
`jobs/{job}/downloads/`) and signs a 24-hour GET for it. Streaming it through
the API would hold a request open for the whole transfer on the service whose
other job is an agent's SSE. The signed URL carries
`response_disposition: attachment` — without it the browser plays the MP4 in a
tab, because the object's own content type says video and a link that plays is
not a download — and a filename built from the match, the moment and its
timecode, since a hex moment id says nothing once the file is on a desktop.

### Publishing to YouTube

**A video belongs to a channel, and a channel belongs to a person.** There is
no service-account path to YouTube, so the desk holds an OAuth client and a
refresh token for one channel. The client is a deployment fact and arrives as
environment (`YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` from Secret Manager);
the refresh token is whatever channel someone connected and lives in Firestore
under `config/youtube`, because it changes without a deploy. `credentials()`
prefers what is stored over what was deployed: a deployment default is a
starting point, not a ceiling.

**Terraform enables the API and cannot create the client.** `youtube.googleapis.com`
is in `local.services`, the client secret gets a Secret Manager secret and
accessor bindings for the API and media service accounts, and the
`youtube_redirect_uri` output prints exactly what to register. The OAuth client
itself is made once by hand in the console — no Google API creates one, and the
IAP OAuth Admin APIs that used to were shut down in March 2026. This is the
same wall federated sign-in hits, and the panel says so rather than offering a
Connect button that can only fail.

**The refresh token never reaches the browser.** `GET /api/integrations/youtube`
reports whether each part is set, and what the channel is called; never a
value. The connect flow is `auth-url` → Google → `GET
/api/integrations/youtube/callback`, which is deliberately *not* behind
`current_user`: it is a top-level navigation from Google carrying no
Authorization header. What stands in for one is the code — single-use, minted
for this deployment's own client, worthless without the secret held here. The
consent URL asks `access_type=offline` **with** `prompt=consent`, because
Google returns a refresh token only on freshly granted consent: approving an
already-approved client hands back an access token that dies in an hour and
nothing that outlives it, and the callback says exactly that when it happens.
`config` is denied to every client by `firestore.rules`' catch-all, so the
token is readable only by the services holding admin credentials.

**The upload is resumable, and every call takes its transport as an argument.**
A moment is tens of megabytes and would fit in one multipart request; a
resumable session is what fails in a way that names the step. Injecting the
transport is what makes the two failures that matter testable at all — a
revoked refresh token (`invalid_grant`, which says nothing an editor can act
on, so the message says "reconnect the channel" instead) and an upload YouTube
starts and then rejects. The error detail is the API's own message and reason,
never the whole body: the body of a failed upload start carries the request
back, and that request has an access token in its headers.

**Instagram and TikTok are named and marked as not built.** They are what this
is for, and a missing option reads as an oversight where a marked one reads as
a plan.

Grounded values get their own rows in the game popup, labelled "(from search)",
and the sources sit at the bottom of it rather than on the card. They qualify
the grounded rows and are meaningless beside a row nobody is looking at.

### web/src/search.js and web/src/cards.js, and the only web tests there are

`app.js` cannot be loaded outside a browser: it imports the Firebase SDK from a
CDN, so `import()` in Node fails on the first line. That is why choosing what a
question asked for lives in its own module that imports **nothing** — pure
functions over plain objects, which `node --test web/tests` can reach. CI runs
it beside `check.mjs`.

It is `search.js` rather than `moments.js` because it answers for both lists:
the moments in a match and the games on the desk are filtered by the same
every-then-any term matching over each record's own words.

`cards.js` is there for the same reason and answers a different question:
*which* card, rather than what goes in it.

Both are worth having because the failure mode is silent and central. A word
added carelessly to `FILTER_NOISE` empties the card the app's own suggestion
chip opens; a phrase the routing does not recognise answers with the wrong
data and hides the prose that would have explained it. Neither says which word
did it.

### web/check.mjs

`node --check` only parses. Three classes of mistake parse perfectly and fail in
a browser, where the symptom is a blank screen or a dead button rather than
anything naming the cause: a `t()` key en-GB does not define, a `$('id')` the
document does not have, and a `<button>` whose data attribute is in no handler's
selector. All three have shipped. The script checks them exactly and runs in CI.

It also checks that **every function called exists**, which needed a real
parser. Three hand-rolled attempts could not tell a call from the word "the" in
a comment, a destructured parameter from an undeclared name, or where a template
literal ends, and each produced more noise than signal. `acorn` does it exactly.
If acorn is not installed the check says it is skipping rather than passing
quietly; CI installs it.

That check exists because this failure shipped three times — `playerMarkup`,
`renderSessions` and `openSession` were each removed by an edit that sliced a
region to the next function and took a neighbour with it. All three blank the
whole screen, because the throw happens inside `render()`, and `node --check`
passes on every one of them.

### Jobs are shared, not owned

Any signed-in user sees and can delete any job. That is a product decision:
accounts are provisioned by hand in Identity Platform, everyone with one is on
the same desk, and a match a colleague uploaded is a match this desk is working
on.

**The boundary moved, it did not go.** Every route is still behind
`current_user`, the Firestore rules still require `request.auth`, and the
services are still reachable only through the load balancer. What changed is
that "may read this" is now "is signed in" rather than "uploaded it".

`ownerUid` is still written on every job, moment and game — as provenance
rather than as a gate. It says who uploaded a match, which is worth knowing
precisely because anyone can now act on it.

`list_jobs`, `knn_search_moments` and `knn_search_games` keep an `owner_uid`
parameter that is accepted and ignored, so an old caller is not silently
answered with a filtered list it did not ask for, and the parameter can be given
meaning again without a signature change.

**The vector indexes needed new ones.** A vector index whose first field is an
equality only serves queries carrying that equality, so dropping the owner
filter needed `moments_knn_all` and `games_knn_all` — the embedding alone. The
owner-prefixed indexes are left in place: they cost nothing idle, and deleting a
vector index is the one operation this file warns about.

Upload objects keep `uploads/<uid>/<job_id>/` paths, so registering an orphan
somebody else left needs the uid the path was written with — `uploaded_by` on
the create-job request, constrained to one path segment so nothing traverses out
of the uploads prefix, and still checked against the object actually existing.

### Sessions and jobs are separate

A session is a conversation; a job is a match. The session notes which job it is
currently about so reopening it comes back to the same place, but that is a
bookmark rather than ownership: several sessions may be about one match, a
session may be about none, and **deleting a session never deletes a job**.

A match is hours of analysis over a multi-gigabyte upload and a session is a few
lines of localStorage. Tying the two together meant tidying the sidebar
destroyed work — not a trade anyone would make deliberately, and far too easy to
make by accident. Matches are deleted from the job card, where the confirmation
names what actually goes.

Nothing creates a session per job. Jobs are reached through the agent and the
job cards, and exist perfectly well without anyone having talked about them.

### The media service needs delete on uploads, not just read

It held `objectViewer` on the uploads bucket and `objectAdmin` on the HLS one,
so deleting a job always ended half done: the package went, the source video
did not, and the job stayed in Firestore because the media step reported the
failure. That is the right order — a job pointing at a missing video is
recoverable, orphaned gigabytes are not — but it meant delete never completed.

`roles/storage.objectUser` rather than `objectAdmin`: this service has no
business changing object ACLs on the bucket users upload into, and the narrower
role is otherwise the same.

### A playback record is not a package

`prepare_playback` used to return early whenever the job carried an `hlsUrl`,
which made a half-deleted job unrecoverable: the record pointed at objects a
failed delete had already removed, so the editor was told playback was ready,
the CDN answered 403, and asking for it again did nothing. It now confirms the
master playlist is in the bucket with `playback_ready` before trusting the
record, and re-encodes when it is not.

The path that check uses has to match the one Transcoder writes to and the one
the CDN URL is built from. A test asserts all three agree, because if they ever
drift the check fails silently for every job and every request re-encodes a
match.

### Deleting media

**Already-gone counts as deleted.** Listing a couple of thousand HLS segments
and then deleting them is not atomic, so an object can disappear between the
two — a second delete of the same job, or a re-encode clearing the prefix. A 404
means the prefix is emptier than it was, not that the operation failed. Treating
one as an error aborted a real job deletion at segment 1400 of about 2000 and
left the job in Firestore pointing at a half-deleted package.

A 503 is not a 404: `delete_prefix` counts real failures separately and
`delete_job_media` refuses to report success while any remain, because dropping
the job document while objects survive leaves gigabytes nothing points at.

Deletes run through a thread pool. Two thousand round trips in series is slow
enough to matter on its own, and the time it takes is also the window in which
something else can remove an object from under the listing.

`delete_object` catches `NotFound` from the delete rather than calling `exists()`
first — a check followed by a delete is two calls with a gap in the middle,
which is the race being guarded against rather than a way to avoid it.

### Static caching

CSS and JS are served `no-cache`, meaning **revalidate**, not "do not store".
Nothing here adds a content hash to a filename, so blind caching means a browser
holding the last release's `app.css` against this release's `index.html`. That
is not hypothetical: the header logo rule shipped, the server served it, and the
page kept the old sizing for an hour because the browser never asked. nginx
answers from the ETag with a 304, so the cost is one conditional request per
file per load. `/assets/` keeps a long cache because those files are stable
within a release — replacing one means renaming it.

**`add_header` does not merge across levels.** A location that declares any
`add_header` of its own inherits none from the server block, so every location
that sets a cache header repeats the three security headers. It reads as
duplication and is the only way to keep them; without it a caching change
silently drops `X-Frame-Options` off the document.

### Brand

**Sportscut was rebranded to Arenos**, on the Arenos design system (brand
standards v1.0 draft, September 2026), ported via the `arenos-design` skill at
`.claude/skills/arenos-design/`. That skill is the source of truth for the
tokens now in `web/src/ds/styles.css` — retune the look by reading it, not by
guessing at a hex.

Unlike the old Modernist brand pink, `--color-accent` (Arenos Amber
`#D4881A`) **is** the logo's colour, not an unrelated one living beside it —
the lockup artwork's amber core is the same value. What the two still don't
share is contrast: amber as a fill takes ink text
(`--color-on-accent: #111111`, both themes), never white — white on `#D4881A`
is roughly 1.9:1 and fails outright. The wordmark and lockups are real vendored
assets under `web/src/assets/` (SVG, filled paths, no live type), served from
the app's own origin, and are picked per theme by `THEMES[...].logo` /
`.logoSignin` in `web/src/settings.js` — the header runs tight on space and
uses the no-tagline cut; the sign-in screen has room for the full lockup.

### UI

Chat-first, originally implementing `SPRTZ AI Chat.dc.html` on the Modernist
design system, now re-skinned onto **Arenos** at `web/src/ds/` — same
structure, new tokens. Geist, amber on near-black by default (dark is the
product's own surface, not a fallback; light is a full peer), small derived
radii (8px cards, 5px inputs/chips, 3px controls) and a single 1px hairline
rather than Modernist's 2px structural rule. Take every colour, space, font and
radius from the tokens; `app.css` introduces none of its own beyond the two
video-ground literals (the thumbnail clock's white and the letterbox matte's
true black — both commented where they live, since a picture's ground does not
follow the page).

**Two themes, not a light/dark toggle bolted on after the fact.** `arenos-dark`
is `ds/styles.css`'s own `:root` — an empty overlay, because dark is the
default rather than something layered on top of light. `arenos-light` is a
full token overlay in `settings.js`, following the same "read the ramp by
step, not by lightness" rule every theme here has: neutral 900 stays near the
video ground in *both* themes, so a thumbnail's backing never inherits the
page's own background.

**Anything a model produced is set in Geist Mono**, never the sans — the
brand's own example is `01:24 · ATH-0842 · conf 0.941 · v2.3.0`, and in this
app that means the moment thumbnail's timecode, a cut's in, out and length in
the publish panel, and page counts. Anything a person wrote (labels, descriptions,
summaries) stays in Geist.

Where the backend genuinely cannot do what the design prototype mocks (post to
a platform, report view counts), the UI **says so** rather than showing a
plausible number. Don't "finish" those by inventing data.

---

## Traps that have already cost time

**Cloud Run cold start.** Python here takes ~100s to bind on Cloud Run against
6s locally (`grpc` 3s → `firestore` 45s → `genai` 72s). Startup probes allow
~5 minutes and every service sets `startup_cpu_boost`; heavy Google imports are
**lazy**, inside the accessors that use them. A tight probe window kills the
container mid-import and its buffered stdout dies with it, so the logs show
*nothing at all* — which reads as a container that never ran. If you see a
startup probe failing with no application output, suspect time, not the image.

> Beware false patterns here. A deployment matrix once appeared to show a
> memory-to-CPU ratio causing failures; it was coincidence, and the belief got
> committed into comments before a plain control deploy disproved it. When a
> platform behaviour looks arbitrary, run the boring control first.

**Uploads are validated against the bytes, and ffmpeg is run as if the input is
hostile.** A filename and a Content-Type are both chosen by the uploader, so the
only evidence a file is a video is that a decoder read it as one — `validate_media`
ffprobes it before analysis and rejects on stream, duration, dimension, pixel-count
and codec grounds, reporting every reason at once.

The protocol allowlist matters more than the format checks: ffmpeg can be steered
by a file's *contents* into opening other URLs, and on GCP that is an SSRF at
`http://169.254.169.254` handing out the worker's access token. `file,https,tls,
crypto,tcp` blocks it — plain `http` is absent. `tcp` **must** be listed or every
GCS read fails, and `-nostdin` must **not** be passed to ffprobe, which has no
such option and swallows the next argument.

**MCP servers use `INGRESS_TRAFFIC_ALL`, not internal-only.** Cloud Run services
calling each other without a VPC connector egress over the public internet, so
internal-only 404s the very callers they exist for. They stay private through
IAM: only the agent and API service accounts hold `run.invoker`, and every call
carries an OIDC token.

**An `api()` path missing its `/api` prefix reaches a bucket, not a 404.** The
load balancer routes `/api/*` to the API and `/jobs/*` to the HLS bucket, so
`api('/jobs/<id>/thumbnails')` is answered by private object storage with a
403 — an authorisation error from a service the caller never meant to address,
about an object that does not exist. The moment thumbnails shipped that way and
read as a signing or IAM fault. `web/check.mjs` now fails on any `api()` call
whose path is not under `/api/`.

**Bucket CORS must list the app's own origin.** The browser PUTs the upload
straight to GCS, so a valid signed URL still fails its preflight if the origin
is not allowed — the error names CORS, not signing. `local.browser_origins`
derives it from the load balancer host and is applied to the uploads, media and
HLS buckets together.

**Signed URLs on Cloud Run need an access token, not just a signer email.**
Metadata credentials carry a token and no private key, so the storage library
cannot sign locally — it raises "you need a private key to sign credentials".
`generate_signed_url` needs *both* `service_account_email` and `access_token` to
route signing through IAM's signBlob, and the service account needs
`roles/iam.serviceAccountTokenCreator` **on itself** (`api_self_sign` in iam.tf).

**Packaging in-process could not be made to survive, which is why it is gone.**
Worth knowing before anyone proposes bringing it back: a copy-remux writes
segments as fast as it reads the source, and the backlog waiting to upload sits
in a filesystem that is really RAM. Serial uploads are latency-bound — a round
trip per 500 KB segment — so on a 3.4 GB match the drain managed 183 MiB while
ffmpeg produced nearly 2 GB. Dropping concurrency from 4 to 1 did not fix it;
neither did parallelising the drain. Transcoder API did, by moving the bytes out
of the container entirely.

Concurrency stays 1 regardless, for the ffmpeg work that remains — probes, cuts,
reframes. Parallelism belongs in `max_instance_count`, where each job gets a
whole container.

Both memory numbers here are measured, not inferred — `Memory limit of 2048 MiB
exceeded with 2103 MiB used`, then 2078 MiB with concurrency already down to 1,
both from the *platform* log rather than the application's. That second reading
is what proved concurrency alone was not the fix. The variable's old "keep at or
below 1GiB per CPU" description was the disproved ratio theory and is gone; it
would have pushed the limit the wrong way.

**A poll that cannot reach the media service is not news about the work.**
The playback wait met a retired Cloud Run instance one poll in — a deploy had
replaced the media service twenty minutes earlier — took the exception as a
dead encode, and marked the job failed with "ConnectError: " while Transcoder
carried on for another half hour. Every wait on a job or an encode polls
through `_poll_tool`, which reports the service as `unreachable` rather than
raising, and the loop keeps waiting up to `_MAX_UNREACHABLE_POLLS` in a row.
Underneath, `call_tool` retries a connection that was never made (four
attempts, 5/15/30 s) — a request that never reached the server cannot have
done anything, so that retry is safe for every tool; a request that was sent
is never retried.

**A stream of only keep-alives is a dead server, not a decoding problem.** When
a container is killed mid-response the SSE body arrives as `: ping` comments and
nothing else. `_decode` used to report "Could not decode MCP response" and quote
the pings, which reads as a protocol bug and sends you to the wrong file; it now
says the stream closed without a result and points at the platform log.

**A stage that dies must record it.** Cloud Run kills a container mid-response
and progress reporting dies with it, so the job keeps its status and reads as
working for ever. Every pipeline stage is wrapped in `@stage(...)`, which marks
the job `failed` with the reason and returns rather than raises — an exception
escaping ends the run before the later stages can report anything.

**The MCP toolsets need `header_provider`, not headers.** The two servers are
private Cloud Run services, so every call needs an OIDC token for that service's
URL. `call_tool` mints one per call and always worked; the *toolsets* were built
with a static empty header dict, so all six model-facing tools were refused
while the pipeline ran fine — the two paths fail independently and only one was
covered.

The failure is silent from the agent's side. Cloud Run rejects a request with no
Authorization header before it reaches the container, and the body never gets
back, so ADK reports "Failed to create MCP session" and the model behaves like
its tools do not exist. The evidence is in the *server's* log — "Empty
Authorization header value" — on a service whose own application log shows
nothing.

Toolsets are built at import time, so a token cannot be baked in: it would
expire an hour into the deployment. `header_provider` is a synchronous callable
ADK invokes before each listing and each call; tokens are cached per audience
for 45 minutes so it is not a metadata round trip per tool call.

**A stored fact must not be a parameter a model fills in.** `analyze_match`
took the sport as a required argument. With one sport registered a model could
guess it safely; the day a second one existed it correctly stopped guessing and
asked — *"What sport is being played in the video?"* — inside a `SequentialAgent`
with nobody to answer. The stage made no tool call, and every stage after it
ran successfully on zero moments and marked the job complete. The
sport is read off the job now, the stage instructions say plainly that a
question is the end of the run rather than a pause, and `inspect_source` returns
the sport so the stages after it inherit the fact instead of asking for it.

**A stage that returns an error does not stop the next stage being asked.**
The pipeline is a sequence of agents. When the download failed, the analysis
ran on nothing, and `finalize_job` wrote "the analysis produced no moments"
over the real reason — the editor was told to re-run a job whose link had
expired. The stages after ingest are `@stage(..., skip_if_failed=True)`: they
read the job first and return `skipped` when it is already `failed`,
`cancelled` or `cancelling`, leaving the earlier stage's reason in place.
Cancelling is the sharper case: the stages after the cancelled analysis ran
on and marked the job **failed** with "the analysis produced no moments",
and the one thing cancelling promises not to do is report the run as broken. Ingest is exempt because a re-run starts
there on a job that is failed by definition, and so is playback, which an
editor asks for on its own.

**A run that analysed nothing is not a finished run.** `finalize_job` — the
`finalize` stage, and the last thing `analysis_pipeline` does — reads the
moment count and nothing else: moments make the job `ready`, none make it
`failed` with a reason. Reporting an empty run as ready reads as a quiet match,
and it is far more often an analysis that never produced anything. There was a
`makeClips` flag here, fixed on the job at registration, saying whether a match
was cut as well as read; with nothing cutting it decided nothing, so it is gone
from the registration routes, the panel and the job document. Jobs that still
carry one are not read for it.

**Nothing on the engine retries a run that dies.** A deploy replaces the Agent
Runtime engine and kills whatever it was doing. Progress reporting dies with it,
so the job keeps the status it had and reads as running for ever. The editor
shows a running job with no movement for 15 minutes as `stalled` and offers
Retry; the agent is told that a stale `updated_at` under a running status means
a dead run, because otherwise it correctly refuses to start "a second run". The
watchdog tick (see *HLS sources, live events, and the watchdog*) now restarts
such a run on its own, twice at most, through `recover_job`.

Merging during an analysis therefore costs that analysis some minutes rather
than the whole run: the upload is still in the bucket and the watchdog re-runs
it. Still do not deploy over a live *event*: the recorder is a job and survives,
but the tick's analysis of the chunk in flight does not.

**The agent scopes `list_jobs` from the session, not from a parameter.**
Editors never see a job id, so the agent has to be able to list their jobs to
answer "what's still processing?" — but an `owner_uid` argument would be an
argument the model fills in, and a uid the model can supply is a uid it can
guess. `pipeline.list_jobs` takes an ADK `ToolContext` and reads
`tool_context.user_id`, which comes from the session the API opened with the
verified Identity Platform uid, and is never shown to the model.

Filtering by status is done in Python after the read, because a Firestore
filter on it would need a composite index per status on top of
`jobs_by_owner_recent`. That read over-fetches: a page of finished jobs would
otherwise hide the running ones underneath it and the agent would answer
"nothing is processing".

**A source can be registered instead of uploaded.** The browser upload is one
non-resumable PUT, and the real equestrian recordings are eight hours and twelve
gigabytes — a dropped connection starts the whole thing again. `gcloud storage
cp` is resumable and parallel, so `POST /api/jobs/from-source` takes a `gs://`
URI for a video already in the bucket and registers a job against it.

**The bucket is not the caller's to choose.** Reading an object named in a
request, with this service's credentials, is a confused deputy unless the set of
readable buckets is fixed by the deployment: it is the uploads bucket plus
whatever `EXTRA_SOURCE_BUCKETS` names, and nothing else. That is the boundary a
browser upload already has; what changes is who does the copying. Size, name and
content type come from the object rather than the request, because the object is
the thing that exists — and whether it is a video at all is still settled by
ffprobe in the ingest stage.

**An upload with no job document is recoverable, not lost.** The browser mints
a job id, PUTs to GCS, then registers the job in a second call — so a failure
between the two strands a match-length file in the bucket with nothing pointing
at it. `GET /api/jobs/pending-uploads` lists objects under the caller's own
`uploads/<uid>/` prefix that have no job, and the editor's "Use last night's
upload" button registers one rather than uploading it again. Registration also
checks the object exists now, so a job cannot be created pointing at nothing.

**The agent's tool list is bound at import time.** `sprtz_agents.agent` builds
`tools=` when the module loads, so whatever is unset *while deploy.py imports it*
is missing from the packaged agent permanently — setting `MCP_CATALOG_URL` on the
deployed engine cannot add it back. The deploy step exports those URLs into its
own process for that reason. Six tools without them, eight with; if a deploy log
says "packaging without the ... MCP toolset(s)", the agent shipped crippled.

**Sign-in is email/password, because that is what the tenant has enabled.**
Google sign-in needs a `defaultSupportedIdpConfig`, which needs an OAuth 2.0 web
client created by hand — the IAP OAuth Admin APIs that used to supply one shut
down in March 2026. The editor only renders a federated button when
`/api/config` reports a provider, so it never offers a method that can only
fail with `auth/operation-not-allowed`. Set `google_oauth_client_id` to enable it.

**There is no self-service sign-up.** Accounts are provisioned in Identity
Platform (console, or `gcloud identity-platform tenants`/Admin SDK); the login
page only signs existing users in. An unknown email is told to ask an
administrator rather than being offered an account, so a public URL does not
hand anyone a tenant login.

**Every hostname the app is served from must be in Identity Platform's
`authorizedDomains`,** or the browser SDK fails sign-in with
`auth/unauthorized-domain`. Terraform includes the load balancer host; add any
new one there rather than only in the console, since an apply rewrites the list.

**Serverless NEG backends reject `timeout_sec`.** A backend service fronting
Cloud Run cannot set a request deadline; the Cloud Run service's own `timeout`
is what applies. The API's is 3600s because the agent's SSE stream stays open
for a whole analysis.

**Cloud Build.**
- The `gcloud` builder image has **no `jq`** — use `python3`, which it does have.
  A `|| true` on the apt-get that installed it turned a missing binary into a
  127 that failed the build *after* a completely successful deploy.
- `waitFor` only resolves against steps declared **earlier** in the list.
- Escape shell variables as `$$NAME`. Only `PROJECT_ID`, `SHORT_SHA` and
  friends are real substitutions; anything you assign in a step is not.
- `E2_HIGHCPU_8` had no capacity in us-south1 — accepted, then `PENDING`
  forever with no error. The pipeline sets no `machineType`.
- A trigger service account with **no roles** also sits in `PENDING` silently.
- **Two builds at once deploy over each other.** Merging twice inside the
  pipeline's ~12-minute runtime runs both, and three things are shared while
  only one is protected. Terraform's GCS backend takes a state lock, so the
  applies cannot interleave. Nothing locks the Cloud Run images or the Agent
  Runtime engine.

  The engine fails loudly: `update()` is refused with `FAILED_PRECONDITION ...
  Current state: UPDATING`, and that build dies having deployed nothing wrong.
  The images fail quietly, which is the one that matters. Terraform applies
  `image_tag=$SHORT_SHA`, and a state lock guarantees mutual exclusion, not
  order — whichever build applies *last* decides what is served, so an older
  build finishing second rolls production back to the older commit while `main`
  says otherwise, with a green build and nothing to say so. It missed by about
  four minutes the first time it happened.

  The `serialise` step waits for older `WORKING` builds on the same trigger
  before `terraform-apply` runs, which fixes both and in the right direction:
  the older one applies first, so the newest commit is always what is left
  serving. Only `WORKING` blocks — a build `PENDING` approval is not running and
  waiting on one would hang every later build behind a decision nobody made. It
  runs while the images build, so it normally costs no wall-clock, and it fails
  open: a gate that cannot read the API lets the build through rather than
  blocking the pipeline on a question it cannot answer.

  **Cancelling a build during its `terraform-apply` step leaves the state
  lock behind.** The GCS backend's `default.tflock` belongs to a build that no
  longer exists, and every later apply fails with "Error acquiring the state
  lock" naming it. Nothing in the pipeline clears it: with no apply running,
  delete `gs://<state bucket>/terraform/<env>/default.tflock` (or
  `terraform force-unlock <id>` from a directory initialised with the build's
  `-backend-config`), then re-run the trigger. Cancel a build before its apply
  or let it finish.

  **A sensitive variable poisons every `for_each` and `count` derived from
  it**, and the error names the type instead. `for_each = var.secret != "" ?
  [1] : []` on a `sensitive = true` variable fails the apply with "Cannot use a
  list of number value in for_each. An iterable collection is required" — so
  the obvious fix is to make it a set of strings, which fails with "Cannot use
  a set of string value in for_each". Nothing is wrong with the type: a value
  derived from a sensitive one carries the mark, and a marked value cannot be
  iterated at all. `nonsensitive(var.secret != "")` is the fix — whether a
  secret exists is not itself a secret — kept in one local so `count` and every
  `dynamic` read the same unmarked boolean.

  Two things hide this. `terraform validate` passes on all of it, and **the
  builder is `hashicorp/terraform:1.9` while a developer machine is on
  1.16**, which tolerates the mark and plans it happily. To reproduce a build
  failure locally, run the builder's version:
  `docker run --rm -v "$PWD:/w" -w /w hashicorp/terraform:1.9 plan`. The google
  provider configures offline against a fake service-account JSON — a generated
  RSA key is enough — so a plan needs no credentials as long as nothing
  refreshes.

  `gcloud builds list` writes "filter keys were not present in any resource" to
  **stderr** when nothing matches. Merged into stdout that reads as a build id
  and the wait never ends, so every call in that step discards stderr.

**CI permissions.** `roles/editor` is not enough. Also needs
`resourcemanager.projectIamAdmin`, `iap.admin`, `firebaserules.admin`,
`iam.serviceAccountAdmin`, `secretmanager.admin`, `datastore.owner`,
`run.admin` (Editor cannot `run.services.setIamPolicy`).

**A managed certificate cannot be replaced under its own name.** The domain
list is immutable, so changing `app_domain` or `cdn_domain` forces a
replacement — and with a fixed `name`, `create_before_destroy` asks Google to
create a second certificate under a name the first one still holds. That is
`Error 409: ... already exists`, and the apply dies having changed nothing.
Both certificate names carry `substr(sha256(<domain>), 0, 8)` so a domain
change is a genuinely new resource: created first, attached to the proxy, then
the old one destroyed.

The failure is at least safe — nothing is deleted, so the old certificate stays
attached and the site keeps serving on the old hostname. What it is not is
obvious: the error names the certificate, not the domain change that caused it.

**A domain cutover is not instant, and there is a window.** Terraform creates
the new certificate, points the proxy at it and destroys the old one, but a
managed certificate is `PROVISIONING` for roughly 10-15 minutes after that —
and a proxy holding only an unprovisioned certificate fails TLS. Both the old
and the new hostname are down for that window. Watch
`gcloud compute ssl-certificates list` rather than guessing.

**Firestore vector indexes replace themselves forever.** Firestore appends
`__name__` to the index it creates, so the remote object never matches the
declared fields. The provider reads that as a change, forces replacement, and
the replacement's create fails 409 because the equivalent index already
exists — so every later apply retries the same doomed replace. Both KNN
indexes carry `ignore_changes = [fields]`. Change a vector definition by
deleting the index and re-applying, never by editing in place.

**Agent Runtime reserves `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`.**
Supplying either fails outright — it injects them itself. `deploy.py` refuses to
run if a reserved name reappears in `--env`.

**The Agent Runtime engine is not a Terraform resource, on purpose.** Terraform's
`google_vertex_ai_reasoning_engine` creates it with `spec.deployment_source`,
while the SDK's `update()` sends `spec.package_spec`, and the API refuses to move
an engine from one to the other. So `agents/deployment/deploy.py` owns the whole
lifecycle: Terraform publishes `agent_display_name`, the script creates-or-updates
by that name, and the API resolves it the same way. Everything the engine needs at
runtime is passed from Terraform outputs through the `deploy-agent` step — if you
add an env var the agent reads, add it there too or it will be silently absent.

**Bootstrap ordering.** The state bucket and Artifact Registry repo cannot be
owned by the Terraform that needs them — `deploy/scripts/bootstrap.sh` creates
both idempotently, and the first apply adopts the registry via guarded import.

**Firestore location is immutable** once the database exists. `preflight.sh`
checks it, but a check that *cannot run* is advisory, not fatal — it once
failed a build claiming Firestore didn't serve a region it serves perfectly
well. A guard that fails a correct config is worse than no guard.

**A green build is not a usable deployment.** CI applies Terraform *defaults*, not
`envs/*.tfvars`. Two settings decide whether the thing actually works, and both
default to empty:

| Trigger substitution | Empty means |
|---|---|
| `_IAP_MEMBERS` | IAP is on but nobody is authorised — no one can reach the editor |
| `_CDN_DOMAIN` | CDN is HTTP-only, so the HTTPS editor blocks HLS playback as mixed content |
| `_APP_DOMAIN` | The editor and API are served from `<lb-ip>.nip.io` rather than a real hostname |

Set them on the trigger (`_IAP_MEMBERS` is comma-separated, e.g.
`user:you@example.com,domain:example.com`). Point the domain's A record at the
`cdn_ip` or `app_ip` output before the matching managed certificate can
provision — **DNS-only, not proxied.** Google's validation reaches the load
balancer directly; a proxying host (Cloudflare's orange cloud, for one) answers
for the domain instead of forwarding to it, and the certificate sits in
`FAILED_NOT_VISIBLE` forever with nothing in this repo's logs to explain why —
the failure is entirely on Google's side, checked against a domain that
resolves to the wrong place from its perspective.

**IAP does not work in this project, and authentication is enforced by the
application instead.** IAP's authorization step ran with an *empty principal*
(`authenticationInfo: {}` in the audit log) on both the Cloud Run built-in
integration and a load-balancer backend service — so no IAM binding could match
and even `allAuthenticatedUsers` was refused, while Policy Troubleshooter
reported `ACCESS: GRANTED` throughout. The project's legacy OAuth brand has zero
clients and the API that could create one shut down in March 2026.

The SPA signs in with Identity Platform and `api/app/core/auth.py` verifies that
token. That also fixes an identity mismatch IAP would have caused: a Firebase
uid is what Firestore rules compare against, whereas an IAP subject is not, so
jobs written under an IAP identity would have been invisible to the browser's
own listeners.

Reach is controlled by **ingress**: both public services accept traffic only
from the load balancer, so their `allUsers` invoker bindings cannot be used to
call them directly, and the `run.app` URLs are dead ends.

**One load balancer, one hostname.** `/` serves the editor and `/api/*` the API,
so they are same-origin — no CORS. With no custom domain, `<lb-ip>.nip.io`
resolves back to the balancer, which is enough for a Google-managed certificate.

**No `google_iap_brand`.** The IAP OAuth Admin APIs were shut down in March
2026. Cloud Run's `iap_enabled` uses a Google-managed client.

**Region split.** `region` and `vertex_region` are separate because Vertex
serves fewer regions than Cloud Run. Both default to `us-central1`.

---

## Adding a sport

Copy `agents/sprtz_agents/sports/handball.py`, define the moment types and the
context a model needs to read the picture, register the profile, and import it
in `sports/__init__.py`. Give it its own `action_results`, `participant_roles`
and `scoreboard_guidance` — those were hard-coded in the prompt while handball
was the only sport, and every one of them is wrong for another: an equestrian
round has no Goal, no Goalkeeper and no scoreline.

**One thing does not read the registry, and it is the one that matters to the
uploader.** The API cannot import the agent package, so the upload panel's list
is the `supported_sports` Terraform variable and the API's own default. That is
a second source of truth and it failed as those do — equestrian was registered,
the analysis could run it, and the panel offered only handball.
`test_supported_sports.py` reads both files and fails when either disagrees with
the registry.

### Disciplines

A sport may have forms with nothing in common but the athlete. Equestrian has
ten: a dressage test and a reining round share a horse and nothing an editor
cuts on.

**One profile, and the discipline is detected per job.** Ten profiles would put
"which discipline?" in the upload panel, where the person filling it in is least
able to answer and most likely to guess — it is a question about the tack, the
obstacles and the movement, which is to say about the footage. The system
instruction asks for it first, every segment reports what it saw, and
`resolve_discipline` settles it across the job.

That consensus is **weighted by confidence, not counted**. An eventing broadcast
shows all three phases, so segments legitimately disagree; counting alone lets
four unsure glimpses of the dressage phase outvote two confident cross-country
ones.

**A placeholder is not a name.** The model writes "unknown" where it can see a
round and cannot read the graphic — the prompt asks for that rather than a
guess — and `canonical_identity` voted it in, so the LeMieux day ended on a
rider called "unknown" on a horse called "unknown", with moments credited to
them. `is_placeholder` leaves those readings out of the vote: a round nobody
could name is named nobody and shows as a dash. It decides what a ride is
called, not which fragments are the same ride — blanking names before fusion
would stop adjacent unnamed fragments joining, and one round would come apart
into as many as it had windows. Because every tick re-fuses the day from the
stored fragments, an event already recorded heals on its next fusion.

**An unrecognised test type keeps its place, and loses only its bar.**
`high_scoring` looked its threshold up by exact lowercase string and skipped
any ride the lookup missed — so a "Freestyle" on 84%, or a German "Kür", was
silently absent from the day's best with nothing to say it had been left out.
An unknown type is measured against the straight-test bar instead. Those bars
(75%, 80%) are dressage's and they are compiled into `rides.py` rather than
living on the sport profile, which is where every other sport-specific fact
belongs: a jumping round is scored in faults and has no percentage to clear.

**An unidentified discipline keeps the whole catalogue.** `types_for` returns
everything rather than nothing for a code it does not know, because answering an
unplaced video with an empty catalogue reports no moments in a video that plainly
has some.

The record stores the **label**, not the code — that is what is displayed and
what someone searches by — and `discipline_by_code` normalises it back. The
title falls back to it too: an equestrian graphic often names nobody, and
`Jumping — CSI Aachen` is a title where the uploaded filename is not.

### An equestrian recording is a competition day

The five real samples are 6.3-8.45 hours and 7-12.6 GB each: **one fixed camera
on a ring for a whole day**, many competitors in sequence, promotional films cut
in between classes, and long stretches of empty arena. Not a broadcast of one
round, which is what the record's shape assumed.

Three things follow, and all three were wrong before the footage was looked at:

- **`MAX_DURATION_SEC` was six hours** and rejected every one of them at
  validation, before anything else ran. It is ten now. Not free: 8.45 hours is
  35 analysis windows against a match's 13, so roughly 8M input tokens a pass.
- **`teams_are_constant` is False.** Consensusing `team1` across the job would
  relabel every competitor as whoever had the longest go. The per-moment reading
  is the only correct one, because the graphic naming them changes every round.
  The game record leaves the teams empty and is titled from the discipline and
  the competition instead.
- **The graphic is a lower third, not a score bug.** Rider, horse, nation, a
  time or a fault count — and often nothing at all. The horse is half the
  competitor, so `participant` is the pair as printed; `team2` stays empty,
  because a name there reads as a fixture between a rider and their own horse.

### Fields a sport asks for

`execution_details` and `harmony_index` exist because equestrian is judged on
how a movement was performed rather than on whether it scored. They live on
`EquestrianMoment`, a **separate response schema**, not as optional fields on
the general one: what a schema asks for is part of the prompt, so putting them
everywhere would have a handball analysis writing paragraphs about a jump shot's
balance for nobody to read. `SportProfile.segment_schema` names it; `None` means
the general shape.

Both are embedded — and for a long time neither was. `_persist_moments`
composed its own `embed_text` and sent it with the moment, and the catalog
prefers a supplied text over its own (`embed_text or action_play_text`), so
`store.action_play_text` was unreachable from the analysis and drifted two
fields behind it with nothing failing. Execution details and the harmony index
never reached a single equestrian vector, which in a sport judged on form is
most of what anyone searches by — "clean take-off", "horse fighting the
contact" are in neither the label nor the summary. Neither did the rider or
the horse: `participant` is the handball question, a shirt number read off a
jersey, and an equestrian moment leaves it empty because the pair is joined
from the ride windows in code rather than read per moment. **What a vector
carries is decided once, in `store.action_play_text`.** Nothing else composes
one, and changing that list only affects moments written after it — the
vectors already stored keep what they were built with until the match is
analysed again. **The vector index itself is unchanged**: the width is
the same and nothing new is filtered or ordered on, so there is no new Firestore
index to declare.

### An event reads as event → rides → moments

A competition day is read by who rode and what happened while they were in the
arena, so the moments card for a match with rides groups its tiles under each
ride rather than printing a rider on every tile of one flat grid. **A group is
a ride — a rider on one horse**, because that is how the class is judged; a
rider on two horses is two groups.

The grouping is built once, in the catalog (`catalog_server/event_tree.py`,
tool `get_event_tree`, route `GET /api/jobs/{id}/event`), from records that
already exist: the game record's `rides` and each moment's `rideOrder`, falling
back to the peak-in-window rule `rides.attach_moments` uses. Not asked of the
model: no analysis window sees a whole ride, so a nested answer would be one
about fragments. `store.event_tree` reads the raw game document because
`_game_out` drops the rides. A moment outside every ride goes to
`unassignedMoments` — rendered as "Outside any ride" — and is never dropped.

The browser fetches the tree only when the open game has rides, again only
when the rides or a moment's `rideOrder` change, and applies it to the live
moments in `web/src/ridegroups.js` (tested) so the filter, sort and
thumbnails keep working. Without a tree — another sport, a failed fetch — the
flat grid stands.

**Two sorts, because two questions are being asked of one screen.** Best
first / match order in the board's head orders the **rides** — best first puts
the round holding the day's strongest moment at the top of the rail, so the
highlight is the first tab rather than somewhere down a running order of forty
— and the same pair inside the pane orders **that ride's moments**. They were
one control, so asking for one rider's best moment re-sorted every rider and
the day's best could not be asked for at all. A ride is ranked by its best
moment rather than by its score: this control sits above the moments, and a
round that scored 68 can still hold the thing worth cutting. The rail sort is
stable, so rounds that found nothing keep their running order among themselves;
a score bar in the question still ranks by total, because that is the order a
question about scores is asking to see. The pane follows the board until
somebody sets it, and follows it again whenever the board changes.

**A question about rides gets the rides card**, not a moments list filtered
by the words "ride" and "scoring" — which is what the composer's own example
"rides scoring more than 70%" used to produce, with the agent's correct answer
hidden behind it. `cards.js` routes ride/rider words to `rides` (unless the
question is cutting, posting or ingesting one); `ridegroups.js` reads the
question against the names the desk holds (whole rider, whole horse, or the
rider's surname — never half a horse's name) and for a score bar ("more than"
is strict, "at least" and "or more" are not). `rideJobFor` opens the event
that ran the named rider, preferring the open one. A total whose check failed
is left out of a score question and counted on the card, the same rule
`list_rides` applies for the agent — `rides.untrusted`, which both sides now
read. They disagreed for as long as each wrote the rule out by hand: a total
can fail two ways, `check_total` writing "mismatch: …" and `apply_grounding`
writing "<source> disagrees: …", and every Python caller tested only
`startswith("mismatch")`. So a ride the published results contradict came back
as one of the day's best while the editor's own card excluded it. "events" and "competitions" are the games
list, as "games" is.

**The player is a ride player.** Opening a moment (or "Play full ride" on a
ride's heading) plays a *range* held in `state.playing` — the moment padded 3s,
or the whole ride — and every control re-aims that one video rather than
building another: −5s/+5s, Loop, speed (1× → 0.5× → 0.25× → 2×, slower first
because judging a movement means watching it slowly), Widen (+5s each side)
and Full ride. **The scrubber is the whole ride** (`playerTimeline`), with the
moment as an amber band between an in and an out marker (`rangeBand`), and
the clock reads the same bar. Playing stops at the moment's out point (or
loops); a seek outside the band sets `free` and the ride runs on, and a chip,
Widen or Play from the end re-arms the out point. Under the player: the
ride, its score tiles (one per judge — the analysis does not split technical
and artistic marks, so they are not shown), where the score came from, the
moments as chips that re-aim the player, and the `ffmpeg` cut of what is
playing. Beside it: the incident scan and the moment's record — or the publish
panel, when Publish has swapped it in. A "looked for, not confirmed" section
used to sit above the incidents, listing what the analysis went looking for in
this ride's own windows and could not confirm; it was removed at the editor's
request, because a list of things that did not happen reads as doubt about the
ones that did (`notesForRide` went with it). The pure parts — speeds, widen
and the trim — are in `player.js`, tested. A player with a summary under it scrolls rather than
sticks, or the summary would slide behind the video on a short screen.

**`get_game` does not carry the rides.** `_game_out` is the game's shape for
an agent's context and leaves out `rides`, `notConfirmed`, `judges` and the
start list. `list_rides` read its rides from it and so told the agent "no
rides recorded" for every event that had them, while the editor's rides card —
reading the raw document through the tree — showed them. It reads
`list_game_rides` now (one document, no moments). Anything else that needs a
field `_game_out` drops wants its own read, not a wider summary.

The producer reads it through `pipeline.get_event`, not the MCP tool: like
`list_rides` it is a wrapper that cuts the tree down (best few moments per
ride, as briefs) so a day of forty rounds fits in the model's context.

**A live event keeps its rides too.** Each chunk stores the rides it saw as
absolute, unstitched `rideFragments` and its `notConfirmed` notes; every tick
fuses the whole day from all chunks' fragments (`live.event_rides`), joins the
stored moments to them through `_patch_moment_identities`, and hands the rides
to the interim record and, with the merged notes, to the final one. Fused
again rather than extended, because a ride crossing a chunk boundary is two
fragments until the second chunk is in. The patch counts `ride_order` as
identity: the tree groups by it, so a renumbered ride left un-rejoined would
put its moments under the wrong rider. The chunks store the discipline code;
the record stores the label, as an upload's does.

## Known gaps

- **No analytics backend.** The design's post-performance card is deliberately
  not rendered.
- **No platform OAuth.** Publish prepares a downloadable package; it does not
  post on anyone's behalf.
- **Classification precision is untuned.** On real footage the model skews
  toward `jump_shot` and pins confidence near 1.00. Fixing that needs labelled
  ground truth and an eval loop, not prompt tweaking.
