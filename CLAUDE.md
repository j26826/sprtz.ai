# Sprtz AI — working notes

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
  media_server/                  ffmpeg: probe, HLS remux, cut, reframe, burn-in
  catalog_server/                Firestore, embeddings, KNN + Gemini rerank
api/         FastAPI behind IAP: signed uploads, signed CDN URLs, agent SSE proxy
web/         SPRTZ AI Editor SPA (chat-first, Modernist design system)
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
  concurrently via `video_metadata` offsets straight off GCS, then merged with
  temporal IoU per moment type. A 3-hour recording is 13 segments.
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

### Search

Retrieval and ranking answer different questions. `knn_search_moments`
over-fetches 4x and has Gemini rerank for relevance. Reranker failure degrades
to vector order — it must never empty the result set.

### Media

The worker never holds a match locally: **Cloud Run's writable filesystem is
memory.** Sources stream over HTTPS with a bearer token (`-ss` becomes a range
seek — a 60s cut reads 16 MB, not 3.2 GB), playback packaging is a
**copy-remux, not a re-encode**, and segments drain to GCS while ffmpeg is
still writing.

A rendition ladder would be hours of CPU and cannot finish inside Cloud Run's
one-hour request ceiling. If one is ever wanted for public delivery, it belongs
in a batch job.

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
