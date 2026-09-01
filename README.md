# Sprtz AI

An agentic SaaS that watches a full sports match and hands an editor a ranked set
of short-form clips ready for TikTok, Instagram Reels and YouTube Shorts.

Handball to start with. Everything sport-specific lives in one module, so adding
a sport does not touch the agents, the tools or the UI.

Google Cloud native throughout: Vertex AI Agent Runtime for the agents, MCP tool
servers on Cloud Run, Firestore in native mode for persistence *and* vector
search, Cloud CDN for playback, IAP with Identity Platform for access, and
Terraform + Cloud Build for everything else.

---

## How it works

1. **Upload.** The browser PUTs the video straight to GCS with a V4 signed URL —
   a three-hour match never passes through the API.
2. **Ingest.** The media server probes it. A 180-minute recording is planned into
   13 overlapping 15-minute segments.
3. **Package and analyse, together.** One `ParallelAgent` transcodes the video to
   an HLS ladder behind the CDN while Gemini 2.5 Flash analyses every segment
   concurrently, reading each range straight out of GCS.
4. **Merge.** Per-segment detections become one absolute-timestamped timeline;
   anything a boundary caused to be reported twice is collapsed.
5. **Score, cut, write.** Moments are ranked, embedded with `gemini-embedding-001`
   for semantic search, cut into clips with per-moment-type lead-in and
   follow-through, and given per-platform copy.
6. **Review.** The editor watches one HLS stream. Clicking a moment seeks to its
   in point and stops at its out point — nothing is rendered to review a
   suggestion.

Everything the agents write lands in Firestore, and the UI holds `onSnapshot`
listeners on it, so the editor's screen updates as the analysis runs with no
polling anywhere.

## What it looks for

18 handball moment types across five categories:

| | |
|---|---|
| **Offense** | Jump Shot · Wing Shot · Pivot Slip-Through · In-Flight (Kempa Trick) |
| **Defense** | Steal / Interception · The Block · Double Save · Empty-Goal Goal |
| **Transition** | First Wave (Fast Break) · Second / Third Wave · Quick Restart |
| **Officiating** | 7-Metre Penalty · Passive Play Warning · 2-Minute Suspension · Red / Blue Card |
| **Tactical** | 7-v-6 Attack · Team Timeout · Last-Second Free Throw |

Each type carries a prior on how it performs as short-form content, plus the
lead-in and follow-through a cut of it needs — a red card wants the foul that
earned it, a wing shot does not.

## Layout

```
agents/       ADK agents deployed to Vertex AI Agent Runtime
  sprtz_agents/
    agent.py            sprtz_producer + analysis_pipeline
    sub_agents/         the five pipeline stages
    sports/             moment taxonomies + the Gemini prompt  ← add sports here
    tools/              segmented analysis, clip planning, MCP access
mcp/          MCP tool servers (Cloud Run)
  media_server/         ffmpeg: probe, HLS, cut, reframe, burn-in
  catalog_server/       Firestore, embeddings, KNN + Gemini rerank
api/          FastAPI behind IAP: signed uploads, signed CDN URLs, agent proxy
web/          The SPRTZ AI Editor SPA
deploy/
  cloudbuild.yaml       the whole CI/CD pipeline
  terraform/            every resource, every API enablement
  scripts/preflight.sh  region support check, run before apply
docs/ARCHITECTURE.md    how and why the pieces fit
```

## Deploying

```bash
# 1. Confirm the target regions can host everything.
PROJECT_ID=<project> REGION=us-south1 bash deploy/scripts/preflight.sh

# 2. Create the two resources Terraform cannot own (state bucket, registry).
PROJECT_ID=<project> REGION=us-south1 bash deploy/scripts/bootstrap.sh

# 3. Apply.
cd deploy/terraform
terraform init -backend-config="bucket=<project>-sprtz-dev-tfstate" \
               -backend-config="prefix=terraform/dev"
terraform apply -var-file=envs/dev.tfvars -var="project_id=<project>"
```

Then push to `main` and Cloud Build takes over: test → build four images →
preflight + bootstrap → `terraform apply` → deploy the agent. The pipeline runs
`bootstrap.sh` itself, so a clean project needs nothing done by hand.

Before the first apply, edit `deploy/terraform/envs/dev.tfvars`:

| Variable | Why |
|---|---|
| `project_id` | Required. |
| `iap_members` | Who may reach the editor, e.g. `["user:you@example.com"]`. |
| `cdn_domain` | Needed for real playback — an HTTPS page cannot load HTTP HLS. Point its A record at the `cdn_ip` output, then re-apply. |
| `google_oauth_client_id/secret` | Optional; enables Google sign-in. Without it the tenant ships email/password only. |

### The trigger's service account

Cloud Build runs as whatever service account the trigger names, and that account
needs more than `roles/editor`. Editor cannot create project IAM bindings, IAP
access bindings, or a Firestore rules release, all of which Terraform does. Grant
it `roles/editor` plus:

```
roles/resourcemanager.projectIamAdmin
roles/iap.admin
roles/firebaserules.admin
```

A trigger whose service account has no roles does not fail — the build sits in
`PENDING` indefinitely, which looks like a queue backlog rather than a
permissions problem.

### Why bootstrap.sh exists

Two resources cannot be owned by the Terraform that needs them:

- **The state bucket** would be holding the state that manages it. The first
  `init` would have nowhere to write, and a `destroy` would delete its own
  backend. Its name is deterministic (`<project>-<app>-<env>-tfstate`) so CI can
  derive it without reading state.
- **The Artifact Registry repository** has to exist before CI pushes images,
  which happens before the full apply. The first apply adopts it with a guarded
  `terraform import`.

### Regions

`region` and `vertex_region` are separate because Vertex AI serves fewer regions
than Cloud Run. Both default to `us-south1`, verified against the live APIs to
support Vertex AI, Agent Runtime, Gemini 2.5 Flash, Cloud Run and Firestore.

Firestore's location is immutable once the database exists — the preflight
checks it before Terraform creates anything.

## Developing

```bash
# Agents
cd agents && uv sync --all-groups
GOOGLE_CLOUD_PROJECT=<project> uv run pytest tests/unit -q
GOOGLE_CLOUD_PROJECT=<project> uv run adk run sprtz_agents

# MCP servers
cd mcp && pip install -r requirements-dev.txt && python -m pytest tests -q

# API
cd api && ENVIRONMENT=local uvicorn app.main:app --reload
```

`ENVIRONMENT=local` bypasses IAP verification. It is set by Terraform in every
deployed environment, so that branch is unreachable in the cloud.

## Adding a sport

Copy `agents/sprtz_agents/sports/handball.py`, define the moment types and the
context a model needs to read the picture, and register the profile. Import it in
`sports/__init__.py`. Nothing else changes.

## Things worth knowing

- **Keep the per-segment prompt short.** Inlining the moment catalogue into it
  made Gemini emit a sequential counter instead of real timestamps — thirty
  "moments" inside three seconds. The catalogue belongs in the system
  instruction, where it also caches. `test_segment_prompt_stays_short` guards it.
- **The reranker can only reorder what retrieval found**, which is why search
  over-fetches 4x before ranking.
- **There is no `google_iap_brand` in the Terraform** on purpose — the IAP OAuth
  Admin APIs it depends on were shut down in March 2026.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the reasoning behind the
agent split, the segmentation strategy and the search design.
