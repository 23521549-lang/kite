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
from typing import Callable, Mapping

from temporalio.exceptions import ApplicationError

from . import sandbox as sbx
from .journal import Journal, append_jsonl, clean_tag, short
from .integration import ReviewBundle, build_review_bundle
from .protocol import WorkerResult
from .store import StoreError
from .temporal_state import RecoveryDecision
from .temporal_workflow import CodingRunResult, CodingTaskInput, RouteLease

HEARTBEAT_SECONDS = 20.0
NO_CHANGES_PATCH = "# Kite: the agent made no changes\n"


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
                 heartbeat_seconds: float = HEARTBEAT_SECONDS) -> None:
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

    # ------------------------------------------------------------------ helpers
    def task_dir(self, task_id: str) -> Path:
        return self.state_dir / "tasks" / task_slug(task_id)

    def _trace(self, task_id: str, event: str, **fields) -> None:
        """One line in the task's own trace.jsonl (every step, as it happens)."""
        append_jsonl(self.task_dir(task_id) / "trace.jsonl", {"event": event, **fields}, clock=self.clock)

    def _small_model(self, model: str) -> str | None:
        return next((alias for alias, base in self.aliases.items() if base == model), None)

    def _docker(self, *args: str) -> None:
        self.runner([*self.settings.docker_command, *args], timeout=60)

    async def _run_container(self, name: str, args: list[str], timeout: int,
                             heartbeat: Callable[[dict], None] | None = None,
                             state: str = "RUNNING", task_id: str = "",
                             watch: sbx.StuckWatch | None = None) -> tuple[int, str, float]:
        """Run one sandbox container, heartbeating Temporal while it works; kill it if cancelled."""
        await asyncio.to_thread(self._docker, "rm", "-f", name)     # leftover from a crashed attempt
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
            self._trace(task_id, "step", step=name.split("-")[1], state=state, seconds=seconds, exit=code,
                        output_chars=len(output), tail=output[-400:] if code != 0 else None,
                        stuck=watch.reason if watch is not None and watch.reason else None)
        return code, output, seconds

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
            settings = sbx.repo_settings(self.settings, work)
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
            code, output, seconds = await self._run_container(
                name, sbx.docker_run_args(self.settings, name=name, workspace=work,
                                          network=self.settings.setup_network, deps=deps)
                + ["sh", "-c", settings.setup_command],
                settings.setup_timeout_seconds, task_id=task.task_id)
            (root / "setup.log").write_text(output, encoding="utf-8")
            if code != 0:
                raise ApplicationError(f"setup command failed (exit {code}) after {seconds}s; "
                                       f"see {root / 'setup.log'}", non_retryable=True)
            if sbx.workspace_state(work) == before and await asyncio.to_thread(sbx.store_deps, cache_root, key, deps):
                self._trace(task.task_id, "deps_stored", key=key)
        patch_base = sbx.commit_setup_changes(work)
        sbx._git(work, "update-ref", "refs/kite/patch-base", patch_base)
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

        graph = await asyncio.to_thread(sbx.code_graph, self.settings, work, root / "graph")
        context = await asyncio.to_thread(sbx.repo_context, work, goal, graph=graph)
        self._trace(task.task_id, "run_start", model=route.model_id, worker=route.worker_id, lease=route.lease_id,
                    task_type=task.task_type, instruction=short(instruction, 1000), context_chars=len(context),
                    code_map=len(graph.symbols) if graph is not None else None)
        try:
            return await self._run_agent(task, route, work, root, slug, deps, patch_base, config_file, goal,
                                         instruction, context, session_id, heartbeat, graph)
        except BaseException as exc:
            # A crash (or cancel) mid-run still leaves a line saying how far it got.
            self._trace(task.task_id, "run_error", error=short(f"{type(exc).__name__}: {exc}", 500))
            self.journal.write("tasks", {"task_id": task.task_id, "task_type": task.task_type,
                                         "model": route.model_id, "worker": route.worker_id, "status": "error",
                                         "error": short(f"{type(exc).__name__}: {exc}", 500)})
            raise

    async def _run_agent(self, task: CodingTaskInput, route: RouteLease, work: Path, root: Path, slug: str,
                         deps: Path, patch_base: str, config_file: Path, goal: str, instruction: str,
                         context: str, session_id: str, heartbeat: Callable[[dict], None],
                         graph: sbx.codemap.CodeGraph | None = None) -> CodingRunResult:
        settings = sbx.repo_settings(self.settings, work)   # the repo's .kite.yml (already validated in setup)
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
        status, code, output = "not_started", None, ""
        fast_status, fast_seconds = "off", 0.0
        if self.settings.fast_path and task.task_type == "coding" and test_command:
            ask = sbx.gateway_asker(self.settings, route.model_id, tag=clean_tag(task.task_id) or slug,
                                    pin=route.worker_id)
            if ask is not None:
                started = time.monotonic()
                step = asyncio.create_task(asyncio.to_thread(
                    sbx.fast_attempt, work, goal, graph, ask, test_command, check_template))
                while True:
                    done, _ = await asyncio.wait({step}, timeout=self.heartbeat_seconds)
                    if done:
                        break
                    heartbeat({"task_id": task.task_id, "state": "FAST_PATH",
                               "seconds": round(time.monotonic() - started)})
                fast = step.result()
                fast_status = fast.status
                with (root / "agent.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} fast {fast.status} "
                                 f"model={route.model_id} =====\n{fast.answer}")
                if fast.status == "applied":
                    fast_code, fast_output, _ = await run_tests("fast")
                    fast_check = await run_check("fast") if fast_code == 0 else (None, "", "")
                    if fast_code == 0 and fast_check[0] in (None, 0):
                        fast_status = "passed"
                        status, code, output = "ok", 0, fast.answer
                        test_code, test_output = 0, fast_output
                        check_code, check_output, check_ran = fast_check
                    else:
                        fast_status = "tests_failed" if fast_code != 0 else "check_failed"
                        await asyncio.to_thread(sbx.restore_tree, work, fast.before)
                        prompt += sbx.fast_note(sbx.FAST_REASONS[fast_status], fast.diff,
                                                fast_output if fast_code != 0 else fast_check[1])
                        test_output = fast_output if fast_code != 0 else ""
                elif fast.status == "not_applied":
                    prompt += sbx.fast_note(f"{sbx.FAST_REASONS['not_applied']} ({fast.reason})", "", "")
                fast_seconds = round(time.monotonic() - started, 1)
                self._trace(task.task_id, "fast_path", outcome=fast_status, seconds=fast_seconds,
                            reason=fast.reason or None, files=fast.files or None, context_chars=fast.context_chars)
        while fast_status != "passed":
            kind = "agent" if attempt == 0 else f"fix{attempt}"
            name = sbx.container_name(kind, slug)
            watch = sbx.StuckWatch()
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

        review = "skipped"
        if sbx.wants_review(self.settings, task.task_type, test_command, status, test_code) and check_code in (None, 0):
            before = await asyncio.to_thread(sbx.snapshot_tree, work)
            name = sbx.container_name("review", slug)
            review_code, review_output, step_seconds = await self._run_container(
                name, sbx.docker_run_args(self.settings, name=name, workspace=work, network=self.settings.network,
                                          config_file=config_file, deps=deps)
                + ["opencode", "run", "-m", f"kite/{route.model_id}",
                   sbx.review_prompt(goal, test_command, await asyncio.to_thread(
                       sbx.codemap.impact, graph, work, patch_base) if graph is not None else "", quick=template,
                       edited=await asyncio.to_thread(sbx.edited_tests, work, patch_base), check=check_template,
                       scratch=await asyncio.to_thread(sbx.scratch_files, work, patch_base))],
                self.settings.agent_timeout_seconds, heartbeat, "REVIEWING", task.task_id, sbx.StuckWatch())
            seconds = round(seconds + step_seconds, 1)
            with (root / "agent.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} review model={route.model_id} "
                             f"exit={review_code} =====\n")
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
            lines.insert(0, f"Kite fast path: the whole fix came from one model call ({fast_seconds}s) and passed "
                            "Kite's tests; no agent session was needed.")
        elif fast_status not in ("off", "skipped"):
            lines.insert(0, f"Kite fast path: tried ({fast_status}, {fast_seconds}s), then the agent took over.")
        if stuck_stops:
            lines.append(f"Kite stopped the agent {stuck_stops} time(s) for repeating the same steps.")
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
            "fast_path": fast_status, "fast_seconds": fast_seconds,
            "verified": verified,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        added = sum(1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
        run = {"task_id": task.task_id, "task_type": task.task_type, "repo": task.repo_path,
               "model": route.model_id, "worker": route.worker_id, "goal": short(task.goal, 500),
               "instruction": short(instruction, 500), "status": "finished", "agent_status": status,
               "agent_exit": code, "seconds": seconds, "fix_rounds": attempt, "review": review,
               "test_command": test_command, "test_exit": test_code, "stuck_stops": stuck_stops,
               "tests_edited": tests_edited, "check_exit": check_code, "scratch_files": scratch,
               "fast_path": fast_status, "fast_seconds": fast_seconds,
               "verified": verified,
               "changed_files": changed, "lines_added": added, "lines_removed": removed,
               "context_chars": len(context)}
        self._trace(task.task_id, "run_end", **{k: v for k, v in run.items() if k not in ("task_id", "goal")})
        self.journal.write("tasks", run)
        return CodingRunResult(task.task_id, task.repo_head, session_id, patch, summary,
                               tuple(changed), int(seconds))

    async def interrupt_opencode(self, task: CodingTaskInput, route: RouteLease, session_id: str, key: str) -> None:
        slug = task_slug(task.task_id)
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
