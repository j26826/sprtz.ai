"""Deploy the Sprtz agent package to an existing Agent Runtime resource.

Terraform creates the resource with a placeholder package and then ignores
source changes, so this script owns the code from the first deploy onward. It
updates in place rather than creating a new resource, because the resource name
is baked into the API service's configuration.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("deploy")


RESERVED_ENV = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


def normalize_resource_name(value: str, project: str, location: str) -> str:
    """Return a fully-qualified reasoningEngines path.

    Terraform's `name` attribute for google_vertex_ai_reasoning_engine is not
    guaranteed to be the full path — depending on provider version it can be a
    bare numeric id, or a path keyed by project number rather than id. The SDK
    only accepts the full form, so normalize here instead of discovering the
    difference during a deploy.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("No Agent Runtime resource name configured.")
    if value.startswith("projects/"):
        return value
    return f"projects/{project}/locations/{location}/reasoningEngines/{value.rsplit('/', 1)[-1]}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument(
        "--resource-name",
        required=True,
        help="Full Agent Runtime resource name from the Terraform output.",
    )
    parser.add_argument("--service-account", default="")
    parser.add_argument("--logs-bucket", default="")
    parser.add_argument("--commit", default="")
    parser.add_argument(
        "--requirements",
        default="requirements.txt",
        help="Requirements file exported by the build.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    import vertexai
    from vertexai import agent_engines

    vertexai.init(
        project=args.project,
        location=args.location,
        staging_bucket=f"gs://{args.logs_bucket}" if args.logs_bucket else None,
    )

    # Imported after vertexai.init so the agent picks up the right project.
    from sprtz_agents.agent_runtime_app import agent_runtime

    # Agent Runtime reserves GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION and
    # rejects the update outright if either is supplied. It injects both itself,
    # and sprtz_agents.config reads them from the environment either way. The
    # same constraint applies to the Terraform resource — keep the two in step.
    env_vars = {
        "GOOGLE_GENAI_USE_VERTEXAI": "True",
    }
    if args.logs_bucket:
        env_vars["LOGS_BUCKET_NAME"] = args.logs_bucket
    if args.commit:
        env_vars["COMMIT_SHA"] = args.commit

    clashes = sorted(set(env_vars) & set(RESERVED_ENV))
    if clashes:
        raise SystemExit(
            f"Agent Runtime reserves {', '.join(clashes)}; remove them from env_vars."
        )

    with open(args.requirements) as handle:
        requirements = [
            line.strip()
            for line in handle
            if line.strip() and not line.startswith(("#", "-e"))
        ]

    resource_name = normalize_resource_name(args.resource_name, args.project, args.location)
    logger.info("updating %s with %d requirements", resource_name, len(requirements))

    try:
        remote = agent_engines.get(resource_name)
    except Exception:
        logger.exception(
            "could not read %s. Terraform creates this resource; run terraform "
            "apply before deploying.",
            resource_name,
        )
        return 1

    try:
        remote.update(
            agent_engine=agent_runtime,
            requirements=requirements,
            extra_packages=["sprtz_agents"],
            env_vars=env_vars,
            display_name="sprtz-producer",
            description="Sprtz AI sports video analysis agent.",
        )
    except Exception:
        logger.exception("deployment failed")
        return 1

    logger.info("deployed %s", resource_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
