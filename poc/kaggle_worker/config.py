"""Validated, provider-neutral operator configuration for Kite."""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Any, Callable


class ConfigError(ValueError):
    pass


# "pull" = a worker that long-polls the Kite gateway (for example a Kaggle notebook).
ADAPTERS = {"opencode", "harness2", "kaggle", "pull", "relay"}
_BUILTIN_ADAPTERS = {"opencode", "harness2", "kaggle", "pull", "relay"}
_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENV_REF = re.compile(r"^env:[A-Z][A-Z0-9_]{0,127}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,199}$")
_NETWORK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_MEMORY = re.compile(r"^[1-9][0-9]*[kmg]$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


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
    # Autostart (pull workers on Kaggle): Kite runs the owner's notebook when work arrives. See launcher.py.
    launch: "LaunchConfig | None" = None
    # Adapter "relay": Kite calls the provider itself (paid API, a server by URL). See relay.py.
    relay: "RelayConfig | None" = None

    @property
    def model_ids(self) -> tuple[str, ...]:
        """All models this worker may serve; ``model`` is always first."""
        return (self.model, *[m for m in self.models if m != self.model])

    @property
    def effective_worker_id(self) -> str:
        return self.worker_id or self.id


@dataclass(frozen=True)
class PodConfig:
    """A rented GPU machine Kite starts when work arrives and stops when idle (pods.py)."""
    pod_id: str                          # the pod's id in the provider's console (RunPod: Pods page)
    provider: str = "runpod"
    port: int = 8000                     # the model server's HTTP port, exposed through the provider's proxy
    price_hour: float | None = None      # USD per hour while the pod runs; None: the price the provider reports
    idle_minutes: int = 30               # stop the pod after this long without a job
    start_timeout: float = 900.0         # seconds from "start" until the model server answers


@dataclass(frozen=True)
class RelayConfig:
    kind: str                            # openai (any OpenAI-compatible API) | anthropic
    base_url: str
    upstream: tuple[tuple[str, str], ...] = ()     # (Kite model name, the provider's model name)
    price_in: float = 0.0                # USD per million prompt tokens (0: free, e.g. your own server)
    price_out: float = 0.0               # USD per million completion tokens
    daily_cap: float | None = None       # USD; at the cap the worker stops taking jobs until 00:00 UTC
    monthly_cap: float | None = None     # USD; until the 1st (UTC)
    timeout: float = 600.0
    # Read the answer as it is generated (SSE) and pass it on to a streaming client. Always on for a
    # pod: RunPod's proxy closes a request that has sent nothing for 100 seconds.
    stream: bool = False
    pod: PodConfig | None = None


POD_PROVIDERS = ("runpod",)
_POD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")


def _validate_pod(raw: Any, where: str, secrets: dict) -> PodConfig:
    pod = _mapping(raw, where)
    unknown = set(pod) - {"provider", "id", "port", "price_hour", "idle_minutes", "start_timeout_seconds"}
    if unknown:
        raise ConfigError(f"{where} has unsupported fields: {', '.join(sorted(unknown))}")
    provider = pod.get("provider", "runpod")
    if provider not in POD_PROVIDERS:
        raise ConfigError(f"{where}.provider must be one of {', '.join(POD_PROVIDERS)}")
    pod_id = pod.get("id")
    if not isinstance(pod_id, str) or not _POD_ID.fullmatch(pod_id):
        raise ConfigError(f"{where}.id must be the pod id from the provider's console")
    port = pod.get("port", 8000)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"{where}.port must be between 1 and 65535")
    price = pod.get("price_hour")
    if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0):
        raise ConfigError(f"{where}.price_hour must be a non-negative number of USD")
    idle = pod.get("idle_minutes", 30)
    if not isinstance(idle, int) or isinstance(idle, bool) or not 5 <= idle <= 720:
        raise ConfigError(f"{where}.idle_minutes must be between 5 and 720")
    start = _number(pod.get("start_timeout_seconds", 900), f"{where}.start_timeout_seconds", minimum=60)
    if start > 3600:
        raise ConfigError(f"{where}.start_timeout_seconds must be at most 3600")
    if not secrets.get("runpod_api_key"):
        raise ConfigError(f"{where}: a pod needs secrets.runpod_api_key (RunPod API key, scope pods:write)")
    return PodConfig(pod_id, provider, port, None if price is None else float(price), idle, float(start))


