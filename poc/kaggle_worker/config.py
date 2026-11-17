"""Validated, provider-neutral operator configuration for Kite."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Callable


class ConfigError(ValueError):
    pass


ADAPTERS = {"opencode", "harness2", "kaggle"}
_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENV_REF = re.compile(r"^env:[A-Z][A-Z0-9_]{0,127}$")


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    adapter: str
    enabled: bool
    model: str
    capabilities: tuple[str, ...]
    quota_gpu_seconds: int
    concurrency: int
    secrets: dict[str, str]
    owner: str = "self"
    worker_id: str | None = None
    roles: tuple[str, ...] = ("coder",)
    models: tuple[str, ...] = ()

    @property
    def model_ids(self) -> tuple[str, ...]:
        """All models this worker may serve; ``model`` is always first."""
        return (self.model, *[m for m in self.models if m != self.model])

    @property
    def effective_worker_id(self) -> str:
        return self.worker_id or self.id


@dataclass(frozen=True)
class RoutingConfig:
    swap_penalty_per_second: float = 0.0
    load_seconds_cached: float = 60.0
    load_seconds_cold: float = 600.0
    default_skill: float = 0.0
    model_skill: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class KiteConfig:
    schema_version: int
    temporal_endpoint: str
    temporal_task_queue: str
    opencode_executable: str
    opencode_host: str
    opencode_port: int
    harness2_checkout: str
    harness2_cli: str
    providers: tuple[ProviderConfig, ...]
    source_path: Path | None = None
    routing: RoutingConfig = field(default_factory=RoutingConfig)


@dataclass(frozen=True)
class ProviderAdapter:
    adapter_id: str
    validate: Callable[[ProviderConfig], None]
    refresh: Callable[[ProviderConfig], dict[str, Any]]


LIVE_ADAPTERS: dict[str, ProviderAdapter] = {}


def register_provider_adapter(adapter: ProviderAdapter) -> None:
    """Register an implementation without changing the task/config schema."""
    if not _ID.fullmatch(adapter.adapter_id):
        raise ConfigError("adapter ID is malformed")
    if adapter.adapter_id == "kaggle":
        raise ConfigError("Kaggle is reserved as a disabled validation-only stub")
    LIVE_ADAPTERS[adapter.adapter_id] = adapter
    ADAPTERS.add(adapter.adapter_id)


def unregister_provider_adapter(adapter_id: str) -> None:
    LIVE_ADAPTERS.pop(adapter_id, None)
    if adapter_id not in {"opencode", "harness2", "kaggle"}:
        ADAPTERS.discard(adapter_id)


def refresh_configured_providers(config: KiteConfig) -> tuple[list[dict[str, Any]], list[str]]:
    """Only invoke implementations named by this config; never mutate enablement."""
    refreshed: list[dict[str, Any]] = []
    unknown: list[str] = []
    for provider in config.providers:
        adapter = LIVE_ADAPTERS.get(provider.adapter)
        if adapter is None:
            unknown.append(provider.id)
            continue
        try:
            adapter.validate(provider)
            observation = adapter.refresh(provider)
            if not isinstance(observation, dict):
                raise ConfigError("adapter refresh must return an object")
            refreshed.append({"provider_id": provider.id, "observation": observation})
        except Exception:
            # Adapter exception messages can contain credentials or endpoint data.
            unknown.append(provider.id)
    return refreshed, unknown


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    return value


def validate_config(raw: Any, *, known_adapters: set[str] | None = None) -> KiteConfig:
    root = _mapping(raw, "config")
    if root.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")
    temporal = _mapping(root.get("temporal"), "temporal")
    opencode = _mapping(root.get("opencode"), "opencode")
    server = _mapping(opencode.get("server"), "opencode.server")
    harness = _mapping(root.get("harness2"), "harness2")
    endpoint = temporal.get("endpoint")
    queue = temporal.get("task_queue")
    executable = opencode.get("executable")
    host = server.get("host")
    port = server.get("port")
    checkout, harness_cli = harness.get("checkout"), harness.get("cli")
    if not all(isinstance(v, str) and v.strip() for v in (endpoint, queue, executable, host, checkout, harness_cli)):
        raise ConfigError("Temporal, OpenCode and Harness² paths/endpoints must be non-empty strings")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("opencode.server.port must be between 1 and 65535")
    providers_raw = root.get("providers")
    if not isinstance(providers_raw, list):
        raise ConfigError("providers must be a list")
    known = ADAPTERS if known_adapters is None else known_adapters
    providers: list[ProviderConfig] = []
    ids: set[str] = set()
    for i, item in enumerate(providers_raw):
        p = _mapping(item, f"providers[{i}]")
        unknown_keys = set(p) - {"id", "adapter", "enabled", "model", "capabilities", "quota", "secrets",
                                 "owner", "worker_id", "roles", "models"}
        if unknown_keys:
            raise ConfigError(f"providers[{i}] has unsupported fields")
        pid, adapter, model = p.get("id"), p.get("adapter"), p.get("model")
        enabled = p.get("enabled", False)
        if not isinstance(pid, str) or not _ID.fullmatch(pid) or pid in ids:
            raise ConfigError(f"providers[{i}].id is malformed or duplicated")
        ids.add(pid)
        if adapter not in known:
            raise ConfigError(f"providers[{i}].adapter is unknown")
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise ConfigError(f"providers[{i}].model is malformed")
        if not isinstance(enabled, bool):
            raise ConfigError(f"providers[{i}].enabled must be boolean")
        caps = p.get("capabilities")
        if not isinstance(caps, list) or not caps or any(not isinstance(x, str) or not _CAPABILITY.fullmatch(x) for x in caps):
            raise ConfigError(f"providers[{i}].capabilities contains malformed values")
        quota = _mapping(p.get("quota"), f"providers[{i}].quota")
        seconds, concurrency = quota.get("gpu_seconds"), quota.get("concurrency")
        if not isinstance(seconds, int) or seconds < 0 or not isinstance(concurrency, int) or concurrency < 1:
            raise ConfigError(f"providers[{i}].quota values are invalid")
        secrets = _mapping(p.get("secrets", {}), f"providers[{i}].secrets")
        for key, ref in secrets.items():
            if not isinstance(key, str) or not _ID.fullmatch(key.replace("_", "-")):
                raise ConfigError(f"providers[{i}].secrets has an invalid key")
            if not isinstance(ref, str) or not (_ENV_REF.fullmatch(ref) or (ref.startswith("file:") and len(ref) > 5)):
                raise ConfigError(f"providers[{i}].secrets.{key} must be an env:NAME or file:PATH reference")
        if adapter == "kaggle" and not (secrets.get("token") or secrets.get("kaggle_token")):
            raise ConfigError("Kaggle requires a token secret reference, even while disabled")
        if adapter == "kaggle" and enabled:
            raise ConfigError("Kaggle adapter is a disabled schema stub and cannot be enabled")
        owner = p.get("owner", "self")
        if not isinstance(owner, str) or not _ID.fullmatch(owner):
            raise ConfigError(f"providers[{i}].owner is malformed")
        worker_id = p.get("worker_id")
        if worker_id is not None and (not isinstance(worker_id, str) or not _ID.fullmatch(worker_id)):
            raise ConfigError(f"providers[{i}].worker_id is malformed")
        roles = p.get("roles", ["coder"])
        if not isinstance(roles, list) or not roles or any(not isinstance(x, str) or not _CAPABILITY.fullmatch(x) for x in roles):
            raise ConfigError(f"providers[{i}].roles contains malformed values")
        extra_models = p.get("models", [])
        if not isinstance(extra_models, list) or any(not isinstance(x, str) or not _MODEL.fullmatch(x) for x in extra_models):
            raise ConfigError(f"providers[{i}].models contains malformed values")
        providers.append(ProviderConfig(
            pid, adapter, enabled, model, tuple(caps), seconds, concurrency, dict(secrets),
            owner=owner, worker_id=worker_id, roles=tuple(roles), models=tuple(extra_models),
        ))
    worker_ids = [p.effective_worker_id for p in providers]
    if len(set(worker_ids)) != len(worker_ids):
        raise ConfigError("provider worker IDs must be unique")
    routing = _validate_routing(root.get("routing", {}))
    return KiteConfig(1, endpoint, queue, executable, host, port, checkout, harness_cli,
                      tuple(providers), routing=routing)


def _number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise ConfigError(f"{label} must be a finite number")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{label} must be at least {minimum}")
    return float(value)


def _validate_routing(raw: Any) -> RoutingConfig:
    """Optional dispatcher settings: model quality per task type and model-switch cost."""
    routing = _mapping(raw, "routing")
    unknown = set(routing) - {"swap_penalty_per_second", "load_seconds_cached", "load_seconds_cold",
                              "default_skill", "model_skill"}
    if unknown:
        raise ConfigError("routing has unsupported fields")
    skill_raw = _mapping(routing.get("model_skill", {}), "routing.model_skill")
    skill: dict[str, dict[str, float]] = {}
    for task_type, scores in skill_raw.items():
        if not isinstance(task_type, str) or not _CAPABILITY.fullmatch(task_type):
            raise ConfigError("routing.model_skill task types must be lowercase identifiers")
        per_model = _mapping(scores, f"routing.model_skill.{task_type}")
        skill[task_type] = {}
        for model, score in per_model.items():
            if not isinstance(model, str) or not _MODEL.fullmatch(model):
                raise ConfigError(f"routing.model_skill.{task_type} has a malformed model ID")
            skill[task_type][model] = _number(score, f"routing.model_skill.{task_type}.{model}")
    return RoutingConfig(
        swap_penalty_per_second=_number(routing.get("swap_penalty_per_second", 0.0),
                                        "routing.swap_penalty_per_second", minimum=0),
        load_seconds_cached=_number(routing.get("load_seconds_cached", 60.0),
                                    "routing.load_seconds_cached", minimum=0),
        load_seconds_cold=_number(routing.get("load_seconds_cold", 600.0),
                                  "routing.load_seconds_cold", minimum=0),
        default_skill=_number(routing.get("default_skill", 0.0), "routing.default_skill"),
        model_skill=skill,
    )


def model_policy_from_config(config: KiteConfig):
    """Build the dispatcher's ModelPolicy from the operator config."""
    from .routing import ModelPolicy

    r = config.routing
    return ModelPolicy(
        skill={(task_type, model): score
               for task_type, scores in r.model_skill.items() for model, score in scores.items()},
        default_skill=r.default_skill,
        swap_penalty_per_second=r.swap_penalty_per_second,
        load_seconds_cached=r.load_seconds_cached,
        load_seconds_cold=r.load_seconds_cold,
    )


