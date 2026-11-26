"""Gói 4 (worker (c)): "Cần bạn quyết" — scan + dedupe ntfy.

The scan only reads the scheduler store, the journal and the spend ledger; the ntfy step only
sends a task id + a short label, never code or goal text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from poc.kaggle_worker import memory
from poc.kaggle_worker.journal import Journal
from poc.kaggle_worker.needs_decision import notify_new_items, scan_needs_decision
from poc.kaggle_worker.relay import SpendLedger

NOW = 1_795_400_000.0


class FakeStore:
    """Scheduler store as the scan uses it: only list_tasks()."""

    def __init__(self, tasks):
        self._tasks = tasks

    def list_tasks(self):
        return self._tasks


def task(task_id, state, created_at, goal="sửa lỗi", last_error=""):
    return {"task_id": task_id, "state": state, "created_at": created_at,
            "payload_json": json.dumps({"goal": goal}), "last_error": last_error}


class FakeNotifier:
    """A Notifier stand-in: records (title, message) instead of sending."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, title, message, *, tags=(), priority=3):
        self.sent.append((title, message))


def scan(tmp_path, tasks, *, actions=(), ledger=None, caps=None, near_cap_pct=80, clock=NOW):
    journal = Journal(tmp_path / "journal")
    for action in actions:
        journal.write("actions", action)
    return scan_needs_decision(FakeStore(tasks), journal, tmp_path, ledger,
                               near_cap_pct=near_cap_pct, caps=caps, clock=lambda: clock)


def write_patch(state_dir, task_id, text="diff --git a/x b/x\n"):
    from poc.kaggle_worker.backend import task_slug

    path = Path(state_dir) / "tasks" / task_slug(task_id) / "result.patch"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- patch_awaiting

def test_patch_awaiting_lists_completed_task_with_patch(tmp_path):
    write_patch(tmp_path, "task-1")
    items = scan(tmp_path, [task("task-1", "COMPLETED", NOW - 100)])
    assert [i["key"] for i in items] == ["patch:task-1"]
    item = items[0]
    assert item["kind"] == "patch_awaiting" and item["task_id"] == "task-1"
    assert item["link"] == "/tasks/task-1" and item["created_at"] == NOW - 100


def test_patch_awaiting_needs_the_patch_file(tmp_path):
    assert scan(tmp_path, [task("task-1", "COMPLETED", NOW - 100)]) == []


def test_patch_awaiting_ignores_non_completed_tasks(tmp_path):
    write_patch(tmp_path, "task-1")
    assert scan(tmp_path, [task("task-1", "LEASED", NOW - 100)]) == []


def test_patch_awaiting_gone_after_apply_or_commit(tmp_path):
    # Each case gets its own state dir: journal actions accumulate in actions.jsonl.
    for name, actions, expected in (
            ("apply", [{"tool": "apply_result", "task_id": "task-1", "ok": True}], []),
            ("commit", [{"tool": "commit_result", "task_id": "task-1", "ok": True}], []),
            ("failed-apply", [{"tool": "apply_result", "task_id": "task-1", "ok": False}],
             ["patch:task-1"]),
    ):
        state_dir = tmp_path / name
        write_patch(state_dir, "task-1")
        assert [i["key"] for i in scan(state_dir, [task("task-1", "COMPLETED", NOW - 100)],
                                       actions=actions)] == expected, name


# ---------------------------------------------------------------- lesson_pending

def test_lesson_pending_lists_memory_queue(tmp_path):
    entry = memory.propose_lesson(tmp_path, "/repo/demo", "task-9", "không dùng print để debug")
    items = scan(tmp_path, [])
    assert [i["key"] for i in items] == [f"lesson:{entry['id']}"]
    assert items[0]["kind"] == "lesson_pending" and items[0]["task_id"] == "task-9"
    assert items[0]["link"] == "/tasks/task-9"


# ---------------------------------------------------------------- quota_near

def ledger_with(worker, usd, ts=NOW):
    return SpendLedger([{"worker": worker, "usd": usd, "ts": ts}], clock=lambda: NOW)


def test_quota_near_at_80_but_not_79(tmp_path):
    caps = {"w1": {"daily_usd": 10.0, "monthly_usd": 100.0}}
    assert scan(tmp_path, [], ledger=ledger_with("w1", 7.9), caps=caps) == []
    items = scan(tmp_path, [], ledger=ledger_with("w1", 8.0), caps=caps)
    assert [i["key"] for i in items] == ["quota:w1:daily"]
    assert "80%" in items[0]["detail"] and items[0]["task_id"] is None