_PRIVATE_HOST = re.compile(r"^(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
                           r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|host\.docker\.internal)$")


def _validate_relay(raw: Any, where: str, secrets: dict, models: tuple[str, ...]) -> RelayConfig:
    from urllib.parse import urlparse

    r = _mapping(raw, where)
    unknown = set(r) - {"kind", "base_url", "upstream", "price", "caps", "timeout_seconds", "stream", "pod"}
    if unknown:
        raise ConfigError(f"{where} has unsupported fields: {', '.join(sorted(unknown))}")
    kind = r.get("kind")
    if kind not in ("openai", "anthropic"):
        raise ConfigError(f"{where}.kind must be openai or anthropic")
    pod = _validate_pod(r["pod"], f"{where}.pod", secrets) if r.get("pod") is not None else None
    if pod is not None and kind != "openai":
        raise ConfigError(f"{where}: a pod serves an OpenAI-compatible API (vLLM, llama-server): kind openai")
    stream = r.get("stream", False)
    if not isinstance(stream, bool):
        raise ConfigError(f"{where}.stream must be true or false")
    if stream and kind != "openai":
        raise ConfigError(f"{where}.stream is for OpenAI-compatible APIs")
    base_url = r.get("base_url") or ("https://api.anthropic.com" if kind == "anthropic" else None)
    if pod is not None and not base_url:
        base_url = f"https://{pod.pod_id}-{pod.port}.proxy.runpod.net/v1"
    url = urlparse(base_url) if isinstance(base_url, str) else None
    if url is None or url.scheme not in ("https", "http") or not url.hostname:
        raise ConfigError(f"{where}.base_url must be an http(s) URL")
    if url.scheme == "http" and not _PRIVATE_HOST.match(url.hostname):
        raise ConfigError(f"{where}.base_url must use https outside this machine and private networks "
                          "(the API key would travel in clear text)")
    if kind == "anthropic" and not secrets.get("api_key"):
        raise ConfigError(f"{where}: Anthropic needs secrets.api_key")
    upstream = _mapping(r.get("upstream", {}), f"{where}.upstream")
    for name, target in upstream.items():
        if name not in models:
            raise ConfigError(f"{where}.upstream names {name}, which is not one of this provider's models")
        if not isinstance(target, str) or not target.strip() or len(target) > 200:
            raise ConfigError(f"{where}.upstream.{name} must be the provider's model name")
    price = _mapping(r.get("price", {}), f"{where}.price")
    caps = _mapping(r.get("caps", {}), f"{where}.caps")
    numbers = {}
    for key, value in (("price_in", price.get("input", 0)), ("price_out", price.get("output", 0)),
                       ("daily_cap", caps.get("daily_usd")), ("monthly_cap", caps.get("monthly_usd")),
                       ("timeout", r.get("timeout_seconds", 600))):
        if value is None:
            numbers[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"{where}: {key} must be a non-negative number")
        numbers[key] = float(value)
    if set(price) - {"input", "output"} or set(caps) - {"daily_usd", "monthly_usd"}:
        raise ConfigError(f"{where}: price takes input/output, caps take daily_usd/monthly_usd")
    if not numbers["timeout"] or numbers["timeout"] > 3600:
        raise ConfigError(f"{where}.timeout_seconds must be between 1 and 3600")
    return RelayConfig(kind, base_url.rstrip("/"), tuple((k, v.strip()) for k, v in upstream.items()),
                       numbers["price_in"], numbers["price_out"], numbers["daily_cap"], numbers["monthly_cap"],
                       numbers["timeout"], stream or pod is not None, pod)


