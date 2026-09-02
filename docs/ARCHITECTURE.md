# Sportscut — Architecture

Sportscut turns a long-form sports video into a ranked set of short-form clips ready
for TikTok, Instagram Reels and YouTube Shorts. Everything runs on Google Cloud
native services and is provisioned by Terraform.

```
                         ┌──────────────────────────────────────┐
  Browser ──► IAP ──────►│  web (Cloud Run, static SPA)         │
   │  Identity Platform  │  "Sportscut editor"                  │
   │                     └──────────────────────────────────────┘
   │                                    │ REST (IAP-signed JWT)
   │                                    ▼
   │                     ┌──────────────────────────────────────┐
   │                     │  api (Cloud Run, FastAPI)            │
   │                     │  signed uploads · signed CDN URLs ·  │
   │                     │  search · agent session proxy (SSE)  │
   │                     └───────────┬──────────────────────────┘
   │                                 │ streamQuery
   │                                 ▼
   │                     ┌──────────────────────────────────────┐
   │                     │  Vertex AI Agent Runtime             │
   │                     │  sprtz_producer (ADK)                │
   │                     │       │                              │
   │                     │       └─► Gemini 2.5 Flash ──────────┼──► gs:// source
   │                     │           13 × 15-min segments,      │    (video_metadata
   │                     │           concurrent, then merged    │     offsets)
   │                     └───────────┬──────────────────────────┘
   │                                 │ MCP (streamable HTTP, OIDC)
   │                     ┌───────────┴───────────┐
   │                     ▼                       ▼
   │        ┌────────────────────┐   ┌────────────────────────────┐
   │        │ mcp-media          │   │ mcp-catalog                │
   │        │ (Cloud Run, ffmpeg)│   │ Firestore · embeddings ·   │
   │        │ probe · HLS · cut  │   │ KNN + Gemini rerank        │
   │        └─────────┬──────────┘   └─────────┬──────────────────┘
   │                  │ HLS package            │
   │                  ▼                        ▼
   │        ┌────────────────────┐      Firestore (native mode)
   │        │ GCS (hls bucket)   │      jobs · moments · clips · events
   │        └─────────┬──────────┘      + vector index (KNN, 768-d)
   │                  │                        │
   │                  ▼                        │
   │        Cloud CDN + external ALB           │
   │        (signed URL prefixes)              │
   │                  │                        │
   └── HLS playback ──┘        realtime onSnapshot ◄──┘
       seek to a moment's start, stop at its end
```

## 1. Agents

All agents are ADK agents packaged into a single Agent Runtime deployment
(`sprtz_agents`). The root agent is an `LlmAgent` that owns the conversation with
the editor; the run itself goes through a `SequentialAgent` so the per-stage
status written to Firestore is predictable and the UI can render it as steps.

```
sprtz_producer  (LlmAgent — talks to the editor)
└── analysis_pipeline  (SequentialAgent)
    ├── ingest_agent
    ├── prepare_and_analyze  (ParallelAgent)
    │   ├── transcode_agent
    │   └── analysis_agent
    ├── clip_agent
    ├── caption_agent
    └── publish_agent
```

| Agent | Responsibility |
|---|---|
| `sprtz_producer` | Talks to the editor. Answers questions about a job, searches the match semantically, adjusts clips, and delegates a full run to `analysis_pipeline`. |
| `ingest_agent` | Probes the upload, records duration/fps/resolution/codec, works out the segment plan. |
| `transcode_agent` | Packages the video into an HLS ladder behind the CDN so the editor can play it. |
| `analysis_agent` | Runs the segmented Gemini 2.5 Flash pass over the whole match, merges the results, embeds and saves the moments. |
| `clip_agent` | Turns moments into publishable cuts: in/out points per moment type, overlap resolution, 9:16 target. |
| `caption_agent` | Per-platform copy — on-screen hook, title, TikTok/Instagram/YouTube captions, hashtags. |
| `publish_agent` | Validates each clip against platform limits and closes the job out. |

### Why transcode and analysis run together

Both read the source from GCS and neither needs the other's output. On a
three-hour recording the HLS package takes longer than the analysis, so running
them in a `ParallelAgent` takes it off the critical path — the editor gets
moments to look at while the stream is still being built.

### Why there is no separate audio agent

Gemini 2.5 Flash consumes the video's audio track in the same pass as the
picture, so commentary emphasis, crowd noise and the whistle are already
evidence available to the single analysis call. A separate speech-to-text stage
would add a service, a failure mode and a cost for signals the model already has.

### Adding a sport

Everything handball-specific lives in `agents/sprtz_agents/sports/handball.py`:
18 moment types across five categories, plus the court and broadcast context.
A new sport is one module registering a `SportProfile` — no agent, tool or UI
code changes.

