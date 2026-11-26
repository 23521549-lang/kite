"""Kite MCP tools for the Vietnamese lead agent: fake Temporal client, real store and Git repos."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from poc.kaggle_worker.config import validate_config
from poc.kaggle_worker.integration import ReviewBundle
from poc.kaggle_worker.mcp_server import KiteOps, KiteToolError, chat_files
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


def make_ops(tmp_path: Path, client: FakeClient, roots=None, status=None) -> KiteOps:
    async def connect(_endpoint):
        return client

    return KiteOps(make_config(tmp_path), db_path=tmp_path / "state" / "scheduler.sqlite3",
                   state_dir=tmp_path / "state", allowed_roots=roots or [tmp_path], connect=connect,
                   gateway_status=lambda: status or {"workers": []})


def test_start_task_queues_and_starts_a_workflow_at_the_repo_head(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    started = asyncio.run(ops.start_task(str(repo), "Phân tích calc.py", task_type="analysis"))
    task_input, workflow_id, queue = client.started[0]
    assert started["task_id"] == workflow_id and workflow_id.startswith("chat-")
    assert (task_input.repo_head, task_input.task_type, queue) == (head, "analysis", "kite-coding-tasks")
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        queued = store.get_task(workflow_id)
    assert queued["state"] == "QUEUED" and queued["task_type"] == "analysis"
    for bad in ({"task_type": "hack"}, {"model": "qwen3.6-fast"}, {"model": "gpt-9"}):
        with pytest.raises(KiteToolError):
            asyncio.run(ops.start_task(str(repo), "x", **bad))
    with pytest.raises(KiteToolError):
        asyncio.run(ops.start_task(str(repo), "   "))


def test_repositories_must_be_git_and_inside_the_allowed_folders(tmp_path):
    repo, _head = make_repo(tmp_path)
    (tmp_path / "allowed" / "plain").mkdir(parents=True)
    ops = make_ops(tmp_path, FakeClient(), roots=[tmp_path / "allowed"])
    with pytest.raises(KiteToolError, match="ngoài"):
        asyncio.run(ops.start_task(str(repo), "x"))
    with pytest.raises(KiteToolError, match="Git"):
        asyncio.run(ops.start_task(str(tmp_path / "allowed" / "plain"), "x"))


def test_instruction_pause_resume_and_cancel_become_workflow_signals(tmp_path):
    client = FakeClient()
    client.state["t1"] = "RUNNING"
    ops = make_ops(tmp_path, client)
    asyncio.run(ops.send_instruction("t1", " Thêm docstring "))
    asyncio.run(ops.pause_task("t1"))
    asyncio.run(ops.resume_task("t1"))
    asyncio.run(ops.cancel_task("t1"))
    assert client.signals == [("t1", "resume_with_instruction", ("Thêm docstring",)), ("t1", "pause", ()),
                              ("t1", "resume_with_instruction", ("",)), ("t1", "cancel", ())]
    with pytest.raises(KiteToolError):
        asyncio.run(ops.send_instruction("t1", "  "))


def test_status_and_result_report_progress_then_the_review_bundle(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    status = asyncio.run(ops.task_status(task_id))
    assert (status["workflow"], status["step"], status["queue_state"]) == ("RUNNING", "CODING", "QUEUED")
    assert "progress" not in status                     # nothing has run yet
    from poc.kaggle_worker.backend import task_slug
    from poc.kaggle_worker.journal import append_jsonl

    trace = tmp_path / "state" / "tasks" / task_slug(task_id) / "trace.jsonl"
    started = time.time() - 200
    for at, record in ((0, {"event": "deps_cached"}), (5, {"event": "run_start", "model": "qwen3.6",
                                                            "worker": "kaggle-han"}),
                       (6, {"event": "fast_start", "helpers": 0}),
                       (90, {"event": "fast_path", "outcome": "tests_failed", "seconds": 84}),
                       (95, {"event": "step_start", "step": "agent", "state": "AGENT_RUNNING"})):
        append_jsonl(trace, record, clock=lambda at=at: started + at)
    status = asyncio.run(ops.task_status(task_id))
    assert status["elapsed"].startswith("3 phút") and status["now"].startswith("agent viết code (đã 1 phút")
    assert status["progress"][-1] == "1 phút 30 giây: đường nhanh: bản sửa trượt test, chuyển cho agent (1 phút 24 giây)"
    assert asyncio.run(ops.task_result(task_id))["ready"] is False

    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = CodingTaskOutcome(task_id, "READY_FOR_REVIEW", str(checkpoint), ReviewBundle(
        task_id, "lease", "kaggle-han", head, "+" * 7000, "Kite test run: PASSED", 20))
    result = asyncio.run(ops.task_result(task_id))
    assert result["ready"] and "PASSED" in result["summary"]
    assert len(result["patch"]) < 7000 and "còn 1000 ký tự" in result["patch"]
    assert result["apply_with"].startswith(f"git -C {repo} apply ")
    with pytest.raises(KiteToolError):
        asyncio.run(ops.task_status("unknown-task"))


def _kill_temporal(ops: KiteOps) -> None:
    """Make every later Temporal connect fail, the way a dead server does."""

    async def dead_connect(_endpoint):
        raise ConnectionRefusedError("127.0.0.1:7233: [Errno 111] Connection refused")

    ops._client = None
    ops._connect = dead_connect


def _set_store_state(tmp_path: Path, task_id: str, state: str) -> None:
    import sqlite3

    conn = sqlite3.connect(tmp_path / "state" / "scheduler.sqlite3")
    try:
        conn.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, task_id))
        conn.commit()
    finally:
        conn.close()


def test_task_status_falls_back_to_store_when_temporal_is_down(tmp_path):
    import hashlib

    from poc.kaggle_worker.backend import task_slug

    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    root = tmp_path / "state" / "tasks" / task_slug(task_id)
    root.mkdir(parents=True, exist_ok=True)
    patch = "diff --git a/calc.py b/calc.py\n+ x = 2\n"
    (root / "result.patch").write_text(patch, encoding="utf-8")
    (root / "checkpoint.json").write_text(json.dumps({"repo_path": str(repo)}), encoding="utf-8")
    (root / "result.json").write_text(json.dumps({"worker": "kaggle-han"}), encoding="utf-8")
    _set_store_state(tmp_path, task_id, "COMPLETED")
    _kill_temporal(ops)
    status = asyncio.run(ops.task_status(task_id))
    assert (status["workflow"], status["queue_state"]) == ("COMPLETED", "COMPLETED")
    assert status["temporal_unavailable"] is True and "error" not in status
    result = asyncio.run(ops.task_result(task_id))
    assert result["ready"] is True and result["temporal_unavailable"] is True
    assert result["patch_sha256"] == hashlib.sha256(patch.encode("utf-8")).hexdigest()
    assert result["worker"] == "kaggle-han" and result["repo_path"] == str(repo)
    assert result["apply_with"] == f"git -C {repo} apply {root / 'result.patch'}"
    assert "summary" not in result      # the summary only lives in the Temporal outcome
    with pytest.raises(KiteToolError):
        asyncio.run(ops.task_status("unknown-task"))


def test_task_status_falls_back_when_workflow_is_gone_but_store_knows_it(tmp_path):
    repo, _head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    del client.state[task_id]           # Temporal is up, but no workflow for this task
    _set_store_state(tmp_path, task_id, "RUNNING")
    status = asyncio.run(ops.task_status(task_id))
    assert (status["workflow"], status["queue_state"]) == ("RUNNING", "RUNNING")
    assert status["temporal_unavailable"] is True and "error" not in status


def test_task_status_falls_back_for_a_queued_task_and_keeps_waiting_text(tmp_path):
    repo, _head = make_repo(tmp_path)
    ops = make_ops(tmp_path, FakeClient())
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    _kill_temporal(ops)
    status = asyncio.run(ops.task_status(task_id))
    assert (status["workflow"], status["queue_state"]) == ("QUEUED", "QUEUED")
    assert status["waiting_for"].startswith("đang chờ worker")


def owner_code(ops: KiteOps, action: str, task_id: str) -> str:
    """A confirmation code the way the owner gets one: `kite tasks confirm` in their own terminal."""
    from poc.kaggle_worker import confirm
    from poc.kaggle_worker.backend import task_slug

    patch = ops.state_dir / "tasks" / task_slug(task_id) / "result.patch"
    return confirm.issue_code(ops.state_dir, action, task_id, confirm.patch_sha256(patch.read_bytes()))


def _dead_temporal_completed_task(tmp_path: Path, repo: Path, head: str) -> tuple[KiteOps, str]:
    """A COMPLETED task (store) with a real patch on disk, Temporal dead.

    Mirrors what the demo has after the F6 round: the workflow finished, its
    patch/checkpoint/result.json are on disk, but no Temporal server answers,
    so task_status/task_result fall back to the store and the disk.
    """
    from poc.kaggle_worker.backend import task_slug

    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    (repo / "calc.py").write_text("x = 2\n")
    patch = subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True, text=True,
                           check=True).stdout
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "calc.py"], check=True)
    root = tmp_path / "state" / "tasks" / task_slug(task_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.patch").write_text(patch, encoding="utf-8")
    (root / "checkpoint.json").write_text(json.dumps({"repo_path": str(repo)}), encoding="utf-8")
    (root / "result.json").write_text(json.dumps({"worker": "kaggle-han"}), encoding="utf-8")
    _set_store_state(tmp_path, task_id, "COMPLETED")
    _kill_temporal(ops)
    return ops, task_id


def test_apply_result_works_through_fallback_when_temporal_is_down(tmp_path):
    repo, head = make_repo(tmp_path)
    ops, task_id = _dead_temporal_completed_task(tmp_path, repo, head)
    result = asyncio.run(ops.task_result(task_id))
    assert result["status"] == "READY_FOR_REVIEW" and result["temporal_unavailable"] is True
    code = owner_code(ops, "apply", task_id)
    applied = asyncio.run(ops.apply_result(task_id, code))
    assert applied["files"] == ["calc.py"] and applied["committed"] is False
    assert (repo / "calc.py").read_text() == "x = 2\n"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip() == head
    with pytest.raises(KiteToolError, match="đã được dùng"):
        asyncio.run(ops.apply_result(task_id, code))


def test_apply_result_with_a_wrong_code_fails_without_burning_the_good_code(tmp_path):
    repo, _head = make_repo(tmp_path)
    ops, task_id = _dead_temporal_completed_task(tmp_path, repo, _head)
    good = owner_code(ops, "apply", task_id)
    with pytest.raises(KiteToolError, match="không đúng"):
        asyncio.run(ops.apply_result(task_id, "khong-phai-ma"))
    applied = asyncio.run(ops.apply_result(task_id, good))
    assert applied["files"] == ["calc.py"]
    assert (repo / "calc.py").read_text() == "x = 2\n"


def test_logged_never_writes_a_confirmation_code_to_the_journal(tmp_path):
    repo, _head = make_repo(tmp_path)
    ops, task_id = _dead_temporal_completed_task(tmp_path, repo, _head)
    code = owner_code(ops, "apply", task_id)
    # The exact call the chat MCP tool and the web apply endpoint both make.
    applied = asyncio.run(ops.logged("apply_result", ops.apply_result, task_id, code))
    assert applied["files"] == ["calc.py"]
    # Reusing the same code fails — the error record is written by logged() too.
    with pytest.raises(KiteToolError, match="đã được dùng"):
        asyncio.run(ops.logged("apply_result", ops.apply_result, task_id, code))
    raw = (tmp_path / "state" / "journal" / "actions.jsonl").read_text(encoding="utf-8")
    assert code not in raw
    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
    apply_records = [r for r in lines if r["tool"] == "apply_result"]
    assert len(apply_records) == 2
    assert all(r["args"]["confirmation_code"] == "[redacted]" for r in apply_records)
    assert all(r["args"]["task_id"] == task_id for r in apply_records)
    assert apply_records[0]["ok"] is True and apply_records[1]["ok"] is False


def test_apply_result_still_refuses_a_completed_task_without_a_patch(tmp_path):
    from poc.kaggle_worker.backend import task_slug

    repo, _head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    root = tmp_path / "state" / "tasks" / task_slug(task_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "checkpoint.json").write_text(json.dumps({"repo_path": str(repo)}), encoding="utf-8")
    _set_store_state(tmp_path, task_id, "COMPLETED")
    _kill_temporal(ops)
    result = asyncio.run(ops.task_result(task_id))
    assert result["status"] == "COMPLETED"      # no patch on disk: never READY_FOR_REVIEW
    with pytest.raises(KiteToolError, match="không có patch"):
        asyncio.run(ops.apply_result(task_id, "khong-phai-ma"))


def test_apply_result_still_refuses_a_completed_task_with_an_empty_patch(tmp_path):
    from poc.kaggle_worker.backend import task_slug

    repo, _head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    root = tmp_path / "state" / "tasks" / task_slug(task_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.patch").write_text("", encoding="utf-8")
    (root / "checkpoint.json").write_text(json.dumps({"repo_path": str(repo)}), encoding="utf-8")
    _set_store_state(tmp_path, task_id, "COMPLETED")
    _kill_temporal(ops)
    result = asyncio.run(ops.task_result(task_id))
    assert result["status"] == "COMPLETED"      # an empty patch is no patch
    with pytest.raises(KiteToolError, match="không có patch"):
        asyncio.run(ops.apply_result(task_id, "khong-phai-ma"))


def test_apply_result_applies_the_patch_without_committing(tmp_path):
    from poc.kaggle_worker.backend import task_slug

    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    with pytest.raises(KiteToolError, match="chưa xong"):
        asyncio.run(ops.apply_result(task_id))
    (repo / "calc.py").write_text("x = 2\n")
    patch = subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "calc.py"], check=True)
    patch_path = tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(patch)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = CodingTaskOutcome(task_id, "READY_FOR_REVIEW", str(checkpoint), ReviewBundle(
        task_id, "lease", "kaggle-han", head, patch, "Kite test run: PASSED", 20))
    with pytest.raises(KiteToolError, match="thiếu mã xác nhận"):
        asyncio.run(ops.apply_result(task_id))
    applied = asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    assert applied["files"] == ["calc.py"] and applied["committed"] is False
    assert (repo / "calc.py").read_text() == "x = 2\n"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip() == head
    with pytest.raises(KiteToolError, match="chưa commit"):
        asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))


def finished_task(tmp_path: Path, client: FakeClient, ops: KiteOps, repo: Path, head: str) -> str:
    """A COMPLETED task whose patch changes calc.py and adds notes.md."""
    from poc.kaggle_worker.backend import task_slug

    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    (repo / "calc.py").write_text("x = 2\n")
    (repo / "notes.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    patch = subprocess.run(["git", "-C", str(repo), "diff", "--cached"], capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard"], check=True)
    patch_path = tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(patch)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = CodingTaskOutcome(task_id, "READY_FOR_REVIEW", str(checkpoint), ReviewBundle(
        task_id, "lease", "kaggle-han", head, patch, "Kite test run: PASSED", 20))
    return task_id


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_commit_result_commits_only_the_applied_patch_files(tmp_path):
    repo, head = make_repo(tmp_path)
    for key, value in (("user.name", "t"), ("user.email", "t@e.com")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    with pytest.raises(KiteToolError, match="chưa được áp"):
        asyncio.run(ops.commit_result(task_id, "Add median"))
    asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    (repo / "other.txt").write_text("not part of the patch\n")
    with pytest.raises(KiteToolError, match="trống"):
        asyncio.run(ops.commit_result(task_id, "  "))
    with pytest.raises(KiteToolError, match="thiếu mã xác nhận"):
        asyncio.run(ops.commit_result(task_id, "Add median"))
    committed = asyncio.run(ops.commit_result(task_id, "Add median", owner_code(ops, "commit", task_id)))
    assert committed["committed"] and not committed["pushed"] and sorted(committed["files"]) == ["calc.py", "notes.md"]
    assert git_out(repo, "log", "-1", "--format=%s") == "Add median"
    assert sorted(git_out(repo, "show", "--name-only", "--format=", "HEAD").split()) == ["calc.py", "notes.md"]
    assert git_out(repo, "status", "--short") == "?? other.txt"


def test_commit_result_refuses_when_other_files_are_staged(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    (repo / "staged.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "staged.txt"], check=True)
    with pytest.raises(KiteToolError, match="staged.txt"):
        asyncio.run(ops.commit_result(task_id, "Add median", owner_code(ops, "commit", task_id)))
    assert git_out(repo, "rev-parse", "HEAD") == head


def test_repo_status_is_read_only(tmp_path):
    repo, head = make_repo(tmp_path)
    (repo / "calc.py").write_text("x = 3\n")
    status = asyncio.run(make_ops(tmp_path, FakeClient()).repo_status(str(repo)))
    assert status["head"] == head and "calc.py" in status["changes"] and "init" in status["recent_commits"]
    assert (repo / "calc.py").read_text() == "x = 3\n"


def test_run_command_uses_an_offline_container_on_a_copy(tmp_path):
    from poc.kaggle_worker.sandbox import SandboxSettings

    repo, _head = make_repo(tmp_path, "proj")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN")
    (repo / "link").symlink_to(secret)
    (repo / "node_modules").mkdir()
    seen = {}

    def runner(args, *, timeout=None):
        work = Path(args[args.index("-w") - 1].split(":")[0])
        seen.update(args=args, timeout=timeout, files=sorted(p.name for p in work.iterdir()),
                    link_is_link=(work / "link").is_symlink())
        (work / "calc.py").write_text("changed in the container\n")
        return subprocess.CompletedProcess(args, 0, "\x1b[32m1 passed\x1b[0m\n" + "y" * 9000, "")

    ops = KiteOps(make_config(tmp_path), db_path=tmp_path / "db.sqlite3", state_dir=tmp_path / "state",
                  allowed_roots=[tmp_path], runner=runner, sandbox_settings=SandboxSettings(uid=1000))
    result = asyncio.run(ops.run_command(str(repo), "python -m pytest -q", timeout_seconds=99999))
    args = seen["args"]
    assert args[args.index("--network") + 1] == "none" and "--cap-drop" in args
    assert args[-3:] == ["sh", "-c", "python -m pytest -q"] and seen["timeout"] == 600
    assert "node_modules" not in seen["files"] and ".git" in seen["files"] and seen["link_is_link"]
    assert result["exit_code"] == 0 and result["truncated"] and len(result["output"]) == 8000
    assert (repo / "calc.py").read_text() == "x = 1\n"           # the real repository is untouched
    assert list((tmp_path / "state" / "exec").iterdir()) == []    # the copy is deleted
    with pytest.raises(KiteToolError):
        asyncio.run(ops.run_command(str(repo), "   "))
    with pytest.raises(KiteToolError, match="ngoài"):
        asyncio.run(ops.run_command("/etc", "ls"))


def test_list_tasks_newest_first_and_worker_status(tmp_path):
    repo, _head = make_repo(tmp_path)
    ops = make_ops(tmp_path, FakeClient(), status={"workers": [{"worker_id": "kaggle-han", "alive": True}]})
    first = asyncio.run(ops.start_task(str(repo), "Việc một"))["task_id"]
    asyncio.run(asyncio.sleep(1.1))
    second = asyncio.run(ops.start_task(str(repo), "Việc hai"))["task_id"]
    listed = asyncio.run(ops.list_tasks())
    assert [t["task_id"] for t in listed] == [second, first] and listed[0]["goal"] == "Việc hai"
    assert asyncio.run(ops.worker_status())["workers"][0]["alive"] is True


def test_chat_files_lock_the_lead_agent_down_and_speak_vietnamese(tmp_path):
    files = chat_files(make_config(tmp_path), lead_model="qwen3.6-fast",
                       kite_command=["/k/kite", "--config", "/c.yaml", "mcp", "serve"], allowed_roots=[Path("/home/u")])
    config = json.loads(files["opencode.json"])
    assert config["model"] == "kite/qwen3.6-fast"
    permission = config["permission"]
    assert (permission["edit"], permission["webfetch"], permission["external_directory"]) == ("deny",) * 3
    assert permission["bash"]["*"] == "ask"
    assert list(permission["bash"])[0] == "*"            # OpenCode: the last matching rule wins
    for dangerous in ("sudo *", "docker *", "curl *", "git push*", "*secrets*"):
        assert permission["bash"][dangerous] == "deny"
    assert config["mcp"]["kite"]["command"] == ["/k/kite", "--config", "/c.yaml", "mcp", "serve"]
    assert config["provider"]["kite"]["options"]["baseURL"] == "http://127.0.0.1:8787/v1"
    assert "tiếng Việt" in files["AGENTS.md"] and "`/home/u`" in files["AGENTS.md"]


def test_context_window_per_model_reaches_opencode(tmp_path):
    from poc.kaggle_worker.config import ConfigError, model_contexts, sandbox_settings_from_config
    from poc.kaggle_worker.sandbox import agent_config

    raw = _raw_config(tmp_path)
    raw["gateway"]["model_context"] = {"qwen3.6": 65536}
    raw["sandbox"] = {"gateway_host": "172.19.0.1"}
    config = validate_config(raw)
    # Aliases follow their base model; models not listed keep 32768.
    assert model_contexts(config, ["qwen3.6", "devstral", "qwen3.6-fast"]) == {
        "qwen3.6": 65536, "devstral": 32768, "qwen3.6-fast": 65536}
    files = chat_files(config, lead_model="qwen3.6", kite_command=["kite"], allowed_roots=[Path("/home/u")])
    models = json.loads(files["opencode.json"])["provider"]["kite"]["models"]
    assert models["qwen3.6"]["limit"]["context"] == 65536 and models["devstral"]["limit"]["context"] == 32768
    sandbox = agent_config(sandbox_settings_from_config(config), model="qwen3.6", models=["qwen3.6", "devstral"])
    limits = {m: spec["limit"]["context"] for m, spec in sandbox["provider"]["kite"]["models"].items()}
    assert limits == {"qwen3.6": 65536, "devstral": 32768}
    # A 128K window shared by the chat and one task (--parallel 2 --kv-unified): 48K chat, 80K agent.
    raw["gateway"]["model_context"] = {"qwen3.6": 131072}
    config = validate_config(raw)
    files = chat_files(config, lead_model="qwen3.6", kite_command=["kite"], allowed_roots=[Path("/home/u")])
    assert json.loads(files["opencode.json"])["provider"]["kite"]["models"]["qwen3.6"]["limit"]["context"] == 49152
    sandbox = agent_config(sandbox_settings_from_config(config), model="qwen3.6", models=["qwen3.6"])
    assert sandbox["provider"]["kite"]["models"]["qwen3.6"]["limit"]["context"] == 81920
    for bad in ({"qwen3.6": 1024}, {"qwen3.6": 10 ** 6}, {"qwen3.6": "64k"}, {"bad name!": 65536}):
        raw["gateway"]["model_context"] = bad
        with pytest.raises(ConfigError):
            validate_config(raw)


def test_mcp_server_exposes_every_tool(tmp_path):
    pytest.importorskip("mcp")
    from poc.kaggle_worker.mcp_server import build_server

    server = build_server(make_ops(tmp_path, FakeClient()))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"start_task", "task_status", "task_result", "apply_result", "commit_result", "repo_status",
                     "run_command", "send_instruction", "pause_task",
                     "resume_task", "cancel_task", "list_tasks", "worker_status", "note_task",
                     # Chat-first: nothing the full-test day needed a terminal for is left out.
                     "wait_task", "task_log", "remember", "memory_show", "memory_forget", "repo_config",
                     "doctor", "cleanup", "worker_restart", "update_kite", "bench_start", "bench_history",
                     # Gói 6 (Kiến thức): đọc kho tài liệu qua MCP.
                     "knowledge_list", "knowledge_read",
                     # Gói 10: tài liệu kinh nghiệm + vòng tự cải tiến.
                     "experience_distill", "experience_show", "experience_approve", "experience_reject",
                     "improve_list", "improve_approve", "improve_reject"}


def fake_process(proc: Path, pid: int, argv: list[str], namespace: str = "pid:[1]") -> None:
    entry = proc / str(pid)
    (entry / "ns").mkdir(parents=True)
    (entry / "ns" / "pid").symlink_to(namespace)
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def test_reload_stops_the_opencode_daemon_and_old_mcp_servers_only(tmp_path):
    from poc.kaggle_worker.mcp_server import stop_stale_chat_processes

    proc, config = tmp_path / "proc", tmp_path / "config.yaml"
    (proc / "self" / "ns").mkdir(parents=True)
    (proc / "self" / "ns" / "pid").symlink_to("pid:[1]")
    kite = "/home/u/kite/.venv/bin/kite"
    fake_process(proc, 10, ["/home/u/.opencode/bin/opencode", "serve", "--service"])
    fake_process(proc, 11, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve", "--allow-root", "/home/u"])
    fake_process(proc, 12, ["/usr/bin/python", kite, "--config", "/other.yaml", "mcp", "serve"])  # another config
    fake_process(proc, 13, ["/usr/local/bin/opencode", "run", "-m", "kite/qwen3.6", "fix"])      # TUI-less agent
    fake_process(proc, 14, ["/usr/local/bin/opencode", "serve"], namespace="pid:[999]")         # inside a container
    fake_process(proc, 15, ["/home/u/.opencode/bin/opencode"])                                   # the TUI itself
    killed = []
    stopped = stop_stale_chat_processes(config, proc=proc, uid=os.getuid(), kill=lambda pid, sig: killed.append(pid))
    assert sorted(killed) == [10, 11] and sorted(item["pid"] for item in stopped) == [10, 11]

    def gone(pid, sig):
        raise ProcessLookupError
    assert stop_stale_chat_processes(config, proc=proc, uid=os.getuid(), kill=gone) == []
    assert stop_stale_chat_processes(config, proc=proc, uid=os.getuid() + 1, kill=gone) == []


def test_chat_only_models_never_run_tasks(tmp_path):
    from dataclasses import replace

    from poc.kaggle_worker.config import model_policy_from_config, validate_config
    from poc.kaggle_worker.routing import ModelPolicy

    with pytest.raises(Exception):
        validate_config({**_raw_config(tmp_path), "routing": {"chat_only": "devstral"}})
    config = validate_config({**_raw_config(tmp_path), "routing": {"chat_only": ["devstral", "devstral"]}})
    assert config.routing.chat_only == ("devstral",)
    assert model_policy_from_config(config).excluded == frozenset({"devstral"})
    ops = make_ops(tmp_path, FakeClient())
    ops.config = config
    repo, _head = make_repo(tmp_path)
    assert "devstral" not in ops.base_models()
    with pytest.raises(KiteToolError):
        asyncio.run(ops.start_task(str(repo), "x", model="devstral"))


def test_lead_agent_tool_calls_are_journaled_with_results_and_errors(tmp_path):
    from poc.kaggle_worker.journal import read_jsonl

    repo, _head = make_repo(tmp_path)
    ops = make_ops(tmp_path, FakeClient())
    started = asyncio.run(ops.logged("start_task", ops.start_task, str(repo), "Sửa calc.py", "coding", None))
    with pytest.raises(KiteToolError):
        asyncio.run(ops.logged("commit_result", ops.commit_result, started["task_id"], ""))
    ok, failed = read_jsonl(tmp_path / "state" / "journal" / "actions.jsonl")
    assert ok["tool"] == "start_task" and ok["ok"] and ok["task_id"] == started["task_id"]
    assert ok["args"]["goal"] == "Sửa calc.py" and ok["args"]["task_type"] == "coding"
    assert failed["tool"] == "commit_result" and not failed["ok"] and "trống" in failed["error"]
    assert failed["task_id"] == started["task_id"]


def test_chat_and_sandbox_requests_carry_a_task_tag(tmp_path):
    from poc.kaggle_worker.sandbox import SandboxSettings, agent_config

    files = chat_files(make_config(tmp_path), lead_model="qwen3.6", kite_command=["kite"], allowed_roots=[tmp_path])
    assert json.loads(files["opencode.json"])["provider"]["kite"]["options"]["headers"] == {"X-Kite-Task": "chat"}
    config = agent_config(SandboxSettings(), model="qwen3.6", models=["qwen3.6"], task_tag="chat-1")
    assert config["provider"]["kite"]["options"]["headers"] == {"X-Kite-Task": "chat-1"}
    assert "headers" not in agent_config(SandboxSettings(), model="qwen3.6", models=["qwen3.6"])["provider"]["kite"]["options"]


def test_lead_cannot_read_files_with_shell_and_is_told_not_to_promise_notifications(tmp_path):
    from poc.kaggle_worker.mcp_server import LEAD_BASH_PERMISSION

    for command in ("ls", "ls -la /home/ubuntu/kite-trial", "cat calc.py", "git log", "grep -rn x ."):
        pattern = command if command in LEAD_BASH_PERMISSION else command.split()[0] + " *"
        assert LEAD_BASH_PERMISSION[pattern] == "deny", command
    assert LEAD_BASH_PERMISSION["*"] == "ask"
    agents = chat_files(make_config(tmp_path), lead_model="qwen3.6", kite_command=["kite"],
                        allowed_roots=[tmp_path])["AGENTS.md"]
    assert "`wait_task`" in agents and "mỗi việc một `start_task`" in agents and "`repo_status`" in agents
    assert "KHÔNG BAO GIỜ nói đã làm xong một việc khi tool của việc đó báo lỗi" in agents


def test_persisted_always_allow_shell_approvals_are_found_and_cleared(tmp_path):
    import sqlite3

    from poc.kaggle_worker.mcp_server import clear_shell_approvals, persisted_shell_approvals

    assert persisted_shell_approvals(tmp_path / "missing.db") == []
    path = tmp_path / "opencode.db"
    db = sqlite3.connect(path)
    db.execute("create table permission (id text, project_id text, action text, resource text, "
               "time_created int, time_updated int)")
    db.executemany("insert into permission values (?, ?, ?, ?, 1, 1)",
                   [("psv_1", "global", "shell", "ls *"), ("psv_2", "p1", "edit", "*.py")])
    db.commit()
    db.close()
    assert [a["pattern"] for a in persisted_shell_approvals(path)] == ["ls *"]
    result = clear_shell_approvals(path)
    assert result["cleared"] == 1 and result["patterns"] == ["ls *"] and Path(result["backup"]).exists()
    assert persisted_shell_approvals(path) == []
    left = sqlite3.connect(path).execute("select id from permission").fetchall()
    assert left == [("psv_2",)]                                   # only shell approvals are removed
    assert sqlite3.connect(result["backup"]).execute("select count(*) from permission").fetchone() == (2,)
    assert clear_shell_approvals(path) == {"cleared": 0}


def test_note_task_records_the_users_verdict_in_the_journal(tmp_path):
    from poc.kaggle_worker.journal import read_jsonl

    ops = make_ops(tmp_path, FakeClient())
    saved = asyncio.run(ops.note_task("chat-20261120-aaaa", "BAD", "sửa luôn cả file test"))
    assert saved["saved"] and saved["verdict"] == "bad"
    (note,) = read_jsonl(tmp_path / "state" / "journal" / "notes.jsonl")
    assert (note["task_id"], note["verdict"], note["text"], note["source"]) == (
        "chat-20261120-aaaa", "bad", "sửa luôn cả file test", "chat")
    for bad in (("chat-1", "meh"), ("", "good")):
        with pytest.raises(KiteToolError):
            asyncio.run(ops.note_task(*bad))
    from poc.kaggle_worker.mcp_server import LEAD_INSTRUCTIONS as agents
    assert "`note_task`" in agents and "`continue_from`" in agents and "tiền tố `kite_`" in agents


def test_a_repo_without_kite_yml_gets_a_proposal_and_never_a_file(tmp_path):
    from poc.kaggle_worker.sandbox import suggest_repo_config

    repo, _ = make_repo(tmp_path)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com", *a],
                                    check=True, capture_output=True)
    (repo / "svc" / "api" / "tests").mkdir(parents=True)
    (repo / "svc" / "api" / "requirements.txt").write_text("fastapi\n")
    (repo / "svc" / "api" / "tests" / "test_api.py").write_text("def test_ok():\n    pass\n")
    run("add", "-A")
    run("commit", "-qm", "service")
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    started = asyncio.run(ops.start_task(str(repo), "Thêm endpoint /health"))
    proposal = started["repo_config"]["config"]
    assert started["repo_config"]["new"] is True
    assert "setup: pip install -q -r svc/api/requirements.txt" in proposal
    assert "test: PYTHONPATH=. python -m pytest -q svc/api/tests" in proposal and "{tests}" in proposal
    assert not (repo / ".kite.yml").exists()                                    # never a file in the repo
    # Kite keeps it, so the task installs and tests the right way without anyone writing .kite.yml.
    saved = Path(started["repo_config"]["file"])
    assert saved.is_file() and "svc/api/requirements.txt" in saved.read_text()
    assert "new" not in asyncio.run(ops.start_task(str(repo), "Thêm endpoint /ready"))["repo_config"]
    status = asyncio.run(ops.repo_status(str(repo)))
    assert status["kite_yml"] == {"present": False, "suggestion": proposal}
    (repo / ".kite.yml").write_text("test: python -m pytest -q svc\n")
    run("add", "-A")
    run("commit", "-qm", "kite")
    assert asyncio.run(ops.repo_status(str(repo)))["kite_yml"]["present"] is True
    assert "repo_config" not in asyncio.run(ops.start_task(str(repo), "x y"))
    # Nothing to install and tests at the root: Kite's defaults fit, no proposal.
    assert suggest_repo_config(["calc.py", "tests/test_calc.py"]) == ""
    assert "pip install -q -e '.[test]'" in suggest_repo_config(
        ["pyproject.toml", "pkg/core.py", "tests/test_core.py"], "[project.optional-dependencies]\ntest = [\"pytest\"]\n")


def test_a_chat_only_model_gets_its_own_tool_less_agent_in_the_chat(tmp_path):
    plain = json.loads(chat_files(make_config(tmp_path), lead_model="qwen3.6-fast", kite_command=["kite"],
                                  allowed_roots=[tmp_path])["opencode.json"])
    assert "agent" not in plain                                   # no chat-only model: nothing changes
    raw = _raw_config(tmp_path)
    raw["providers"][0]["models"].append("r1-distill")
    raw["routing"] = {"chat_only": ["r1-distill"]}
    config = validate_config(raw)
    files = chat_files(config, lead_model="qwen3.6-fast", kite_command=["kite"], allowed_roots=[tmp_path])
    opencode = json.loads(files["opencode.json"])
    agent = opencode["agent"]["suy-luan"]
    assert agent["model"] == "kite/r1-distill" and agent["mode"] == "primary" and agent["tools"] == {"*": False}
    assert agent["permission"] == {"edit": "deny", "bash": "deny", "webfetch": "deny"}
    assert "KHÔNG có tool" in agent["prompt"] and "r1-distill" in agent["prompt"] and "Tab" in agent["prompt"]
    assert opencode["model"] == "kite/qwen3.6-fast"                      # the lead agent stays the default
    assert "## Agent suy luận" in files["AGENTS.md"] and "đừng gọi `start_task` với model này" in files["AGENTS.md"]
    ops = KiteOps(config, db_path=tmp_path / "s.sqlite3", state_dir=tmp_path, allowed_roots=[tmp_path])
    assert "r1-distill" not in ops.base_models()                         # tasks can never name it
