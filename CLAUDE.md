# Sportscut — working notes

An agentic SaaS that watches a full sports match and proposes short-form clips
for TikTok, Instagram Reels and YouTube Shorts. Handball first; the sport
taxonomy is pluggable.

Read `docs/ARCHITECTURE.md` for *why* the pieces fit together. This file is for
working *in* the repo: conventions, commands, and the things that have already
cost a debugging cycle.

---

## Layout

```
agents/      ADK agents on Vertex AI Agent Runtime (the product's brain)
  sprtz_agents/agent.py          sprtz_producer + analysis_pipeline
  sprtz_agents/sub_agents/       the six pipeline stages
  sprtz_agents/sports/           moment taxonomies + Gemini prompt  ← add sports here
  sprtz_agents/tools/            segmented analysis, clip planning, MCP access
mcp/         MCP tool servers (private Cloud Run)
  media_server/                  Transcoder API for HLS; ffmpeg: probe, cut, reframe, burn-in
  catalog_server/                Firestore, embeddings, KNN + Gemini rerank
api/         FastAPI behind IAP: signed uploads, signed CDN URLs, agent SSE proxy
web/         Sportscut editor SPA (chat-first, Modernist design system)
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
  --with pydantic pytest tests -q                       # 17 tests

# API
cd api && ENVIRONMENT=local uvicorn app.main:app --reload   # bypasses IAP

# Terraform (needs the binary; not installed by default here)
cd deploy/terraform && terraform fmt -recursive && terraform validate
```

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

- **Gemini 2.5 Flash** for video analysis, **gemini-embedding-001** (768-dim)
  for semantic search. The embedding width must equal the Firestore vector
  index dimension exactly or queries fail at read time, not write time.
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

Cards are normally chosen from the finished reply, which is useless for
anything long: an analysis would have shown its progress widget an hour after
the progress was worth watching. `ask()` takes the cards to attach up front for
that reason, and the upload, retry, re-analyse and cancel paths all pass the
jobs card so the stage strip is on screen from the moment the run starts. The
Firestore listener re-renders on every job write, so it follows by itself
afterwards.

Segment completion is what moves the bar during analysis, counted rather than
indexed because segments finish out of order.

An empty `status` passed to `update_job_status` leaves the status alone.
Progress updates arrive once per segment and have no opinion about status, so
without that they would blank the field the whole UI reads.

### Cancelling and deleting

**Cancel is a flag, not a kill.** The run is a sequence of calls on Agent Runtime
with no handle to interrupt, so stages check `cancel_requested` between steps and
stop at the next boundary. Moments already found are saved rather than discarded
— cancelling should not also destroy the hour that was already paid for.

**Delete removes media first, then Firestore.** A failure after the media is gone
leaves a job pointing at a missing video, which is recoverable; the other order
leaves orphaned gigabytes nothing refers to. Firestore does not cascade, so
moments, clips and events are deleted explicitly, and the `games` record is a
separate top-level document that an imagined cascade would miss entirely.

**Re-analysing clears first.** Without `clear_analysis` the previous run's
moments stay put and the new ones land beside them: the same play twice, with a
count that grows on every retry.

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

Packaging is independent of the analysis, so a job can hold moments and have
nothing to play. `prepare_playback` is a root-agent tool as well as a pipeline
stage for that reason — re-running a whole analysis to fix playback would be an
hour spent on the wrong thing — and the player offers it when `/playback`
returns 409. Each encode clears the job's HLS prefix first: Transcoder names
segments differently from the ffmpeg packager that preceded it, so nothing is
ever overwritten, and a playlist left by a half-finished run is one the CDN will
serve.

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

Reviewing a moment is a seek to its in point with a stop at its out point — that
is what replaces a timeline.

### Sessions

