"""Gói 3 — task origin stamp (chat_session_id / environment_id), structured apply
errors (last_apply_error), and rerun_task. Fake Temporal client, real store and Git repos."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from poc.kaggle_worker.integration import ReviewBundle
from poc.kaggle_worker.mcp_server import (
    ApplyConflictError,
    KiteOps,
    KiteToolError,
    _parse_failed_files,
)
from poc.kaggle_worker.protocol import ScheduledTask
from poc.kaggle_worker.store import SQLiteSchedulerStore, StoreError
from poc.kaggle_worker.temporal_workflow import CodingTaskOutcome

from test_mcp_server import FakeClient, make_ops, make_repo


# ------------------------------------------------------------------ store / protocol

def sample_task(**changes):
    data = dict(
        task_id="task-a", role="coder", model_id="model-a",
        repo_head="abc123", branch="agent/task-a", payload_json='{"goal":"fix"}',
        estimated_gpu_seconds=60, priority=1, created_at=100.0,
    )
    data.update(changes)
    return ScheduledTask(**data)


def test_stamped_task_round_trips_through_the_store(tmp_path):
    store = SQLiteSchedulerStore(tmp_path / "s.sqlite3")
    try:
        store.enqueue(sample_task(chat_session_id="sess-1", environment_id="env-1"))
        row = store.get_task("task-a")
        assert row["chat_session_id"] == "sess-1"
        assert row["environment_id"] == "env-1"
        assert row["last_apply_error"] is None
        assert row["main_repo"] is None          # Gói 2's column: absent here, read as None
        assert store.list_tasks()[0]["chat_session_id"] == "sess-1"
        # Idempotent for the identical task (stamps included in the comparison).
        store.enqueue(sample_task(chat_session_id="sess-1", environment_id="env-1"))
        with pytest.raises(StoreError):
            store.enqueue(sample_task(chat_session_id="sess-2", environment_id="env-1"))
    finally:
        store.close()


def test_unstamped_task_reads_as_unknown_origin(tmp_path):
    store = SQLiteSchedulerStore(tmp_path / "s.sqlite3")
    try:
        store.enqueue(sample_task())
        row = store.get_task("task-a")
        assert row["chat_session_id"] is None and row["environment_id"] is None
    finally:
        store.close()


def test_store_migrates_older_tasks_schema_without_dropping_rows(tmp_path):
    db = tmp_path / "older.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute(
        """CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, role TEXT NOT NULL, model_id TEXT,
            repo_head TEXT NOT NULL, branch TEXT NOT NULL, payload_json TEXT NOT NULL,
            estimated_gpu_seconds INTEGER NOT NULL, priority INTEGER NOT NULL,
            created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'QUEUED',
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
        )"""
    )
    connection.execute(
        "INSERT INTO tasks (task_id, role, repo_head, branch, payload_json,"
        " estimated_gpu_seconds, priority, created_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("task-old", "coder", "abc", "agent/task-old", '{"goal":"fix"}', 60, 1, 100.0, "QUEUED"),
    )
    connection.commit()
    connection.close()

    migrated = SQLiteSchedulerStore(db)
    try:
        columns = {row["name"] for row in migrated._connection.execute("PRAGMA table_info(tasks)")}
        assert {"chat_session_id", "environment_id", "last_apply_error"} <= columns
        row = migrated.get_task("task-old")
        assert row["state"] == "QUEUED" and row["task_type"] == "coding"
        assert row["chat_session_id"] is None and row["environment_id"] is None
        assert row["last_apply_error"] is None
    finally:
        migrated.close()


def test_blank_origin_stamp_is_rejected():
    with pytest.raises(ValueError):
        sample_task(chat_session_id="  ")
    with pytest.raises(ValueError):
        sample_task(environment_id="")


def test_set_last_apply_error_round_trips_and_clears(tmp_path):
    store = SQLiteSchedulerStore(tmp_path / "s.sqlite3")
    try:
        store.enqueue(sample_task())
        payload = {"code": "apply_conflict", "head": "abc", "failed_files": ["a.py"],
                   "at": 123.0, "task_id": "task-a"}
        store.set_last_apply_error("task-a", payload)
        assert json.loads(store.get_task("task-a")["last_apply_error"]) == payload
        store.set_last_apply_error("task-a", None)
        assert store.get_task("task-a")["last_apply_error"] is None
        with pytest.raises(StoreError):
            store.set_last_apply_error("task-missing", payload)
    finally:
        store.close()


# ------------------------------------------------------------------ start_task stamping

def test_start_task_stamps_origin_and_list_tasks_returns_it(tmp_path):
    repo, _head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    started = asyncio.run(ops.start_task(str(repo), "Sửa calc.py",
                                         chat_session_id="sess-9", environment_id="env-7"))
    assert started["chat_session_id"] == "sess-9" and started["environment_id"] == "env-7"
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        assert store.get_task(started["task_id"])["chat_session_id"] == "sess-9"
    listed = asyncio.run(ops.list_tasks())
    assert listed[0]["chat_session_id"] == "sess-9" and listed[0]["environment_id"] == "env-7"
    assert listed[0]["last_apply_error"] is None

    plain = asyncio.run(ops.start_task(str(repo), "Việc không gắn session"))
    assert plain["chat_session_id"] is None and plain["environment_id"] is None
    with pytest.raises(KiteToolError):
        asyncio.run(ops.start_task(str(repo), "x", chat_session_id="  "))


# ------------------------------------------------------------------ apply conflicts

def owner_code(ops: KiteOps, action: str, task_id: str) -> str:
    """A confirmation code the way the owner gets one: `kite tasks confirm` in their own terminal."""
    from poc.kaggle_worker import confirm
    from poc.kaggle_worker.backend import task_slug

    patch = ops.state_dir / "tasks" / task_slug(task_id) / "result.patch"
    return confirm.issue_code(ops.state_dir, action, task_id, confirm.patch_sha256(patch.read_bytes()))


def finished_task(tmp_path: Path, client: FakeClient, ops: KiteOps, repo: Path, head: str) -> str:
    """A COMPLETED task whose patch changes calc.py x = 1 -> x = 2."""
    from poc.kaggle_worker.backend import task_slug

    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
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
    return task_id


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def move_head_conflicting(repo: Path) -> str:
    """Commit a change to the same line the task patch touches: the patch no longer applies."""
    (repo / "calc.py").write_text("x = 99\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "conflicting change")
    return git(repo, "rev-parse", "HEAD")


def test_parse_failed_files_keeps_order_and_dedupes():
    stderr = ("error: patch failed: b.py:10\n"
              "error: b.py: patch does not apply\n"
              "error: patch failed: a.py:3\n"
              "error: patch failed: b.py:22\n")
    assert _parse_failed_files(stderr) == ["b.py", "a.py"]
    assert _parse_failed_files("nothing failed here") == []


def test_apply_conflict_is_structured_and_persisted(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    new_head = move_head_conflicting(repo)

    with pytest.raises(ApplyConflictError) as caught:
        asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    error = caught.value
    assert isinstance(error, KiteToolError)                       # still a plain tool error to chat
    assert error.code == "apply_conflict"
    assert error.head == new_head
    assert error.failed_files == ["calc.py"]
    assert str(error).startswith("patch không áp được (repo đã đổi so với lúc chạy task?): ")

    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        stored = json.loads(store.get_task(task_id)["last_apply_error"])
    assert stored["code"] == "apply_conflict" and stored["head"] == new_head
    assert stored["failed_files"] == ["calc.py"] and stored["task_id"] == task_id
    assert isinstance(stored["at"], float)
    # Visible through task_status, which is what GET /api/tasks/{id} returns as "status".
    status = asyncio.run(ops.task_status(task_id))
    assert status["last_apply_error"]["code"] == "apply_conflict"
    assert asyncio.run(ops.list_tasks())[0]["last_apply_error"]["code"] == "apply_conflict"

    # The old confirmation code was not consumed by the failed apply, and the patch still
    # applies on the original HEAD: applying there succeeds and clears the stored error.
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard", head], check=True)
    applied = asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    assert applied["applied"] is True
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        assert store.get_task(task_id)["last_apply_error"] is None
    assert asyncio.run(ops.task_status(task_id))["last_apply_error"] is None


def test_dirty_repo_apply_error_stays_a_plain_tool_error(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    (repo / "calc.py").write_text("x = 3\n")          # dirty working tree: not a patch conflict
    with pytest.raises(KiteToolError) as caught:
        asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    assert type(caught.value) is KiteToolError       # no structured fields
    assert not hasattr(caught.value, "code")
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        assert store.get_task(task_id)["last_apply_error"] is None


# ------------------------------------------------------------------ rerun_task

def test_rerun_continues_when_the_patch_still_applies(tmp_path):
    repo, _head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py", task_type="analysis",
                                         model="devstral", chat_session_id="sess-9",
                                         environment_id="env-7"))["task_id"]
    finished = finished_task(tmp_path, client, ops, repo, _head)  # noqa: F841 (patch fixture)
    # finished_task creates its own task; rerun the stamped one by giving it the same patch
    # + a checkpoint in its own task dir (where _task_patch reads it).
    from poc.kaggle_worker.backend import task_slug

    patch_src = tmp_path / "state" / "tasks" / task_slug(finished) / "result.patch"
    patch_dst = tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch"
    patch_dst.parent.mkdir(parents=True)
    patch_dst.write_text(patch_src.read_text())
    (patch_dst.parent / "checkpoint.json").write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"

    rerun = asyncio.run(ops.rerun_task(task_id))
    assert "fallback" not in rerun
    task_input, workflow_id, _queue = client.started[-1]
    assert rerun == {"task_id": workflow_id} and workflow_id != task_id
    assert task_input.base_task == task_id and task_input.task_type == "analysis"
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        row = store.get_task(rerun["task_id"])
    assert row["chat_session_id"] == "sess-9" and row["environment_id"] == "env-7"
    assert json.loads(row["payload_json"])["goal"].startswith("Sửa calc.py")


def test_rerun_falls_back_to_a_fresh_task_when_the_patch_no_longer_merges(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py", chat_session_id="sess-9",
                                         environment_id="env-7"))["task_id"]
    finished_task(tmp_path, client, ops, repo, head)
    from poc.kaggle_worker.backend import task_slug

    # Give the stamped task the finished patch + checkpoint, then move HEAD conflictingly.
    other = [t for t in client.started if t[1] != task_id][0][1]
    patch_src = tmp_path / "state" / "tasks" / task_slug(other) / "result.patch"
    patch_dst = tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch"
    patch_dst.parent.mkdir(parents=True)
    patch_dst.write_text(patch_src.read_text())
    (patch_dst.parent / "checkpoint.json").write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"
    move_head_conflicting(repo)

    rerun = asyncio.run(ops.rerun_task(task_id))
    task_input, workflow_id, _queue = client.started[-1]
    assert rerun == {"task_id": workflow_id, "fallback": True,
                     "note": "Lưu ý: patch cũ không gộp được lên HEAD mới nên chạy lại từ đầu."}
    assert workflow_id != task_id
    assert task_input.base_task == ""
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        stored_goal = json.loads(store.get_task(rerun["task_id"])["payload_json"])["goal"]
    assert stored_goal.startswith("Sửa calc.py")
    assert "Lưu ý: patch cũ không gộp được lên HEAD mới nên chạy lại từ đầu." in stored_goal


def test_rerun_without_a_known_repo_fails_closed(tmp_path):
    repo, _head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    # No checkpoint (never ran), no main_repo column: there is no repo to rerun on.
    with pytest.raises(KiteToolError, match="không biết repo"):
        asyncio.run(ops.rerun_task(task_id))
    with pytest.raises(KiteToolError):
        asyncio.run(ops.rerun_task("task-khong-co"))


# ------------------------------------------------------------------ dismiss_apply_error

def test_dismiss_apply_error_clears_the_store_and_leaves_a_journal_note(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    move_head_conflicting(repo)
    with pytest.raises(ApplyConflictError):
        asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        assert store.get_task(task_id)["last_apply_error"] is not None

    dismissed = asyncio.run(ops.dismiss_apply_error(task_id))
    assert dismissed == {"ok": True, "task_id": task_id}
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        assert store.get_task(task_id)["last_apply_error"] is None
    assert asyncio.run(ops.task_status(task_id))["last_apply_error"] is None
    assert asyncio.run(ops.list_tasks())[0]["last_apply_error"] is None
    notes = ops.journal.read("notes")
    assert any(n.get("task_id") == task_id and n.get("text") == "patch đã bỏ" for n in notes)
    # The task detail journal (GET /api/tasks/{id} "journal") shows the note too.
    from poc.kaggle_worker.journal import show_task

    assert any(n.get("text") == "patch đã bỏ"
               for n in show_task(ops.journal, task_id, None)["notes"])


def test_dismiss_apply_error_is_idempotent_and_fails_closed_for_unknown_tasks(tmp_path):
    repo, _head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    assert asyncio.run(ops.dismiss_apply_error(task_id)) == {"ok": True, "task_id": task_id}
    assert asyncio.run(ops.dismiss_apply_error(task_id[-4:])) == {"ok": True, "task_id": task_id}
    with pytest.raises(KiteToolError, match="không có task"):
        asyncio.run(ops.dismiss_apply_error("task-khong-co"))
