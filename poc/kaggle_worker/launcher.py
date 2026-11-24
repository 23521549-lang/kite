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

What is pushed: by default the bootstrap notebook (``launch.notebook: stub``, stub.py), one cell that
downloads the worker code from the gateway when it runs; ``full`` pushes this repository's notebook.

``launch.via: browser``: instead of pushing through the API (which Kaggle may queue for hours), Kite
opens the notebook's editor in its own browser (browser.py). With the owner's "Kite tự bấm" switch
on, Kite presses Save Version -> Save & Run All -> Save there; otherwise the owner presses them (the
start then waits in state ``waiting`` until Kaggle shows a new run). Kaggle gives Secrets to a run
saved on kaggle.com, so the notebook's ``KITE_WORKER_TOKEN`` works.

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
from .stub import stub_notebook

NOTEBOOK = Path(__file__).with_name("kaggle_model_worker.ipynb")
LAUNCH_LINE = re.compile(r"^KITE_LAUNCH = \{\}.*$", re.M)
RUNNING = {"queued", "running", "new_script"}
LAUNCH_WINDOW_SECONDS = 6 * 3600       # a pushed run starts well within this; later starts are manual runs
# Kaggle can keep a run pushed through its API queued for hours while "Save Version" pressed on
# kaggle.com starts at once (22 Nov: version 11 pushed by Kite queued 200+ min, version 12 saved by
# hand at the same moment ran at once). After this long in the queue, tell the person to press it.
QUEUE_NOTIFY_SECONDS = 180.0
MAX_FAILURES = 2                       # runs that stopped before connecting: then wait for `--force`
WAIT_CLICK_SECONDS = 3600.0            # launch.via browser: how long to wait for Save Version by hand


def kaggle_url(kernel: str) -> str:
    """Where to open the notebook to press Save Version (the editor)."""
    return f"https://www.kaggle.com/code/{kernel}/edit"


_ERROR_LINE = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit)|Error)\b\s*:")


def log_error_line(log: str) -> str:
    """The line that says why a Kaggle run stopped, from ``kaggle kernels logs`` output (a JSON list
    of {stream_name, time, data} or plain text): the last ``SomethingError: ...`` line."""
    text = log
    try:
        entries = json.loads(log)
        if isinstance(entries, list):
            text = "".join(str(e.get("data", "")) for e in entries if isinstance(e, dict))
    except (json.JSONDecodeError, TypeError):
        pass
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    hits = [line for line in lines if _ERROR_LINE.match(line)]
    return (hits[-1] if hits else "")[:300]

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
    state: str = "idle"            # idle | launching | waiting (for Save Version by hand) | starting | alive | error
    since: float = 0.0
    detail: str = ""
    version: str | None = None
    reason: str = ""
    kaggle: str | None = None      # last status Kaggle reported for the run: queued | running | error | ...
    kaggle_since: float = 0.0      # when Kaggle first reported that status
    failures: int = 0              # runs in a row that stopped before the worker connected
    pushed_at: float = 0.0         # this start: when the notebook was pushed (0: a run already there)
    running_at: float = 0.0        # this start: when Kaggle first said the run is running
    notified: bool = False         # this start: the person was told to press Save Version
    # The worker was seen gone after the gateway's first heartbeat window: when it connects again
    # without a run Kite pushed, someone started it (Save Version on kaggle.com). A worker that only
    # reconnects after a gateway restart is not counted (22 Nov: version 14, saved by hand after Kite's
    # version 13 was cancelled, left "connected: 0" in the journal).
    absent_seen: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def report(self, now: float) -> dict[str, Any]:
        return {"state": self.state, "seconds": round(now - self.since) if self.since else None,
                "detail": self.detail, "kernel": self.spec.kernel, "version": self.version,
                "kaggle": self.kaggle,
                "kaggle_seconds": round(now - self.kaggle_since) if self.kaggle and self.kaggle_since else None,
                "reason": self.reason, "idle_minutes": self.spec.idle_minutes, "token": self.spec.token,
                "failures": self.failures, "url": kaggle_url(self.spec.kernel)}

    def saw(self, status: str | None, now: float) -> None:
        if status != self.kaggle:
            self.kaggle, self.kaggle_since = status, now


