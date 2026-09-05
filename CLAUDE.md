# Arenos — working notes

An agentic SaaS that watches a full sports match and proposes short-form clips
for TikTok, Instagram Reels and YouTube Shorts. Handball and equestrian today;
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
  sprtz_agents/sub_agents/       the six pipeline stages
  sprtz_agents/sports/           moment taxonomies + Gemini prompt  ← add sports here
  sprtz_agents/tools/            segmented analysis, clip planning, MCP access
mcp/         MCP tool servers (private Cloud Run)
  media_server/                  Transcoder API for HLS; ffmpeg: probe, cut, reframe, burn-in
  catalog_server/                Firestore, embeddings, KNN + Gemini rerank
api/         FastAPI behind IAP: signed uploads, signed CDN URLs, agent SSE proxy
web/         Arenos editor SPA (chat-first, Arenos design system)
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

### Something must be selected

Moments, clips, events and the game record are all read through listeners
`selectJob` opens, so with no job selected every one of those cards is empty
however much has been analysed — and an empty card reads as "the analysis found
nothing" rather than "no match is open". Decoupling sessions from jobs removed
the line that used to select the newest job, and took that context with it.

`ensureJobContext` selects the most recent match when nothing else has, and
never overrides a session that names its own.

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
end of is not a page. Jobs, the activity feed and the reel page too: fifty jobs
and eighty events in one message bury the conversation as surely as two hundred
moments did.

### The two detail widgets

A moment row and a game row are the same shape on purpose: a headline worth
reading, the facts that qualify it underneath, and everything else behind one
Details button into a shared popup.

**The moment plays inside its own popup and nowhere else**, in the wider left
column, autoplaying from the in point and stopping at the out point. The row's
thumbnail opens it, and so does a clip's Play in the reel — that one passing
the clip's own trim rather than the moment's. The row used to hold a player
slot of its own, which meant two elements carrying the same `data-slot` and the
wrong one winning on document order; it also meant playing a clip from the reel
did nothing at all unless that moment's row happened to be rendered somewhere
to receive it. The video takes
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
app that means the moment thumbnail's timecode, per-clip durations, clip
numbering and page counts. Anything a person wrote (labels, descriptions,
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
with nobody to answer. The stage made no tool call, and clips, captions and
publish all ran successfully on zero moments and marked the job complete. The
sport is read off the job now, the stage instructions say plainly that a
question is the end of the run rather than a pause, and `inspect_source` returns
the sport so the stages after it inherit the fact instead of asking for it.

**A run that analysed nothing is not a finished run.** "0 of 0 clips ready to
publish" reads as a match with no highlights in it. `finalize_job` marks the job
`failed` with a reason when there are no clips *and* no moments — a quiet match
still only needs attention, because that is a real outcome.

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

Both are embedded. In a sport judged on form they carry most of what anyone
searches by — "clean take-off", "horse fighting the contact" are in neither the
label nor the summary. **The vector index itself is unchanged**: the width is
the same and nothing new is filtered or ordered on, so there is no new Firestore
index to declare.

## Known gaps

- **No analytics backend.** The design's post-performance card is deliberately
  not rendered.
- **No platform OAuth.** Publish prepares a downloadable package; it does not
  post on anyone's behalf.
- **Classification precision is untuned.** On real footage the model skews
  toward `jump_shot` and pins confidence near 1.00. Fixing that needs labelled
  ground truth and an eval loop, not prompt tweaking.