@dataclass(frozen=True)
class LaunchConfig:
    kernel: str                         # "<kaggle user>/<notebook slug>" of the owner's worker notebook
    idle_minutes: int = 30              # a notebook Kite started ends after this long without a job
    # "per-launch": a short-lived token written into the pushed notebook. "kaggle-secret": the notebook's
    # Kaggle Secret, which Kaggle does not give a run pushed through its API (HTTP 400, version 8 log).
    token: str = "per-launch"
    accelerator: str = "NvidiaTeslaT4"  # Kaggle CLI name of "GPU T4 x2"
    max_hours: float = 11.5             # Kaggle ends sessions at 12 h
    model: str | None = None            # model to load first when no request names one
    kaggle_username: str | None = None  # only with a legacy key (secrets.kaggle_key)
    # How a run starts. "api": Kite pushes the notebook through the Kaggle API (Kaggle may queue it for
    # hours). "browser": Kite opens the notebook in its own browser (browser.py), where you press Save
    # Version, or Kite presses it when you switched "Kite tự bấm" on.
    via: str = "api"
    # What Kite pushes: "stub" is one small cell that downloads the worker code from Kite when it runs
    # (stub.py), so a run never has old code; "full" is this repository's whole notebook.
    notebook: str = "stub"
    # SHA-256 of the prebuilt llama-server binary in the owner's kite-llama-bin dataset. The notebook
    # only runs the prebuilt when the file it finds hashes to this; unset or mismatched means it
    # builds from source (pinned commit) instead of executing an unverified binary.
    llama_server_sha256: str | None = None


LAUNCH_TOKEN_MODES = ("kaggle-secret", "per-launch")
LAUNCH_VIA = ("api", "browser")
LAUNCH_NOTEBOOKS = ("stub", "full")
_KERNEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}/[a-z0-9][a-z0-9-]{0,99}$")
_ACCELERATOR = re.compile(r"^[A-Za-z0-9]{2,40}$")


def _validate_launch(raw: Any, label: str, secrets: dict[str, str], models: tuple[str, ...]) -> LaunchConfig:
    launch = _mapping(raw, label)
    unknown = set(launch) - {"kernel", "idle_minutes", "token", "accelerator", "max_hours", "model",
                             "kaggle_username", "via", "notebook", "llama_server_sha256"}
    if unknown:
        raise ConfigError(f"{label} has unsupported fields")
    kernel = launch.get("kernel")
    if not isinstance(kernel, str) or not _KERNEL.fullmatch(kernel):
        raise ConfigError(f"{label}.kernel must be <kaggle user>/<notebook slug> as in the notebook URL")
    idle = launch.get("idle_minutes", 30)
    if not isinstance(idle, int) or isinstance(idle, bool) or not 5 <= idle <= 720:
        raise ConfigError(f"{label}.idle_minutes must be between 5 and 720")
    token = launch.get("token", "per-launch")
    if token not in LAUNCH_TOKEN_MODES:
        raise ConfigError(f"{label}.token must be one of {', '.join(LAUNCH_TOKEN_MODES)}")
    accelerator = launch.get("accelerator", "NvidiaTeslaT4")
    if not isinstance(accelerator, str) or not _ACCELERATOR.fullmatch(accelerator):
        raise ConfigError(f"{label}.accelerator is malformed")
    hours = _number(launch.get("max_hours", 11.5), f"{label}.max_hours", minimum=0.5)
    if hours > 12:
        raise ConfigError(f"{label}.max_hours cannot exceed Kaggle's 12 hours")
    model = launch.get("model")
    if model is not None and model not in models:
        raise ConfigError(f"{label}.model must be one of this worker's models")
    username = launch.get("kaggle_username")
    if username is not None and (not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username)):
        raise ConfigError(f"{label}.kaggle_username is malformed")
    if not secrets.get("kaggle_api_token") and not (secrets.get("kaggle_key") and username):
        raise ConfigError(f"{label} needs secrets.kaggle_api_token (or secrets.kaggle_key with "
                          "launch.kaggle_username) to run the notebook through the Kaggle API")
    via = launch.get("via", "api")
    if via not in LAUNCH_VIA:
        raise ConfigError(f"{label}.via must be one of {', '.join(LAUNCH_VIA)}")
    notebook = launch.get("notebook", "stub")
    if notebook not in LAUNCH_NOTEBOOKS:
        raise ConfigError(f"{label}.notebook must be one of {', '.join(LAUNCH_NOTEBOOKS)}")
    sha256 = launch.get("llama_server_sha256")
    if sha256 is not None:
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ConfigError(f"{label}.llama_server_sha256 must be a 64-character hex SHA-256 digest "
                              "(sha256sum of the llama-server binary in your kite-llama-bin dataset)")
        sha256 = sha256.lower()
    return LaunchConfig(kernel, idle, token, accelerator, hours, model, username, via, notebook, sha256)


