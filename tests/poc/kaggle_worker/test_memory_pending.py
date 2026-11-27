"""Pending memory queue: model proposals are inert until the owner approves them."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from poc.kaggle_worker import memory
from tests.poc.kaggle_worker.test_mcp_server import FakeClient, make_ops, make_repo


def _journal_task(state: Path, task_id: str, repo: Path) -> None:
    folder = state / "journal"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tasks.jsonl").write_text(
        json.dumps({"task_id": task_id, "repo": str(repo)}) + "\n", encoding="utf-8")


def test_proposed_lesson_is_inert_until_approved(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    entry = memory.propose_lesson(state, repo, "chat", "Đừng sửa file test")
    assert memory.lessons(state, repo) == [] and memory.lessons_note(state, repo) == ""
    assert [e["id"] for e in memory.list_pending(state)] == [entry["id"]]
    approved = memory.approve_pending(state, entry["id"], clock=lambda: 1_700_000_000.0)
    assert approved["approved"] and memory.list_pending(state) == []
    note = memory.lessons_note(state, repo)
    assert "Đừng sửa file test" in note and "chủ máy đã duyệt" in note


def test_proposed_preference_reaches_agents_block_only_after_approval(tmp_path):
    state = tmp_path / "state"
    entry = memory.propose_preference(state, "Trả lời ngắn gọn")
    assert memory.preferences(state) == [] and memory.preferences_block(state) == ""
    memory.approve_pending(state, entry["id"], clock=lambda: 1_700_000_000.0)
    block = memory.preferences_block(state)
    assert "Trả lời ngắn gọn" in block and "chủ máy đã duyệt" in block


def test_rejected_entry_never_becomes_effective(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    entry = memory.propose_lesson(state, repo, "chat", "Bài học bị từ chối")
    assert memory.reject_pending(state, entry["id"])["approved"] is False
    assert memory.list_pending(state) == [] and memory.lessons(state, repo) == []
    with pytest.raises(ValueError, match="không có mục chờ duyệt"):
        memory.approve_pending(state, entry["id"])


def test_propose_note_queues_only_bad_verdicts_with_a_reason(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    _journal_task(state, "t-1", repo)
    assert memory.propose_note(state, "t-1", "good", "tốt") is None
    assert memory.propose_note(state, "t-1", "bad", "") is None
    assert memory.propose_note(state, "unknown", "bad", "sai") is None
    entry = memory.propose_note(state, "t-1", "bad", "Quên cập nhật README")
    assert entry is not None and entry["kind"] == "lesson"
    assert memory.lessons(state, repo) == []                       # still only queued
    memory.approve_pending(state, entry["id"])
    assert "Quên cập nhật README" in memory.lessons_note(state, repo)


def test_memory_show_pending_is_filtered_by_repo(tmp_path):
    # A repo-scoped memory_show must not leak another repo's pending lessons
    # (or chat preferences) into that repo's view.
    repo_a, _ = make_repo(tmp_path, "a")
    repo_b, _ = make_repo(tmp_path, "b")
    state = tmp_path / "state"
    entry_a = memory.propose_lesson(state, repo_a, "t-a", "Bài học của A")
    entry_b = memory.propose_lesson(state, repo_b, "t-b", "Bài học của B")
    entry_pref = memory.propose_preference(state, "Trả lời ngắn")
    ops = make_ops(tmp_path, FakeClient())
    shown_a = asyncio.run(ops.memory_show(str(repo_a)))
    assert [e["id"] for e in shown_a["pending"]] == [entry_a["id"]]
    assert shown_a["pending"][0]["task_id"] == "t-a"
    shown_b = asyncio.run(ops.memory_show(str(repo_b)))
    assert [e["id"] for e in shown_b["pending"]] == [entry_b["id"]]
    shown_all = asyncio.run(ops.memory_show(""))
    assert [e["id"] for e in shown_all["pending"]] == [
        entry_a["id"], entry_b["id"], entry_pref["id"]]


def test_proposals_from_several_processes_at_once_are_all_kept(tmp_path):
    # Every chat session has its own MCP server process, and the web and the worker write the
    # queue too: without a lock around read-modify-write they lost each other's entries
    # (4 x 50 proposals at once kept a few dozen).
    import subprocess
    import sys

    code = ("import sys; from pathlib import Path; from poc.kaggle_worker import memory\n"
            "for i in range(50): memory.propose_preference(Path(sys.argv[1]), f'{sys.argv[2]}-{i}')")
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(n)]) for n in range(4)]
    assert [child.wait(timeout=120) for child in children] == [0, 0, 0, 0]
    assert len({entry["text"] for entry in memory.list_pending(tmp_path)}) == 200
    assert not list((tmp_path / "memory").glob("*.tmp"))
