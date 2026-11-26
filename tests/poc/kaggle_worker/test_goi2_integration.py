"""Gói 2 (agent trưởng — tích hợp): the three lead edits on top of the workers' code.

1. ``KiteOps.list_tasks`` carries the four origin stamps (chat_session_id,
   environment_id, main_repo, last_apply_error) — the list view now matches
   the detail view (worker (a) flagged the gap).
2. A Default session's first task on an unregistered repo auto-registers the
   repo as an environment (the task needs a real env to be stamped with);
   the owner can rename it or add a PAT later in the Environments page.
   A locked repo-env session's first task on a different, unregistered repo
   still refuses: a locked session never changes repo.
3. ``env_cost_usd`` (web) counts only task-attributed journal spend.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from poc.kaggle_worker.mcp_server import KiteToolError
from tests.poc.kaggle_worker.test_goi2_copies import (
    FakeClient,
    db_rows,
    make_ops,
    make_repo,
    seed_contract_schema,
    seed_session,
)


def seed_web_repo(state_dir: Path, repo: Path, env_id: int, name: str = "repo") -> None:
    """Insert one row into the web store's ``repos`` table (id chosen by the test)."""
    db = sqlite3.connect(state_dir / "web.sqlite3")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS repos (id INTEGER PRIMARY KEY, "
                   "name VARCHAR(100) UNIQUE NOT NULL, path TEXT NOT NULL, "
                   "url TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)")
        db.execute("INSERT INTO repos (id, name, path, url, created_at) VALUES (?, ?, ?, '', ?)",
                   (env_id, name, str(repo), time.time()))
        db.commit()
    finally:
        db.close()


def web_repo_rows(state_dir: Path) -> list[dict]:
    return db_rows(state_dir / "web.sqlite3", "SELECT * FROM repos")


# ------------------------------------------------------------------ list_tasks stamps

def test_list_tasks_carries_origin_stamps(tmp_path):
    repo, _head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "sess-m", environment_id="default")
    client = FakeClient()
    ops = make_ops(tmp_path, client, session_id="sess-m")
    started = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))
    listed = asyncio.run(ops.list_tasks())
    assert len(listed) == 1
    row = listed[0]
    assert row["task_id"] == started["task_id"]
    assert row["chat_session_id"] == "sess-m"
    assert row["environment_id"] == started["environment_id"]
    assert row["main_repo"] == str(repo)
    assert row["last_apply_error"] is None


# ------------------------------------------------------------------ auto-register / strict lock

def test_default_first_task_auto_registers_unregistered_repo(tmp_path):
    repo, _head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "sess-m", environment_id="default")
    client = FakeClient()
    ops = make_ops(tmp_path, client, session_id="sess-m")
    started = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))
    env_id = started["environment_id"]
    assert env_id.isdigit() and env_id != "default"
    rows = web_repo_rows(tmp_path / "state")
    assert len(rows) == 1
    assert rows[0]["id"] == int(env_id) and rows[0]["path"] == str(repo)
    moves = db_rows(db_path, "SELECT * FROM chat_session_moves WHERE session_id='sess-m'")
    assert len(moves) == 1 and moves[0]["to_env"] == env_id and moves[0]["trigger"] == "task"
    assert db_rows(db_path, "SELECT environment_id FROM chat_sessions "
                            "WHERE kite_session_id='sess-m'")[0]["environment_id"] == env_id
    # A second session on the same repo reuses the row: no duplicate environment.
    seed_session(db_path, "sess-n", environment_id="default")
    ops_n = make_ops(tmp_path, client, session_id="sess-n")
    started_n = asyncio.run(ops_n.start_task(str(repo), "Sửa calc.py"))
    assert started_n["environment_id"] == env_id
    assert len(web_repo_rows(tmp_path / "state")) == 1


def test_locked_session_first_task_refuses_unregistered_repo(tmp_path):
    repo, _head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_session(db_path, "sess-locked", environment_id="42")
    client = FakeClient()
    ops = make_ops(tmp_path, client, session_id="sess-locked")
    with pytest.raises(KiteToolError, match="chưa đăng ký thành môi trường"):
        asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))
    assert client.started == []


def test_locked_session_first_task_proceeds_on_its_registered_repo(tmp_path):
    repo, _head = make_repo(tmp_path)
    db_path = tmp_path / "state" / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    seed_web_repo(tmp_path / "state", repo, env_id=42)
    seed_session(db_path, "sess-locked", environment_id="42")
    client = FakeClient()
    ops = make_ops(tmp_path, client, session_id="sess-locked")
    started = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))
    assert started["environment_id"] == "42"
    assert started["main_repo"] == str(repo)


# ------------------------------------------------------------------ cost per environment

def env_cost_usd(*args, **kwargs):
    """The web back end's helper, imported on use: the tests above need no web extra."""
    pytest.importorskip("fastapi")
    from poc.kaggle_worker.web.app import env_cost_usd as real

    return real(*args, **kwargs)


def test_env_cost_usd_counts_only_task_spend():
    tasks = [{"task_id": "t1", "environment_id": "7"},
             {"task_id": "t2", "environment_id": "9"}]
    journal = SimpleNamespace(read=lambda name: [
        {"tag": "t1", "usd": 0.02},
        {"tag": "t1-b2", "usd": 0.01},          # branch retry of t1
        {"tag": "t2", "usd": 1.0},
        {"tag": "chat", "usd": 5.0},            # chat spend: no task link
        {"tag": "bench-x", "usd": 3.0},         # bench spend: no task link
        {"kind": "pod", "usd": 2.0},            # pod spend: no tag
        {"tag": "t-unknown", "usd": 9.0},       # task that no longer exists
        {"tag": "t1"},                          # spend record without usd
    ])
    assert env_cost_usd(journal, tasks, "7") == 0.03
    assert env_cost_usd(journal, tasks, "9") == 1.0
    assert env_cost_usd(journal, tasks, "missing") == 0.0


def test_env_cost_usd_unreadable_journal_is_unknown_not_zero():
    class BrokenJournal:
        def read(self, name):
            raise OSError("journal gone")

    assert env_cost_usd(BrokenJournal(), [], "7") is None


def test_env_cost_usd_month_only_counts_current_utc_month():
    # Fixed "now": 2026-11-23 02:13 UTC (the same NOW as the Gói 2 session
    # tests). The ledger's month is a UTC month (relay.py _month), so the
    # filter uses gmtime, not local time.
    now = 1_795_400_000.0
    this_month = now - 86400        # 2026-11-22
    last_month = now - 40 * 86400   # 2026-10-14
    tasks = [{"task_id": "t1", "environment_id": "7"}]
    journal = SimpleNamespace(read=lambda name: [
        {"tag": "t1", "usd": 0.02, "ts": this_month},
        {"tag": "t1-b2", "usd": 0.01, "ts": this_month},   # branch retry, this month
        {"tag": "t1", "usd": 1.0, "ts": last_month},       # last month: all-time only
        {"tag": "t1", "usd": 9.0},                         # no ts: no provable month
    ])
    assert env_cost_usd(journal, tasks, "7") == 10.03
    assert env_cost_usd(journal, tasks, "7", month_only=True, now=now) == 0.03
    assert env_cost_usd(journal, tasks, "7", month_only=True, now=last_month) == 1.0

    class BrokenJournal:
        def read(self, name):
            raise OSError("journal gone")

    assert env_cost_usd(BrokenJournal(), [], "7", month_only=True, now=now) is None
