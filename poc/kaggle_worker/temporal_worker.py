"""Temporal worker construction and runtime entry point."""

from __future__ import annotations

from collections.abc import Sequence

from temporalio.client import Client
from temporalio.worker import Worker

from .temporal_workflow import CodingTaskWorkflow

DEFAULT_TASK_QUEUE = "kite-coding-tasks"
DEFAULT_TEMPORAL_ENDPOINT = "127.0.0.1:7233"


def build_worker(
    client: Client,
    activities: Sequence[object],
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
) -> Worker:
    """Build a worker without starting any model/provider process."""
    if not task_queue.strip():
        raise ValueError("task_queue must be non-empty")
    if not activities:
        raise ValueError("at least one activity implementation is required")
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[CodingTaskWorkflow],
        activities=list(activities),
    )


async def run_worker(
    activities: Sequence[object],
    *,
    endpoint: str = DEFAULT_TEMPORAL_ENDPOINT,
    task_queue: str = DEFAULT_TASK_QUEUE,
) -> None:
    """Connect to Temporal and serve the Kite task queue."""
    if not endpoint.strip():
        raise ValueError("endpoint must be non-empty")
    client = await Client.connect(endpoint)
    worker = build_worker(client, activities, task_queue=task_queue)
    await worker.run()