## 2. Segmented analysis

A full match is far too long for one model call, so `analyse_segments`:

1. Splits the video into 15-minute windows overlapping by 20 seconds.
2. Sends each window to Gemini 2.5 Flash concurrently (bounded by a semaphore),
   using `video_metadata` start/end offsets so the model reads the range straight
   out of GCS — no clipping, no re-upload.
3. Merges the per-window results into one absolute-timestamped timeline,
   collapsing anything a boundary caused to be reported twice.

A 180-minute recording becomes 13 segments. The overlap exists so an action
straddling a boundary is seen whole by at least one window; the merge resolves
the duplicate by temporal IoU within a moment type, keeping the wider time range
and the more confident reading. One failed segment is reported and skipped
rather than failing the match.

### Three findings from real footage that shaped this

**Prompt length caused fabricated timestamps.** An earlier version inlined the
full 18-type catalogue into every per-segment prompt. On real match footage the
model stopped reporting observed positions and emitted a sequential counter
instead — thirty "moments" inside a three-second span, all at identical
confidence. Moving the catalogue into the system instruction (where it is also
byte-stable, so it caches) fixed it: timestamps then spread properly across the
window. `test_segment_prompt_stays_short` guards the regression.

**Timecodes beat float seconds.** The model is asked for `MM:SS` within the clip
rather than float seconds. Anything landing outside the window is rejected rather
than clamped, because a clamp would place a moment at a timestamp nobody observed.

**1 fps, not 2.** Doubling the sample rate doubled the token cost *and* made
timestamps markedly worse — a longer frame sequence pushes the model toward
counting rather than reading.

## 3. Playback

`transcode_hls` produces a 360p/540p/720p HLS ladder with 2-second segments and
uploads it to the HLS bucket, which sits behind Cloud CDN via a backend bucket on
an external Application Load Balancer.

Access uses **Cloud CDN signed cookies** rather than a public bucket, and the
choice of cookie over signed URL is forced by HLS rather than preferred.

A playlist references its segments *relatively*. A player resolving
`v0_00000.ts` against the playlist URL discards any query string the playlist
carried, so a signed URL authorises the playlist and nothing else — every one of
the several thousand segment requests then arrives unsigned. A cookie is
attached by the browser to all of them without the player knowing anything about
it. The API mints the cookie from a key in Secret Manager, signing the job's
`URLPrefix` and scoping the cookie's `Path` to the same job so several jobs can
hold valid cookies at once despite Cloud CDN fixing the cookie's name.

> **This needs custom domains.** A browser only sends the cookie to the CDN if
> the CDN host falls under the cookie's domain, so the API and the CDN must share
> a registrable domain (`api.sprtz.ai` + `cdn.sprtz.ai`, cookie domain
> `.sprtz.ai`). On the default `*.run.app` hostnames it cannot work at all:
> `run.app` is on the Public Suffix List, so no cookie can span two services
> there. `/api/jobs/{id}/playback` reports `cookie_set: false` in that case and
> the editor says so, rather than letting the player fail with an opaque 403.

Playlists carry `max-age=60` so a re-transcode is picked up; segments are
immutable and carry a one-year immutable TTL.

The editor plays this one stream for everything. Reviewing a key moment is a seek
to its start time with a stop at its out point — nothing is rendered to watch a
suggestion. Clips are only rendered to MP4 on export.

## 4. MCP servers

Two servers, split by runtime shape rather than by domain. Both are private Cloud
Run services; the agent calls them with an OIDC identity token and neither has
public ingress.

### `mcp-media` — ffmpeg-backed, long timeouts, high CPU

The worker never holds a match locally. On Cloud Run every writable path is
memory-backed, and a three-hour match is a ~3 GB source plus a comparable HLS
package — bigger than any sensible instance. So:

- **Sources are read over HTTPS**, straight from GCS with a bearer token;
  ffmpeg's `-ss` becomes a range seek, so cutting a 30-second clip out of a
  3 GB match reads megabytes.
- **Playback packaging is a copy-remux, not a re-encode.** Uploads are already
  H.264 at delivery-grade bitrates; segmentation is all review playback needs.
  A remux is I/O-bound and takes minutes, where a rendition ladder for the same
  source is hours of CPU that cannot finish inside Cloud Run's one-hour request
  ceiling. If a ladder is ever wanted for public delivery, it belongs in a
  batch job.
- **Finished segments drain to GCS while ffmpeg is still writing** and are
  deleted locally, so disk stays bounded to the last few segments whatever the
  match length.

### Cold start, and why the probe budget is large

Python is slow to start here. Measured on a live Cloud Run revision with
startup CPU boost: `grpc` 3s, `google.cloud.firestore` 45s, `google.genai` 72s,
the server module 100s — against 6s for the same image on a developer machine.

