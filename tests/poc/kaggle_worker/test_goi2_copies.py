"""Gói 2 (worker b): session copies, PAT, apply/rerun on the main repo, teardown.

Every test uses a real Git repository and a real scheduler store; the Temporal
client is faked (same pattern as tests/poc/kaggle_worker/test_mcp_server.py),
so no worker or network is involved. The contract tables/columns
(``chat_sessions``, ``chat_session_moves``, ``session_copies``, the task
columns) are seeded here exactly as the contract specifies them — worker (a)
owns creating them in ``store.py``; these tests only assume the shape.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest

from poc.kaggle_worker.config import validate_config
from poc.kaggle_worker.github_env import GitHubEnvError, clone_with_pat, pull_with_pat
from poc.kaggle_worker.integration import ReviewBundle
from poc.kaggle_worker.mcp_server import (
    ApplyConflictError,
    KiteOps,
    KiteToolError,
    stop_stale_chat_processes,
    sweep_stale_mcp_processes,
)
from poc.kaggle_worker.session_copies import (
    SessionCopyError,
    copy_size,
    delete_copy,
    ensure_copy,
    sync_copy,
)
from poc.kaggle_worker.store import SQLiteSchedulerStore
from poc.kaggle_worker.temporal_workflow import CodingTaskOutcome


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


def make_config(tmp_path: Path):
    return validate_config(_raw_config(tmp_path))


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


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com", *args],
                   check=True, capture_output=True, text=True)


class FakeHandle:
    def __init__(self, client, workflow_id):
        self.client, self.workflow_id = client, workflow_id

    async def describe(self):
        if self.workflow_id not in self.client.state:
            raise RuntimeError("workflow not found")
        return SimpleNamespace(status=SimpleNamespace(name=self.client.state[self.workflow_id]))

    async def query(self, _query):
        return "CODING"

    async def signal(self, signal, *args):
        self.client.signals.append((self.workflow_id, signal.__name__, args))

    async def result(self):
        return self.client.outcomes[self.workflow_id]


class FakeClient:
    def __init__(self):
        self.started, self.signals, self.state, self.outcomes = [], [], {}, {}

    async def start_workflow(self, _run, task_input, *, id, task_queue):
        self.started.append((task_input, id, task_queue))
        self.state[id] = "RUNNING"

    def get_workflow_handle(self, workflow_id):
        return FakeHandle(self, workflow_id)

    def get_workflow_handle_for(self, _run, workflow_id):
        return FakeHandle(self, workflow_id)


def make_ops(tmp_path: Path, client: FakeClient, session_id: str | None = None,
             roots=None) -> KiteOps:
    async def connect(_endpoint):
        return client

    return KiteOps(make_config(tmp_path), db_path=tmp_path / "state" / "scheduler.sqlite3",
                   state_dir=tmp_path / "state", allowed_roots=roots or [tmp_path], connect=connect,
                   gateway_status=lambda: {"workers": []}, session_id=session_id)


def seed_contract_schema(db_path: Path) -> None:
    """The Gói 2/Gói 3 schema, exactly as the contract specifies it."""
    with SQLiteSchedulerStore(db_path):
        pass
    db = sqlite3.connect(db_path)
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                kite_session_id TEXT PRIMARY KEY, opencode_session_id TEXT, title TEXT,
                environment_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'chat',
                status TEXT NOT NULL DEFAULT 'active', created_at INTEGER, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS chat_session_moves (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, from_env TEXT NOT NULL,
                to_env TEXT NOT NULL, moved_at INTEGER NOT NULL,
                trigger TEXT NOT NULL, note TEXT);
            CREATE TABLE IF NOT EXISTS session_copies (
                session_id TEXT PRIMARY KEY, env_id TEXT NOT NULL, copy_path TEXT NOT NULL,
                base_head TEXT, created_at INTEGER, state TEXT NOT NULL DEFAULT 'active');
        """)
        cols = {r[1] for r in db.execute("PRAGMA table_info(tasks)").fetchall()}
        for name, ctype in (("chat_session_id", "TEXT"), ("environment_id", "TEXT"),
                            ("main_repo", "TEXT"), ("last_apply_error", "TEXT")):
            if name not in cols:
                db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ctype}")
        db.commit()
    finally:
        db.close()


