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
                 test_returncodes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
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
        if kind == "test":
            code = self.test_returncodes.pop(0) if self.test_returncodes else self.test_returncode
            return CP(cmd, code, "2 passed\n" if code == 0 else "E   AssertionError: average\n1 failed\n", "")
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
    assert fake.kinds() == ["setup", "agent", "test"]
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
    assert fake.kinds() == ["setup", "agent", "test", "fix1", "test"]
    assert "1 vòng sửa" in result.summary and "PASSED" in result.summary
    fix = next(c for c in fake.calls if c[1] == "run" and "kite-fix1-" in " ".join(c))
    assert "THẤT BẠI" in fix[-1] and "AssertionError: average" in fix[-1]
    first = next(c for c in fake.calls if c[1] == "run" and "kite-agent-" in " ".join(c))
    assert "Cách làm:" in first[-1] and "calc.py" in first[-1]