@dataclass(frozen=True)
class RoutingConfig:
    swap_penalty_per_second: float = 0.0
    load_seconds_cached: float = 60.0
    load_seconds_cold: float = 600.0
    default_skill: float = 0.0
    model_skill: dict[str, dict[str, float]] = field(default_factory=dict)
    # Models without reliable tool calling: chat / Q&A through the gateway only, never a sandbox agent.
    chat_only: tuple[str, ...] = ()


@dataclass(frozen=True)
class GatewayConfig:
    """Model gateway for pull workers. The client API stays on loopback; only /worker/* is
    published (through Caddy) at ``public_url``."""
    host: str = "127.0.0.1"
    port: int = 8787
    public_url: str | None = None
    job_timeout_seconds: float = 1800.0
    heartbeat_timeout_seconds: float = 90.0
    # alias name -> {"model": base model, "params": extra request fields}
    aliases: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Context window (tokens) per base model, as llama-server is started on the worker (the Kaggle
    # notebook's ROSTER "ctx"). OpenCode is told this limit; models not listed get DEFAULT_CONTEXT.
    model_context: dict[str, int] = field(default_factory=dict)


DEFAULT_CONTEXT = 32768


def split_context(total: int) -> tuple[int, int]:
    """(agent, chat) context budgets for one model window shared by two requests at once.

    A worker serves the person chatting next to one task (llama-server --parallel 2 --kv-unified:
    both use the same KV cache of ``total`` tokens). OpenCode compacts each conversation before its
    own limit, so the two limits together must fit the window: the chat keeps 48K, the task's agent
    the rest (bench v6: two agents at once filled 128K and failed 23 requests). Only the large
    windows that the notebook shares between two requests are split; smaller ones stay whole.
    """
    if total < 98304:
        return total, total
    return total - 49152, 49152


@dataclass(frozen=True)
class SandboxConfig:
    """Per-task Docker sandbox. The gateway also listens on ``gateway_host`` (the host's address
    on the internal sandbox network) so sandboxed agents can reach it without Internet access."""
    image: str = "kite-sandbox:latest"
    network: str = "kite-sandbox-net"
    gateway_host: str = "172.19.0.1"
    memory: str = "1g"
    cpus: float = 1.0
    pids_limit: int = 512
    agent_timeout_seconds: int = 3600
    test_timeout_seconds: int = 900
    test_command: str = "python -m pytest -q"
    setup_command: str = ""
    setup_network: str = "bridge"
    setup_timeout_seconds: int = 1800
    docker_command: tuple[str, ...] = ("docker",)
    fix_rounds: int = 2
    self_review: bool = True
    code_map: bool = True
    deps_cache: bool = True
    quick_tests: bool = True
    fast_path: bool = True
    fast_review: bool = True
    fast_retries: int = 0             # more one-call fixes after a failed one, told why it failed (0-3)
    fast_parallel: bool = True        # also ask an idle worker that has the model loaded (best-of-N)


@dataclass(frozen=True)
class NotifyConfig:
    """Push notifications when a task finishes (``kite notify setup``); ``ntfy`` is a secret reference
    (file:PATH or env:NAME) holding the topic URL, e.g. https://ntfy.sh/kite-<random>."""
    ntfy: str | None = None


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
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    sandbox: SandboxConfig | None = None
    notify: NotifyConfig = field(default_factory=NotifyConfig)


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
    if adapter_id not in _BUILTIN_ADAPTERS:
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
                                 "owner", "worker_id", "roles", "models", "launch", "relay"}
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
        if adapter == "pull" and not secrets.get("worker_token"):
            raise ConfigError(f"providers[{i}] uses adapter 'pull' and needs secrets.worker_token")
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
        launch = None
        if p.get("launch") is not None:
            if adapter != "pull":
                raise ConfigError(f"providers[{i}].launch is only for adapter 'pull'")
            all_models = (model, *[m for m in extra_models if m != model])
            launch = _validate_launch(p["launch"], f"providers[{i}].launch", secrets, all_models)
        relay = None
        if adapter == "relay":
            relay = _validate_relay(p.get("relay"), f"providers[{i}].relay", secrets,
                                    (model, *[m for m in extra_models if m != model]))
        elif p.get("relay") is not None:
            raise ConfigError(f"providers[{i}].relay is only for adapter 'relay'")
        providers.append(ProviderConfig(
            pid, adapter, enabled, model, tuple(caps), seconds, concurrency, dict(secrets),
            owner=owner, worker_id=worker_id, roles=tuple(roles), models=tuple(extra_models), launch=launch,
            relay=relay,
        ))
    worker_ids = [p.effective_worker_id for p in providers]
    if len(set(worker_ids)) != len(worker_ids):
        raise ConfigError("provider worker IDs must be unique")
    routing = _validate_routing(root.get("routing", {}))
    gateway = _validate_gateway(root.get("gateway", {}))
    sandbox = _validate_sandbox(root.get("sandbox"))
    return KiteConfig(1, endpoint, queue, executable, host, port, checkout, harness_cli,
                      tuple(providers), routing=routing, gateway=gateway, sandbox=sandbox,
                      notify=_validate_notify(root.get("notify", {})))