def seed_session(db_path: Path, session_id: str, environment_id: str = "default",
                 status: str = "active") -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute("INSERT OR REPLACE INTO chat_sessions "
                   "(kite_session_id, environment_id, kind, status, created_at, updated_at) "
                   "VALUES (?, ?, 'chat', ?, ?, ?)",
                   (session_id, environment_id, status, int(time.time()), int(time.time())))
        db.commit()
    finally:
        db.close()


def db_rows(db_path: Path, sql: str, params=()) -> list[dict]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    finally:
        db.close()


def finish_task(tmp_path: Path, client: FakeClient, ops: KiteOps, task_id: str,
                repo_for_checkpoint: Path, head: str, patch: str) -> None:
    """Mark a task COMPLETED with a review bundle whose checkpoint points at
    ``repo_for_checkpoint`` (the session copy for session tasks)."""
    from poc.kaggle_worker.backend import task_slug

    patch_path = tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repo_path": str(repo_for_checkpoint)}))
    # _task_patch/_continue_patch read the checkpoint from the task dir.
    (patch_path.parent / "checkpoint.json").write_text(
        json.dumps({"repo_path": str(repo_for_checkpoint)}))
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = CodingTaskOutcome(
        task_id, "READY_FOR_REVIEW", str(checkpoint),
        ReviewBundle(task_id, "lease", "kaggle-han", head, patch, "Kite test run: PASSED", 20))


def owner_code(ops: KiteOps, action: str, task_id: str) -> str:
    from poc.kaggle_worker import confirm
    from poc.kaggle_worker.backend import task_slug

    patch = ops.state_dir / "tasks" / task_slug(task_id) / "result.patch"
    return confirm.issue_code(ops.state_dir, action, task_id, confirm.patch_sha256(patch.read_bytes()))


def patch_changing_calc(repo: Path, new_text: str) -> str:
    (repo / "calc.py").write_text(new_text)
    patch = subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True, text=True,
                           check=True).stdout
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "calc.py"], check=True)
    return patch


# ------------------------------------------------------------------ session_copies