An Identity Platform ID token lasts an hour and the SDK renews it only when
something asks, so a tab left open sends a stale one and gets a 401 that reads
as "logged out". Three things address that, and the second is the one that
matters: persistence is set explicitly, a timer refreshes at 45 minutes, and
**`api()` retries once on a 401 with a force-refreshed token**. The retry is
what turns an expiry into a pause nobody notices; once only, because a second
401 is a real authentication failure.

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
render, so switching language re-renders rather than reloads. The greeting is
rebuilt only when it is the only message on screen — rewriting something the
editor has already read would be worse than leaving it.

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

### The cards

Eight, each reachable from `attachCards` and each with an empty state:
`ingestCard`, `jobsCard` (the stage strip), `gameCard`, `gamesCard`,
`momentsCard`, `reelCard`, `publishCard`, `activityCard`. `showActivity` was
never set by anything, so that card could not appear at all until this was
audited — a renderer nobody routes to is dead code that looks alive.

**No card returns an empty string.** Rendering nothing is indistinguishable from
a card that failed to render, and the two have very different answers: "No
moments yet" is information, a blank space is a bug report. `emptyCard` says
which.

**Lists page rather than truncate.** The moments list used to stop at six, which
looks like an analysis that found six; showing all two hundred would bury the
conversation. Ten a page, with the page held on the message so scrolling back to
an earlier answer finds it where it was left. `pageOf` clamps out-of-range pages
rather than rendering blank.

### The two detail widgets

A moment row and a game row are the same shape on purpose: a headline worth
reading, the facts that qualify it underneath, and everything else behind one
Details button into a shared two-column popup. What differs is that a moment has
a thumbnail to play and a game does not, so they have separate grids — reusing
`.moment-row` for a game squeezes the headline into the 72px thumb column.

Grounded values get their own rows in the game popup, labelled "(from search)",
and the sources sit at the bottom of it rather than on the card. They qualify
the grounded rows and are meaningless beside a row nobody is looking at.

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

`ownerUid` is still written on every job, moment, clip and game — as provenance
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

The wordmark and app icon are real assets under `web/src/assets/`, served from
the app's own origin. The brand pink in them is **not** the Modernist accent —
`--color-accent` stays `#ec3013` and the logo carries its own colour. Do not
reconcile them by editing the tokens without deciding that deliberately: every
other accent in the app comes from that variable.

### UI

Chat-first, implementing `SPRTZ AI Chat.dc.html` on the vendored **Modernist**
design system at `web/src/ds/`. Archivo, red on light, **zero corner radius**,
2px rules, flush-left labels. Take every colour, space and radius from the
tokens; `app.css` introduces none of its own. The one deliberate literal is the
video letterbox matte, commented as such.

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

**Nothing retries a run that dies.** A deploy replaces the Agent Runtime engine
and kills whatever it was doing. Progress reporting dies with it, so the job
keeps the status it had and reads as running for ever. The editor shows a
running job with no movement for 15 minutes as `stalled` and offers Retry; the
agent is told that a stale `updated_at` under a running status means a dead run,
because otherwise it correctly refuses to start "a second run".

Merging during an analysis therefore costs that analysis. It is not lost data —
the upload is still in the bucket and the job can be re-run.

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

**CI permissions.** `roles/editor` is not enough. Also needs
`resourcemanager.projectIamAdmin`, `iap.admin`, `firebaserules.admin`,
`iam.serviceAccountAdmin`, `secretmanager.admin`, `datastore.owner`,
`run.admin` (Editor cannot `run.services.setIamPolicy`).

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

Set them on the trigger (`_IAP_MEMBERS` is comma-separated, e.g.
`user:you@example.com,domain:example.com`). Point the domain's A record at the
`cdn_ip` output before the managed certificate can provision.

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
in `sports/__init__.py`. Nothing else changes — the agents, tools and UI all
read the registry.

## Known gaps

- **No analytics backend.** The design's post-performance card is deliberately
  not rendered.
- **No platform OAuth.** Publish prepares a downloadable package; it does not
  post on anyone's behalf.
- **Classification precision is untuned.** On real footage the model skews
  toward `jump_shot` and pins confidence near 1.00. Fixing that needs labelled
  ground truth and an eval loop, not prompt tweaking.
