"""Pending memory queue: model proposals are inert until the owner approves them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from poc.kaggle_worker import memory


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