Two consequences are baked into the configuration:

- **Heavy imports are lazy.** `catalog_server.store` and `media_server.gcs`
  import the Google libraries inside their accessors, so the process binds a
  port and answers `/healthz` in seconds and pays the cost on first use, warm
  for the life of the instance.
- **Startup probes allow ~5 minutes** and every service sets
  `startup_cpu_boost`. A tight window kills the container mid-import, and
  because stdout is still buffered the logs show *nothing* — which reads as a
  container that never ran rather than one that was not given time to. That
  failure mode cost a full debugging cycle: it was misread first as an image
  problem, then as a memory-to-CPU ratio, before instrumenting the import chain
  showed plain slowness.

| Tool | Purpose |
|---|---|
| `probe_media` | Duration, resolution, fps, codecs. Reads the file header first, so a 3 GB match does not cross the wire just to read its duration. |
| `transcode_hls` | The HLS ladder, uploaded to the CDN bucket. |
| `cut_clip` | Render an in/out range to a standalone MP4 for export. |
| `reframe_vertical` | 9:16 or 1:1 with a blurred fill, so a wide court shot still reads on a phone. |
| `burn_captions` | Burn the on-screen hook over the opening second. |
| `render_preview` | Quick proxy of a proposed cut. |

### `mcp-catalog` — Firestore, embeddings and search

| Tool | Purpose |
|---|---|
| `create_job` / `get_job` / `update_job_status` | Job lifecycle. |
| `record_media_info` / `record_playback` | Probe results and the CDN playback URL. |
| `emit_event` | Append to `jobs/{id}/events` — what the UI streams live. |
| `upsert_moments` / `list_moments` | Moments. Embeddings are generated here so the vector width can never drift from the index. |
| `upsert_clips` / `list_clips` / `update_clip` | Clip suggestions. `update_clip` rejects derived fields rather than writing them. |
| `knn_search_moments` | Vector retrieval plus Gemini reranking. |

### Search: retrieve, then rerank

Retrieval and ranking answer different questions. The embedding index finds
moments *worded like* the query, which is not the same as moments that *answer*
it. So `knn_search_moments` over-fetches (4x the requested limit, capped at 60)
and hands the candidates to Gemini 2.5 Flash to score for relevance.

Measured on the query *"the goalkeeper single-handedly kept them in the game"*:
vector similarity alone put two conceded goals and a timeout on top, because all
three mention a goalkeeper. The reranker moved both double saves to the top and
scored the conceded goal 0.1, reasoning "the opposite of keeping the team in the
game".

Each result carries `similarity` (vector), `rerank_score` and `rerank_reason`
(shown in the UI), and `rank`. A null `rerank_score` means the reranker was
unavailable and the vector order stands — reranking failure degrades the
ordering, it never empties the result set.

## 5. Firestore data model

```
users/{uid}
  displayName, email, defaultPlatforms[], createdAt

jobs/{jobId}
  ownerUid, title, sport, status, stage, progress,
  source:   { gcsUri, bytes, originalName },
  media:    { durationSec, fps, width, height, videoCodec, audioCodec,
              bitrate, segmentCount },
  playback: { hlsUrl, posterUrl, renditions[], segmentSeconds, readyAt },
  counts:   { moments, clips },
  error, createdAt, updatedAt

jobs/{jobId}/events/{eventId}          # realtime agent activity feed
  ts, agent, level, stage, message, data

jobs/{jobId}/moments/{momentId}
  startSec, endSec, type, label, description,
  confidence, excitement, highlightScore,
  evidence[], scoreboard, isGoal, segmentIndexes[],
  embedding: Vector(768),              # KNN index
  createdAt

jobs/{jobId}/clips/{clipId}
  momentId, startSec, endSec, durationSec, platforms[],
  aspect, hookText, title, rationale,
  captions: { tiktok, instagram, youtube },
  hashtags[], status, renderUri, thumbnailUri, score

renders/{renderId}
  jobId, clipId, state, outputUri, requestedBy, startedAt, finishedAt
```

`embedding` is indexed with a Firestore vector index (`COSINE`, 768 dims) so
`knn_search_moments` is a single `find_nearest` query — no separate vector store.

### Realtime sync

The SPA holds three `onSnapshot` listeners per open job: the job document
(status/progress), the `events` subcollection (agent activity feed), and the
`clips` subcollection (suggestion grid). Agents write through `mcp-catalog`, so
the UI updates without any polling or websocket of our own.

## 6. Identity

- **Identity Platform** is the identity provider (email/password out of the box;
  Google sign-in once an OAuth web client is supplied).
- **IAP** fronts the `web` and `api` Cloud Run services using Cloud Run's built-in
  integration, so unauthenticated traffic never reaches application code.
