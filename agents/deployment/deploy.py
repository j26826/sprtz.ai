"""Create or update the Sprtz agent on Vertex AI Agent Runtime.

This script owns the engine's whole lifecycle, deliberately.

An earlier design had Terraform create the engine from a seed source archive
and this script update it afterwards. That cannot work: Terraform's
`google_vertex_ai_reasoning_engine` creates the engine with
`spec.deployment_source`, while the SDK's `update()` sends `spec.package_spec`,
and the API refuses to move an engine from one to the other —

    Cannot update the agent engine deployed with spec.deployment_source to use
    spec.package_spec. Please continue using spec.deployment_source for updates.

So the engine is not a Terraform resource. Terraform publishes the display name
(`agent_display_name`) that both sides agree on; this script looks the engine up
by that name, creates it when absent and updates it when present. Nothing has to
discover an id the other invented.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("deploy")

# Agent Runtime injects these and rejects the request if they are supplied.
RESERVED_ENV = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--service-account", default="")
    parser.add_argument("--logs-bucket", default="")
    parser.add_argument("--commit", default="")
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Environment variable for the deployed agent. Repeatable.",
    )
    parser.add_argument("--requirements", default="requirements.txt")
    return parser.parse_args()


def build_env(pairs: list[str], logs_bucket: str, commit: str) -> dict[str, str]:
    env: dict[str, str] = {"GOOGLE_GENAI_USE_VERTEXAI": "True"}
    for pair in pairs:
        key, _, value = pair.partition("=")
        key = key.strip()
        if key and value:
            env[key] = value
    if logs_bucket:
        env["LOGS_BUCKET_NAME"] = logs_bucket
    if commit:
        env["COMMIT_SHA"] = commit

    clashes = sorted(set(env) & set(RESERVED_ENV))
    if clashes:
        raise SystemExit(
            f"Agent Runtime reserves {', '.join(clashes)}; remove them from --env."
        )
    return env


def read_requirements(path: str) -> list[str]:
    """Read a requirements file, dropping comments and editable installs.

    The comment test must run on the *stripped* line. `uv export` indents its
    provenance comments — "    # via aiohttp" — so testing the raw line lets
    them through as requirements, and the Agent Runtime SDK then reports
    "Failed to parse constraint: # via aiohttp" for each one.
    """
    requirements: list[str] = []
    with open(path) as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith(("#", "-e")):
                continue
            # An inline "pkg==1.0  # via x" comment is not a constraint either.
            requirements.append(line.split("#", 1)[0].strip())
    return [r for r in requirements if r]


def find_existing(agent_engines, display_name: str):
    """Return the engine with this display name, or None.

    Matched exactly rather than by the server-side filter alone, so a name that
    is a prefix of another cannot be updated by mistake.
    """
    try:
        matches = [
            engine
            for engine in agent_engines.list(filter=f'display_name="{display_name}"')
            if getattr(engine, "display_name", None) == display_name
        ]
    except Exception:
        logger.exception("could not list existing agent engines")
        raise

    if len(matches) > 1:
        raise SystemExit(
            f"{len(matches)} engines share the display name {display_name!r}. "
            "Delete the duplicates before deploying."
        )
    return matches[0] if matches else None


def main() -> int:
    args = parse_args()

    import vertexai
    from vertexai import agent_engines

    vertexai.init(
        project=args.project,
        location=args.location,
        staging_bucket=f"gs://{args.logs_bucket}" if args.logs_bucket else None,
    )

    # Imported after vertexai.init so the agent resolves the right project.
    from sprtz_agents.agent_runtime_app import agent_runtime

    env_vars = build_env(args.env, args.logs_bucket, args.commit)
    requirements = read_requirements(args.requirements)

    common = {
        "requirements": requirements,
        "extra_packages": ["sprtz_agents"],
        "env_vars": env_vars,
        "display_name": args.display_name,
        "description": "Sportscut sports video analysis agent.",
    }
    if args.service_account:
        common["service_account"] = args.service_account

    existing = find_existing(agent_engines, args.display_name)

    try:
        if existing is None:
            logger.info(
                "creating %r with %d requirements", args.display_name, len(requirements)
            )
            remote = agent_engines.create(agent_engine=agent_runtime, **common)
        else:
            logger.info(
                "updating %s with %d requirements", existing.resource_name, len(requirements)
            )
            remote = existing.update(agent_engine=agent_runtime, **common)
    except Exception:
        logger.exception("deployment failed")
        return 1

    logger.info("deployed %s", getattr(remote, "resource_name", args.display_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
