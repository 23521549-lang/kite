"""Review-only adapter for results returned by isolated model workers."""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import WorkerResult


class StaleWorkerResult(ValueError):
    """Raised when a worker result no longer belongs to the expected task state."""


@dataclass(frozen=True)
class ReviewBundle:
    task_id: str
    lease_id: str
    worker_id: str
    base_repo_head: str
    patch: str
    summary: str
    usage_gpu_seconds: int


def build_review_bundle(
    result: WorkerResult,
    *,
    expected_repo_head: str,
    expected_task_id: str,
    expected_lease_id: str,
    expected_worker_id: str,
) -> ReviewBundle:
    """Validate worker identity and return its patch as a reviewable value.

    This function deliberately has no repository, filesystem, git, or provider
    dependencies. Applying a patch and authorizing any push are separate steps.
    """
    expected = {
        "task_id": expected_task_id,
        "lease_id": expected_lease_id,
        "worker_id": expected_worker_id,
        "HEAD": expected_repo_head,
    }
    actual = {
        "task_id": result.task_id,
        "lease_id": result.lease_id,
        "worker_id": result.worker_id,
        "HEAD": result.base_repo_head,
    }
    for field, value in expected.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"expected {field} must be a non-empty string")
        if actual[field] != value:
            raise StaleWorkerResult(
                f"worker result {field} mismatch: expected {value!r}, got {actual[field]!r}"
            )
    return ReviewBundle(
        task_id=result.task_id,
        lease_id=result.lease_id,
        worker_id=result.worker_id,
        base_repo_head=result.base_repo_head,
        patch=result.patch,
        summary=result.summary,
        usage_gpu_seconds=result.usage_gpu_seconds,
    )
