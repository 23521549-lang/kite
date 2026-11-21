"""SandboxActivityBackend: each workflow step against a fake Docker, plus one full Temporal run."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import time

import pytest
from temporalio.exceptions import ApplicationError

from poc.kaggle_worker import sandbox as sbx
from poc.kaggle_worker.backend import SandboxActivityBackend, task_slug
from poc.kaggle_worker.protocol import OwnerPolicy, ScheduledTask, WorkerProfile
from poc.kaggle_worker.store import SQLiteSchedulerStore
from poc.kaggle_worker.temporal_workflow import CodingTaskInput

SETTINGS = sbx.SandboxSettings(uid=1000, setup_command="pip install -r requirements.txt")
MODELS = ["qwen3.6", "devstral", "qwen3.6-fast"]
ALIASES = {"qwen3.6-fast": "qwen3.6"}
CP = subprocess.CompletedProcess


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) - 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo, git(repo, "rev-parse", "HEAD")


class FakeDocker:
    def __init__(self, *, test_returncode: int = 0, setup_returncode: int = 0,
                 test_returncodes: list[int] | None = None, review: str = "none",
                 check_returncodes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.check_returncodes = list(check_returncodes or [])
        self.review = review
        self.test_returncode = test_returncode
        self.test_returncodes = list(test_returncodes or [])
        self.setup_returncode = setup_returncode

    def __call__(self, cmd, *, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[1] in ("rm", "kill"):
            return CP(cmd, 0, "", "")
        kind = cmd[cmd.index("--name") + 1].split("-")[1]
        work = Path(cmd[cmd.index("-v") + 1].rsplit(":/work", 1)[0])
        if kind == "setup":
            (work / "generated_by_setup.txt").write_text("artefact\n")
            return CP(cmd, self.setup_returncode, "installed\n", "")
        if kind == "agent" or kind.startswith("fix"):
            (work / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) if xs else 0\n")
            return CP(cmd, 0, "\x1b[0mĐã sửa calc.py\n", "")
        if kind == "review":
            if self.review == "improve":
                (work / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) if xs else 0.0\n")
            elif self.review == "break":
                (work / "calc.py").write_text("def average(xs):\n    return 1 / 0\n")
            return CP(cmd, 0, "Đã rà soát\n", "")
        if kind == "test":
            code = self.test_returncodes.pop(0) if self.test_returncodes else self.test_returncode
            return CP(cmd, code, "2 passed\n" if code == 0 else "E   AssertionError: average\n1 failed\n", "")
        if kind == "quick":
            return CP(cmd, 0, "1 passed\n", "")
        if kind == "repro":
            return CP(cmd, 1, "FAILED test_kite_repro.py::test_empty_list_gives_zero\n", "")
        if kind == "check":
            code = self.check_returncodes.pop(0) if self.check_returncodes else 0
            return CP(cmd, code, "" if code == 0 else "calc.py:2:12: F821 Undefined name `total`\n", "")
        raise AssertionError(f"unexpected docker call: {cmd}")

    def kinds(self) -> list[str]:
        return [c[c.index("--name") + 1].split("-")[1] for c in self.calls if c[1] == "run"]

    def agent_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "run" and "opencode" in c]


def make_store(tmp_path: Path, quota: int = 36000) -> SQLiteSchedulerStore:
    store = SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3")
    now = time.time()
    store.register_owner_policy(OwnerPolicy("han", True, True, 2, quota, now))
    store.register_worker(WorkerProfile("kaggle-han", "han", "kaggle-han", ("coder",), ("qwen3.6", "devstral"),
                                        True, True, now, 1, capabilities=("coding",), loaded_model="qwen3.6"))
    return store


def make_task(store, repo: Path, head: str, task_id: str = "Task 1/fix") -> CodingTaskInput:
    store.enqueue(ScheduledTask(task_id, "coder", None, head, "agent/t", json.dumps({"goal": "Sửa average"}),
                                900, 0, time.time()))
    return CodingTaskInput(task_id, "Sửa average", str(repo), head, "agent/t", (), None, 900)


def make_backend(tmp_path: Path, store, fake: FakeDocker, settings=SETTINGS) -> SandboxActivityBackend:
    return SandboxActivityBackend(store, settings, state_dir=tmp_path / "state", models=MODELS,
                                  aliases=ALIASES, runner=fake, heartbeat_seconds=0.01)


def test_backend_runs_every_step_and_settles_the_lease(tmp_path):
    repo, head = make_repo(tmp_path)
    store, fake = make_store(tmp_path), FakeDocker()
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, head)
    beats: list[dict] = []

    async def scenario():
        await backend.validate_task(task, "k1")
        work = await backend.prepare_workspace(task, "k2")
        assert await backend.prepare_workspace(task, "k2-retry") == work      # reused, no second setup
        route = await backend.reserve_route(task, "k3")
        session = await backend.prepare_opencode_session(task, route, work, None, "k4")
        await backend.heartbeat_route(task, route, "k5")
        result = await backend.run_opencode(task, route, work, "", session, "k6", beats.append)
        checkpoint = await backend.checkpoint_and_stop(task, route, result, "k7")
        bundle = await backend.build_review_bundle(task, route, result, checkpoint, "k8")
        await backend.release_route(task, route, result, "COMPLETED", "k9")
        return work, route, result, checkpoint, bundle

    work, route, result, checkpoint, bundle = asyncio.run(scenario())
    assert fake.kinds() == ["setup", "agent", "test", "review"]
    assert (route.worker_id, route.model_id) == ("kaggle-han", "qwen3.6")
    assert result.changed_files == ("calc.py",)
    assert "if xs else 0" in result.patch and "generated_by_setup" not in result.patch
    assert "PASSED" in result.summary and "\x1b" not in result.summary
    assert bundle.patch == result.patch and bundle.worker_id == "kaggle-han"
    assert json.loads(Path(checkpoint).read_text())["changed_files"] == ["calc.py"]
    assert store.get_task_state(task.task_id) == "COMPLETED"
    assert store.get_owner_policy("han").quota_remaining_seconds < 36000
    assert Path(work).parent.name == task_slug(task.task_id) and "/" not in task_slug(task.task_id)
    agent = fake.agent_calls()[0]
    assert agent[agent.index("--network") + 1] == SETTINGS.network
    assert agent[agent.index("-m") + 1] == "kite/qwen3.6"
    config = json.loads((Path(work).parent / "opencode.json").read_text())
    assert config["small_model"] == "kite/qwen3.6-fast"


def test_followup_instruction_reuses_the_clone_and_cancel_kills_and_releases(tmp_path):
    repo, head = make_repo(tmp_path)
    store, fake = make_store(tmp_path), FakeDocker()
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, head, "task-2")

    async def scenario():
        work = await backend.prepare_workspace(task, "k1")
        route = await backend.reserve_route(task, "k2")
        session = await backend.prepare_opencode_session(task, route, work, None, "k3")
        await backend.run_opencode(task, route, work, "", session, "k4", lambda _d: None)
        second = await backend.run_opencode(task, route, work, "Thêm docstring", session, "k5", lambda _d: None)
        await backend.interrupt_opencode(task, route, session, "k6")
        await backend.release_route(task, route, second, "CANCELLED", "k7")
        return second

    second = asyncio.run(scenario())
    assert "Thêm docstring" in fake.agent_calls()[-1][-1]
    assert second.changed_files == ("calc.py",)                              # still against the base commit
    assert ["docker", "kill", f"kite-agent-{task_slug('task-2')}"] in fake.calls
    assert store.get_task_state("task-2") == "CANCELLED"


def test_validation_rejects_unknown_commit_and_model_without_retry(tmp_path):
    repo, head = make_repo(tmp_path)
    backend = make_backend(tmp_path, make_store(tmp_path), FakeDocker())
    for task in (CodingTaskInput("t", "g", str(repo), "0" * 40, "agent/t", (), None, 900),
                 CodingTaskInput("t", "g", str(repo), head, "agent/t", (), "gpt-9", 900),
                 CodingTaskInput("t", "g", str(tmp_path / "missing"), head, "agent/t", (), None, 900)):
        with pytest.raises(ApplicationError) as error:
            asyncio.run(backend.validate_task(task, "k"))
        assert error.value.non_retryable


def test_failed_setup_stops_the_task_without_retry(tmp_path):
    repo, head = make_repo(tmp_path)
    store, fake = make_store(tmp_path), FakeDocker(setup_returncode=1)
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, head, "task-3")
    with pytest.raises(ApplicationError) as error:
        asyncio.run(backend.prepare_workspace(task, "k"))
    assert error.value.non_retryable and "setup command failed" in str(error.value)
    assert fake.kinds() == ["setup"]


def test_temporal_workflow_runs_a_task_end_to_end_in_the_sandbox(tmp_path):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from poc.kaggle_worker.temporal_activities import TemporalKiteActivities
    from poc.kaggle_worker.temporal_state import TemporalActivityJournal
    from poc.kaggle_worker.temporal_workflow import CodingTaskWorkflow

    repo, head = make_repo(tmp_path)
    store, fake = make_store(tmp_path), FakeDocker()
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, head, "e2e-sandbox-1")

    async def scenario():
        journal = TemporalActivityJournal(tmp_path / "state" / "activities.sqlite3")
        activities = TemporalKiteActivities(backend, journal, route_heartbeat_interval_seconds=0.05)
        methods = [activities.validate_task, activities.prepare_workspace, activities.reserve_route,
                   activities.prepare_opencode_session, activities.run_opencode,
                   activities.interrupt_opencode, activities.checkpoint_and_stop,
                   activities.build_review_bundle, activities.release_route]
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue="kite-sandbox-e2e",
                              workflows=[CodingTaskWorkflow], activities=methods):
                outcome = await env.client.execute_workflow(
                    CodingTaskWorkflow.run, task, id=task.task_id, task_queue="kite-sandbox-e2e")
        journal.close()
        return outcome

    outcome = asyncio.run(scenario())
    assert outcome.status == "READY_FOR_REVIEW"
    assert "if xs else 0" in outcome.review_bundle.patch
    assert "PASSED" in outcome.review_bundle.summary
    assert store.get_task_state(task.task_id) == "COMPLETED"


def test_ensure_namespace_creates_it_once_and_tolerates_an_existing_one():
    from poc.kaggle_worker import cli

    calls = []

    class Service:
        def __init__(self, error=None):
            self.error = error

        async def register_namespace(self, request):
            calls.append(request.namespace)
            if self.error:
                raise self.error

    class FakeClient:
        namespace = "default"

        def __init__(self, error=None):
            self.workflow_service = Service(error)

    assert asyncio.run(cli._ensure_temporal_namespace(FakeClient(), settle_seconds=0)) is True
    assert asyncio.run(cli._ensure_temporal_namespace(
        FakeClient(RuntimeError("Namespace already exists")), settle_seconds=0)) is False
    with pytest.raises(RuntimeError):
        asyncio.run(cli._ensure_temporal_namespace(FakeClient(RuntimeError("permission denied")), settle_seconds=0))
    assert calls == ["default", "default", "default"]


def test_signal_to_a_finished_workflow_is_a_one_line_error(tmp_path, monkeypatch, capsys):
    import temporalio.client
    from temporalio.service import RPCError, RPCStatusCode

    from poc.kaggle_worker import cli

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "q"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": [],
    }))

    class Handle:
        async def signal(self, *_args):
            raise RPCError("workflow execution already completed", RPCStatusCode.NOT_FOUND, b"")

    class FakeClient:
        def get_workflow_handle(self, _workflow_id):
            return Handle()

    async def fake_connect(_endpoint):
        return FakeClient()

    monkeypatch.setattr(temporalio.client.Client, "connect", staticmethod(fake_connect))
    assert cli.main(["--config", str(config_path), "workflow", "wake", "demo-1"]) == 1
    err = capsys.readouterr().err
    assert "already completed" in err and "Traceback" not in err


def test_failed_kite_tests_go_back_to_the_agent_with_the_output(tmp_path):
    repo, head = make_repo(tmp_path)
    store, fake = make_store(tmp_path), FakeDocker(test_returncodes=[1, 0])
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, head, "task-fix")
    beats: list[dict] = []

    async def scenario():
        work = await backend.prepare_workspace(task, "k1")
        route = await backend.reserve_route(task, "k2")
        session = await backend.prepare_opencode_session(task, route, work, None, "k3")
        return await backend.run_opencode(task, route, work, "", session, "k4", beats.append)

    result = asyncio.run(scenario())
    assert fake.kinds() == ["setup", "agent", "test", "fix1", "test", "review"]
    assert "1 vòng sửa" in result.summary and "PASSED" in result.summary
    assert "rà soát: không cần sửa" in result.summary
    fix = next(c for c in fake.calls if c[1] == "run" and "kite-fix1-" in " ".join(c))
    assert "THẤT BẠI" in fix[-1] and "AssertionError: average" in fix[-1]
    first = next(c for c in fake.calls if c[1] == "run" and "kite-agent-" in " ".join(c))
    assert "Cách làm:" in first[-1] and "calc.py" in first[-1]


def run_backend_task(tmp_path: Path, fake, task_id: str):
    repo, head = make_repo(tmp_path)
    store = make_store(tmp_path)
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, head, task_id)

    async def scenario():
        work = await backend.prepare_workspace(task, "k1")
        route = await backend.reserve_route(task, "k2")
        session = await backend.prepare_opencode_session(task, route, work, None, "k3")
        return await backend.run_opencode(task, route, work, "", session, "k4", lambda beat: None)

    return backend, asyncio.run(scenario())


def test_backend_self_review_keeps_good_changes_and_rolls_back_bad_ones(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "bad").mkdir()
    good = FakeDocker(review="improve")
    backend, result = run_backend_task(tmp_path / "good", good, "task-review-good")
    assert good.kinds() == ["setup", "agent", "test", "review", "test"]
    assert "else 0.0" in result.patch and "đã sửa thêm" in result.summary
    saved = json.loads((backend.task_dir("task-review-good") / "result.json").read_text())
    assert saved["review"] == "kept" and saved["verified"] is True
    bad = FakeDocker(review="break", test_returncodes=[0, 1])
    backend, result = run_backend_task(tmp_path / "bad", bad, "task-review-bad")
    assert "1 / 0" not in result.patch and "if xs else 0" in result.patch
    assert "trả lại bản trước" in result.summary and "PASSED" in result.summary


def test_backend_journals_each_run_and_traces_every_step(tmp_path):
    from poc.kaggle_worker.journal import read_jsonl

    fake = FakeDocker(test_returncodes=[1, 0], review="improve")
    backend, result = run_backend_task(tmp_path, fake, "task-journal")
    (run,) = read_jsonl(tmp_path / "state" / "journal" / "tasks.jsonl")
    assert run["task_id"] == "task-journal" and run["status"] == "finished" and run["verified"] is True
    assert (run["fix_rounds"], run["review"], run["test_exit"]) == (1, "kept", 0)
    assert run["changed_files"] == ["calc.py"] and run["lines_added"] >= 1 and run["context_chars"] > 0
    trace = read_jsonl(backend.task_dir("task-journal") / "trace.jsonl")
    steps = [e["step"] for e in trace if e["event"] == "step"]
    assert steps == ["setup", "agent", "test", "fix1", "test", "review", "test"]
    events = [e["event"] for e in trace if e["event"] != "step_start"]     # step_start: progress only
    assert events[1] == "run_start" and events[-1] == "run_end"
    failed_test = next(e for e in trace if e["event"] == "step" and e["exit"] == 1)
    assert "AssertionError" in failed_test["tail"]                    # failing output kept for tracing
    config = json.loads((backend.task_dir("task-journal") / "opencode.json").read_text())
    worker = next(e["worker"] for e in trace if e["event"] == "run_start")
    assert worker and config["provider"]["kite"]["options"]["headers"] == {
        "X-Kite-Task": "task-journal", "X-Kite-Worker": worker}


def test_backend_uses_the_repository_kite_yml_and_rejects_a_broken_one(tmp_path):
    from poc.kaggle_worker.journal import read_jsonl

    repo, _ = make_repo(tmp_path)
    (repo / ".kite.yml").write_text("setup: pip install -q -r svc.txt\ntest: python -m pytest -q svc\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "kite config")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    store = make_store(tmp_path)
    fake = FakeDocker()
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, head, "task-kite-yml")

    async def scenario():
        work = await backend.prepare_workspace(task, "k1")
        route = await backend.reserve_route(task, "k2")
        return await backend.run_opencode(task, route, work, "", "s", "k4", lambda beat: None)

    result = asyncio.run(scenario())
    runs = [c for c in fake.calls if c[1] == "run"]
    setup = next(c for c in runs if c[c.index("--name") + 1].startswith("kite-setup"))
    test = next(c for c in runs if c[c.index("--name") + 1].startswith("kite-test"))
    assert setup[-1] == "pip install -q -r svc.txt" and test[-1] == "python -m pytest -q svc"
    assert "`python -m pytest -q svc`" in result.summary
    trace = read_jsonl(backend.task_dir("task-kite-yml") / "trace.jsonl")
    assert any(e["event"] == "repo_config" and e["test"] == "python -m pytest -q svc" for e in trace)

    (repo / ".kite.yml").write_text("test_timeout: 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "broken")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    broken = make_task(store, repo, head, "task-kite-yml-broken")
    with pytest.raises(ApplicationError, match="test_timeout"):
        asyncio.run(backend.prepare_workspace(broken, "k1"))


def test_backend_builds_the_code_map_and_uses_it_in_the_prompts(tmp_path, monkeypatch):
    from poc.kaggle_worker import codemap
    from poc.kaggle_worker.journal import read_jsonl
    from test_codemap import graph_data

    built = []

    def build(workspace, out_dir, **kwargs):
        path = Path(out_dir) / "graphify-out" / "graph.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(graph_data()))
        built.append(Path(workspace))
        return path

    monkeypatch.setattr(codemap, "build", build)
    fake = FakeDocker(review="improve")
    backend, result = run_backend_task(tmp_path, fake, "task-map")
    first, review = fake.agent_calls()[0][-1], fake.agent_calls()[-1][-1]
    assert "Bản đồ code" in first and "main.py:4 (report())" in first
    assert "RÀ SOÁT" in review and "những chỗ đang dùng nó" in review and "main.py:4 (report())" in review
    assert built and built[0].name == "work" and "else 0.0" in result.patch
    trace = read_jsonl(backend.task_dir("task-map") / "trace.jsonl")
    assert next(e for e in trace if e["event"] == "run_start")["code_map"] == 8


def test_backend_reuses_installed_dependencies_for_the_next_task(tmp_path):
    from poc.kaggle_worker.journal import read_jsonl

    class DepsOnly(FakeDocker):
        def __call__(self, cmd, *, timeout=None):
            if cmd[1] == "run" and cmd[cmd.index("--name") + 1].split("-")[1] == "setup":
                self.calls.append(list(cmd))
                deps = Path(next(v for v in cmd if v.endswith(":/deps")).rsplit(":/deps", 1)[0])
                (deps / "python").mkdir(parents=True, exist_ok=True)
                return CP(cmd, 0, "installed\n", "")
            return super().__call__(cmd, timeout=timeout)

    repo, head = make_repo(tmp_path)
    store = make_store(tmp_path)
    first, second = DepsOnly(), DepsOnly()

    tasks = {i: make_task(store, repo, head, f"task-deps-{i}") for i in (1, 2)}

    def prepare(fake, i):
        backend = make_backend(tmp_path, store, fake)
        return backend, asyncio.run(backend.prepare_workspace(tasks[i], "k1"))

    backend, _ = prepare(first, 1)
    assert "setup" in first.kinds()
    assert (backend.task_dir("task-deps-1") / "deps").is_symlink()
    backend, work = prepare(second, 2)
    assert "setup" not in second.kinds()                                  # same requirements: reused
    trace = read_jsonl(backend.task_dir("task-deps-2") / "trace.jsonl")
    assert any(e["event"] == "deps_cached" for e in trace)
    assert prepare(DepsOnly(), 2)[1] == work                              # a retry keeps the prepared clone


def test_backend_stops_a_looping_agent_and_says_so(tmp_path):
    from poc.kaggle_worker.journal import read_jsonl
    from test_sandbox import LOOP

    class Looping(FakeDocker):
        def __call__(self, cmd, *, timeout=None):
            result = super().__call__(cmd, timeout=timeout)
            if cmd[1] == "run" and cmd[cmd.index("--name") + 1].split("-")[1] == "agent":
                return CP(cmd, 0, LOOP, "")
            return result

    fake = Looping(test_returncodes=[1, 0])
    backend, result = run_backend_task(tmp_path, fake, "task-loop")
    assert "Kite stopped the agent 1 time(s)" in result.summary
    assert "Lần trước Kite đã dừng bạn vì lặp lại" in fake.agent_calls()[1][-1]        # the fix round
    (record,) = read_jsonl(backend.state_dir / "journal" / "tasks.jsonl")
    assert record["stuck_stops"] == 1 and record["verified"] and record["tests_edited"] == []
    trace = read_jsonl(backend.task_dir("task-loop") / "trace.jsonl")
    assert any(e.get("stuck") for e in trace if e["event"] == "step")


def test_backend_runs_the_repository_check_after_the_tests_and_reports_it(tmp_path):
    from poc.kaggle_worker.journal import read_jsonl

    repo, _ = make_repo(tmp_path)
    (repo / ".kite.yml").write_text("test: python -m pytest -q\ncheck: ruff check {py_files}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "kite config")
    head = git(repo, "rev-parse", "HEAD")
    store = make_store(tmp_path)
    fake = FakeDocker(check_returncodes=[1, 0], review="improve")
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, head, "task-check")

    async def scenario():
        work = await backend.prepare_workspace(task, "k1")
        route = await backend.reserve_route(task, "k2")
        return await backend.run_opencode(task, route, work, "", "s", "k4", lambda beat: None)

    result = asyncio.run(scenario())
    assert fake.kinds() == ["setup", "agent", "test", "check", "fix1", "test", "check", "review", "test", "check"]
    fix = fake.agent_calls()[1][-1]
    assert "Test đã QUA, nhưng Kite chạy kiểm tra `ruff check calc.py`" in fix and "F821" in fix
    assert "Kite check `ruff check calc.py` (no network): PASSED." in result.summary
    (record,) = read_jsonl(backend.state_dir / "journal" / "tasks.jsonl")
    assert record["check_exit"] == 0 and record["verified"] and record["review"] == "kept"
    saved = json.loads((backend.task_dir("task-check") / "result.json").read_text())
    assert saved["check_command"] == "ruff check calc.py" and saved["scratch_files"] == []
    trace = read_jsonl(backend.task_dir("task-check") / "trace.jsonl")
    assert any(e["event"] == "repo_config" and e["check"] == "ruff check {py_files}" for e in trace)


def test_backend_never_verifies_a_task_whose_check_keeps_failing(tmp_path):
    repo, _ = make_repo(tmp_path)
    (repo / ".kite.yml").write_text("test: python -m pytest -q\ncheck: ruff check {py_files}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "kite config")
    store = make_store(tmp_path)
    fake = FakeDocker(check_returncodes=[1, 1, 1])
    backend = make_backend(tmp_path, store, fake)
    task = make_task(store, repo, git(repo, "rev-parse", "HEAD"), "task-check-bad")

    async def scenario():
        work = await backend.prepare_workspace(task, "k1")
        route = await backend.reserve_route(task, "k2")
        return await backend.run_opencode(task, route, work, "", "s", "k4", lambda beat: None)

    result = asyncio.run(scenario())
    assert "review" not in fake.kinds() and "FAILED (exit 1)" in result.summary and "F821" in result.summary
    saved = json.loads((backend.task_dir("task-check-bad") / "result.json").read_text())
    assert saved["verified"] is False and saved["check_exit_code"] == 1 and saved["fix_rounds_used"] == 2


def test_backend_fast_path_solves_with_one_call_or_hands_over_to_the_agent(tmp_path, monkeypatch):
    from poc.kaggle_worker.journal import read_jsonl
    from test_sandbox import FIX, REPRO

    asked = []

    def asker(settings, model, *, tag, pin=None, timeout=900):
        def ask(prompt):
            asked.append((model, tag, pin, prompt))
            return "ỔN" if "RÀ SOÁT cuối" in prompt else REPRO if "TRƯỚC khi sửa" in prompt else FIX
        return ask

    monkeypatch.setattr(sbx, "gateway_asker", asker)
    (tmp_path / "ok").mkdir()
    fake = FakeDocker()
    backend, result = run_backend_task(tmp_path / "ok", fake, "task-fast")
    assert fake.kinds() == ["setup", "repro", "quick", "test"]      # test first, one call to fix, one to review
    assert "if xs else 0" in result.patch and "Kite fast path: the fix came from one model call" in result.summary
    assert "failed before the fix and passes now" in result.summary and "test_empty_list_gives_zero" in result.patch
    assert asked[0][1:3] == ("task-fast", "kaggle-han") and "calc.py" in asked[1][3]
    (record,) = read_jsonl(backend.state_dir / "journal" / "tasks.jsonl")
    assert record["fast_path"] == "passed" and record["verified"] and record["agent_status"] == "ok"
    assert record["review_mode"] == "one_call" and record["review"] == "unchanged" and len(asked) == 3
    assert record["repro"] == "reproduced"
    trace = read_jsonl(backend.task_dir("task-fast") / "trace.jsonl")
    assert any(e["event"] == "fast_path" and e["outcome"] == "passed" for e in trace)
    # Progress for task_status: the steps as they happened, in Vietnamese.
    from poc.kaggle_worker.backend import task_progress

    report = task_progress(trace, trace[-1]["ts"] + 5)
    joined = "\n".join(report["progress"])
    assert "đường nhanh, test tái hiện viết từ yêu cầu: lỗi trên code cũ" in joined
    assert "đường nhanh, bản sửa 1/2: qua hết test" in joined and "rà soát một lần gọi xong" in joined
    assert "kết thúc: Kite đã tự chạy test và qua" in report["progress"][-1] and "now" not in report
    assert [e["event"] for e in trace].count("step_start") == [e["event"] for e in trace].count("step")

    (tmp_path / "bad").mkdir()
    fake = FakeDocker(test_returncodes=[1, 1, 0])
    backend, result = run_backend_task(tmp_path / "bad", fake, "task-fast-bad")
    # Two candidates (the second told why the first failed), then the agent.
    assert fake.kinds()[:7] == ["setup", "repro", "quick", "test", "quick", "test", "agent"]
    assert "LẦN THỬ TRƯỚC CỦA KITE CHƯA ĐẠT" in asked[-1][3]
    assert "Kite đã thử một bản sửa nhanh" in fake.agent_calls()[0][-1]
    assert "Kite fast path: tried (tests_failed" in result.summary
    saved = json.loads((backend.task_dir("task-fast-bad") / "result.json").read_text())
    assert saved["fast_path"] == "tests_failed" and saved["verified"] is True and saved["fast_attempts"] == 2