def _validate_notify(raw: Any) -> NotifyConfig:
    notify = _mapping(raw, "notify")
    if set(notify) - {"ntfy"}:
        raise ConfigError("notify accepts only ntfy")
    ref = notify.get("ntfy")
    if ref is not None and (not isinstance(ref, str)
                            or not (_ENV_REF.fullmatch(ref) or (ref.startswith("file:") and len(ref) > 5))):
        raise ConfigError("notify.ntfy must be an env:NAME or file:PATH reference to the topic URL")
    return NotifyConfig(ntfy=ref)


def _number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise ConfigError(f"{label} must be a finite number")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{label} must be at least {minimum}")
    return float(value)


def _int(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{label} must be an integer of at least {minimum}")
    return value


def _validate_routing(raw: Any) -> RoutingConfig:
    """Optional dispatcher settings: model quality per task type and model-switch cost."""
    routing = _mapping(raw, "routing")
    unknown = set(routing) - {"swap_penalty_per_second", "load_seconds_cached", "load_seconds_cold",
                              "default_skill", "model_skill", "chat_only"}
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
    chat_only = routing.get("chat_only", [])
    if not isinstance(chat_only, list) or not all(isinstance(m, str) and _MODEL.fullmatch(m) for m in chat_only):
        raise ConfigError("routing.chat_only must be a list of model IDs")
    return RoutingConfig(
        swap_penalty_per_second=_number(routing.get("swap_penalty_per_second", 0.0),
                                        "routing.swap_penalty_per_second", minimum=0),
        load_seconds_cached=_number(routing.get("load_seconds_cached", 60.0),
                                    "routing.load_seconds_cached", minimum=0),
        load_seconds_cold=_number(routing.get("load_seconds_cold", 600.0),
                                  "routing.load_seconds_cold", minimum=0),
        default_skill=_number(routing.get("default_skill", 0.0), "routing.default_skill"),
        model_skill=skill,
        chat_only=tuple(dict.fromkeys(chat_only)),
    )


def _validate_gateway(raw: Any) -> GatewayConfig:
    gw = _mapping(raw, "gateway")
    unknown = set(gw) - {"host", "port", "public_url", "job_timeout_seconds",
                         "heartbeat_timeout_seconds", "aliases", "model_context"}
    if unknown:
        raise ConfigError("gateway has unsupported fields")
    host = gw.get("host", "127.0.0.1")
    if host not in _LOOPBACK:
        # /v1/* has no client authentication: it must never listen on a public interface.
        raise ConfigError("gateway.host must be a loopback address; publish /worker/* through Caddy")
    port = gw.get("port", 8787)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("gateway.port must be between 1 and 65535")
    public_url = gw.get("public_url")
    if public_url is not None and (not isinstance(public_url, str) or not public_url.startswith("https://")):
        raise ConfigError("gateway.public_url must be an https:// URL")
    aliases: dict[str, dict[str, Any]] = {}
    for name, spec in _mapping(gw.get("aliases", {}), "gateway.aliases").items():
        if not isinstance(name, str) or not _MODEL.fullmatch(name):
            raise ConfigError("gateway.aliases has a malformed alias name")
        spec = _mapping(spec, f"gateway.aliases.{name}")
        base = spec.get("model")
        if not isinstance(base, str) or not _MODEL.fullmatch(base):
            raise ConfigError(f"gateway.aliases.{name}.model is malformed")
        params = _mapping(spec.get("params", {}), f"gateway.aliases.{name}.params")
        if {"model", "messages", "stream"} & set(params):
            raise ConfigError(f"gateway.aliases.{name}.params cannot override model, messages or stream")
        aliases[name] = {"model": base, "params": dict(params)}
    model_context: dict[str, int] = {}
    for name, tokens in _mapping(gw.get("model_context", {}), "gateway.model_context").items():
        if not isinstance(name, str) or not _MODEL.fullmatch(name):
            raise ConfigError("gateway.model_context has a malformed model name")
        if not isinstance(tokens, int) or isinstance(tokens, bool) or not 8192 <= tokens <= 262144:
            raise ConfigError(f"gateway.model_context.{name} must be a token count between 8192 and 262144")
        model_context[name] = tokens
    return GatewayConfig(
        host=host, port=port, public_url=public_url,
        job_timeout_seconds=_number(gw.get("job_timeout_seconds", 1800.0),
                                    "gateway.job_timeout_seconds", minimum=1),
        heartbeat_timeout_seconds=_number(gw.get("heartbeat_timeout_seconds", 90.0),
                                          "gateway.heartbeat_timeout_seconds", minimum=10),
        aliases=aliases, model_context=model_context,
    )


def model_contexts(config: KiteConfig, models) -> dict[str, int]:
    """Context window of each served model; an alias has the context of its base model."""
    gateway = config.gateway
    result = {}
    for name in models:
        base = gateway.aliases.get(name, {}).get("model", name)
        result[name] = gateway.model_context.get(base, DEFAULT_CONTEXT)
    return result


def _validate_sandbox(raw: Any) -> SandboxConfig | None:
    if raw is None:
        return None
    sb = _mapping(raw, "sandbox")
    unknown = set(sb) - {"image", "network", "gateway_host", "memory", "cpus", "pids_limit",
                         "agent_timeout_seconds", "test_timeout_seconds", "test_command",
                         "setup_command", "setup_network", "setup_timeout_seconds", "docker_command",
                         "fix_rounds", "self_review", "code_map", "deps_cache", "quick_tests", "fast_path", "fast_review",
                         "fast_retries", "fast_parallel"}
    if unknown:
        raise ConfigError("sandbox has unsupported fields")
    image = sb.get("image", "kite-sandbox:latest")
    if not isinstance(image, str) or not _IMAGE.fullmatch(image):
        raise ConfigError("sandbox.image is malformed")
    network = sb.get("network", "kite-sandbox-net")
    if not isinstance(network, str) or not _NETWORK.fullmatch(network) or network in {"host", "bridge", "none"}:
        raise ConfigError("sandbox.network must name a dedicated internal Docker network")
    host = sb.get("gateway_host", "172.19.0.1")
    try:
        address = ipaddress.ip_address(host)
    except (TypeError, ValueError) as exc:
        raise ConfigError("sandbox.gateway_host must be an IP address") from exc
    if address.version != 4 or not address.is_private or address.is_loopback:
        raise ConfigError("sandbox.gateway_host must be the host's private IPv4 address on the sandbox network")
    memory = sb.get("memory", "1g")
    if not isinstance(memory, str) or not _MEMORY.fullmatch(memory):
        raise ConfigError("sandbox.memory must look like 512m or 1g")
    test_command = sb.get("test_command", "python -m pytest -q")
    if not isinstance(test_command, str):
        raise ConfigError("sandbox.test_command must be a string ('' to skip tests)")
    setup_command = sb.get("setup_command", "")
    if not isinstance(setup_command, str):
        raise ConfigError("sandbox.setup_command must be a string ('' for no setup)")
    setup_network = sb.get("setup_network", "bridge")
    if not isinstance(setup_network, str) or not _NETWORK.fullmatch(setup_network) or setup_network == "host":
        raise ConfigError("sandbox.setup_network must name a Docker network other than host")
    docker_command = sb.get("docker_command", ["docker"])
    if not isinstance(docker_command, list) or not docker_command or any(
            not isinstance(x, str) or not x.strip() for x in docker_command):
        raise ConfigError("sandbox.docker_command must be a non-empty list such as [docker] or [sudo, docker]")
    return SandboxConfig(
        image=image, network=network, gateway_host=str(address), memory=memory,
        cpus=_number(sb.get("cpus", 1.0), "sandbox.cpus", minimum=0.1),
        pids_limit=_int(sb.get("pids_limit", 512), "sandbox.pids_limit", minimum=64),
        agent_timeout_seconds=_int(sb.get("agent_timeout_seconds", 3600), "sandbox.agent_timeout_seconds", minimum=60),
        test_timeout_seconds=_int(sb.get("test_timeout_seconds", 900), "sandbox.test_timeout_seconds", minimum=10),
        test_command=test_command, setup_command=setup_command, setup_network=setup_network,
        setup_timeout_seconds=_int(sb.get("setup_timeout_seconds", 1800), "sandbox.setup_timeout_seconds", minimum=30),
        docker_command=tuple(docker_command),
        fix_rounds=_bounded_fix_rounds(sb.get("fix_rounds", 2)),
        self_review=_flag(sb.get("self_review", True), "sandbox.self_review"),
        code_map=_flag(sb.get("code_map", True), "sandbox.code_map"),
        deps_cache=_flag(sb.get("deps_cache", True), "sandbox.deps_cache"),
        quick_tests=_flag(sb.get("quick_tests", True), "sandbox.quick_tests"),
        fast_path=_flag(sb.get("fast_path", True), "sandbox.fast_path"),
        fast_review=_flag(sb.get("fast_review", True), "sandbox.fast_review"),
        fast_retries=_fast_retries(sb.get("fast_retries", 0)),
        fast_parallel=_flag(sb.get("fast_parallel", True), "sandbox.fast_parallel"),
    )


def _fast_retries(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
        raise ConfigError("sandbox.fast_retries must be an integer from 0 to 3")
    return value


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _bounded_fix_rounds(value: Any) -> int:
    rounds = _int(value, "sandbox.fix_rounds", minimum=0)
    if rounds > 5:
        raise ConfigError("sandbox.fix_rounds must be between 0 and 5")
    return rounds


def resolve_secret(reference: str) -> str:
    """Read an ``env:NAME`` or ``file:PATH`` secret reference. Never log the result."""
    if reference.startswith("env:"):
        value = os.environ.get(reference[4:], "")
    elif reference.startswith("file:"):
        try:
            value = Path(reference[5:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read secret file {reference[5:]}") from exc
    else:
        raise ConfigError("secret reference must be env:NAME or file:PATH")
    value = value.strip()
    if not value:
        raise ConfigError("secret reference resolved to an empty value")
    return value


def gateway_workers(config: KiteConfig) -> list:
    """WorkerAuth entries for every enabled provider that uses adapter ``pull`` or ``relay``. A relay
    worker runs inside the gateway and never authenticates over HTTP: its token is a random one."""
    from .gateway import WorkerAuth
    from .relay import internal_token

    return [
        WorkerAuth(p.effective_worker_id,
                   resolve_secret(p.secrets["worker_token"]) if p.adapter == "pull" else internal_token(),
                   p.model_ids)
        for p in config.providers if p.adapter in ("pull", "relay") and p.enabled
    ]


def gateway_models(config: KiteConfig) -> list[str]:
    """Model names the gateway serves: base models of enabled pull and relay workers, then usable aliases."""
    base: list[str] = []
    for p in config.providers:
        if p.adapter in ("pull", "relay") and p.enabled:
            base.extend(m for m in p.model_ids if m not in base)
    return base + [name for name, spec in config.gateway.aliases.items() if spec["model"] in base]


def sandbox_settings_from_config(config: KiteConfig):
    """SandboxSettings for ``kite sandbox ...``; the agent reaches the gateway on the sandbox network."""
    from .sandbox import SandboxSettings

    if config.sandbox is None:
        raise ConfigError("config has no sandbox block")
    sb = config.sandbox
    return SandboxSettings(
        image=sb.image, network=sb.network,
        gateway_url=f"http://{sb.gateway_host}:{config.gateway.port}/v1",
        host_gateway_url=f"http://{config.gateway.host}:{config.gateway.port}/v1",
        memory=sb.memory, cpus=sb.cpus, pids_limit=sb.pids_limit,
        agent_timeout_seconds=sb.agent_timeout_seconds, test_timeout_seconds=sb.test_timeout_seconds,
        test_command=sb.test_command, setup_command=sb.setup_command,
        setup_network=sb.setup_network, setup_timeout_seconds=sb.setup_timeout_seconds,
        docker_command=sb.docker_command, fix_rounds=sb.fix_rounds, self_review=sb.self_review,
        code_map=sb.code_map, deps_cache=sb.deps_cache, quick_tests=sb.quick_tests, fast_path=sb.fast_path, fast_review=sb.fast_review,
        fast_retries=sb.fast_retries, fast_parallel=sb.fast_parallel,
        model_context=model_contexts(config, gateway_models(config)),
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
        excluded=frozenset(r.chat_only),
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
            # Several accounts of one owner each run their own tasks: the owner's limit is the sum.
            max_concurrent_sessions=sum(p.concurrency for p in providers),
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
    sandbox = None
    if config.sandbox is not None:
        sb = config.sandbox
        sandbox = {"image": sb.image, "network": sb.network, "gateway_host": sb.gateway_host,
                   "memory": sb.memory, "cpus": sb.cpus, "pids_limit": sb.pids_limit,
                   "agent_timeout_seconds": sb.agent_timeout_seconds,
                   "test_timeout_seconds": sb.test_timeout_seconds,
                   "test_command": sb.test_command, "setup_command": sb.setup_command,
                   "setup_network": sb.setup_network, "setup_timeout_seconds": sb.setup_timeout_seconds,
                   "docker_command": list(sb.docker_command)}
    return {
        "schema_version": config.schema_version,
        "temporal": {"endpoint": config.temporal_endpoint, "task_queue": config.temporal_task_queue},
        "opencode": {"executable": config.opencode_executable, "server": {"host": config.opencode_host, "port": config.opencode_port}},
        "harness2": {"checkout": config.harness2_checkout, "cli": config.harness2_cli},
        "providers": [{"id": p.id, "adapter": p.adapter, "enabled": p.enabled, "model": p.model,
                       "models": list(p.model_ids), "owner": p.owner, "worker_id": p.effective_worker_id,
                       "roles": list(p.roles),
                       "capabilities": list(p.capabilities), "quota": {"gpu_seconds": p.quota_gpu_seconds, "concurrency": p.concurrency},
                       "secrets": {k: "<reference-redacted>" for k in p.secrets},
                       **({"launch": {"kernel": p.launch.kernel, "idle_minutes": p.launch.idle_minutes,
                                      "token": p.launch.token}} if p.launch else {})}
                      for p in config.providers],
        "routing": {"swap_penalty_per_second": config.routing.swap_penalty_per_second,
                    "load_seconds_cached": config.routing.load_seconds_cached,
                    "load_seconds_cold": config.routing.load_seconds_cold,
                    "default_skill": config.routing.default_skill,
                    "model_skill": config.routing.model_skill,
                    "chat_only": list(config.routing.chat_only)},
        "gateway": {"host": config.gateway.host, "port": config.gateway.port,
                    "public_url": config.gateway.public_url,
                    "job_timeout_seconds": config.gateway.job_timeout_seconds,
                    "heartbeat_timeout_seconds": config.gateway.heartbeat_timeout_seconds,
                    "aliases": config.gateway.aliases},
        "sandbox": sandbox,
    }
