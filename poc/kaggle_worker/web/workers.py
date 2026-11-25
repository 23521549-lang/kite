"""Provider and Worker tabs: the workers as config.yaml has them, merged with what the gateway sees, and
the edits the web makes to config.yaml (validated first, a backup kept, keys in 0600 files).

The config file stays the one source of truth, so ``kite worker ...`` on the command line and the web
always show the same workers. A change takes effect when the gateway restarts (``/api/system/restart``).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import secrets as token_secrets
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlparse

from ..config import ConfigError, validate_config

TYPES = ("kaggle", "openai", "anthropic", "runpod")
_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")

PROVIDER_TYPES = [
    {"type": "kaggle", "label": "Kaggle", "cost": "miễn phí, quota GPU theo tuần",
     "fields": ["id", "owner", "models", "kernel", "kaggle_api_token", "via", "idle_minutes"],
     "secrets": ["kaggle_api_token"],
     "note": "Kite tạo worker token; dán nó vào Kaggle Secrets tên KITE_WORKER_TOKEN, rồi dán ô khởi động "
             "(kite worker stub) vào notebook."},
    {"type": "openai", "label": "OpenAI-compatible", "cost": "theo token",
     "fields": ["id", "base_url", "models", "api_key", "price_in", "price_out", "daily_cap", "monthly_cap",
                "concurrency"],
     "secrets": ["api_key"],
     "note": "OpenAI, OpenRouter, Groq, DeepSeek, Gemini, vLLM, Ollama, llama-server..."},
    {"type": "anthropic", "label": "Anthropic (Claude)", "cost": "theo token",
     "fields": ["id", "models", "api_key", "price_in", "price_out", "daily_cap", "monthly_cap", "concurrency"],
     "secrets": ["api_key"], "note": ""},
    {"type": "runpod", "label": "Máy GPU thuê (RunPod)", "cost": "theo giờ máy bật",
     "fields": ["id", "pod_id", "port", "models", "runpod_api_key", "api_key", "price_hour", "idle_minutes",
                "daily_cap", "monthly_cap", "concurrency"],
     "secrets": ["runpod_api_key", "api_key"],
     "note": "Tạo sẵn pod chạy vLLM hoặc llama-server; Kite chỉ bật và tắt nó."},
]
SECRET_FIELDS = ("api_key", "runpod_api_key", "kaggle_api_token")

try:
    from .presets import PROVIDER_PRESETS
except ImportError:  # the brand catalog (presets.py) is optional: without it every relay is "custom"
    PROVIDER_PRESETS = []


def kind_of(provider) -> str:
    if provider.adapter == "pull":
        return "kaggle"
    if provider.relay is not None and provider.relay.pod is not None:
        return "runpod"
    return provider.relay.kind if provider.relay is not None else provider.adapter


def brand_for(provider, presets=PROVIDER_PRESETS) -> str:
    """The brand a worker belongs to, derived — never stored in config. A pod is RunPod whatever
    its (auto-derived proxy) URL says; a relay is the preset whose base_url has the same host;
    an unmatched relay host is a custom endpoint; a pull worker is Kaggle."""
    relay = provider.relay
    if relay is not None and relay.pod is not None:
        return "RunPod"
    if relay is not None and relay.base_url:
        host = urlparse(relay.base_url).hostname
        if host:
            for preset in presets:
                if urlparse(str(preset.get("base_url") or "")).hostname == host:
                    return str(preset.get("label") or preset.get("brand") or host)
            return f"Tùy chỉnh ({host})"
    if provider.adapter == "pull":
        return "Kaggle"
    kind = kind_of(provider)
    label = next((t["label"] for t in PROVIDER_TYPES if t["type"] == kind), None)
    return label or kind


# ---------------------------------------------------------------- reading
def worker_rows(config, status: dict | None, ledger, auto_click: Callable[[str], bool]) -> list[dict]:
    """One row per pull or relay worker: config, what the gateway sees, spend against caps."""
    live = {w["worker_id"]: w for w in (status or {}).get("workers") or []}
    rows = []
    for p in config.providers:
        if p.adapter not in ("pull", "relay"):
            continue
        wid = p.effective_worker_id
        seen = live.get(wid, {})
        kind = kind_of(p)
        day, month = ledger.spent(wid) if ledger is not None else (0.0, 0.0)
        row: dict[str, Any] = {
            "id": wid, "type": kind, "brand": brand_for(p, PROVIDER_PRESETS),
            "label": p.label,
            "enabled": p.enabled, "owner": p.owner, "models": list(p.model_ids),
            "concurrency": p.concurrency, "alive": bool(seen.get("alive")), "busy": bool(seen.get("busy")),
            "running": seen.get("running", 0), "slots": seen.get("slots"), "loaded_model": seen.get("loaded_model"),
            "version": seen.get("version"), "draining": bool(seen.get("draining")),
            "seconds_since_heartbeat": seen.get("seconds_since_heartbeat"),
            "switch": seen.get("switch") or {"on": True, "auto": True},
            "start": seen.get("launch"),                 # Kaggle autostart or pod state, when Kite starts it
            "spend": {"today_usd": round(day, 4), "month_usd": round(month, 4)},
            "in_gateway": wid in live,
        }
        if p.launch is not None:
            row["kaggle"] = {"kernel": p.launch.kernel, "via": p.launch.via, "notebook": p.launch.notebook,
                             "idle_minutes": p.launch.idle_minutes, "auto_click": auto_click(wid),
                             "url": f"https://www.kaggle.com/code/{p.launch.kernel}/edit"}
        if p.relay is not None:
            r = p.relay
            row["api"] = {"kind": r.kind, "base_url": r.base_url, "stream": r.stream,
                          "price": {"input": r.price_in, "output": r.price_out},
                          "caps": {"daily_usd": r.daily_cap, "monthly_usd": r.monthly_cap},
                          "upstream": dict(r.upstream)}
            if r.pod is not None:
                row["pod"] = {"provider": r.pod.provider, "id": r.pod.pod_id, "port": r.pod.port,
                              "price_hour": r.pod.price_hour, "idle_minutes": r.pod.idle_minutes}
        rows.append(row)
    return rows


# ---------------------------------------------------------------- writing config.yaml
def _load(path: Path) -> dict:
    import yaml

    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def _save(path: Path, raw: dict) -> Path:
    import yaml

    validate_config(raw)
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    text = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n" if path.suffix == ".json"
            else yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
    return backup


SEALED_PREFIX = "kite-sealed:v1:"


def write_secret(path: Path, value: str, sealer=None) -> None:
    """A key in its own 0600 file (atomic; the folder 0700). The value never goes back to the browser.

    With a ``sealer`` (the web's AES-256-GCM Sealer) the content is sealed under the web key and
    marked with :data:`SEALED_PREFIX`, so a copy of the secrets folder alone does not hand the keys
    over. Without one (the CLI) the file keeps the legacy plaintext form ``resolve_secret`` reads.
    Readers that meet the prefix must unseal with the same key — see :func:`read_secret`."""
    content = value.strip()
    if sealer is not None:
        content = SEALED_PREFIX + sealer.seal(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content + "\n")
    os.replace(temp, path)


def read_secret(path: Path, sealer=None) -> str:
    """What a secret file holds. A sealed file needs the sealer that sealed it. A legacy plaintext
    file read with a sealer is rewritten sealed — the migration to sealed-at-rest happens on the
    first read through the web layer, with permissions unchanged."""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith(SEALED_PREFIX):
        if sealer is None:
            raise ConfigError(f"{path.name} đã được niêm phong (sealed); cần khóa web để đọc")
        return sealer.unseal(text[len(SEALED_PREFIX):])
    if sealer is not None and text:
        write_secret(path, text, sealer=sealer)
    return text


def _models(items: list[str]) -> tuple[list[str], dict[str, str]]:
    names, upstream = [], {}
    for item in items:
        name, _, target = str(item).partition("=")
        if name.strip():
            names.append(name.strip())
            if target.strip():
                upstream[name.strip()] = target.strip()
    if not names:
        raise ConfigError("cần ít nhất một model")
    return names, upstream


def _caps(body: dict) -> dict:
    return {k: body[f] for k, f in (("daily_usd", "daily_cap"), ("monthly_usd", "monthly_cap"))
            if body.get(f) is not None}


def _display_label(body: dict) -> str | None:
    """The display name to store, if the owner typed one; None means 'show the id'."""
    label = body.get("label")
    if label is None:
        return None
    if not isinstance(label, str):
        raise ConfigError("label phải là chuỗi")
    return label.strip() or None


def build_provider(body: dict, raw: dict, secrets_dir: Path) -> tuple[dict, dict[Path, str], dict]:
    """The config entry for a new worker, the key files to write, and what to tell the owner."""
    kind, wid = body.get("type"), str(body.get("id") or "")
    if kind not in TYPES:
        raise ConfigError(f"type phải là một trong: {', '.join(TYPES)}")
    if not _ID.fullmatch(wid):
        raise ConfigError("id chỉ gồm chữ thường, số và '-', bắt đầu bằng chữ (ví dụ kaggle-han, openrouter)")
    if any(wid in (p.get("id"), p.get("worker_id")) for p in raw.get("providers") or []):
        raise ConfigError(f"worker {wid} đã có")
    files: dict[Path, str] = {}
    report: dict[str, Any] = {"added": wid, "type": kind}
    concurrency = int(body.get("concurrency") or (1 if kind == "kaggle" else 2))
    if kind == "kaggle":
        template = next((p for p in raw.get("providers") or [] if p.get("adapter") == "pull"), None)
        names = list(body.get("models") or []) or (
            [template["model"], *(template.get("models") or [])] if template else [])
        names, _ = _models(names)
        token = token_secrets.token_urlsafe(32)
        files[secrets_dir / f"worker_token_{wid}"] = token
        report["worker_token"] = token
        report["next"] = ("Dán worker_token vào Kaggle (Add-ons → Secrets) tên KITE_WORKER_TOKEN, bật cho notebook; "
                          "dán ô khởi động (kite worker stub) vào notebook; khởi động lại gateway.")
        provider: dict[str, Any] = {
            "id": wid, "adapter": "pull", "enabled": True, "owner": str(body.get("owner") or "self"),
            "model": names[0], "models": names[1:],
            "capabilities": (template or {}).get("capabilities") or ["coding"],
            "quota": (template or {}).get("quota") or {"gpu_seconds": 108000, "concurrency": 1},
            "secrets": {"worker_token": f"file:{secrets_dir / f'worker_token_{wid}'}"}}
        if template and template.get("roles"):
            provider["roles"] = template["roles"]
        label = _display_label(body)
        if label is not None:
            provider["label"] = label
        if body.get("kernel"):
            if not body.get("kaggle_api_token"):
                raise ConfigError("tự bật notebook cần Kaggle API token (kaggle.com/settings → API)")
            path = secrets_dir / f"kaggle_api_token_{wid}"
            files[path] = str(body["kaggle_api_token"])
            provider["secrets"]["kaggle_api_token"] = f"file:{path}"
            provider["launch"] = {"kernel": body["kernel"], "idle_minutes": int(body.get("idle_minutes") or 30),
                                  "token": "per-launch", "via": body.get("via") or "api"}
        return provider, files, report
    names, upstream = _models(list(body.get("models") or []))
    relay: dict[str, Any] = {"kind": "openai" if kind == "runpod" else kind}
    if body.get("base_url"):
        relay["base_url"] = str(body["base_url"]).strip()
    if upstream:
        relay["upstream"] = upstream
    if _caps(body):
        relay["caps"] = _caps(body)
    secrets: dict[str, str] = {}
    if kind == "runpod":
        if not body.get("runpod_api_key"):
            raise ConfigError("máy RunPod cần RunPod API key (quyền pods:write)")
        relay["pod"] = {"provider": "runpod", "id": body.get("pod_id"), "port": int(body.get("port") or 8000),
                        "idle_minutes": int(body.get("idle_minutes") or 30)}
        if body.get("price_hour") is not None:
            relay["pod"]["price_hour"] = float(body["price_hour"])
        relay["timeout_seconds"] = 1800
        path = secrets_dir / f"runpod_api_key_{wid}"
        files[path] = str(body["runpod_api_key"])
        secrets["runpod_api_key"] = f"file:{path}"
    else:
        relay["price"] = {"input": float(body.get("price_in") or 0), "output": float(body.get("price_out") or 0)}
        if (relay["price"]["input"] or relay["price"]["output"]) and not _caps(body):
            report["warning"] = "worker trả tiền chưa có hạn mức chi theo ngày hoặc tháng"
    if body.get("api_key"):
        path = secrets_dir / f"api_key_{wid}"
        files[path] = str(body["api_key"])
        secrets["api_key"] = f"file:{path}"
    provider = {"id": wid, "adapter": "relay", "enabled": True, "owner": "self", "model": names[0],
                "models": names[1:], "capabilities": ["coding"],
                "quota": {"gpu_seconds": 0, "concurrency": concurrency}, "secrets": secrets, "relay": relay}
    label = _display_label(body)
    if label is not None:
        provider["label"] = label
    return provider, files, report


def add_worker(config_path: Path, body: dict, sealer=None) -> dict:
    raw = _load(config_path)
    secrets_dir = config_path.parent / "secrets"
    provider, files, report = build_provider(body, raw, secrets_dir)
    raw["providers"] = [*(raw.get("providers") or []), provider]
    validate_config(raw)                       # before any key is written: a typo costs nothing
    for path, value in files.items():
        write_secret(path, value, sealer=sealer)
    report["backup"] = str(_save(config_path, raw))
    report["restart_needed"] = True
    return report


EDITABLE = {"enabled", "price_in", "price_out", "price_hour", "daily_cap", "monthly_cap", "idle_minutes",
            "concurrency", "via", "kernel", "base_url", "models", "label", *SECRET_FIELDS}


def update_worker(config_path: Path, worker_id: str, changes: dict, sealer=None) -> dict:
    unknown = set(changes) - EDITABLE
    if unknown:
        raise ConfigError(f"không sửa được: {', '.join(sorted(unknown))}")
    raw = _load(config_path)
    provider = next((p for p in raw.get("providers") or [] if worker_id in (p.get("id"), p.get("worker_id"))), None)
    if provider is None or provider.get("adapter") not in ("pull", "relay"):
        raise ConfigError(f"không có worker {worker_id}")
    secrets_dir = config_path.parent / "secrets"
    files: dict[Path, str] = {}
    relay, launch = provider.get("relay") or {}, provider.get("launch")
    pod = relay.get("pod") if relay else None
    for key, value in changes.items():
        if key == "enabled":
            provider["enabled"] = bool(value)
        elif key == "concurrency":
            provider.setdefault("quota", {})["concurrency"] = int(value)
        elif key == "models":
            names, upstream = _models(list(value))
            provider["model"], provider["models"] = names[0], names[1:]
            if relay is not None and provider.get("adapter") == "relay":
                relay["upstream"] = upstream
        elif key in ("price_in", "price_out") and relay and not pod:
            relay.setdefault("price", {})["input" if key == "price_in" else "output"] = float(value or 0)
        elif key in ("daily_cap", "monthly_cap") and relay:
            caps = relay.setdefault("caps", {})
            name = "daily_usd" if key == "daily_cap" else "monthly_usd"
            if value is None:
                caps.pop(name, None)
            else:
                caps[name] = float(value)
        elif key == "price_hour" and pod is not None:
            if value is None:
                pod.pop("price_hour", None)
            else:
                pod["price_hour"] = float(value)
        elif key == "idle_minutes" and (pod is not None or launch):
            (pod if pod is not None else launch)["idle_minutes"] = int(value)
        elif key in ("via", "kernel") and launch:
            launch[key] = value
        elif key == "base_url" and relay:
            relay["base_url"] = str(value).strip()
        elif key == "label":
            if not isinstance(value, str):
                raise ConfigError("label phải là chuỗi")
            if value.strip():
                provider["label"] = value.strip()
            else:
                provider.pop("label", None)
        elif key in SECRET_FIELDS and value:
            name = {"api_key": "api_key", "runpod_api_key": "runpod_api_key",
                    "kaggle_api_token": "kaggle_api_token"}[key]
            path = secrets_dir / f"{name}_{worker_id}"
            files[path] = str(value)
            provider.setdefault("secrets", {})[name] = f"file:{path}"
        else:
            raise ConfigError(f"{key} không áp dụng cho worker {worker_id}")
    validate_config(raw)
    for path, value in files.items():
        write_secret(path, value, sealer=sealer)
    backup = _save(config_path, raw)
    return {"updated": worker_id, "fields": sorted(changes), "backup": str(backup), "restart_needed": True}


def remove_worker(config_path: Path, worker_id: str) -> dict:
    raw = _load(config_path)
    providers = raw.get("providers") or []
    provider = next((p for p in providers if worker_id in (p.get("id"), p.get("worker_id"))), None)
    if provider is None or provider.get("adapter") not in ("pull", "relay"):
        raise ConfigError(f"không có worker {worker_id}")
    raw["providers"] = [p for p in providers if p is not provider]
    backup = _save(config_path, raw)
    secrets_dir = (config_path.parent / "secrets").resolve()
    removed = []
    for reference in (provider.get("secrets") or {}).values():
        path = Path(str(reference)[5:]).resolve() if str(reference).startswith("file:") else None
        # Only this worker's own key files in Kite's secrets folder (never a file shared with another worker).
        shared = any(reference in (p.get("secrets") or {}).values() for p in raw["providers"])
        if path is not None and path.parent == secrets_dir and path.name.endswith(f"_{worker_id}") and not shared:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return {"removed": worker_id, "secret_files_removed": removed, "backup": str(backup), "restart_needed": True}


# ---------------------------------------------------------------- connection tests (nothing is saved)
async def test_connection(body: dict, *, session_factory=None, runner=None) -> dict:
    """Try the key and address the owner typed: list the API's models, read the pod, or ask Kaggle."""
    import aiohttp

    kind = body.get("type")
    factory = session_factory or aiohttp.ClientSession
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        if kind in ("openai", "anthropic"):
            base = str(body.get("base_url") or ("https://api.anthropic.com" if kind == "anthropic" else "")).rstrip("/")
            if not base.startswith(("https://", "http://")):
                return {"ok": False, "detail": "cần base URL dạng https://..."}
            headers = ({"x-api-key": str(body.get("api_key") or ""), "anthropic-version": "2023-06-01"}
                       if kind == "anthropic" else
                       ({"Authorization": f"Bearer {body['api_key']}"} if body.get("api_key") else {}))
            url = base + ("/v1/models" if kind == "anthropic" else "/models")
            async with factory() as session:
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    text = await response.text()
                    if response.status != 200:
                        return {"ok": False, "detail": f"HTTP {response.status}: {text[:200]}"}
                data = json.loads(text) if text.strip() else {}
                models = [m.get("id") for m in data.get("data") or [] if isinstance(m, dict)][:50]
                result = {"ok": True, "detail": f"kết nối được; {len(models)} model", "models": models}
                model = _typed_model(body) or (models[0] if models else None)
                if model:
                    result.update(await _chat_probe(session, kind, base, headers, model, timeout))
                return result
        if kind == "runpod":
            from ..pods import PodError, RunPodClient

            try:
                pod = await RunPodClient(str(body.get("runpod_api_key") or ""),
                                         session_factory=session_factory).get(str(body.get("pod_id") or ""))
            except PodError as exc:
                return {"ok": False, "detail": str(exc)}
            return {"ok": True, "detail": f"pod {pod.get('id')} đang {pod.get('status')}", "status": pod.get("status"),
                    "price_hour": pod.get("cost")}
        if kind == "kaggle":
            return await asyncio.to_thread(_kaggle_test, body, runner)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return {"ok": False, "detail": f"không kết nối được ({type(exc).__name__})"}
    return {"ok": False, "detail": f"không thử được loại {kind}"}


def _typed_model(body: dict) -> str | None:
    """The first model the owner typed in the form ('name' or 'name=the-api's-own-id'): the id the API
    itself knows is the one a test call has to use."""
    for item in body.get("models") or []:
        name, _, target = str(item).partition("=")
        if name.strip():
            return (target or name).strip()
    return None


async def _chat_probe(session, kind: str, base: str, headers: dict, model: str, timeout) -> dict:
    """One smallest-possible chat call against a listed API: proves the model itself answers (not only
    that the key lists models), times it, and notes whether the API reports token usage — without a
    usage figure Kite can only estimate what every later call costs. A failed probe never undoes the
    successful listing: the caller still gets ok=True with model_ok=False and the reason in chat_detail."""
    import aiohttp

    if kind == "anthropic":
        url, payload = base + "/v1/messages", {"model": model, "max_tokens": 1,
                                               "messages": [{"role": "user", "content": "ping"}]}
    else:
        url, payload = base + "/chat/completions", {"model": model, "max_tokens": 1,
                                                    "messages": [{"role": "user", "content": "ping"}]}
    probe: dict[str, Any] = {"model_tested": model}
    started = time.monotonic()
    try:
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as response:
            text = await response.text()
            probe["latency_ms"] = round((time.monotonic() - started) * 1000)
            if response.status != 200:
                probe.update(model_ok=False, chat_detail=f"HTTP {response.status}: {text[:200]}")
                return probe
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        probe["latency_ms"] = round((time.monotonic() - started) * 1000)
        probe.update(model_ok=False, chat_detail=f"không gọi được ({type(exc).__name__})")
        return probe
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        data = {}
    usage = data.get("usage") if isinstance(data, dict) else None
    probe["model_ok"] = True
    probe["usage_reported"] = isinstance(usage, dict) and bool(usage)
    return probe


def _kaggle_test(body: dict, runner=None) -> dict:
    from ..launcher import kaggle_command, parse_status

    kernel, token = str(body.get("kernel") or ""), str(body.get("kaggle_api_token") or "")
    if not kernel or not token:
        return {"ok": False, "detail": "cần notebook (<user>/<slug>) và Kaggle API token"}
    with tempfile.TemporaryDirectory(prefix="kite-kaggle-") as folder:
        token_file = Path(folder) / "token"
        write_secret(token_file, token)
        env = {k: v for k, v in os.environ.items() if not k.startswith("KAGGLE_")}
        env.update(KAGGLE_CONFIG_DIR=folder, KAGGLE_API_TOKEN=str(token_file))
        command = [kaggle_command(), "kernels", "status", kernel]
        if runner is not None:
            code, output = runner(command, env, 60)
        else:
            try:
                done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
                code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"ok": False, "detail": f"không chạy được Kaggle CLI: {exc}"}
    status = parse_status(output) if code == 0 else None
    if status is None:
        return {"ok": False, "detail": "Kaggle không trả lời: " + output.strip()[-200:]}
    return {"ok": True, "detail": f"notebook {kernel}: lần chạy gần nhất {status}", "status": status}
