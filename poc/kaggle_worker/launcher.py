"""Autostart: run a worker's Kaggle notebook when work arrives, let it end itself when idle.

A pull worker with ``launch`` in its provider config is started by Kite instead of by hand:

* A request reaches the gateway and no connected worker serves its model, the chat opens
  (``kite mcp serve`` starts), or ``kite worker start`` is run.
* Kite pulls the owner's notebook metadata from Kaggle (its attached datasets and settings stay as
  set in the Kaggle UI), puts this repository's notebook in its place with ``KITE_LAUNCH`` filled in,
  and pushes it as a new version that runs with GPU T4 x2 ("Save & Run All").
* The notebook sees ``KITE_LAUNCH``: it loads the requested model first, skips the sample task, and
  ends the session after ``idle_minutes`` without a job, so the weekly GPU quota is not spent idle.

Kaggle API credentials are read from the provider's secrets (``kaggle_api_token``, or the legacy
``kaggle_key`` with ``launch.kaggle_username``) and handed to the Kaggle CLI in its environment
only; ``KAGGLE_API_TOKEN`` points at the 0600 secret file, so the token is never copied.

Worker token: ``kaggle-secret`` (default) keeps using the notebook's Kaggle Secret
``KITE_WORKER_TOKEN``. Kaggle may not attach Secrets to a version pushed through the API; then the
run fails at the worker cell and ``kite worker status`` shows it. ``per-launch`` instead writes a
fresh token into the pushed notebook that the gateway accepts only until that run's time is up
(the gateway keeps its SHA-256 digest, never the token).
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets as token_secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from .config import KiteConfig, LaunchConfig, resolve_secret

NOTEBOOK = Path(__file__).with_name("kaggle_model_worker.ipynb")
LAUNCH_LINE = re.compile(r"^KITE_LAUNCH = \{\}.*$", re.M)
RUNNING = {"queued", "running", "new_script"}
LAUNCH_WINDOW_SECONDS = 6 * 3600       # a pushed run starts well within this; later starts are manual runs

# (command, env, timeout) -> (exit code, combined output)
Runner = Callable[[list[str], dict[str, str], float], tuple[int, str]]


def default_runner(command: list[str], env: dict[str, str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"{command[1:3]} timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def kaggle_command() -> str:
    """The Kaggle CLI installed next to Kite (``.venv/bin/kaggle``), else the one on PATH."""
    sibling = Path(sys.executable).with_name("kaggle")
    return str(sibling) if sibling.exists() else (shutil.which("kaggle") or "kaggle")


def parse_status(output: str) -> str | None:
    """``kaggle kernels status`` prints ``<kernel> has status "KernelWorkerStatus.RUNNING"``."""
    match = re.search(r'has status "(?:KernelWorkerStatus\.)?([A-Za-z_]+)"', output)
    return match.group(1).lower() if match else None


def parse_failure(output: str) -> str:
    match = re.search(r'Failure message: "(.*)"', output)
    return match.group(1)[:300] if match else ""


def inject_launch(notebook: dict, values: dict[str, Any]) -> dict:
    """Return a copy of the notebook with its ``KITE_LAUNCH = {}`` line replaced by ``values``."""
    nb = copy.deepcopy(notebook)
    literal = "KITE_LAUNCH = " + repr(values) + "   # điền bởi Kite khi tự bật notebook"
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", []))
        if LAUNCH_LINE.search(source):
            source = LAUNCH_LINE.sub(lambda _m: literal, source, count=1)
            cell["source"] = source.splitlines(keepends=True)
            cell["outputs"], cell["execution_count"] = [], None
            return nb
    raise ValueError("the notebook has no `KITE_LAUNCH = {}` line (update kaggle_model_worker.ipynb)")


@dataclass
class LaunchState:
    worker_id: str
    spec: LaunchConfig
    state: str = "idle"            # idle | launching | starting | alive | error
    since: float = 0.0
    detail: str = ""
    version: str | None = None
    reason: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def report(self, now: float) -> dict[str, Any]:
        return {"state": self.state, "seconds": round(now - self.since) if self.since else None,
                "detail": self.detail, "kernel": self.spec.kernel, "version": self.version,
                "reason": self.reason, "idle_minutes": self.spec.idle_minutes, "token": self.spec.token}


class KaggleLauncher:
    """Starts the Kaggle notebooks of pull workers that have ``launch`` configured."""

    def __init__(self, config: KiteConfig, gateway: Any, *, state_dir: Path, runner: Runner = default_runner,
                 clock: Callable[[], float] = time.time, notebook: Path = NOTEBOOK,
                 command: str | None = None, startup_seconds: float = 1500.0, retry_seconds: float = 600.0,
                 grace_seconds: float | None = None, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.config = config
        self.gateway = gateway
        self.state_dir = Path(state_dir)
        self.runner = runner
        self.clock = clock
        self.notebook = Path(notebook)
        self.command = command or kaggle_command()
        self.startup_seconds = startup_seconds      # pushed but no heartbeat yet: still booting until then
        self.retry_seconds = retry_seconds          # after a failed start, wait before starting again
        # Right after the gateway (re)starts, running workers have not heartbeated yet: wait one
        # heartbeat timeout before deciding that none is connected.
        self.grace_seconds = gateway.heartbeat_timeout_seconds if grace_seconds is None else grace_seconds
        self.sleep = sleep
        self.providers = {p.effective_worker_id: p for p in config.providers
                          if p.adapter == "pull" and p.enabled and p.launch is not None
                          and p.effective_worker_id in gateway.workers}
        self.states = {wid: LaunchState(wid, p.launch) for wid, p in self.providers.items()}
        self._pending: set[str] = set()
        self._tasks: set[asyncio.Task] = set()      # keep references: the loop only holds weak ones

    # ------------------------------------------------------------------ tokens
    def _token_file(self, worker_id: str) -> Path:
        return self.state_dir / "secrets" / f"launch_token_{worker_id}.json"

    def load_tokens(self) -> int:
        """Give the gateway the per-launch token digests of runs that may still be going."""
        loaded = 0
        for worker_id in self.providers:
            path = self._token_file(worker_id)
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(saved, dict) and saved.get("expires_at", 0) > self.clock():
                self.gateway.set_launch_token(worker_id, str(saved["digest"]), float(saved["expires_at"]))
                loaded += 1
        return loaded

    def _new_launch_token(self, worker_id: str, hours: float) -> str:
        token = token_secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = self.clock() + hours * 3600 + 1800
        path = self._token_file(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temp = path.with_name(path.name + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"digest": digest, "expires_at": expires_at}, stream)
        os.replace(temp, path)
        self.gateway.set_launch_token(worker_id, digest, expires_at)
        return token

    # ------------------------------------------------------------------ Kaggle CLI
    def _env(self, worker_id: str, config_dir: str) -> dict[str, str]:
        provider = self.providers[worker_id]
        env = {k: v for k, v in os.environ.items() if not k.startswith("KAGGLE_")}
        env["KAGGLE_CONFIG_DIR"] = config_dir          # never read or write ~/.kaggle
        reference = provider.secrets.get("kaggle_api_token")
        if reference:
            # The Kaggle CLI accepts a path to a file that holds the token: point it at the secret file.
            env["KAGGLE_API_TOKEN"] = reference[5:] if reference.startswith("file:") else resolve_secret(reference)
        else:
            env["KAGGLE_USERNAME"] = provider.launch.kaggle_username or ""
            env["KAGGLE_KEY"] = resolve_secret(provider.secrets["kaggle_key"])
        return env

    def _kaggle(self, worker_id: str, args: list[str], timeout: float = 120) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="kite-kaggle-") as config_dir:
            return self.runner([self.command, *args], self._env(worker_id, config_dir), timeout)

    def kernel_status(self, worker_id: str) -> tuple[str | None, str]:
        code, output = self._kaggle(worker_id, ["kernels", "status", self.states[worker_id].spec.kernel], 60)
        status = parse_status(output) if code == 0 else None
        return status, (parse_failure(output) if status else output.strip()[-300:])

    def _push(self, worker_id: str, model: str | None, reason: str) -> tuple[bool, str, str | None]:
        """Pull the notebook's metadata, push this repo's notebook with KITE_LAUNCH, run it."""
        spec = self.states[worker_id].spec
        with tempfile.TemporaryDirectory(prefix="kite-launch-") as folder:
            code, output = self._kaggle(worker_id, ["kernels", "pull", spec.kernel, "-p", folder, "-m"], 180)
            meta_path = Path(folder) / "kernel-metadata.json"
            if code != 0 or not meta_path.exists():
                return False, "không lấy được notebook từ Kaggle: " + output.strip()[-300:], None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("kernel_type") != "notebook":
                return False, f"{spec.kernel} không phải notebook", None
            code_file = Path(folder) / str(meta.get("code_file") or "kite-worker.ipynb")
            launch: dict[str, Any] = {"worker_id": worker_id, "idle_minutes": spec.idle_minutes,
                                      "reason": reason,
                                      "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock())),
                                      # Opened by hand later (not this run): the notebook ignores KITE_LAUNCH.
                                      "expires": int(self.clock() + LAUNCH_WINDOW_SECONDS)}
            if model or spec.model:
                launch["model"] = model or spec.model
            if self.config.gateway.public_url:
                launch["gateway_url"] = self.config.gateway.public_url
            if spec.token == "per-launch":
                launch["token"] = self._new_launch_token(worker_id, spec.max_hours)
            notebook = inject_launch(json.loads(self.notebook.read_text(encoding="utf-8")), launch)
            code_file.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
            meta.update(code_file=code_file.name, enable_gpu=True, enable_internet=True, is_private=True)
            meta.pop("enable_tpu", None)
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            code, output = self._kaggle(worker_id, [
                "kernels", "push", "-p", folder, "--accelerator", spec.accelerator,
                "-t", str(int(spec.max_hours * 3600))], 300)
        version = re.search(r"Kernel version (\d+) successfully pushed", output)
        if code != 0 or not version:
            return False, "Kaggle không nhận notebook: " + output.strip()[-300:], None
        return True, "", version.group(1)

    # ------------------------------------------------------------------ public
    def _candidates(self, model: str | None, worker: str | None) -> list[str]:
        if worker is not None:
            if worker not in self.states:
                raise ValueError(f"worker {worker} has no launch config (providers[].launch)")
            return [worker]
        return [wid for wid in self.states if model is None or model in self.gateway.workers[wid].model_ids]

    def _refresh(self, state: LaunchState) -> None:
        if self.gateway.present(state.worker_id):
            if state.state != "alive":
                state.state, state.since, state.detail = "alive", self.clock(), ""
        elif state.state == "alive":
            state.state, state.since, state.detail = "idle", self.clock(), "worker đã ngắt kết nối"

    def status(self) -> dict[str, dict[str, Any]]:
        now = self.clock()
        for state in self.states.values():
            self._refresh(state)
        return {wid: state.report(now) for wid, state in self.states.items()}

    def on_demand(self, model: str) -> None:
        """Gateway hook: a request for ``model`` arrived and no connected worker serves it."""
        if model in self._pending or not self._candidates(model, None):
            return
        self._pending.add(model)
        task = asyncio.get_running_loop().create_task(self.wake(model=model, reason="request"))
        self._tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._pending.discard(model)
            self._tasks.discard(finished)

        task.add_done_callback(done)

    async def wake(self, *, model: str | None = None, worker: str | None = None, reason: str = "manual",
                   force: bool = False) -> dict[str, Any]:
        """Make sure a worker that serves ``model`` is connected or starting; start one if needed."""
        candidates = self._candidates(model, worker)
        if not candidates:
            return {"state": "none", "detail": "không worker nào có launch cho model này"}
        waited = self.clock() - self.gateway.started_at
        if waited < self.grace_seconds and not force:
            await self.sleep(self.grace_seconds - waited)
        for wid in candidates:
            state = self.states[wid]
            self._refresh(state)
            if state.state == "alive" or (state.state in ("launching", "starting") and not force):
                return {"worker": wid, **state.report(self.clock())}
        for wid in candidates:
            state = self.states[wid]
            if (state.state == "error" and not force
                    and self.clock() - state.since < self.retry_seconds):
                continue
            return await self._launch(state, model, reason, force)
        state = self.states[candidates[0]]
        return {"worker": state.worker_id, **state.report(self.clock())}

    async def _launch(self, state: LaunchState, model: str | None, reason: str, force: bool) -> dict[str, Any]:
        async with state.lock:
            self._refresh(state)
            if state.state == "alive" or (state.state in ("launching", "starting") and not force):
                return {"worker": state.worker_id, **state.report(self.clock())}
            state.state, state.since, state.reason, state.detail = "launching", self.clock(), reason, ""
            try:
                status, detail = await asyncio.to_thread(self.kernel_status, state.worker_id)
                if status in RUNNING and not force:
                    # A run already exists (pushed earlier, or Kite restarted): let it connect.
                    state.state, state.detail = "starting", f"Kaggle đang chạy notebook ({status})"
                elif status is None and "404" not in detail and "not found" not in detail.lower():
                    state.state, state.detail = "error", "không đọc được trạng thái trên Kaggle: " + detail
                else:
                    ok, error, version = await asyncio.to_thread(self._push, state.worker_id, model, reason)
                    if ok:
                        state.state, state.version = "starting", version
                        state.detail = f"đã đẩy notebook version {version}; worker kết nối sau ~5–10 phút"
                    else:
                        state.state, state.detail = "error", error
            except Exception as exc:
                state.state, state.detail = "error", f"{type(exc).__name__}: {exc}"[:300]
            state.since = self.clock()
            if state.state == "error":
                print(f"kite gateway: autostart {state.worker_id}: {state.detail}", file=sys.stderr)
            return {"worker": state.worker_id, **state.report(self.clock())}

    async def check_starting(self) -> None:
        """A start that never connected: ask Kaggle what happened (run in the gateway's background loop)."""
        for state in self.states.values():
            self._refresh(state)
            if state.state != "starting" or self.clock() - state.since < self.startup_seconds:
                continue
            status, detail = await asyncio.to_thread(self.kernel_status, state.worker_id)
            if status in RUNNING:
                state.since = self.clock() - self.startup_seconds / 2      # still booting: look again later
                state.detail = f"Kaggle vẫn đang chạy notebook ({status}) nhưng worker chưa kết nối"
            else:
                state.state, state.since = "error", self.clock()
                state.detail = (f"notebook dừng ({status or 'không rõ'}) trước khi worker kết nối"
                                + (f": {detail}" if detail else "")
                                + ("; nếu lỗi ở Secrets, đặt launch.token: per-launch" if state.spec.token ==
                                   "kaggle-secret" else ""))

    async def watch(self, every: float = 60.0) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.check_starting()
            except Exception as exc:
                print(f"kite gateway: autostart check failed: {exc}", file=sys.stderr)