def test_ensure_copy_creates_lazy_copy_and_fast_forwards(tmp_path):
    repo, head = make_repo(tmp_path)
    state = tmp_path / "state"
    seed_contract_schema(tmp_path / "state" / "scheduler.sqlite3")
    copy = ensure_copy("env1", "sess1", repo, state_dir=state,
                       db_path=tmp_path / "state" / "scheduler.sqlite3")
    assert copy == state / "envs" / "env1" / "sessions" / "sess1" / "work"
    assert git_out(copy, "branch", "--show-current") == "kite-session/sess1"
    assert git_out(copy, "rev-parse", "HEAD") == head
    assert git_out(copy, "config", "--get", "remote.origin.url") == str(repo.resolve())
    assert copy_size(copy) > 0
    assert db_rows(tmp_path / "state" / "scheduler.sqlite3",
                   "SELECT * FROM session_copies WHERE session_id='sess1'")[0]["state"] == "active"
    (repo / "calc.py").write_text("x = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "two")
    again = ensure_copy("env1", "sess1", repo, state_dir=state,
                        db_path=tmp_path / "state" / "scheduler.sqlite3")
    assert again == copy and (copy / "calc.py").read_text() == "x = 2\n"
    assert git_out(copy, "rev-parse", "HEAD") == git_out(repo, "rev-parse", "HEAD")


def test_sync_copy_refuses_when_there_is_no_copy_yet_and_delete_is_best_effort(tmp_path):
    repo, _head = make_repo(tmp_path)
    state = tmp_path / "state"
    with pytest.raises(SessionCopyError):
        sync_copy("env1", "sess9", repo, state_dir=state)
    copy = ensure_copy("env1", "sess9", repo, state_dir=state)
    assert sync_copy("env1", "sess9", repo, state_dir=state) == copy
    assert delete_copy("env1", "sess9", state_dir=state) is True
    assert not copy.exists()
    assert delete_copy("env1", "sess9", state_dir=state) is True  # already gone
    with pytest.raises(SessionCopyError):
        ensure_copy("bad/env", "sess9", repo, state_dir=state)


# ------------------------------------------------------------------ start_task on copies

def test_session_b_is_not_blocked_by_dirty_main_repo(tmp_path):
    repo, head = make_repo(tmp_path)
    seed_contract_schema(tmp_path / "state" / "scheduler.sqlite3")
    seed_session(tmp_path / "state" / "scheduler.sqlite3", "sess-a")
    seed_session(tmp_path / "state" / "scheduler.sqlite3", "sess-b")
    (repo / "calc.py").write_text("x = 99\n")  # uncommitted on the MAIN repo
    client = FakeClient()
    ops_a = make_ops(tmp_path, client, session_id="sess-a")
    started_a = asyncio.run(ops_a.start_task(str(repo), "Phân tích calc.py", task_type="analysis"))
    ops_b = make_ops(tmp_path, client, session_id="sess-b")
    started_b = asyncio.run(ops_b.start_task(str(repo), "Phân tích calc.py", task_type="analysis"))
    input_a, input_b = client.started[0][0], client.started[1][0]
    assert input_a.repo_path != str(repo) and input_b.repo_path != str(repo)
    assert input_a.repo_path != input_b.repo_path
    assert started_a["main_repo"] == started_b["main_repo"] == str(repo)
    assert started_a["copy"] == input_a.repo_path and started_b["copy"] == input_b.repo_path
    assert input_a.repo_head == input_b.repo_head == head
    assert git_out(repo, "rev-parse", "HEAD") == head  # main untouched, still dirty
    assert git_out(Path(input_a.repo_path), "status", "--porcelain") == ""


def test_default_session_moves_to_repo_on_first_task(tmp_path):
    repo, _head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "sess-m", environment_id="default")
    client = FakeClient()
    ops = make_ops(tmp_path, client, session_id="sess-m")
    started = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))
    moves = db_rows(db_path, "SELECT * FROM chat_session_moves WHERE session_id='sess-m'")
    assert len(moves) == 1
    assert moves[0]["from_env"] == "default" and moves[0]["trigger"] == "task"
    assert moves[0]["note"] == started["task_id"]
    assert moves[0]["to_env"] == started["environment_id"]
    assert db_rows(db_path, "SELECT environment_id FROM chat_sessions WHERE kite_session_id='sess-m'")[
        0]["environment_id"] == started["environment_id"]
    row = db_rows(db_path, "SELECT chat_session_id, environment_id, main_repo FROM tasks "
                            "WHERE task_id=?", (started["task_id"],))[0]
    assert (row["chat_session_id"], row["environment_id"], row["main_repo"]) == (
        "sess-m", started["environment_id"], str(repo))


# ------------------------------------------------------------------ _finished_patch / apply

def test_finished_patch_returns_main_repo_and_apply_deletes_copy(tmp_path):
    repo, head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "sess-a")
    client = FakeClient()
    ops = make_ops(tmp_path, client, session_id="sess-a")
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    copy = Path(client.started[0][0].repo_path)
    patch = patch_changing_calc(repo, "x = 2\n")
    finish_task(tmp_path, client, ops, task_id, copy, head, patch)
    copy_before = (copy / "calc.py").read_text()
    applied = asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    assert applied["repo"] == str(repo) and applied["files"] == ["calc.py"]
    assert (repo / "calc.py").read_text() == "x = 2\n"          # main changed
    assert copy_before == "x = 1\n"                             # copy was untouched
    assert applied["copy_deleted"] is True and not copy.exists()
    assert db_rows(db_path, "SELECT last_apply_error FROM tasks WHERE task_id=?",
                   (task_id,))[0]["last_apply_error"] is None


