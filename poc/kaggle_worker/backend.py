"""Production backend for the Temporal coding workflow: every task runs in the Kite sandbox.

Each workflow step maps onto the sandbox from ``sandbox.py``:

* ``prepare_workspace`` clones the pinned commit into ``<state>/tasks/<slug>/work`` and runs the
  optional setup command (the only step with network access). Re-running it reuses the clone.
* ``reserve_route`` / ``heartbeat_route`` / ``release_route`` go through the scheduler store, so the
  task-type dispatcher, leases and GPU quota apply.
* ``run_opencode`` runs ``opencode run`` in the offline agent container with the routed model, then
  Kite runs the test command itself in a container with no network and returns the patch with that
  evidence in the summary.
* ``interrupt_opencode`` kills the task's containers (pause, cancel, reject).

The agent never talks to an OpenCode server on this path, so the permission prompt that used to
hang a task (E1) and the bypassable ``git push`` rule (E5) do not apply: the container has no
network, no Git remote and no credentials.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Callable, Mapping
import urllib.request

from temporalio.exceptions import ApplicationError

from . import sandbox as sbx
from .journal import Journal, append_jsonl, clean_tag, read_jsonl, short, task_usage
from . import memory
from .integration import ReviewBundle, build_review_bundle
from .protocol import WorkerResult
from .store import StoreError
from .temporal_state import RecoveryDecision
from .temporal_workflow import CodingRunResult, CodingTaskInput, RouteLease

HEARTBEAT_SECONDS = 20.0
NO_CHANGES_PATCH = "# Kite: the agent made no changes\n"
CANCEL_MARKER = "cancel_requested"     # in the task folder: written by `cancel_task` / `kite workflow cancel`
ACTION_TRACE_SECONDS = 10.0            # at most one agent_action line per this many seconds in trace.jsonl


class TaskCancelled(Exception):
    """The person cancelled the task: stop at the next step instead of running the rest."""


def cancel_marker(state_dir: Path, task_id: str) -> Path:
    return Path(state_dir) / "tasks" / task_slug(task_id) / CANCEL_MARKER


def request_cancel(state_dir: Path, task_id: str) -> Path:
    """Mark a task cancelled before the workflow's cancel signal: the run activity then stops at once
    (containers killed, model requests dropped) instead of finishing the steps it is in (step 2 of
    the full-test day: a cancelled task kept the worker for 15 more minutes)."""
    path = cancel_marker(state_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(time.strftime("%Y-%m-%d %H:%M:%S\n"), encoding="utf-8")
    return path


STEP_LABELS = {"setup": "cài thư viện (có mạng)", "repro": "chạy test tái hiện", "quick": "chạy các test liên quan",
               "fast": "chạy test trên bản sửa nhanh",
               "test": "chạy bộ test của repo", "check": "kiểm tra code", "agent": "agent viết code",
               "review": "agent rà soát lại bản sửa"}
FAST_OUTCOMES = {"passed": "bản sửa qua hết test", "tests_failed": "bản sửa trượt test, chuyển cho agent",
                 "repro_failed": "bản sửa trượt test tái hiện, chuyển cho agent",
                 "repro_disputed": "bản sửa qua test của repo nhưng lệch một phần test tái hiện, giữ lại để agent "
                                   "xem ca nào sai",
                 "old_tests_failed": "bản sửa qua test tái hiện nhưng làm lỗi test có sẵn, giữ lại để agent xem test "
                                     "cũ khẳng định hành vi mà yêu cầu đổi hay bản sửa làm hỏng",
                 "check_failed": "bản sửa trượt bước kiểm tra, chuyển cho agent",
                 "not_applied": "bản sửa không áp được, chuyển cho agent",
                 "declined": "model nói không sửa được trong một lần, chuyển cho agent",
                 "skipped": "không tìm được đoạn code liên quan, chuyển cho agent",
                 "error": "lỗi khi gọi model, chuyển cho agent"}


def duration_vi(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} giây"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} phút" + (f" {seconds} giây" if seconds else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours} giờ" + (f" {minutes} phút" if minutes else "")


def _step_label(step: str) -> str:
    if re.fullmatch(r"fix\d+", step or ""):
        return f"agent sửa theo lỗi test (vòng {step[3:]})"
    return STEP_LABELS.get(step, step or "bước")


def task_progress(trace: list[dict], now: float, limit: int = 8) -> dict:
    """What a running (or finished) task has done, from its trace.jsonl, in Vietnamese: total time,
    the step running now and for how long, and the last ``limit`` finished steps."""
    lines: list[str] = []
    phase = step = None           # (label, started at): the fast path / one-call review; a container
    action, actions = "", 0       # what the agent did last in the running step, and how many tool calls

    def add(text: str, at: Any) -> None:
        offset = f"{duration_vi(at - trace[0]['ts'])}: " if isinstance(at, (int, float)) else ""
        lines.append(offset + text)

    for record in trace:
        event, at = record.get("event"), record.get("ts")
        if event == "step_start":
            step = (_step_label(record.get("step", "")), at)
            action, actions = "", 0
        elif event == "agent_action":
            action, actions = str(record.get("action") or ""), int(record.get("count") or 0)
        elif event == "step":
            step = None
            action, actions = "", 0
            code = record.get("exit")
            label, took = _step_label(record.get("step", "")), duration_vi(record.get("seconds") or 0)
            text = f"xong {label} ({took})" if code == 0 else f"{label}: lỗi, exit {code} ({took})"
            add(text + (f"; Kite dừng agent vì lặp: {record['stuck']}" if record.get("stuck") else ""), at)
        elif event == "fast_start":
            phase = ("đường nhanh: model viết bản sửa trong một lần gọi"
                     + (f" (thêm {record['helpers']} worker rảnh viết song song)" if record.get("helpers") else ""), at)
        elif event == "fast_note":
            add(f"đường nhanh, {record.get('note')}", at)
        elif event == "fast_path":
            phase = None
            add(f"đường nhanh: {FAST_OUTCOMES.get(record.get('outcome'), record.get('outcome'))} "
                f"({duration_vi(record.get('seconds') or 0)})", at)
        elif event == "review_start":
            phase = ("rà soát bản sửa (một lần gọi model)", at)
        elif event == "review_end":
            phase = None
            add(f"rà soát một lần gọi xong ({duration_vi(record.get('seconds') or 0)})", at)
        elif event == "deps_cached":
            add("dùng lại thư viện đã cài, bỏ qua bước cài", at)
        elif event == "run_start":
            add(f"bắt đầu: model {record.get('model')} trên worker {record.get('worker')}", at)
        elif event == "run_end":
            phase = step = None
            add("kết thúc: " + ("Kite đã tự chạy test và qua" if record.get("verified") else
                                "chưa qua được test của Kite") + f" (agent: {record.get('agent_status')})", at)
        elif event == "run_error":
            phase = step = None
            add(f"dừng vì lỗi: {record.get('error')}", at)
        elif event == "run_cancelled":
            phase = step = None
            add("đã hủy theo yêu cầu: dừng mọi bước đang chạy", at)
        elif event == "continue_from":
            add(f"tiếp tục từ kết quả task {record.get('task')} (bản sửa trước đã có sẵn)", at)
    current = step or phase
    report: dict[str, Any] = {"elapsed": duration_vi(now - trace[0]["ts"]) if trace else None,
                              "progress": lines[-limit:]}
    if current is not None:
        report["now"] = f"{current[0]} (đã {duration_vi(now - current[1])})"
        if step is not None and action:
            report["now"] += f"; vừa {action} ({actions} thao tác)"
    return report


def task_slug(task_id: str) -> str:
    """A Docker- and path-safe name that stays unique for any task ID."""
    base = re.sub(r"[^a-z0-9-]+", "-", task_id.lower()).strip("-")[:40] or "task"
    return f"{base}-{hashlib.sha1(task_id.encode('utf-8')).hexdigest()[:8]}"


# How the self-review pass ended, as shown in the task summary.
REVIEW_NOTES = {"unchanged": "; rà soát: không cần sửa", "kept": "; rà soát: đã sửa thêm, test vẫn qua",
                "reverted": "; rà soát: làm hỏng test nên đã trả lại bản trước"}


class SandboxActivityBackend:
    """``KiteActivityBackend`` implementation used by ``kite workflow worker``."""

    def __init__(self, store, settings: sbx.SandboxSettings, *, state_dir: Path, models,
                 aliases: Mapping[str, str] | None = None, lease_seconds: int = 600,
                 max_observation_age_seconds: int = 300, runner=sbx.default_runner,
                 clock: Callable[[], float] = time.time,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 notifier: Callable[..., None] | None = None) -> None:
        self.store = store
        self.settings = settings
        self.state_dir = Path(state_dir)
        self.models = list(models)
        self.aliases = dict(aliases or {})          # alias -> base model, e.g. qwen3.6-fast -> qwen3.6
        self.lease_seconds = lease_seconds
        self.max_observation_age_seconds = max_observation_age_seconds
        self.runner = runner
        self.clock = clock
        self.heartbeat_seconds = heartbeat_seconds
        self.journal = Journal(self.state_dir / "journal", clock=clock)
        self.notifier = notifier                  # notify.Notifier: a push message when a task finishes

    # ------------------------------------------------------------------ helpers
    def task_dir(self, task_id: str) -> Path:
        return self.state_dir / "tasks" / task_slug(task_id)

    def _trace(self, task_id: str, event: str, **fields) -> None:
        """One line in the task's own trace.jsonl (every step, as it happens)."""
        append_jsonl(self.task_dir(task_id) / "trace.jsonl", {"event": event, **fields}, clock=self.clock)

    def _notify(self, run: dict) -> None:
        if self.notifier:
            from .notify import task_message

            title, message, tags, priority = task_message(run)
            self.notifier(title, message, tags=tags, priority=priority)

    def _small_model(self, model: str) -> str | None:
        return next((alias for alias, base in self.aliases.items() if base == model), None)

    def _docker(self, *args: str) -> str:
        result = self.runner([*self.settings.docker_command, *args], timeout=60)
        return str(getattr(result, "stdout", "") or "")

    async def _run_container(self, name: str, args: list[str], timeout: int,
                             heartbeat: Callable[[dict], None] | None = None,
                             state: str = "RUNNING", task_id: str = "",
                             watch: sbx.StuckWatch | None = None) -> tuple[int, str, float]:
        """Run one sandbox container, heartbeating Temporal while it works; kill it if cancelled."""
        if task_id:
            self._check_cancelled(task_id)
        await asyncio.to_thread(self._docker, "rm", "-f", name)     # leftover from a crashed attempt
        if task_id:
            self._trace(task_id, "step_start", step=name.split("-")[1], state=state)
        started = time.monotonic()
        step = asyncio.create_task(asyncio.to_thread(
            sbx._run_step, self.settings, self.runner, name=name, args=args, timeout=timeout, watch=watch))
        try:
            while True:
                done, _ = await asyncio.wait({step}, timeout=self.heartbeat_seconds)
                if done:
                    break
                if heartbeat is not None:
                    heartbeat({"task_id": task_id, "state": state, "container": name,
                               "seconds": round(time.monotonic() - started)})
        except asyncio.CancelledError:
            await asyncio.to_thread(self._docker, "kill", name)
            raise
        code, output = step.result()
        seconds = round(time.monotonic() - started, 1)
        if task_id:
            self._check_cancelled(task_id)
            self._trace(task_id, "step", step=name.split("-")[1], state=state, seconds=seconds, exit=code,
                        output_chars=len(output), tail=output[-400:] if code != 0 else None,
                        stuck=watch.reason if watch is not None and watch.reason else None)
        return code, output, seconds

    def _check_cancelled(self, task_id: str) -> None:
        if cancel_marker(self.state_dir, task_id).exists():
            raise TaskCancelled(task_id)

    def _cancellable(self, task_id: str, ask: Callable[[str], str] | None) -> Callable[[str], str] | None:
        """A model asker that refuses to start a request once the task is cancelled."""
        if ask is None:
            return None

        def guarded(prompt: str) -> str:
            self._check_cancelled(task_id)
            return ask(prompt)
        return guarded

    def _action_tracer(self, task_id: str, kind: str) -> Callable[[str], None]:
        """The agent's tool calls into trace.jsonl, at most one line per ACTION_TRACE_SECONDS."""
        state = {"count": 0, "last": 0.0}

        def on_action(action: str) -> None:
            state["count"] += 1
            now = time.monotonic()
            if now - state["last"] >= ACTION_TRACE_SECONDS:
                state["last"] = now
                self._trace(task_id, "agent_action", step=kind, action=action, count=state["count"])
        return on_action

    def _gateway_cancel(self, tag: str) -> None:
        """Drop the task's model requests at the gateway (queued ones and the one generating)."""
        base = (self.settings.host_gateway_url or self.settings.gateway_url).rstrip("/")
        root = base[:-len("/v1")] if base.endswith("/v1") else base
        request = urllib.request.Request(root + "/kite/cancel", json.dumps({"tag": tag}).encode(),
                                         {"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=10).read()
        except Exception:                 # the gateway may be down: the containers are killed anyway
            pass

    def _worker_result(self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult) -> WorkerResult:
        return WorkerResult(task.task_id, route.lease_id, route.worker_id, result.base_repo_head,
                            result.patch or NO_CHANGES_PATCH, result.summary, result.usage_gpu_seconds)

    # ------------------------------------------------------------------ KiteActivityBackend
    async def reconcile_operation(self, operation_name: str, operation_key: str, *context: object) -> RecoveryDecision:
        # Every step is safe to repeat: the clone is reused, containers are recreated by name,
        # the store's reservation is idempotent per key, and leases settle once.
        return RecoveryDecision(recovered=False)

    async def validate_task(self, task: CodingTaskInput, key: str) -> None:
        repo = Path(task.repo_path)
        if sbx._git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
            raise ApplicationError(f"{repo} is not a Git repository", non_retryable=True)
        if sbx._git(repo, "cat-file", "-t", task.repo_head, check=False) != "commit":
            raise ApplicationError(f"commit {task.repo_head} does not exist in {repo}", non_retryable=True)
        if task.preferred_model_id and task.preferred_model_id not in self.models:
            raise ApplicationError(f"model {task.preferred_model_id} is not served by the gateway",
                                   non_retryable=True)
        policy = getattr(self.store, "model_policy", None)
        if task.preferred_model_id and policy is not None and \
                self.aliases.get(task.preferred_model_id, task.preferred_model_id) in policy.excluded:
            raise ApplicationError(f"model {task.preferred_model_id} is chat-only (routing.chat_only): it cannot "
                                   "run a sandbox task", non_retryable=True)
        if not task.goal.strip():
            raise ApplicationError("task goal is empty", non_retryable=True)

    async def prepare_workspace(self, task: CodingTaskInput, key: str) -> str:
        root = self.task_dir(task.task_id)
        work, deps = root / "work", root / "deps"
        if (work / ".git").exists():
            base = sbx._git(work, "rev-parse", "--verify", "-q", "refs/kite/base", check=False)
            ready = sbx._git(work, "rev-parse", "--verify", "-q", "refs/kite/patch-base", check=False)
            if base == task.repo_head and ready:
                return str(work)
            if base and base != task.repo_head:
                raise ApplicationError(f"{work} was prepared for commit {base}, not {task.repo_head}",
                                       non_retryable=True)
        if deps.is_symlink():                      # a link into the shared deps cache: drop the link only
            deps.unlink()
        for leftover in (work, deps):              # an earlier attempt stopped half-way: start over
            if leftover.exists():
                shutil.rmtree(leftover)
        root.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(sbx.prepare_clone, Path(task.repo_path), work,
                                    head=task.repo_head, require_clean=False)
        except sbx.SandboxError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        sbx._git(work, "update-ref", "refs/kite/base", task.repo_head)
        (deps / "cache").mkdir(parents=True, exist_ok=True)
        try:
            settings = sbx.repo_settings(self.settings, work, memory.repo_config_path(self.state_dir, task.repo_path))
        except sbx.SandboxError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        if settings is not self.settings:
            self._trace(task.task_id, "repo_config", setup=settings.setup_command, test=settings.test_command,
                        check=settings.check_command or None)
        cache_root = self.state_dir / "deps-cache" if settings.deps_cache else None
        key = sbx.deps_key(settings, work, settings.setup_command) if cache_root is not None else None
        cached = sbx.cached_deps(cache_root, key)
        if cached is not None:
            sbx.link_deps(deps, cached)
            self._trace(task.task_id, "deps_cached", key=key)
        elif settings.setup_command:
            before = sbx.workspace_state(work)
            name = sbx.container_name("setup", task_slug(task.task_id))
            try:
                code, output, seconds = await self._run_container(
                    name, sbx.docker_run_args(self.settings, name=name, workspace=work,
                                              network=self.settings.setup_network, deps=deps)
                    + ["sh", "-c", settings.setup_command],
                    settings.setup_timeout_seconds, task_id=task.task_id)
            except TaskCancelled:
                return str(work)              # the workflow sees the cancel and ends before any run
            (root / "setup.log").write_text(output, encoding="utf-8")
            if code != 0:
                raise ApplicationError(f"setup command failed (exit {code}) after {seconds}s; "
                                       f"see {root / 'setup.log'}", non_retryable=True)
            if sbx.workspace_state(work) == before and await asyncio.to_thread(sbx.store_deps, cache_root, key, deps):
                self._trace(task.task_id, "deps_stored", key=key)
        patch_base = sbx.commit_setup_changes(work)
        sbx._git(work, "update-ref", "refs/kite/patch-base", patch_base)
        if task.base_patch:
            # "Sửa tiếp": the previous task's result goes into the clone as uncommitted changes, so the
            # agent builds on it and this task's patch holds both (it applies to the same commit).
            applied = await asyncio.to_thread(sbx.apply_patch_file, work, Path(task.base_patch))
            if applied:
                raise ApplicationError(f"the previous result does not apply to {task.repo_head[:12]}: {applied}",
                                       non_retryable=True)
            self._trace(task.task_id, "continue_from", task=task.base_task or None)
        return str(work)

    async def reserve_route(self, task: CodingTaskInput, key: str) -> RouteLease | None:
        reservation = await asyncio.to_thread(
            self.store.reserve_for_task, task.task_id, key, self.clock(),
            self.lease_seconds, self.max_observation_age_seconds)
        if reservation is None:
            return None
        return RouteLease(task.task_id, reservation.lease.lease_id, reservation.lease.owner_id,
                          reservation.provider_id, reservation.lease.worker_id,
                          reservation.model_id, reservation.reserved_gpu_seconds)

    async def prepare_opencode_session(self, task: CodingTaskInput, route: RouteLease,
                                       workspace_path: str, prior_session_id: str | None, key: str) -> str:
        return prior_session_id or f"sandbox-{task_slug(task.task_id)}"

    async def heartbeat_route(self, task: CodingTaskInput, route: RouteLease, key: str) -> None:
        await asyncio.to_thread(self.store.heartbeat, route.lease_id, self.clock(), self.lease_seconds)

    async def run_opencode(self, task: CodingTaskInput, route: RouteLease, workspace_path: str,
                           instruction: str, session_id: str, key: str,
                           heartbeat: Callable[[dict], None]) -> CodingRunResult:
        root, work, slug = self.task_dir(task.task_id), Path(workspace_path), task_slug(task.task_id)
        deps = root / "deps"
        patch_base = sbx._git(work, "rev-parse", "refs/kite/patch-base")
        config_file = root / "opencode.json"
        config_file.write_text(json.dumps(sbx.agent_config(
            self.settings, model=route.model_id, models=self.models,
            small_model=self._small_model(route.model_id),
            task_tag=clean_tag(task.task_id) or slug, worker_pin=route.worker_id), indent=2), encoding="utf-8")
        goal = task.goal.strip()
        if instruction.strip():
            goal += f"\n\nChỉ dẫn bổ sung của người dùng (ưu tiên làm theo): {instruction.strip()}"
        # What the person said went wrong in earlier tasks on this repository (memory.py).
        lessons = memory.lessons_note(self.state_dir, task.repo_path)
        goal += lessons

        graph = await asyncio.to_thread(sbx.code_graph, self.settings, work, root / "graph")
        context = await asyncio.to_thread(sbx.repo_context, work, goal, graph=graph)
        self._trace(task.task_id, "run_start", model=route.model_id, worker=route.worker_id, lease=route.lease_id,
                    task_type=task.task_type, instruction=short(instruction, 1000), context_chars=len(context),
                    code_map=len(graph.symbols) if graph is not None else None,
                    lessons=lessons.count("\n- ") or None)
        try:
            return await self._run_agent(task, route, work, root, slug, deps, patch_base, config_file, goal,
                                         instruction, context, session_id, heartbeat, graph)
        except TaskCancelled:
            self._trace(task.task_id, "run_cancelled")
            self.journal.write("tasks", {"task_id": task.task_id, "task_type": task.task_type,
                                         "repo": task.repo_path, "model": route.model_id,
                                         "worker": route.worker_id, "status": "cancelled"})
            return CodingRunResult(task.task_id, task.repo_head, session_id, "", "Task đã bị hủy theo yêu cầu.",
                                   (), 0)
        except BaseException as exc:
            # A crash (or cancel) mid-run still leaves a line saying how far it got.
            self._trace(task.task_id, "run_error", error=short(f"{type(exc).__name__}: {exc}", 500))
            failed = {"task_id": task.task_id, "task_type": task.task_type, "repo": task.repo_path,
                      "model": route.model_id, "worker": route.worker_id, "status": "error",
                      "error": short(f"{type(exc).__name__}: {exc}", 500)}
            self.journal.write("tasks", failed)
            if not isinstance(exc, asyncio.CancelledError):
                self._notify(failed)
            raise

    async def _run_agent(self, task: CodingTaskInput, route: RouteLease, work: Path, root: Path, slug: str,
                         deps: Path, patch_base: str, config_file: Path, goal: str, instruction: str,
                         context: str, session_id: str, heartbeat: Callable[[dict], None],
                         graph: sbx.codemap.CodeGraph | None = None) -> CodingRunResult:
        # The repo's .kite.yml, or the config Kite keeps for it (already validated in setup).
        settings = sbx.repo_settings(self.settings, work, memory.repo_config_path(self.state_dir, task.repo_path))
        template = sbx.quick_template(settings)
        test_command = settings.test_command
        check_template = settings.check_command if test_command else ""
        prompt = sbx.agent_prompt(goal, settings.test_command, task_type=task.task_type, context=context,
                                  quick=template, check=check_template)
        attempt, seconds, test_code, test_output = 0, 0.0, None, ""
        check_code, check_output, check_ran = None, "", ""

        async def run_tests(after: str, last_output: str = "") -> tuple[int, str, str]:
            """The related tests first (seconds), the full suite only if they pass."""
            tests = await asyncio.to_thread(sbx.select_tests, work, patch_base, graph, last_output)
            quick = sbx.quick_test_command(template, tests)
            if quick:
                name = sbx.container_name("quick", slug)
                code, output, _ = await self._run_container(
                    name, sbx.docker_run_args(self.settings, name=name, workspace=work, network="none", deps=deps)
                    + ["sh", "-c", quick], settings.test_timeout_seconds, heartbeat, "TESTING", task.task_id)
                with (root / "test.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"\n===== quick after {after} exit={code} =====\n{output}")
                if code == 1:
                    return code, output, quick
            name = sbx.container_name("test", slug)
            code, output, _ = await self._run_container(
                name, sbx.docker_run_args(self.settings, name=name, workspace=work, network="none", deps=deps)
                + ["sh", "-c", test_command], settings.test_timeout_seconds, heartbeat, "TESTING", task.task_id)
            with (root / "test.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n===== after {after} exit={code} =====\n{output}")
            return code, output, test_command

        async def run_check(after: str) -> tuple[int | None, str, str]:
            """After the tests pass: Python syntax of the changed files, then the repo's own check."""
            files = await asyncio.to_thread(sbx.changed_files, work, patch_base)
            problems = await asyncio.to_thread(sbx.syntax_errors, work, files)
            if problems:
                with (root / "test.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"\n===== syntax after {after} =====\n{problems}")
                return 1, problems, sbx.syntax_command(problems)
            command = sbx.check_command(check_template, files)
            if not command:
                return None, "", ""
            name = sbx.container_name("check", slug)
            code, output, _ = await self._run_container(
                name, sbx.docker_run_args(self.settings, name=name, workspace=work, network="none", deps=deps)
                + ["sh", "-c", command], settings.test_timeout_seconds, heartbeat, "TESTING", task.task_id)
            with (root / "test.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n===== check after {after} exit={code} =====\n{output}")
            return code, output, command
        stuck_stops = 0
        tools = {"agent_steps": 0, "tool_calls": 0, "repeat_reads": 0}

        def count_tools(text: str) -> None:
            stats = sbx.tool_stats(text)
            for key in tools:
                tools[key] += stats[key]
        status, code, output = "not_started", None, ""
        fast_status, fast_seconds, ask, repro, fast_attempts = "off", 0.0, None, "off", 0
        if self.settings.fast_path and task.task_type == "coding" and test_command:
            ask = self._cancellable(task.task_id, sbx.gateway_asker(
                self.settings, route.model_id, tag=clean_tag(task.task_id) or slug, pin=route.worker_id))
            if ask is not None:
                started = time.monotonic()
                loop = asyncio.get_running_loop()

                # sbx.fast_path runs in a thread; its test runs are this loop's containers.
                def in_loop(coroutine):
                    return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

                async def one(kind: str, command: str) -> tuple[int, str]:
                    name = sbx.container_name(kind, slug)
                    code_, output_, _ = await self._run_container(
                        name, sbx.docker_run_args(self.settings, name=name, workspace=work, network="none",
                                                  deps=deps) + ["sh", "-c", command],
                        settings.test_timeout_seconds, heartbeat, "TESTING", task.task_id)
                    with (root / "test.log").open("a", encoding="utf-8") as stream:
                        stream.write(f"\n===== {kind} exit={code_} =====\n{output_}")
                    return code_, output_

                # Best-of-N: another idle worker with the same model writes a second candidate meanwhile.
                others = await asyncio.to_thread(sbx.idle_helpers, self.settings, route.model_id,
                                                 exclude=route.worker_id)
                helpers = [self._cancellable(task.task_id, sbx.gateway_asker(
                    self.settings, route.model_id, tag=f"{clean_tag(task.task_id) or slug}-b{i + 2}", pin=other))
                    for i, other in enumerate(others)]
                self._trace(task.task_id, "fast_start", helpers=len(helpers))
                step = asyncio.create_task(asyncio.to_thread(
                    sbx.fast_path, work, goal, graph, ask, test_command=test_command, check=check_template,
                    template=template, run_tests=lambda after: in_loop(run_tests(after)),
                    run_check=lambda after: in_loop(run_check(after)),
                    run_one=lambda kind, command: in_loop(one(kind, command)),
                    helpers=helpers, retries=self.settings.fast_retries,
                    progress=lambda line: self._trace(task.task_id, "fast_note", note=line)))
                while True:
                    done, _ = await asyncio.wait({step}, timeout=self.heartbeat_seconds)
                    if done:
                        break
                    heartbeat({"task_id": task.task_id, "state": "FAST_PATH",
                               "seconds": round(time.monotonic() - started)})
                fast = step.result()
                fast_status, repro, fast_attempts = fast.status, fast.repro, fast.attempts
                with (root / "agent.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} fast repro {fast.repro} "
                                 f"model={route.model_id} =====\n{fast.repro_answer}")
                    stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} fast {fast.status} "
                                 f"({fast.attempts} bản) model={route.model_id} =====\n{fast.answer}")
                    for line in fast.log:
                        stream.write(f"\n{line}")
                if fast.status == "passed":
                    status, code, output = "ok", 0, fast.answer
                    test_code, test_output = 0, fast.test_output
                    check_code, check_output = fast.check_code, fast.check_output
                    check_ran = check_template if fast.check_code is not None else ""
                else:
                    prompt += fast.note
                    test_output = fast.failing_output
                fast_seconds = round(time.monotonic() - started, 1)
                self._trace(task.task_id, "fast_path", outcome=fast_status, repro=repro, seconds=fast_seconds,
                            attempts=fast_attempts, parallel=len(helpers),
                            reason=fast.reason or None, files=fast.files or None, context_chars=fast.context_chars)
        while fast_status != "passed":
            kind = "agent" if attempt == 0 else f"fix{attempt}"
            name = sbx.container_name(kind, slug)
            watch = sbx.StuckWatch(on_action=self._action_tracer(task.task_id, kind))
            code, output, step_seconds = await self._run_container(
                name, sbx.docker_run_args(self.settings, name=name, workspace=work, network=self.settings.network,
                                          config_file=config_file, deps=deps)
                + ["opencode", "run", "-m", f"kite/{route.model_id}", prompt],
                self.settings.agent_timeout_seconds, heartbeat, "AGENT_RUNNING" if attempt == 0 else "FIXING",
                task.task_id, watch)
            seconds = round(seconds + step_seconds, 1)
            with (root / "agent.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {kind} model={route.model_id} "
                             f"exit={code} =====\n")
                stream.write(output)
            status = sbx.AGENT_STATUS.get(code, "failed")
            stuck_stops += status == "stuck"
            count_tools(output)
            if not test_command:
                break
            test_code, test_output, ran = await run_tests(kind, test_output)
            failed_check = ""
            check_code, check_output, check_ran = None, "", ""      # the check only counts on passing tests
            if test_code == 0:
                check_code, check_output, check_ran = await run_check(kind)
                if check_code in sbx.CHECK_UNAVAILABLE:
                    break                           # the tool is missing: setup's job, not the agent's
                if check_code not in (None, 0):
                    failed_check = check_ran
            if ((test_code == 0 and not failed_check) or status not in ("ok", "stuck")
                    or attempt >= self.settings.fix_rounds):
                break
            attempt += 1
            if failed_check:
                prompt = sbx.fix_prompt(goal, test_command, check_output, attempt, self.settings.fix_rounds,
                                        quick=template, stuck=watch.reason, check=failed_check)
            else:
                prompt = sbx.fix_prompt(goal, ran, test_output, attempt, self.settings.fix_rounds,
                                        sbx.codemap.failing_targets(graph, test_output) if graph is not None else "",
                                        quick=template, stuck=watch.reason)

        review, review_mode = "skipped", ""
        if sbx.wants_review(self.settings, task.task_type, test_command, status, test_code) and check_code in (None, 0):
            before = await asyncio.to_thread(sbx.snapshot_tree, work)
            # A fast fix that passed, or a small agent patch whose tests pass, gets the one-call review; a
            # second agent session (5-30 minutes, thrown away 5 times out of 7 after a passed fast fix on the
            # polyglot exercises) is kept for big agent changes.
            small = (fast_status != "passed" and status == "ok" and test_code == 0
                     and sum(sbx.patch_size(sbx._git(work, "diff", patch_base, before, check=False)))
                     <= sbx.SMALL_REVIEW_LINES)
            if (fast_status == "passed" or small) and self.settings.fast_review and ask is not None:
                review_mode = "one_call"
                self._trace(task.task_id, "review_start", mode=review_mode)
                started = time.monotonic()
                step = asyncio.create_task(asyncio.to_thread(
                    sbx.fast_review, work, goal, patch_base, graph, ask, test_command, check_template))
                while True:
                    done, _ = await asyncio.wait({step}, timeout=self.heartbeat_seconds)
                    if done:
                        break
                    heartbeat({"task_id": task.task_id, "state": "REVIEWING",
                               "seconds": round(time.monotonic() - started)})
                review_code, review_output = step.result()
                step_seconds = round(time.monotonic() - started, 1)
                self._trace(task.task_id, "review_end", mode=review_mode, exit=review_code, seconds=step_seconds)
                if review_code != 0:
                    await asyncio.to_thread(sbx.restore_tree, work, before)
            else:
                review_mode = "agent"
            name = sbx.container_name("review", slug)
            if review_mode == "agent":
                review_code, review_output, step_seconds = await self._run_container(
                    name, sbx.docker_run_args(self.settings, name=name, workspace=work, network=self.settings.network,
                                              config_file=config_file, deps=deps)
                    + ["opencode", "run", "-m", f"kite/{route.model_id}",
                       sbx.review_prompt(goal, test_command, await asyncio.to_thread(
                           sbx.codemap.impact, graph, work, patch_base) if graph is not None else "", quick=template,
                           edited=await asyncio.to_thread(sbx.edited_tests, work, patch_base), check=check_template,
                           scratch=await asyncio.to_thread(sbx.scratch_files, work, patch_base))],
                    self.settings.agent_timeout_seconds, heartbeat, "REVIEWING", task.task_id,
                    sbx.StuckWatch(on_action=self._action_tracer(task.task_id, "review")))
            seconds = round(seconds + step_seconds, 1)
            if review_mode == "agent":
                count_tools(review_output)
            with (root / "agent.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} review ({review_mode}) "
                             f"model={route.model_id} exit={review_code} =====\n")
                stream.write(review_output)
            if await asyncio.to_thread(sbx.snapshot_tree, work) == before:
                review = "unchanged"
            else:
                code_after, test_after, _ = await run_tests("review")
                check_after, check_after_output, check_after_ran = (
                    await run_check("review") if code_after == 0 else (None, "", ""))
                if review_code == 0 and code_after == 0 and check_after in (None, 0):
                    review, output, test_output = "kept", review_output, test_after
                    check_code, check_output, check_ran = check_after, check_after_output, check_after_ran
                else:
                    await asyncio.to_thread(sbx.restore_tree, work, before)
                    review = "reverted"

        patch_path = root / "result.patch"
        tests_edited = await asyncio.to_thread(sbx.edited_tests, work, patch_base)
        scratch = await asyncio.to_thread(sbx.scratch_files, work, patch_base)
        changed = await asyncio.to_thread(sbx.collect_patch, work, patch_base, patch_path)
        verified = status in ("ok", "stuck") and test_code == 0 and check_code in (None, 0) and bool(changed)
        patch = patch_path.read_text(encoding="utf-8")

        lines = [f"Agent {route.model_id} on {route.worker_id}: {status} (exit {code}, {seconds}s"
                 f"{f', {attempt} vòng sửa sau test lỗi' if attempt else ''}"
                 f"{REVIEW_NOTES.get(review, '')}).",
                 f"Files changed: {', '.join(changed) if changed else 'none'}."]
        if fast_status == "passed":
            evidence = {"reproduced": "a reproduction test written from the goal failed before the fix and passes "
                                      "now (it is part of the patch)",
                        "not_reproduced": "its reproduction test also passed before the fix, so the agent reviewed it",
                        "broken": "its reproduction test failed for setup reasons (sandbox, fixtures), not the bug, "
                                  "so the agent reviewed it",
                        }.get(repro, "no reproduction test, so the agent reviewed it")
            lines.insert(0, f"Kite fast path: the fix came from "
                            f"{'one model call' if fast_attempts <= 1 else f'candidate {fast_attempts} of a few one-call fixes'}"
                            f" ({fast_seconds}s in all) and passed "
                            f"Kite's tests; {evidence}; no agent session was needed to write it.")
        elif fast_status not in ("off", "skipped"):
            lines.insert(0, f"Kite fast path: tried ({fast_status}, {fast_seconds}s), then the agent took over.")
        if stuck_stops:
            lines.append(f"Kite stopped the agent {stuck_stops} time(s) for repeating the same steps.")
        added, removed = sbx.patch_size(patch)
        # What it cost: model time on the workers (the Kaggle quota) and tokens, from the gateway journal.
        usage = task_usage(self.journal, clean_tag(task.task_id) or slug)
        lines.append(f"Cost: {usage['model_requests']} model calls, {round(usage['gpu_seconds'] / 60, 1)} GPU "
                     f"minutes, {usage['prompt_tokens']} prompt + {usage['completion_tokens']} generated tokens"
                     + (f"; agent: {tools['tool_calls']} tool calls, {tools['repeat_reads']} re-reads"
                        if tools["tool_calls"] else "")
                     + f"; patch +{added}/-{removed} lines.")
        if tests_edited:
            lines.append(f"NOTE: existing tests were changed: {', '.join(tests_edited)} - check that this was "
                         "intended and not just to make tests pass.")
        if scratch:
            lines.append(f"NOTE: new files that look like experiments: {', '.join(scratch)} - drop them if they "
                         "are not part of the change.")
        if test_code is None:
            lines.append("Kite test run: skipped (no sandbox.test_command).")
        else:
            verdict = "PASSED" if test_code == 0 else f"FAILED (exit {test_code})"
            lines.append(f"Kite test run `{settings.test_command}` (no network): {verdict}.")
            lines.append(sbx._tail(test_output, 1500).strip())
        if check_code is not None:
            if check_code in sbx.CHECK_UNAVAILABLE:
                lines.append(f"Kite check `{check_ran}` could not run (exit {check_code}): does `setup` in .kite.yml "
                             "install its tool?")
            else:
                lines.append(f"Kite check `{check_ran}` (no network): "
                             f"{'PASSED' if check_code == 0 else f'FAILED (exit {check_code})'}.")
            if check_code != 0:
                lines.append(sbx._tail(check_output, 1000).strip())
        # Analysis / review / docs answers live in the agent's output: keep much more of it.
        lines += ["Agent output (tail):", sbx._tail(output, 1500 if task.task_type == "coding" else 6000).strip()]
        summary = "\n".join(line for line in lines if line)
        (root / "result.json").write_text(json.dumps({
            "task_id": task.task_id, "model": route.model_id, "worker": route.worker_id,
            "agent_status": status, "agent_exit_code": code, "agent_seconds": seconds,
            "changed_files": changed, "test_exit_code": test_code, "fix_rounds_used": attempt,
            "review": review, "stuck_stops": stuck_stops, "tests_edited": tests_edited,
            "check_command": check_ran, "check_exit_code": check_code, "scratch_files": scratch,
            "fast_path": fast_status, "fast_seconds": fast_seconds, "review_mode": review_mode, "repro": repro,
            "fast_attempts": fast_attempts, "patch_added": added, "patch_removed": removed, **tools, **usage,
            "verified": verified,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        run = {"task_id": task.task_id, "task_type": task.task_type, "repo": task.repo_path,
               "model": route.model_id, "worker": route.worker_id, "goal": short(task.goal, 500),
               "instruction": short(instruction, 500), "status": "finished", "agent_status": status,
               "agent_exit": code, "seconds": seconds, "fix_rounds": attempt, "review": review,
               "test_command": test_command, "test_exit": test_code, "stuck_stops": stuck_stops,
               "tests_edited": tests_edited, "check_exit": check_code, "scratch_files": scratch,
               "fast_path": fast_status, "fast_seconds": fast_seconds, "review_mode": review_mode,
               "repro": repro, "fast_attempts": fast_attempts, "verified": verified,
               "changed_files": changed, "lines_added": added, "lines_removed": removed,
               "context_chars": len(context), **tools, **usage}
        trace = read_jsonl(root / "trace.jsonl")          # the whole task, setup included
        if trace and isinstance(trace[0].get("ts"), (int, float)):
            run["wall_seconds"] = round(self.clock() - trace[0]["ts"], 1)
        self._trace(task.task_id, "run_end", **{k: v for k, v in run.items() if k not in ("task_id", "goal")})
        self.journal.write("tasks", run)
        self._notify(run)
        return CodingRunResult(task.task_id, task.repo_head, session_id, patch, summary,
                               tuple(changed), int(seconds))

    async def interrupt_opencode(self, task: CodingTaskInput, route: RouteLease, session_id: str, key: str) -> None:
        slug = task_slug(task.task_id)
        if cancel_marker(self.state_dir, task.task_id).exists():
            # Cancelled: every container of the task (fast path, repro, tests, agent...) and every model
            # request it has at the gateway; the run then stops at its next step (TaskCancelled).
            await asyncio.to_thread(self._gateway_cancel, clean_tag(task.task_id) or slug)
            names = await asyncio.to_thread(self._docker, "ps", "-q", "--filter", f"name=-{slug}")
            for container in (names or "").split():
                await asyncio.to_thread(self._docker, "kill", container)
            return
        for kind in ("agent", "test", "setup", "review",
                     *(f"fix{i}" for i in range(1, self.settings.fix_rounds + 1))):
            await asyncio.to_thread(self._docker, "kill", sbx.container_name(kind, slug))

    async def checkpoint_and_stop(self, task: CodingTaskInput, route: RouteLease,
                                  result: CodingRunResult, key: str) -> str:
        root = self.task_dir(task.task_id)
        path = root / "checkpoint.json"
        path.write_text(json.dumps({
            "task_id": task.task_id, "repo_path": task.repo_path, "repo_head": task.repo_head,
            "branch": task.branch, "route": asdict(route), "session_id": result.session_id,
            "changed_files": list(result.changed_files), "summary": result.summary,
            "patch_path": str(root / "result.patch"), "saved_at": time.time(),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)

    async def build_review_bundle(self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult,
                                  checkpoint_path: str, key: str) -> ReviewBundle:
        return build_review_bundle(self._worker_result(task, route, result),
                                   expected_repo_head=task.repo_head, expected_task_id=task.task_id,
                                   expected_lease_id=route.lease_id, expected_worker_id=route.worker_id)

    async def release_route(self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult | None,
                            outcome: str, key: str) -> None:
        now = self.clock()
        try:
            if outcome == "COMPLETED" and result is not None:
                await asyncio.to_thread(self.store.complete, route.lease_id,
                                        self._worker_result(task, route, result), now)
            elif outcome == "CANCELLED":
                await asyncio.to_thread(self.store.cancel, task.task_id, now)
            else:
                await asyncio.to_thread(self.store.fail, route.lease_id, f"task {outcome.lower()}", False, now)
        except StoreError as exc:
            # Already settled by an earlier attempt, or the lease expired: never fail the workflow here.
            print(f"kite: release_route({outcome}) for {task.task_id}: {exc}", file=sys.stderr)