def kaggle_env(provider: Any, config_dir: str) -> dict[str, str]:
    """Environment for the Kaggle CLI with this provider's credentials (never ~/.kaggle)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("KAGGLE_")}
    env["KAGGLE_CONFIG_DIR"] = config_dir
    reference = provider.secrets.get("kaggle_api_token")
    if reference:
        # The Kaggle CLI accepts a path to a file that holds the token: point it at the secret file.
        env["KAGGLE_API_TOKEN"] = reference[5:] if reference.startswith("file:") else resolve_secret(reference)
    else:
        env["KAGGLE_USERNAME"] = (provider.launch.kaggle_username or "") if provider.launch else ""
        env["KAGGLE_KEY"] = resolve_secret(provider.secrets["kaggle_key"])
    return env


class KaggleLauncher:
    """Starts the Kaggle notebooks of pull workers that have ``launch`` configured."""

    def __init__(self, config: KiteConfig, gateway: Any, *, state_dir: Path, runner: Runner = default_runner,
                 clock: Callable[[], float] = time.time, notebook: Path = NOTEBOOK,
                 command: str | None = None, startup_seconds: float = 1500.0, retry_seconds: float = 600.0,
                 grace_seconds: float | None = None, sleep: Callable[[float], Any] = asyncio.sleep,
                 notify: Callable[..., None] | None = None, journal: Callable[[dict], None] | None = None,
                 browser: Any = None, auto_click: Callable[[str], bool] | None = None) -> None:
        self.config = config
        self.browser = browser          # browser.KiteBrowser for launch.via: browser
        if auto_click is None:
            from .browser import auto_click_on

            auto_click = lambda worker: auto_click_on(Path(state_dir), worker)   # noqa: E731
        self.auto_click = auto_click    # the owner's "Kite tự bấm" switch, read at each start
        self.notify = notify            # ntfy: (title, message, tags=, priority=); never carries code or goals
        self.journal = journal          # one line per push / run start / connect / failure ("launches")
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
        self.allow: Callable[[str], bool] = lambda worker: True   # starters.Switches.auto: "start it for me"
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
        return kaggle_env(self.providers[worker_id], config_dir)

    def _kaggle(self, worker_id: str, args: list[str], timeout: float = 120) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="kite-kaggle-") as config_dir:
            return self.runner([self.command, *args], self._env(worker_id, config_dir), timeout)

    def kernel_status(self, worker_id: str) -> tuple[str | None, str]:
        code, output = self._kaggle(worker_id, ["kernels", "status", self.states[worker_id].spec.kernel], 60)
        status = parse_status(output) if code == 0 else None
        return status, (parse_failure(output) if status else output.strip()[-300:])

    def kernel_error(self, worker_id: str) -> str:
        """Why the latest run stopped, from its log (``kaggle kernels logs``; older CLIs: empty)."""
        code, output = self._kaggle(worker_id, ["kernels", "logs", self.states[worker_id].spec.kernel], 120)
        return log_error_line(output) if code == 0 else ""

    # ------------------------------------------------------------------ telling the person
    def _record(self, state: LaunchState, event: str, **fields: Any) -> None:
        if self.journal is None:
            return
        try:
            self.journal({"event": event, "worker": state.worker_id, "kernel": state.spec.kernel,
                          "version": state.version, "reason": state.reason, **fields})
        except Exception as exc:
            print(f"kite gateway: launch journal error: {exc}", file=sys.stderr)

    def _tell(self, title: str, message: str, *, priority: int = 4) -> None:
        if not self.notify:
            return
        try:
            self.notify(title, message, tags=("rocket",), priority=priority)
        except Exception as exc:
            print(f"kite gateway: notify error: {exc}", file=sys.stderr)

    def browser_hint(self) -> str:
        port = self.config.gateway.port
        return (f"Trình duyệt Kite: ssh -L {port}:127.0.0.1:{port} vào máy Kite, rồi mở "
                f"http://127.0.0.1:{port}/kite/browser")

    def starting(self, model: str) -> bool:
        """Gateway hook: a worker that serves ``model`` is being started (keep its requests queued)."""
        return any(state.state in ("launching", "waiting", "starting")
                   for wid, state in self.states.items() if model in self.gateway.workers[wid].model_ids)

    def wait_note(self, model: str) -> str:
        """What the person chatting is told while no worker can answer (gateway hook)."""
        states = [s for wid, s in self.states.items() if model in self.gateway.workers[wid].model_ids]
        if not states:
            return ""
        state = states[0]
        self._refresh(state)
        now = self.clock()
        doing = {
            "launching": "Kite đang đẩy notebook lên Kaggle",
            "starting": (f"Kaggle đang xếp hàng notebook {round((now - state.kaggle_since) / 60)} phút"
                         if state.kaggle in ("queued", "new_script") and state.kaggle_since
                         else "notebook đang chạy trên Kaggle (đang nạp model)" if state.kaggle == "running"
                         else "Kite đang bật notebook"),
            "error": f"lần bật trước bị lỗi: {state.detail}"[:300],
        }.get(state.state, "Kite đang bật notebook")
        if state.state == "waiting":
            return (f"Worker {state.worker_id} chưa chạy nên chưa trả lời được: {state.detail}. "
                    f"{self.browser_hint()}. Worker kết nối khoảng 2–3 phút sau khi notebook chạy; khi đó nhắn lại.")
        return (f"Worker {state.worker_id} chưa chạy nên chưa trả lời được ({doing}). Muốn nhanh: mở "
                f"{kaggle_url(state.spec.kernel)} rồi bấm Save Version (Save & Run All); Kaggle thường cấp "
                "GPU ngay cho cách này. Worker kết nối khoảng 2–3 phút sau khi notebook chạy; khi đó nhắn lại.")

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
            if spec.llama_server_sha256:
                # The notebook only runs the prebuilt llama-server when the binary hashes to this.
                launch["llama_server_sha256"] = spec.llama_server_sha256
            # The bootstrap notebook needs the gateway's public address to fetch the worker code.
            base = (stub_notebook(self.config.gateway.public_url)
                    if spec.notebook == "stub" and self.config.gateway.public_url
                    else json.loads(self.notebook.read_text(encoding="utf-8")))
            notebook = inject_launch(base, launch)
            code_file.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
            meta.update(code_file=code_file.name, enable_gpu=True, enable_internet=True, is_private=True)
            meta.pop("enable_tpu", None)
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            # Like "Save & Run All" in the Kaggle UI: no session-timeout flag (the notebook drains itself
            # before Kaggle's 12 h limit). Bench v5 day: a push with -t 41400 sat in Kaggle's queue for
            # 30 minutes while UI runs started at once.
            code, output = self._kaggle(worker_id, [
                "kernels", "push", "-p", folder, "--accelerator", spec.accelerator], 300)
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
        return [wid for wid in self.states if (model is None or model in self.gateway.workers[wid].model_ids)
                and self.allow(wid)]

    def _refresh(self, state: LaunchState) -> None:
        if self.gateway.present(state.worker_id):
            if state.state != "alive":
                now = self.clock()
                if state.state in ("launching", "waiting", "starting"):
                    self._record(state, "alive",
                                 since_push_s=round(now - state.pushed_at) if state.pushed_at else None,
                                 queued_s=round((state.running_at or now) - state.pushed_at)
                                 if state.pushed_at else None)
                elif state.absent_seen:
                    self._record(state, "alive", by_hand=True)
                state.state, state.since, state.detail = "alive", now, ""
                state.failures, state.notified, state.absent_seen = 0, False, False
            return
        if self.clock() - self.gateway.started_at >= self.grace_seconds:
            state.absent_seen = True
        if state.state == "alive":
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
            if state.state == "alive" or (state.state in ("launching", "waiting", "starting") and not force):
                return {"worker": wid, **state.report(self.clock())}
        for wid in candidates:
            state = self.states[wid]
            if state.failures >= MAX_FAILURES and not force:
                continue        # it failed MAX_FAILURES times in a row: pushing again would fail the same way
            if (state.state == "error" and not force
                    and self.clock() - state.since < self.retry_seconds):
                continue
            return await self._launch(state, model, reason, force)
        state = self.states[candidates[0]]
        return {"worker": state.worker_id, **state.report(self.clock())}

    async def _launch(self, state: LaunchState, model: str | None, reason: str, force: bool) -> dict[str, Any]:
        async with state.lock:
            self._refresh(state)
            if state.state == "alive" or (state.state in ("launching", "waiting", "starting") and not force):
                return {"worker": state.worker_id, **state.report(self.clock())}
            state.state, state.since, state.reason, state.detail = "launching", self.clock(), reason, ""
            try:
                status, detail = await asyncio.to_thread(self.kernel_status, state.worker_id)
                state.saw(status, self.clock())
                if status in RUNNING and not force:
                    # A run already exists (pushed earlier, or Kite restarted): let it connect.
                    state.state, state.detail = "starting", f"Kaggle đang chạy notebook ({status})"
                elif status is None and "404" not in detail and "not found" not in detail.lower():
                    state.state, state.detail = "error", "không đọc được trạng thái trên Kaggle: " + detail
                elif state.spec.via == "browser":
                    await self._start_in_browser(state)
                else:
                    ok, error, version = await asyncio.to_thread(self._push, state.worker_id, model, reason)
                    if ok:
                        state.state, state.version = "starting", version
                        state.saw("queued", self.clock())
                        state.pushed_at, state.running_at, state.notified = self.clock(), 0.0, False
                        state.detail = (f"đã đẩy notebook version {version}; chờ Kaggle cấp GPU rồi "
                                        "worker kết nối sau ~5–10 phút")
                        self._record(state, "push")
                    else:
                        state.state, state.detail = "error", error
                        state.failures += 1
                        self._record(state, "failed", error=error[:300])
            except Exception as exc:
                state.state, state.detail = "error", f"{type(exc).__name__}: {exc}"[:300]
            state.since = self.clock()
            if state.state == "error":
                print(f"kite gateway: autostart {state.worker_id}: {state.detail}", file=sys.stderr)
            return {"worker": state.worker_id, **state.report(self.clock())}

    async def _start_in_browser(self, state: LaunchState) -> None:
        """launch.via: browser. Open the notebook's editor in Kite's browser; press Save Version only
        when the owner switched "Kite tự bấm" on for this worker."""
        if self.browser is None:
            state.state = "error"
            state.detail = ("launch.via: browser cần trình duyệt Kite: pip install -e .[browser] rồi "
                            "playwright install --with-deps chromium, restart kite-gateway")
            return
        clicks = bool(self.auto_click(state.worker_id))
        try:
            if clicks:
                result = await self.browser.save_version(state.spec.kernel)
            else:
                await self.browser.open(kaggle_url(state.spec.kernel))
                result = None
        except Exception as exc:
            state.state, state.detail = "error", f"trình duyệt Kite lỗi: {type(exc).__name__}: {exc}"[:300]
            state.failures += 1
            self._record(state, "failed", via="browser", error=state.detail)
            return
        now = self.clock()
        state.pushed_at, state.running_at, state.notified, state.version = now, 0.0, False, None
        if result and result.get("ok"):
            state.state = "starting"
            state.saw("queued", now)
            state.detail = "Kite đã bấm Save Version trong trình duyệt của Kite; chờ Kaggle chạy notebook"
            self._record(state, "push", via="browser", clicked=True)
            return
        why = ("Kaggle đòi đăng nhập" if result and result.get("step") == "login"
               else f"Kite không bấm được (bước {result.get('step')})" if result else "")
        state.state = "waiting"
        state.detail = ((why + ": " if why else "notebook đã mở trong trình duyệt Kite: ")
                        + "bấm Save Version → Save & Run All → Save")
        self._record(state, "waiting", via="browser", step=(result or {}).get("step"))
        self._tell("Kite cần bạn bấm Save Version", f"{state.worker_id}: {state.detail}. {self.browser_hint()}")

    async def _check_waiting(self, state: LaunchState) -> None:
        """launch.via: browser, nobody pressed yet: a new queued or running run means someone did."""
        now = self.clock()
        status, _detail = await asyncio.to_thread(self.kernel_status, state.worker_id)
        if status in RUNNING:
            state.state, state.since = "starting", now
            state.saw(status, now)
            state.detail = "có bản chạy mới trên Kaggle; chờ worker kết nối"
            self._record(state, "push", via="browser", clicked=False)
        elif now - state.since >= WAIT_CLICK_SECONDS:
            state.state, state.since = "idle", now
            state.detail = "hết chờ bấm Save Version; Kite mở lại khi có việc"
            self._record(state, "cancelled", via="browser")
        else:
            state.detail = (state.detail.split(" (chờ ")[0]
                            + f" (chờ {round((now - state.since) / 60)} phút)")

    async def check_starting(self) -> None:
        """While a start has not connected, ask Kaggle every minute what the run is doing: say how long it
        has been queued or running, and report a run that stopped at once instead of waiting it out."""
        for state in self.states.values():
            self._refresh(state)
            if state.state == "waiting":
                await self._check_waiting(state)
                continue
            if state.state != "starting":
                continue
            now = self.clock()
            status, detail = await asyncio.to_thread(self.kernel_status, state.worker_id)
            if status is None:
                state.detail = "không hỏi được Kaggle trạng thái notebook: " + detail[-200:]
                continue
            state.saw(status, now)
            minutes = (now - state.kaggle_since) / 60
            url = kaggle_url(state.spec.kernel)
            if status == "queued" or status == "new_script":
                state.detail = (f"Kaggle đang xếp hàng GPU cho version {state.version or '?'} ({minutes:.0f} phút); "
                                f"muốn nhanh: mở {url} và bấm Save Version")
                if now - state.kaggle_since >= QUEUE_NOTIFY_SECONDS and not state.notified:
                    state.notified = True
                    self._tell("Kite cần GPU: bấm Save Version",
                               f"{state.worker_id}: Kaggle đang xếp hàng notebook {minutes:.0f} phút. "
                               f"Mở {url} và bấm Save Version để chạy ngay.")
            elif status == "running":
                if not state.running_at:
                    state.running_at = now
                    self._record(state, "running",
                                 queued_s=round(now - state.pushed_at) if state.pushed_at else None)
                state.detail = (f"notebook đang chạy {minutes:.0f} phút, worker chưa kết nối"
                                + (" (quá lâu: xem log version trên Kaggle)" if now - state.kaggle_since
                                   > self.startup_seconds else " (đang nạp model)"))
            elif status.startswith("cancel"):
                # Cancelled on kaggle.com (or replaced by a version saved there): not a failure.
                state.state, state.since = "idle", now
                state.detail = "bản trên Kaggle đã bị hủy; Kite đẩy lại khi có việc"
                self._record(state, "cancelled")
            else:
                why = await asyncio.to_thread(self.kernel_error, state.worker_id)
                state.state, state.since = "error", now
                state.failures += 1
                state.detail = (f"notebook dừng ({status}) trước khi worker kết nối"
                                + (f": {why}" if why else f": {detail}" if detail else "")
                                + ("; nếu lỗi Secret, đặt launch.token: per-launch"
                                   if state.spec.token == "kaggle-secret" and status == "error" else "")
                                + (f"; đã lỗi {state.failures} lần liền: Kite ngừng tự bật, sửa xong chạy "
                                   f"kite worker start {state.worker_id} --force" if state.failures >= MAX_FAILURES
                                   else ""))
                self._record(state, "failed", status=status, error=(why or detail)[:300])
                self._tell("Kite: notebook worker bị lỗi",
                           f"{state.worker_id}: {(why or status)[:200]}. Mở {url} để xem log.")
                print(f"kite gateway: autostart {state.worker_id}: {state.detail}", file=sys.stderr)

    async def watch(self, every: float = 60.0) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.check_starting()
            except Exception as exc:
                print(f"kite gateway: autostart check failed: {exc}", file=sys.stderr)