def sync_config_to_store(config: KiteConfig, store: Any, *, now: float) -> list[dict[str, Any]]:
    """Register each configured provider as an owner + worker in the scheduler store.

    Re-running is safe: an existing owner keeps its remaining quota (it is consumed by tasks
    and must not be reset by a sync) and an existing worker keeps its heartbeat state
    (readiness, loaded and cached models). A newly registered worker is not ready until its
    first heartbeat.
    """
    from .protocol import OwnerPolicy, WorkerProfile

    summary: list[dict[str, Any]] = []
    owners: dict[str, list[ProviderConfig]] = {}
    for provider in config.providers:
        owners.setdefault(provider.owner, []).append(provider)
    for owner_id, providers in owners.items():
        existing = store.get_owner_policy(owner_id)
        configured_quota = max(p.quota_gpu_seconds for p in providers)
        store.register_owner_policy(OwnerPolicy(
            owner_id=owner_id,
            owner_authorized=True,
            policy_approved=any(p.enabled for p in providers),
            max_concurrent_sessions=max(p.concurrency for p in providers),
            quota_remaining_seconds=(existing.quota_remaining_seconds if existing else configured_quota),
            quota_observed_at=(existing.quota_observed_at if existing else float(now)),
        ))
    for provider in config.providers:
        worker_id = provider.effective_worker_id
        previous = store.get_worker(worker_id)
        store.register_worker(WorkerProfile(
            worker_id=worker_id,
            owner_id=provider.owner,
            provider_id=provider.id,
            roles=provider.roles,
            model_ids=provider.model_ids,
            enabled=provider.enabled,
            provider_ready=previous.provider_ready if previous else False,
            status_observed_at=previous.status_observed_at if previous else float(now),
            max_concurrency=provider.concurrency,
            capabilities=provider.capabilities,
            loaded_model=previous.loaded_model if previous else None,
            cached_models=previous.cached_models if previous else (),
        ))
        summary.append({"worker_id": worker_id, "owner": provider.owner, "provider_id": provider.id,
                        "enabled": provider.enabled, "models": list(provider.model_ids),
                        "new": previous is None})
    return summary


