"""Quota-aware facade for atomic store-backed task assignment."""

from __future__ import annotations

from .protocol import TaskLease, WorkerResult
from .store import SchedulerStore


class WorkerScheduler:
    """Routes queued tasks to an eligible registered worker."""

    def __init__(self, store: SchedulerStore, *, max_observation_age_seconds: int = 300) -> None:
        if type(max_observation_age_seconds) is not int or max_observation_age_seconds <= 0:
            raise ValueError("max_observation_age_seconds must be a positive integer")
        self._store = store
        self._max_observation_age_seconds = max_observation_age_seconds

    def dispatch_next(self, worker_id: str, now: float, lease_seconds: int) -> TaskLease | None:
        return self._store.claim_next(
            worker_id, now, lease_seconds, self._max_observation_age_seconds
        )

    def heartbeat(self, lease_id: str, now: float, lease_seconds: int) -> TaskLease:
        return self._store.heartbeat(lease_id, now, lease_seconds)

    def complete(self, lease_id: str, result: WorkerResult, now: float) -> None:
        self._store.complete(lease_id, result, now)

    def fail(self, lease_id: str, error: str, retryable: bool, now: float) -> None:
        self._store.fail(lease_id, error, retryable, now)

    def cancel(self, task_id: str, now: float) -> None:
        self._store.cancel(task_id, now)