- The SPA additionally signs in to the Firebase JS SDK against the same tenant so
  it can open Firestore listeners directly. Security rules restrict every
  document to `ownerUid == request.auth.uid` and make the client read-only —
  every write goes through the agents.
- `api` verifies the `x-goog-iap-jwt-assertion` header on every request and takes
  the caller identity from it. Ownership on a new job comes from that verified
  assertion, never from the request body.

> **Note on IAP OAuth:** there is deliberately no `google_iap_brand` or
> `google_iap_client` in the Terraform. Those drive the IAP OAuth Admin APIs,
> which were deprecated in January 2025 and permanently shut down in March 2026 —
> new projects cannot use them. Cloud Run's `iap_enabled` uses a Google-managed
> OAuth client and needs no brand.

## 7. Deployment

`deploy/cloudbuild.yaml` is the single CI entry point, triggered on merge to
`main`. In order:

1. **Verify** — agent unit tests + ruff, MCP unit tests.
2. **Build** — four images in parallel (`api`, `web`, `mcp-catalog`, `mcp-media`).
3. **Preflight** — `deploy/scripts/preflight.sh` checks the target regions
   against the live APIs before anything is created. Firestore's location is
   immutable once the database exists and Vertex is not offered everywhere, so
   this is much cheaper to catch here than mid-apply.
4. **Bootstrap** — `deploy/scripts/bootstrap.sh` idempotently creates the two
   resources Terraform cannot own: the state bucket (it would be holding the
   state that manages it) and the Artifact Registry repository (images are
   pushed before the full apply). The apply then adopts the registry with a
   guarded `terraform import`.
5. **Terraform apply** — enables every API and creates everything else.
6. **Deploy the agent** — last, because its tools are the MCP services Terraform
   just created.

The Agent Runtime engine is deliberately *not* a Terraform resource. Terraform
creates such an engine from a source archive (`spec.deployment_source`), the SDK
updates it as a package (`spec.package_spec`), and the API will not move an
engine between the two. `agents/deployment/deploy.py` therefore owns its
lifecycle end to end: Terraform publishes the `agent_display_name` both sides
agree on, the script creates or updates the engine under that name, and the API
resolves it the same way — so neither side has to discover an id the other
invented.

### CI permissions

The trigger's service account needs `roles/editor` **plus**
`roles/resourcemanager.projectIamAdmin`, `roles/iap.admin`,
`roles/firebaserules.admin`, `roles/iam.serviceAccountAdmin`,
`roles/secretmanager.admin` and `roles/datastore.owner`. Editor alone cannot
create project IAM bindings, set IAM policy on service accounts or secrets,
create the Firestore database, or release Firestore rules — each of which this
Terraform does.

A service account with no roles produces no error — the build stays `PENDING`
forever, which reads as a queue backlog rather than a permissions failure.

### Two silent PENDING traps

Cloud Build reports both of these as an ordinary queue wait, with no error and
no failed step:

- **A service account with no roles.** The build never starts.
- **An unavailable machine type.** `E2_HIGHCPU_8` was accepted in us-south1 but
  had no capacity there, so builds queued indefinitely. The pipeline sets no
  `machineType` and uses the default pool, which starts immediately.

If a build sits in `PENDING`, check those two before anything else.

### Regions

`region` (Cloud Run, GCS, Artifact Registry, Cloud Build) and `vertex_region`
(Agent Runtime, Gemini, embeddings) are separate variables. They both default to
`us-central1`. Keeping them separate means the app can sit in a region Vertex
does not serve, without the whole deploy failing.

> The preflight probes Gemini with a real `generateContent` call, not a GET on
> the publisher model resource — that GET returns 404 in regions that serve the
> model perfectly well.

### Trigger substitutions

The Cloud Build trigger should set:

| Substitution | Example |
|---|---|
| `_REGION` | `us-south1` |
| `_ENVIRONMENT` | `dev` or `prod` |
| `_APP_NAME` | `sprtz` |
| `_AR_REPO` | `sprtz-dev-containers` |
| `_TF_STATE_BUCKET` | the `tf_state_bucket` Terraform output |

The trigger's service account needs the roles granted to the `cloudbuild`
service account in `iam.tf`.

### Bootstrapping a clean project

```bash
PROJECT_ID=<project> REGION=us-central1 bash deploy/scripts/preflight.sh
PROJECT_ID=<project> REGION=us-central1 bash deploy/scripts/bootstrap.sh

cd deploy/terraform
terraform init \
  -backend-config="bucket=<project>-sprtz-dev-tfstate" \
  -backend-config="prefix=terraform/dev"
terraform apply -var-file=envs/dev.tfvars -var="project_id=<project>"
```

CI runs the same two scripts, so pushing to `main` on a clean project works
without any of this being done by hand.
