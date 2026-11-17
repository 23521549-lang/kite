"""Provider-neutral worker and model selection policy."""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Mapping, Sequence

from .protocol import OwnerPolicy, WorkerProfile


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_nonnegative_int(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class RoutingRequest:
    role: str
    model_id: str | None
    estimated_gpu_seconds: int

    def __post_init__(self) -> None:
        _require_text("role", self.role)
        if self.model_id is not None:
            _require_text("model_id", self.model_id)
        if type(self.estimated_gpu_seconds) is not int or self.estimated_gpu_seconds <= 0:
            raise ValueError("estimated_gpu_seconds must be a positive integer")


@dataclass(frozen=True)
class WorkerLoad:
    active_sessions: int
    owner_active_sessions: int
    owner_reserved_gpu_seconds: int

    def __post_init__(self) -> None:
        _require_nonnegative_int("active_sessions", self.active_sessions)
        _require_nonnegative_int("owner_active_sessions", self.owner_active_sessions)
        _require_nonnegative_int("owner_reserved_gpu_seconds", self.owner_reserved_gpu_seconds)


@dataclass(frozen=True)
class RouteDecision:
    provider_id: str
    worker_id: str
    owner_id: str
    model_id: str
    reason: str


def select_worker(
    request: RoutingRequest,
    workers: Sequence[WorkerProfile],
    owners: Mapping[str, OwnerPolicy],
    loads: Mapping[str, WorkerLoad],
    *,
    now: float,
    max_observation_age_seconds: int = 300,
) -> RouteDecision | None:
    """Choose the least-loaded eligible worker and model for a task.

    Worker and owner load is a caller-supplied snapshot. Callers which persist a
    lease must recheck the snapshot and reservation in one atomic transaction.
    """
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now must be a finite timestamp")
    if type(max_observation_age_seconds) is not int or max_observation_age_seconds <= 0:
        raise ValueError("max_observation_age_seconds must be a positive integer")

    eligible: list[tuple[tuple[float, float, str], WorkerProfile, OwnerPolicy, str]] = []
    for worker in workers:
        owner = owners.get(worker.owner_id)
        load = loads.get(worker.worker_id)
        if owner is None or load is None:
            continue
        if not worker.enabled or not worker.provider_ready:
            continue
        if request.role not in worker.roles:
            continue
        if worker.status_observed_at > now or now - worker.status_observed_at > max_observation_age_seconds:
            continue
        if not owner.owner_authorized or not owner.policy_approved:
            continue
        if owner.quota_remaining_seconds is None:
            continue
        if owner.quota_observed_at > now or now - owner.quota_observed_at > max_observation_age_seconds:
            continue
        if load.active_sessions >= worker.max_concurrency:
            continue
        if load.owner_active_sessions >= owner.max_concurrent_sessions:
            continue
        available_quota = owner.quota_remaining_seconds - load.owner_reserved_gpu_seconds
        if request.estimated_gpu_seconds > available_quota:
            continue

        if request.model_id is not None:
            if request.model_id not in worker.model_ids:
                continue
            model_id = request.model_id
        else:
            model_id = worker.model_ids[0]
        load_score = (
            load.active_sessions / worker.max_concurrency,
            load.owner_active_sessions / owner.max_concurrent_sessions,
            worker.worker_id,
        )
        eligible.append((load_score, worker, owner, model_id))

    if not eligible:
        return None

    _, worker, owner, model_id = min(eligible, key=lambda entry: entry[0])
    reason = (
        f"selected {worker.provider_id}/{worker.worker_id} with model {model_id}: "
        "role, health, authorization, quota, and concurrency requirements are satisfied"
    )
    return RouteDecision(
        provider_id=worker.provider_id,
        worker_id=worker.worker_id,
        owner_id=owner.owner_id,
        model_id=model_id,
        reason=reason,
    )
