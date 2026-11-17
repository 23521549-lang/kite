"""Validated, provider-neutral operator configuration for Kite."""
from __future__ import annotations

from dataclasses import dataclass
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
        unknown_keys = set(p) - {"id", "adapter", "enabled", "model", "capabilities", "quota", "secrets"}
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
        providers.append(ProviderConfig(pid, adapter, enabled, model, tuple(caps), seconds, concurrency, dict(secrets)))
    return KiteConfig(1, endpoint, queue, executable, host, port, checkout, harness_cli, tuple(providers))


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
                       "capabilities": list(p.capabilities), "quota": {"gpu_seconds": p.quota_gpu_seconds, "concurrency": p.concurrency},
                       "secrets": {k: "<reference-redacted>" for k in p.secrets}} for p in config.providers],
    }
