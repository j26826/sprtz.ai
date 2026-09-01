# sprtz-agents

ADK agents for Sprtz AI, deployed to Vertex AI Agent Runtime.

- `sprtz_agents/agent.py` — `sprtz_producer` root agent and the `analysis_pipeline`
- `sprtz_agents/sub_agents/stages.py` — the five pipeline stages
- `sprtz_agents/sports/` — per-sport moment taxonomies and the Gemini analysis prompt
- `sprtz_agents/tools/` — segmented analysis, clip planning, MCP access

## Local

```bash
uv sync --all-groups
export GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=us-south1
uv run pytest
uv run adk run sprtz_agents
```

See `docs/ARCHITECTURE.md` for how the pieces fit together.