def test_apply_conflict_is_structured_code_survives_and_error_persists(tmp_path):
    repo, head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "sess-a")
    seed_session(db_path, "sess-b")
    client = FakeClient()
    ops_a = make_ops(tmp_path, client, session_id="sess-a")
    ops_b = make_ops(tmp_path, client, session_id="sess-b")
    task_a = asyncio.run(ops_a.start_task(str(repo), "Sửa calc.py"))["task_id"]
    task_b = asyncio.run(ops_b.start_task(str(repo), "Sửa calc.py"))["task_id"]
    copy_a, copy_b = Path(client.started[0][0].repo_path), Path(client.started[1][0].repo_path)
    finish_task(tmp_path, client, ops_a, task_a, copy_a, head, patch_changing_calc(repo, "x = 2\n"))
    finish_task(tmp_path, client, ops_b, task_b, copy_b, head, patch_changing_calc(repo, "x = 3\n"))
    asyncio.run(ops_a.apply_result(task_a, owner_code(ops_a, "apply", task_a)))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "apply A")
    new_head = git_out(repo, "rev-parse", "HEAD")
    assert new_head != head
    with pytest.raises(ApplyConflictError) as caught:
        asyncio.run(ops_b.apply_result(task_b, owner_code(ops_b, "apply", task_b)))
    err = caught.value
    assert err.code == "apply_conflict" and err.task_id == task_b
    assert err.head == new_head and err.failed_files == ["calc.py"]
    stored = db_rows(db_path, "SELECT last_apply_error FROM tasks WHERE task_id=?", (task_b,))[0]
    recorded = json.loads(stored["last_apply_error"])
    assert (recorded["code"], recorded["head"], recorded["failed_files"]) == (
        "apply_conflict", new_head, ["calc.py"])
    # The confirmation code was NOT consumed by the failed apply: a fresh code
    # still reaches the conflict check instead of "thiếu mã xác nhận".
    with pytest.raises(ApplyConflictError):
        asyncio.run(ops_b.apply_result(task_b, owner_code(ops_b, "apply", task_b)))


# ------------------------------------------------------------------ rerun

def _session_task(tmp_path, client, session_id, repo, head):
    ops = make_ops(tmp_path, client, session_id=session_id)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    return ops, task_id, Path(client.started[-1][0].repo_path)


def test_rerun_continues_from_old_patch_on_new_head(tmp_path):
    repo, head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "sess-a")
    client = FakeClient()
    ops, task_id, copy = _session_task(tmp_path, client, "sess-a", repo, head)
    finish_task(tmp_path, client, ops, task_id, copy, head, patch_changing_calc(repo, "x = 2\n"))
    (repo / "other.txt").write_text("hi\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "unrelated")
    rerun = asyncio.run(ops.rerun_task(task_id))
    assert rerun["task_id"] != task_id and "fallback" not in rerun
    assert rerun["task_id"] != task_id
    new_input = client.started[-1][0]
    assert new_input.base_task == task_id and new_input.base_patch != ""
    assert new_input.repo_path == str(copy)


def test_rerun_falls_back_to_fresh_task_when_continue_patch_fails(tmp_path):
    repo, head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "sess-a")
    client = FakeClient()
    ops, task_id, copy = _session_task(tmp_path, client, "sess-a", repo, head)
    finish_task(tmp_path, client, ops, task_id, copy, head, patch_changing_calc(repo, "x = 2\n"))
    (repo / "calc.py").write_text("x = 9\n")  # conflicting change on the main repo
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "conflicting")
    rerun = asyncio.run(ops.rerun_task(task_id))
    assert rerun.get("fallback") is True and rerun["task_id"] != task_id
    new_input = client.started[-1][0]
    assert new_input.base_task == "" and new_input.base_patch == ""
    assert "chạy lại từ đầu" in new_input.goal


# ------------------------------------------------------------------ github_env (fake git)

_STUB_GIT = """#!/bin/sh
# Fake git: proves the PAT travels only through the askpass environment,
# never in argv, and that the URL is passed through untouched.
{
echo "ARGV:$*"
if [ -n "$GIT_ASKPASS" ]; then echo "ASKPASS_SET:1"; fi
if [ -n "$KITE_PAT_ASKPASS" ]; then echo "PAT_ENV_SET:1"; fi
} >> "$KITE_STUB_LOG"
case "$*" in
*rev-parse*HEAD*) echo 00000000000000000000000000000000000000aa ;;
*clone*--quiet*)
  dest=$(eval "echo \\${$#}")
  mkdir -p "$dest" ;;
*pull*) ;;
esac
exit 0
"""


def _stub_git_env(tmp_path: Path, monkeypatch) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    stub = bindir / "git"
    stub.write_text(_STUB_GIT)
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("KITE_STUB_LOG", str(log))
    return log


