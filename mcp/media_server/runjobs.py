"""Cloud Run Jobs — the batch work this service starts and polls.

Two things run as jobs rather than in this container: the HLS download
(``jobs/hls2mp4``, a Rust binary that streams a whole VOD playlist into one
object) and the live capture (``media_server.live_capture``, which follows a
live playlist for hours). Both are the wrong shape for a request-driven
service — a request has a one-hour ceiling and a live event is longer — and
the right shape for a job, which runs to completion with a deadline of a day.

Same pattern as Transcoder: start returns as soon as the execution is
accepted, and the caller polls. The client is imported inside the functions
for the usual reason: the Google libraries cost the better part of two minutes
to import on Cloud Run and the server must answer its health check first.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def run(job_name: str, env: dict[str, str]) -> str:
    """Start one execution of ``job_name`` with ``env`` laid over its template.

    Returns the execution's resource name, which is the handle
    :func:`execution_state` polls. ``job_name`` is the full
    ``projects/…/locations/…/jobs/…`` form Terraform publishes.
    """
    from google.cloud import run_v2

    request = run_v2.RunJobRequest(
        name=job_name,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name=k, value=v) for k, v in env.items()],
                )
            ],
            task_count=1,
        ),
    )
    operation = run_v2.JobsClient().run_job(request=request)
    # The long-running operation's metadata is the Execution, and its name is
    # known as soon as the request is accepted — there is no need to wait for
    # the operation, which only completes when the execution does.
    execution = operation.metadata
    name = getattr(execution, "name", "") or ""
    if not name:
        raise RuntimeError(f"run_job on {job_name} returned no execution name")
    logger.info("started %s", name)
    return name


def qualify(execution_name: str, job_name: str) -> str:
    """The full resource name of an execution, given possibly only its id.

    A Cloud Run Job sees itself as ``CLOUD_RUN_EXECUTION``, which is the bare
    id; the API wants ``projects/…/jobs/…/executions/<id>`` and, handed the
    bare id, reads it as a project — "Permission denied on resource project
    sprtz-dev-live-capture-mp5hp". ``job_name`` is the job's full name, from
    which the execution's is one segment further.
    """
    name = (execution_name or "").strip()
    if not name or "/" in name:
        return name
    if not job_name:
        return name
    return f"{job_name.rstrip('/')}/executions/{name}"


def execution_state(execution_name: str) -> dict[str, Any]:
    """Where an execution is: ``running``, ``succeeded`` or ``failed``.

    A task that failed and one that was cancelled are both ``failed`` here;
    the caller cares whether the output exists, and in neither case does it.
    """
    from google.cloud import run_v2

    execution = run_v2.ExecutionsClient().get_execution(name=execution_name)
    completed = bool(getattr(execution, "completion_time", None)) and \
        execution.completion_time.timestamp() > 0
    if not completed:
        return {"state": "running", "execution": execution_name}
    if execution.succeeded_count >= 1 and execution.failed_count == 0:
        return {"state": "succeeded", "execution": execution_name}
    return {
        "state": "failed", "execution": execution_name,
        "failed_count": int(execution.failed_count),
        "cancelled_count": int(getattr(execution, "cancelled_count", 0) or 0),
    }


def cancel(execution_name: str) -> None:
    """Ask a running execution to stop. Already finished is not an error."""
    from google.cloud import run_v2

    run_v2.ExecutionsClient().cancel_execution(name=execution_name)


_SEGMENT_LINE = re.compile(r"^\[(\d+)/(\d+)\] Streaming segment")


def parse_segment_progress(text: str) -> tuple[int, int] | None:
    """``(done, total)`` from a download job's ``[n/N] Streaming segment`` line."""
    match = _SEGMENT_LINE.match(text or "")
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    return (done, total) if total > 0 else None


def execution_progress(execution_name: str) -> dict[str, Any] | None:
    """How far a download execution has got, read from its own log.

    The download streams the whole playlist into one object through a single
    resumable upload, so nothing is visible in the bucket until it finishes.
    What is visible is the job's log: one ``[n/N] Streaming segment`` line per
    segment. The newest one is the progress. Best effort — a log that has not
    caught up yet, or a query that fails, is ``None`` rather than an error,
    because the poll that asks is deciding whether to keep waiting, not
    whether the download is working.
    """
    from google.cloud import logging as cloud_logging

    short = execution_name.rsplit("/", 1)[-1]
    filter_ = (
        'resource.type="cloud_run_job" '
        f'AND labels."run.googleapis.com/execution_name"="{short}" '
        'AND textPayload:"Streaming segment"'
    )
    entries = cloud_logging.Client().list_entries(
        filter_=filter_, order_by=cloud_logging.DESCENDING, max_results=1)
    for entry in entries:
        parsed = parse_segment_progress(str(entry.payload or ""))
        if parsed:
            done, total = parsed
            return {"segments_done": done, "segments_total": total,
                    "fraction": min(1.0, done / total)}
    return None
