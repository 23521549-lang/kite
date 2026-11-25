"""Provider-neutral types for on-demand model workers and scheduling."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from pathlib import Path
from typing import Mapping, Protocol


def _require_text(field_name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_finite_time(field_name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite number")


def _require_positive_int(field_name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_nonnegative_int(field_name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _require_capabilities(field_name: str, values: object) -> None:
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{field_name} must be a non-empty tuple of strings")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{field_name} must contain only non-empty strings")


def _require_optional_names(field_name: str, values: object) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be a tuple of strings")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{field_name} must contain only non-empty strings")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


class WorkerStatus(str, Enum):
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class TaskState(str, Enum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    system_prompt: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model_id: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckpointRef:
    task_id: str
    path: Path


@dataclass(frozen=True)
class WorkerProfile:
    worker_id: str
    owner_id: str
    provider_id: str
    roles: tuple[str, ...]
    model_ids: tuple[str, ...]
    enabled: bool
    provider_ready: bool
    status_observed_at: float
    max_concurrency: int
    # Dispatcher metadata. Defaults keep older callers and stored rows valid.
    capabilities: tuple[str, ...] = ()
    loaded_model: str | None = None
    cached_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("worker_id", self.worker_id)
        _require_text("owner_id", self.owner_id)
        _require_text("provider_id", self.provider_id)
        _require_capabilities("roles", self.roles)
        _require_capabilities("model_ids", self.model_ids)
        _require_optional_names("capabilities", self.capabilities)
        _require_optional_names("cached_models", self.cached_models)
        if self.loaded_model is not None:
            _require_text("loaded_model", self.loaded_model)
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if type(self.provider_ready) is not bool:
            raise ValueError("provider_ready must be a boolean")
        _require_finite_time("status_observed_at", self.status_observed_at)
        _require_positive_int("max_concurrency", self.max_concurrency)


@dataclass(frozen=True)
class OwnerPolicy:
    owner_id: str
    owner_authorized: bool
    policy_approved: bool
    max_concurrent_sessions: int
    quota_remaining_seconds: int | None
    quota_observed_at: float

    def __post_init__(self) -> None:
        _require_text("owner_id", self.owner_id)
        if type(self.owner_authorized) is not bool:
            raise ValueError("owner_authorized must be a boolean")
        if type(self.policy_approved) is not bool:
            raise ValueError("policy_approved must be a boolean")
        _require_positive_int("max_concurrent_sessions", self.max_concurrent_sessions)
        if self.quota_remaining_seconds is not None:
            _require_nonnegative_int("quota_remaining_seconds", self.quota_remaining_seconds)
        _require_finite_time("quota_observed_at", self.quota_observed_at)


@dataclass(frozen=True)
class ScheduledTask:
    task_id: str
    role: str
    model_id: str | None
    repo_head: str
    branch: str
    payload_json: str
    estimated_gpu_seconds: int
    priority: int
    created_at: float
    task_type: str = "coding"
    required_capabilities: tuple[str, ...] = ()
    # Gói 3: origin stamp, set once at creation; None = unknown origin (old rows).
    chat_session_id: str | None = None
    environment_id: str | None = None

    def __post_init__(self) -> None:
        _require_text("task_id", self.task_id)
        _require_text("role", self.role)
        _require_text("task_type", self.task_type)
        _require_optional_names("required_capabilities", self.required_capabilities)
        if self.chat_session_id is not None:
            _require_text("chat_session_id", self.chat_session_id)
        if self.environment_id is not None:
            _require_text("environment_id", self.environment_id)
        if self.model_id is not None:
            _require_text("model_id", self.model_id)
        _require_text("repo_head", self.repo_head)
        _require_text("branch", self.branch)
        if not isinstance(self.payload_json, str):
            raise ValueError("payload_json must be a JSON object encoded as a string")
        try:
            payload = json.loads(self.payload_json, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError("payload_json must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("payload_json must encode a JSON object")
        _require_positive_int("estimated_gpu_seconds", self.estimated_gpu_seconds)
        if type(self.priority) is not int:
            raise ValueError("priority must be an integer")
        _require_finite_time("created_at", self.created_at)


@dataclass(frozen=True)
class TaskLease:
    lease_id: str
    task_id: str
    worker_id: str
    owner_id: str
    expires_at: float

    def __post_init__(self) -> None:
        _require_text("lease_id", self.lease_id)
        _require_text("task_id", self.task_id)
        _require_text("worker_id", self.worker_id)
        _require_text("owner_id", self.owner_id)
        _require_finite_time("expires_at", self.expires_at)


@dataclass(frozen=True)
class WorkerResult:
    task_id: str
    lease_id: str
    worker_id: str
    base_repo_head: str
    patch: str
    summary: str
    usage_gpu_seconds: int

    def __post_init__(self) -> None:
        _require_text("task_id", self.task_id)
        _require_text("lease_id", self.lease_id)
        _require_text("worker_id", self.worker_id)
        _require_text("base_repo_head", self.base_repo_head)
        _require_text("patch", self.patch)
        _require_text("summary", self.summary)
        _require_nonnegative_int("usage_gpu_seconds", self.usage_gpu_seconds)


class WorkerProvider(Protocol):
    def start(self, task_id: str) -> str: ...

    def status(self, session_id: str) -> WorkerStatus: ...

    def invoke(self, session_id: str, request: ModelRequest) -> ModelResponse: ...

    def stop(self, session_id: str) -> None: ...