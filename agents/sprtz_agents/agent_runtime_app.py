"""Agent Runtime entrypoint.

Terraform points the Agent Runtime resource at ``agent_runtime`` in this module
(see deploy/terraform/agent_runtime.tf).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import vertexai
from dotenv import load_dotenv
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.cloud import logging as google_cloud_logging
from pydantic import BaseModel, Field
from vertexai.agent_engines.templates.adk import AdkApp

from sprtz_agents.agent import app as adk_app

load_dotenv()

logger = logging.getLogger(__name__)


class Feedback(BaseModel):
    """Editor feedback on a suggested clip, logged for the quality flywheel."""

    job_id: str
    clip_id: str | None = None
    score: float = Field(ge=0.0, le=1.0)
    action: str = Field(description="What the editor did: kept, discarded, re-cut, published.")
    comment: str = ""
    log_type: str = "feedback"
    service_name: str = "sprtz-producer"


class SprtzAgentApp(AdkApp):
    def set_up(self) -> None:
        vertexai.init()
        super().set_up()
        logging.basicConfig(level=logging.INFO)
        self._logging_client = google_cloud_logging.Client()
        self._logger = self._logging_client.logger(__name__)

    def register_feedback(self, feedback: dict[str, Any]) -> None:
        """Record what the editor did with a suggestion.

        Which clips get discarded is the strongest available signal on whether
        the scoring priors in the sport profile are right.
        """
        payload = Feedback.model_validate(feedback)
        self._logger.log_struct(payload.model_dump(), severity="INFO")

    def register_operations(self) -> dict[str, list[str]]:
        operations = super().register_operations()
        operations[""] = [*operations.get("", []), "register_feedback"]
        return operations


_logs_bucket = os.environ.get("LOGS_BUCKET_NAME")

agent_runtime = SprtzAgentApp(
    app=adk_app,
    artifact_service_builder=lambda: (
        GcsArtifactService(bucket_name=_logs_bucket) if _logs_bucket else InMemoryArtifactService()
    ),
)
