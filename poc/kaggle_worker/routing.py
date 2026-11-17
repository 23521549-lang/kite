"""Provider-neutral worker and model selection policy."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    task_type: str = "coding"
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("role", self.role)
        if self.model_id is not None:
            _require_text("model_id", self.model_id)
        if type(self.estimated_gpu_seconds) is not int or self.estimated_gpu_seconds <= 0:
            raise ValueError("estimated_gpu_seconds must be a positive integer")
        _require_text("task_type", self.task_type)
        if not isinstance(self.required_capabilities, tuple) or any(
            not isinstance(c, str) or not c.strip() for c in self.required_capabilities
        ):
            raise ValueError("required_capabilities must be a tuple of non-empty strings")


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
class ModelPolicy:
    """How the dispatcher trades model quality against the cost of loading a model.

    ``skill`` maps ``(task_type, model_id)`` to a quality score, ideally measured on the
    project's own tasks (for example a Harness² pass rate in [0, 1]). A pair without a score
    gets ``default_skill``. ``swap_penalty_per_second`` (lambda) converts load time into
    score units so a worker only switches model when the quality gain is worth the wait.
    """

    skill: Mapping[tuple[str, str], float] = field(default_factory=dict)
    default_skill: float = 0.0
    swap_penalty_per_second: float = 0.0
    load_seconds_cached: float = 60.0
    load_seconds_cold: float = 600.0

    def __post_init__(self) -> None:
        for (task_type, model_id), score in self.skill.items():
            _require_text("skill task_type", task_type)
            _require_text("skill model_id", model_id)
            _require_finite("skill score", score)
        for name in ("default_skill", "swap_penalty_per_second", "load_seconds_cached", "load_seconds_cold"):
            _require_finite(name, getattr(self, name))
        if self.swap_penalty_per_second < 0 or self.load_seconds_cached < 0 or self.load_seconds_cold < 0:
            raise ValueError("swap penalty and load times must be non-negative")

    def quality(self, task_type: str, model_id: str) -> tuple[float, bool]:
        key = (task_type, model_id)
        if key in self.skill:
            return float(self.skill[key]), True
        return self.default_skill, False

    def swap_seconds(self, worker: WorkerProfile, model_id: str) -> float:
        if worker.loaded_model == model_id:
            return 0.0
        if model_id in worker.cached_models:
            return self.load_seconds_cached
        return self.load_seconds_cold


@dataclass(frozen=True)
class RouteDecision:
    provider_id: str
    worker_id: str
    owner_id: str
    model_id: str
    reason: str
    requires_model_load: bool = False
    score: float | None = None


def _require_finite(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _eligible(
    request: RoutingRequest,
    worker: WorkerProfile,
    owner: OwnerPolicy | None,
    load: WorkerLoad | None,
    now: float,
    max_age: int,
) -> bool:
    if owner is None or load is None:
        return False
    if not worker.enabled or not worker.provider_ready:
        return False
    if request.role not in worker.roles:
        return False
    if not set(request.required_capabilities) <= set(worker.capabilities):
        return False
    if worker.status_observed_at > now or now - worker.status_observed_at > max_age:
        return False
    if not owner.owner_authorized or not owner.policy_approved:
        return False
    if owner.quota_remaining_seconds is None:
        return False
    if owner.quota_observed_at > now or now - owner.quota_observed_at > max_age:
        return False
    if load.active_sessions >= worker.max_concurrency:
        return False
    if load.owner_active_sessions >= owner.max_concurrent_sessions:
        return False
    available_quota = owner.quota_remaining_seconds - load.owner_reserved_gpu_seconds
    return request.estimated_gpu_seconds <= available_quota


def select_worker(
    request: RoutingRequest,
    workers: Sequence[WorkerProfile],
    owners: Mapping[str, OwnerPolicy],
    loads: Mapping[str, WorkerLoad],
    *,
    now: float,
    max_observation_age_seconds: int = 300,
    model_policy: ModelPolicy | None = None,
) -> RouteDecision | None:
    """Choose an eligible worker and model for a task.

    Without ``model_policy`` the least-loaded eligible worker wins and the requested (or first)
    model is used. With a policy each (worker, model) pair is scored as
    ``quality(task_type, model) - lambda * swap_seconds(worker, model)``; ties go to the
    less-loaded worker. Load is a caller-supplied snapshot: callers that persist a lease must
    recheck it and reserve in one atomic transaction.
    """
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now must be a finite timestamp")
    if type(max_observation_age_seconds) is not int or max_observation_age_seconds <= 0:
        raise ValueError("max_observation_age_seconds must be a positive integer")

    candidates: list[tuple[tuple, WorkerProfile, OwnerPolicy, str, float | None, str]] = []
    for worker in workers:
        owner = owners.get(worker.owner_id)
        load = loads.get(worker.worker_id)
        if not _eligible(request, worker, owner, load, now, max_observation_age_seconds):
            continue
        assert owner is not None and load is not None
        load_score = (
            load.active_sessions / worker.max_concurrency,
            load.owner_active_sessions / owner.max_concurrent_sessions,
        )
        if request.model_id is not None:
            if request.model_id not in worker.model_ids:
                continue
            models = (request.model_id,)
        else:
            models = worker.model_ids if model_policy is not None else worker.model_ids[:1]
        for model_id in models:
            if model_policy is None:
                key = (*load_score, worker.worker_id, model_id)
                candidates.append((key, worker, owner, model_id, None, "least-loaded eligible worker"))
                continue
            quality, measured = model_policy.quality(request.task_type, model_id)
            swap = model_policy.swap_seconds(worker, model_id)
            score = quality - model_policy.swap_penalty_per_second * swap
            note = (
                f"{request.task_type} quality {quality:g} ({'measured' if measured else 'default'}), "
                f"load {swap:g}s, score {score:g}"
            )
            key = (-score, *load_score, worker.worker_id, model_id)
            candidates.append((key, worker, owner, model_id, score, note))

    if not candidates:
        return None

    _, worker, owner, model_id, score, note = min(candidates, key=lambda entry: entry[0])
    reason = (
        f"selected {worker.provider_id}/{worker.worker_id} with model {model_id}: {note}; "
        "role, capabilities, health, authorization, quota, and concurrency requirements are satisfied"
    )
    return RouteDecision(
        provider_id=worker.provider_id,
        worker_id=worker.worker_id,
        owner_id=owner.owner_id,
        model_id=model_id,
        reason=reason,
        requires_model_load=worker.loaded_model != model_id,
        score=score,
    )