def test_clone_and_pull_with_pat_never_put_pat_in_argv_or_url(tmp_path, monkeypatch):
    log = _stub_git_env(tmp_path, monkeypatch)
    pat = tmp_path / "github_pat_env1"
    pat.write_text("SECRET-PAT-123\n")
    dest = clone_with_pat("https://github.com/acme/demo.git", tmp_path / "demo", pat)
    assert dest.is_dir()
    head = pull_with_pat(tmp_path / "demo", pat)
    assert head.startswith("00000000000000000000000000000000000000")
    lines = log.read_text()
    assert "SECRET-PAT-123" not in lines
    assert "ASKPASS_SET:1" in lines and "PAT_ENV_SET:1" in lines
    assert "https://github.com/acme/demo.git" in lines
    with pytest.raises(GitHubEnvError):
        clone_with_pat("https://token123@github.com/acme/demo.git", tmp_path / "x", pat)
    with pytest.raises(GitHubEnvError):
        clone_with_pat("git@github.com:acme/demo.git", tmp_path / "x", pat)
    with pytest.raises(GitHubEnvError):
        clone_with_pat("https://github.com/acme/demo.git", tmp_path / "x", tmp_path / "nope")
    sealed = tmp_path / "sealed"
    sealed.write_text("kite-sealed:v1:AAAA\n")
    with pytest.raises(GitHubEnvError, match="niêm phong"):
        clone_with_pat("https://github.com/acme/demo.git", tmp_path / "y", sealed)


# ------------------------------------------------------------------ teardown / sweeper

def fake_process(proc: Path, pid: int, argv: list[str], namespace: str = "pid:[1]") -> None:
    entry = proc / str(pid)
    (entry / "ns").mkdir(parents=True)
    (entry / "ns" / "pid").symlink_to(namespace)
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def _fake_proc_tree(tmp_path: Path) -> tuple[Path, Path]:
    proc, config = tmp_path / "proc", tmp_path / "config.yaml"
    (proc / "self" / "ns").mkdir(parents=True)
    (proc / "self" / "ns" / "pid").symlink_to("pid:[1]")
    return proc, config


def test_stop_stale_kills_only_the_named_session(tmp_path):
    proc, config = _fake_proc_tree(tmp_path)
    kite = "/home/u/kite/.venv/bin/kite"
    fake_process(proc, 11, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve",
                            "--session-id", "s1"])
    fake_process(proc, 12, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve",
                            "--session-id", "s2"])
    fake_process(proc, 13, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve"])
    killed = []
    stopped = stop_stale_chat_processes(config, proc=proc, uid=os.getuid(),
                                        kill=lambda pid, sig: killed.append(pid), session_id="s1")
    assert killed == [11] and [item["pid"] for item in stopped] == [11]
    killed.clear()
    stopped = stop_stale_chat_processes(config, proc=proc, uid=os.getuid(),
                                        kill=lambda pid, sig: killed.append(pid))
    assert sorted(killed) == [11, 12, 13]


def test_sweep_removes_orphaned_session_processes_only(tmp_path):
    proc, config = _fake_proc_tree(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "s1", status="active")
    seed_session(db_path, "s2", status="archived")
    (tmp_path / "state" / "chat-sessions" / "s1").mkdir(parents=True)
    (tmp_path / "state" / "chat-sessions" / "s2").mkdir(parents=True)
    kite = "/home/u/kite/.venv/bin/kite"
    fake_process(proc, 11, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve",
                            "--session-id", "s1"])   # active: keep
    fake_process(proc, 12, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve",
                            "--session-id", "s2"])   # archived: kill
    fake_process(proc, 13, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve",
                            "--session-id", "s3"])   # no row, no chat dir: kill
    fake_process(proc, 14, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve"])
    fake_process(proc, 15, ["/home/u/.opencode/bin/opencode", "serve", "--service"])
    killed = []
    swept = sweep_stale_mcp_processes(tmp_path / "state", config, proc=proc, uid=os.getuid(),
                                      kill=lambda pid, sig: killed.append(pid))
    assert sorted(killed) == [12, 13]
    assert {item["session_id"]: item["reason"] for item in swept} == {
        "s2": "session_archived_or_deleted", "s3": "chat_dir_missing"}
