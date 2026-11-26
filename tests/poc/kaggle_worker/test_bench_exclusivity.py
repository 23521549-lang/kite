"""Bench exclusivity, both directions (Gói 7, worker (c)).

* Forward: ``bench_start`` refuses while a task holds the worker (state LEASED in the
  store) and names the running tasks with their ``/tasks/<id>`` links.
* Reverse: ``start_task`` (chat and web) refuses while a bench run holds its lock, and
  the direct ``kite bench run`` CLI path refuses while a task is LEASED.

Fake Temporal client, real store and Git repos (same pattern as
``tests/poc/kaggle_worker/test_mcp_server.py``); no gateway, Docker or network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import subprocess

import pytest

from poc.kaggle_worker import bench
from poc.kaggle_worker.cli import check_no_leased_tasks
from poc.kaggle_worker.config import ConfigError, validate_config
from poc.kaggle_worker.mcp_server import KiteOps, KiteToolError
from poc.kaggle_worker.store import SQLiteSchedulerStore


def _raw_config(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "kite-coding-tasks"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": [{
            "id": "kaggle-han", "adapter": "pull", "enabled": True, "owner": "han",
            "model": "qwen3.6", "models": ["devstral"], "capabilities": ["coding"],
            "quota": {"gpu_seconds": 108000, "concurrency": 1},
            "secrets": {"worker_token": f"file:{tmp_path / 'token'}"},
        }],
        "gateway": {"aliases": {"qwen3.6-fast": {"model": "qwen3.6", "params": {}}}},
    }


def make_ops(tmp_path: Path, client) -> KiteOps:
    spawned: list[list[str]] = []

    async def connect(_endpoint):
        return client

    return KiteOps(validate_config(_raw_config(tmp_path)),
                   db_path=tmp_path / "state" / "scheduler.sqlite3",
                   state_dir=tmp_path / "state", allowed_roots=[tmp_path], connect=connect,
                   gateway_status=lambda: {"workers": []},
                   spawn=lambda cmd, log: spawned.append(cmd))


class FakeClient:
    def __init__(self):
        self.started, self.state = [], {}

    async def start_workflow(self, _run, task_input, *, id, task_queue):
        self.started.append((task_input, id, task_queue))
        self.state[id] = "RUNNING"

    def get_workflow_handle(self, workflow_id):
        raise AssertionError("not used here")

    def get_workflow_handle_for(self, _run, workflow_id):
        raise AssertionError("not used here")


def make_repo(root: Path, name: str = "repo") -> tuple[Path, str]:
    repo = root / name
    repo.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com", *a],
                                    check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    (repo / "calc.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    return repo, run("rev-parse", "HEAD")


def start_one_task(tmp_path: Path, ops: KiteOps, client: FakeClient, name: str = "repo") -> str:
    repo, _head = make_repo(tmp_path, name)
    started = asyncio.run(ops.start_task(str(repo), "Phân tích calc.py", task_type="analysis"))
    assert client.started and started["task_id"] == client.started[-1][1]
    return started["task_id"]


def mark_leased(db_path: Path, task_id: str) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE tasks SET state='LEASED' WHERE task_id=?", (task_id,))
        assert db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0] == "LEASED"


# ------------------------------------------------------------------ forward: bench_start
def test_bench_start_refuses_while_a_task_is_leased(tmp_path):
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = start_one_task(tmp_path, ops, client)
    mark_leased(tmp_path / "state" / "scheduler.sqlite3", task_id)
    with pytest.raises(KiteToolError) as excinfo:
        asyncio.run(ops.bench_start(confirm=True, label="t1"))
    message = str(excinfo.value)
    assert task_id in message and "Phân tích calc.py" in message
    assert f"/tasks/{task_id}" in message


def test_bench_start_runs_when_no_task_is_leased(tmp_path):
    ops = make_ops(tmp_path, FakeClient())
    started = asyncio.run(ops.bench_start(confirm=True, label="t1"))
    assert started["started"] is True and started["out"].endswith("/bench/t1")


def test_bench_start_refuses_before_confirm_when_a_task_is_leased(tmp_path):
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = start_one_task(tmp_path, ops, client)
    mark_leased(tmp_path / "state" / "scheduler.sqlite3", task_id)
    with pytest.raises(KiteToolError, match="/tasks/"):
        asyncio.run(ops.bench_start(confirm=False, label="t1"))


# ------------------------------------------------------------------ reverse: start_task
def test_start_task_refuses_while_a_bench_run_holds_the_lock(tmp_path):
    ops = make_ops(tmp_path, FakeClient())
    repo, _head = make_repo(tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    lock = bench.acquire_lock(tmp_path / "state", label="v9", out=tmp_path / "state" / "bench" / "v9")
    try:
        assert bench.running_bench(tmp_path / "state") is not None
        with pytest.raises(KiteToolError) as excinfo:
            asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))
        assert "v9" in str(excinfo.value) and "đo" in str(excinfo.value)
    finally:
        bench.release_lock(lock)


def test_start_task_runs_once_the_bench_lock_is_released(tmp_path):
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    repo, _head = make_repo(tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    lock = bench.acquire_lock(tmp_path / "state", label="v9", out=tmp_path / "state" / "bench" / "v9")
    bench.release_lock(lock)
    assert bench.running_bench(tmp_path / "state") is None
    started = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))
    assert started["task_id"] == client.started[-1][1]


# ------------------------------------------------------------------ reverse: direct CLI path
def test_cli_bench_run_refuses_while_a_task_is_leased(tmp_path):
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = start_one_task(tmp_path, ops, client)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    mark_leased(db_path, task_id)
    with pytest.raises(ConfigError) as excinfo:
        check_no_leased_tasks(db_path)
    message = str(excinfo.value)
    assert task_id in message and f"/tasks/{task_id}" in message


def test_cli_bench_run_gate_passes_with_no_leased_task(tmp_path):
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    start_one_task(tmp_path, ops, client)          # QUEUED, not LEASED: a bench may still start
    assert check_no_leased_tasks(tmp_path / "state" / "scheduler.sqlite3") == []


def test_leased_tasks_lists_only_leased(tmp_path):
    from poc.kaggle_worker.mcp_server import leased_tasks

    client = FakeClient()
    ops = make_ops(tmp_path, client)
    leased_id = start_one_task(tmp_path, ops, client, "repo-a")
    queued_id = start_one_task(tmp_path, ops, client, "repo-b")
    mark_leased(tmp_path / "state" / "scheduler.sqlite3", leased_id)
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        found = leased_tasks(store)
    assert [t["task_id"] for t in found] == [leased_id]
    assert found[0]["link"] == f"/tasks/{leased_id}" and "Phân tích" in found[0]["title"]
    assert queued_id not in [t["task_id"] for t in found]
