"""The model object, and the location it has to be called from.

A model and its location travel as a pair in this project: the Flash
generations after 2.5 are served only through Vertex's `global` location, and a
regional endpoint answers 404 for them rather than falling back.

The analysis and the reranker each build their own `genai.Client`, so each
already carries its own location. The root agent and the stage agents build no
client at all — ADK builds one for them out of the environment, and on Agent
Runtime that environment's `GOOGLE_CLOUD_LOCATION` is injected by the platform,
which refuses a deployment that tries to set it (`deploy.py` checks for exactly
that). So there is no environment variable that can move them.

ADK's own answer is to hand it a model whose client is pinned, which is what
this is. With `SPRTZ_MODEL_LOCATION` unset, or equal to the engine's own
region, the plain model object is returned and nothing about the call changes.
"""

from __future__ import annotations

from functools import cached_property

from google.adk.models import Gemini
from google.genai import Client, types

from sprtz_agents.config import get_settings


class PinnedGemini(Gemini):
    """A Gemini asked from a location of its own rather than the engine's."""

    location: str = ""

    @cached_property
    def api_client(self) -> Client:
        settings = get_settings()
        return Client(vertexai=True, project=settings.project_id, location=self.location)


def gemini(model: str = "", *, attempts: int = 3) -> Gemini:
    """The engine's model, called from wherever it is served.

    `model` defaults to the engine's own. The pinned form is used only when a
    location has actually been asked for and differs from the engine's region,
    so nothing changes for a deployment that sets neither.
    """
    settings = get_settings()
    built = {
        "model": model or settings.model,
        "retry_options": types.HttpRetryOptions(attempts=attempts),
    }
    where = settings.model_location
    if where and where != settings.location:
        return PinnedGemini(location=where, **built)
    return Gemini(**built)