def load_config(path: Path) -> KiteConfig:
    try:
        content = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            raw = json.loads(content)
        else:
            import yaml
            raw = yaml.safe_load(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config: {exc}") from exc
    except ImportError as exc:
        raise ConfigError("YAML support requires PyYAML; use JSON or install project test dependencies") from exc
    cfg = validate_config(raw)
    return KiteConfig(**{**cfg.__dict__, "source_path": path.resolve()})


def redact_config(config: KiteConfig) -> dict[str, Any]:
    """Return operator-safe metadata; secret values are never resolved here."""
    return {
        "schema_version": config.schema_version,
        "temporal": {"endpoint": config.temporal_endpoint, "task_queue": config.temporal_task_queue},
        "opencode": {"executable": config.opencode_executable, "server": {"host": config.opencode_host, "port": config.opencode_port}},
        "harness2": {"checkout": config.harness2_checkout, "cli": config.harness2_cli},
        "providers": [{"id": p.id, "adapter": p.adapter, "enabled": p.enabled, "model": p.model,
                       "models": list(p.model_ids), "owner": p.owner, "worker_id": p.effective_worker_id,
                       "roles": list(p.roles),
                       "capabilities": list(p.capabilities), "quota": {"gpu_seconds": p.quota_gpu_seconds, "concurrency": p.concurrency},
                       "secrets": {k: "<reference-redacted>" for k in p.secrets}} for p in config.providers],
        "routing": {"swap_penalty_per_second": config.routing.swap_penalty_per_second,
                    "load_seconds_cached": config.routing.load_seconds_cached,
                    "load_seconds_cold": config.routing.load_seconds_cold,
                    "default_skill": config.routing.default_skill,
                    "model_skill": config.routing.model_skill},
    }