def test_quota_near_monthly_and_custom_threshold(tmp_path):
    caps = {"w1": {"daily_usd": 10.0, "monthly_usd": 100.0}}
    # 79 USD all today: the daily cap (790%) fires, the monthly one (79%) does not.
    items = scan(tmp_path, [], ledger=ledger_with("w1", 79.0), caps=caps)
    assert [i["key"] for i in items] == ["quota:w1:daily"]
    # Split across two days: 7 today (70% of daily) + 73 yesterday (month 80%) → monthly only.
    day_start = NOW - (NOW % 86400)
    ledger = SpendLedger([{"worker": "w1", "usd": 7.0, "ts": NOW},
                          {"worker": "w1", "usd": 73.0, "ts": day_start - 3600}], clock=lambda: NOW)
    items = scan(tmp_path, [], ledger=ledger, caps=caps)
    assert [i["key"] for i in items] == ["quota:w1:monthly"]
    # A higher threshold (from config) silences the same spend.
    assert scan(tmp_path, [], ledger=ledger_with("w1", 8.0), caps=caps, near_cap_pct=90) == []
    assert scan(tmp_path, [], ledger=ledger_with("w1", 9.0), caps=caps, near_cap_pct=90) != []


def test_quota_near_skipped_without_caps_or_ledger(tmp_path):
    assert scan(tmp_path, [], ledger=ledger_with("w1", 8.0), caps=None) == []
    assert scan(tmp_path, [], ledger=None, caps={"w1": {"daily_usd": 10.0}}) == []
    # A worker with no cap at all is skipped, not reported.
    assert scan(tmp_path, [], ledger=ledger_with("w1", 8.0), caps={"w1": {}}) == []


def test_near_cap_pct_must_be_in_range(tmp_path):
    for bad in (0, -1, 101, "80"):
        with pytest.raises(ValueError):
            scan(tmp_path, [], near_cap_pct=bad)


# ---------------------------------------------------------------- task_failed

def test_task_failed_within_24h_only(tmp_path):
    old = task("task-old", "FAILED", NOW - 25 * 3600, last_error="hết GPU")
    new = task("task-new", "FAILED", NOW - 3600, last_error="hết GPU")
    items = scan(tmp_path, [old, new])
    assert [i["key"] for i in items] == ["failed:task-new"]
    assert "hết GPU" in items[0]["detail"]


# ---------------------------------------------------------------- notify dedupe

def test_notify_sends_once_per_key_and_no_goal_text(tmp_path):
    write_patch(tmp_path, "task-1")
    items = scan(tmp_path, [task("task-1", "COMPLETED", NOW - 100, goal="BÍ MẬT: sửa hàm tính lương")])
    notifier = FakeNotifier()
    state_path = tmp_path / "needs_decision" / "notified.json"
    assert notify_new_items(notifier, items, state_path, clock=lambda: NOW) == items
    assert len(notifier.sent) == 1
    title, message = notifier.sent[0]
    assert title == "Kite: cần bạn quyết" and message.startswith("task-1\n")
    assert "BÍ MẬT" not in message and "lương" not in message
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"patch:task-1": NOW}
    # A second scan sends nothing for the same key.
    assert notify_new_items(notifier, items, state_path, clock=lambda: NOW) == []
    assert len(notifier.sent) == 1
    # A new key is still sent.
    write_patch(tmp_path, "task-2")
    more = scan(tmp_path, [task("task-1", "COMPLETED", NOW - 100),
                           task("task-2", "COMPLETED", NOW - 50)])
    assert [i["key"] for i in notify_new_items(notifier, more, state_path, clock=lambda: NOW)] \
        == ["patch:task-2"]


def test_notify_with_no_notifier_sends_nothing(tmp_path):
    write_patch(tmp_path, "task-1")
    items = scan(tmp_path, [task("task-1", "COMPLETED", NOW - 100)])
    assert notify_new_items(None, items, tmp_path / "needs_decision" / "notified.json") == []
    assert not (tmp_path / "needs_decision" / "notified.json").exists()


def test_notify_skips_a_key_whose_send_failed(tmp_path):
    class Failing:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("ntfy down")

    write_patch(tmp_path, "task-1")
    items = scan(tmp_path, [task("task-1", "COMPLETED", NOW - 100)])
    assert notify_new_items(Failing(), items, tmp_path / "needs_decision" / "notified.json") == []
