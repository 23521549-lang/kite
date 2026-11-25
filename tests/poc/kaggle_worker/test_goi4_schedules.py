"""Gói 4 (worker a): schedules — store, engine, router, OpenCode starter."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from poc.kaggle_worker.protocol import ScheduledTask
from poc.kaggle_worker.schedules import (
    BANGKOK_TZ, OpenCodeStarterError, ScheduleStore, SchedulerEngine,
    UrllibOpenCodeStarter, compute_next_run, daily_at_bangkok,
)
from poc.kaggle_worker.store import SQLiteSchedulerStore
from poc.kaggle_worker.web.schedules_api import create_router


def bangkok_ts(text: str) -> float:
    """Epoch for a 'YYYY-MM-DD HH:MM' wall time in Asia/Bangkok."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=BANGKOK_TZ).timestamp()


def bangkok_str(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, BANGKOK_TZ).strftime("%Y-%m-%d %H:%M")


# 25/11/2026 is a Wednesday (ISO weekday 3). Verified with `date -d`.
WEDNESDAY = "2026-11-25 10:00"


@pytest.fixture
def store(tmp_path):
    instance = ScheduleStore(tmp_path / "scheduler.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def chat_dir(tmp_path):
    target = tmp_path / "chat"
    target.mkdir()
    (target / "opencode.json").write_text("{}", encoding="utf-8")
    return target


class FakeStarter:
    def __init__(self):
        self.sessions: list[tuple[str, str, str]] = []
        self.prompts: list[tuple[str, str]] = []
        self.fail = False

    def create_session(self, directory: str, title: str) -> str:
        if self.fail:
            raise OpenCodeStarterError("opencode down")
        session_id = f"ses-{len(self.sessions) + 1}"
        self.sessions.append((directory, title, session_id))
        return session_id

    def send_prompt(self, session_id: str, text: str) -> None:
        if self.fail:
            raise OpenCodeStarterError("opencode down")
        self.prompts.append((session_id, text))


def force_due(store: ScheduleStore, schedule_id: str, *, next_run: float, last_run: float | None = None):
    """Test-only: move a schedule's next/last run (simulates time passing)."""
    with sqlite3.connect(store.path, timeout=10.0) as db:
        if last_run is None:
            db.execute("UPDATE schedules SET next_run=? WHERE id=?", (next_run, schedule_id))
        else:
            db.execute(
                "UPDATE schedules SET next_run=?, last_run=? WHERE id=?",
                (next_run, last_run, schedule_id),
            )


# ------------------------------------------------------------------ next_run math

def test_daily_crosses_bangkok_midnight():
    assert bangkok_str(compute_next_run("daily", "00:30", (), bangkok_ts("2026-11-25 23:30"))) == "2026-11-26 00:30"
    assert bangkok_str(compute_next_run("daily", "00:30", (), bangkok_ts("2026-11-25 00:15"))) == "2026-11-25 00:30"
    assert bangkok_str(compute_next_run("daily", "23:30", (), bangkok_ts("2026-11-25 23:30"))) == "2026-11-26 23:30"


def test_weekly_right_weekday():
    # Wednesday 10:00, run at 09:00 on Wednesdays -> next Wednesday.
    assert bangkok_str(compute_next_run("weekly", "09:00", (3,), bangkok_ts(WEDNESDAY))) == "2026-12-02 09:00"
    # Same week Thursday 09:00 is still ahead.
    assert bangkok_str(compute_next_run("weekly", "09:00", (4,), bangkok_ts(WEDNESDAY))) == "2026-11-26 09:00"
    # Wednesday 08:00, run at 09:00 today is still ahead.
    assert bangkok_str(compute_next_run("weekly", "09:00", (3,), bangkok_ts("2026-11-25 08:00"))) == "2026-11-25 09:00"


def test_once_future_and_past():
    assert bangkok_str(compute_next_run("once", "09:00", (), bangkok_ts("2026-11-25 08:00"))) == "2026-11-25 09:00"
    assert compute_next_run("once", "09:00", (), bangkok_ts("2026-11-25 10:00")) is None


def test_daily_at_bangkok_helper():
    due_fn = daily_at_bangkok("00:00")
    assert due_fn(datetime.fromtimestamp(bangkok_ts("2026-11-25 15:00"), BANGKOK_TZ)).strftime("%Y-%m-%d %H:%M") == "2026-11-26 00:00"
    assert due_fn(datetime.fromtimestamp(bangkok_ts("2026-11-25 00:00"), BANGKOK_TZ)).strftime("%Y-%m-%d %H:%M") == "2026-11-26 00:00"


# ------------------------------------------------------------------ store CRUD

def test_create_once_past_is_expired(store):
    schedule = store.create(plan_path="plans/b.md", kind="once", run_at="09:00",
                            now=bangkok_ts("2026-11-25 10:00"))
    assert schedule.status == "expired"
    assert schedule.next_run is None
    assert store.due(bangkok_ts("2026-11-25 10:00")) == []


def test_create_daily_and_crud(store):
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="00:30",
                            now=bangkok_ts("2026-11-25 23:30"))
    assert schedule.status == "enabled"
    assert bangkok_str(schedule.next_run) == "2026-11-26 00:30"
    assert store.get(schedule.id) == schedule
    assert [item.id for item in store.list()] == [schedule.id]
    assert store.due(bangkok_ts("2026-11-26 00:29")) == []
    assert [item.id for item in store.due(bangkok_ts("2026-11-26 00:30"))] == [schedule.id]
    updated = store.update(schedule.id, status="disabled", now=bangkok_ts("2026-11-26 00:30"))
    assert updated.status == "disabled"
    assert store.due(bangkok_ts("2026-11-26 00:30")) == []
    assert store.delete(schedule.id) is True
    assert store.get(schedule.id) is None
    assert store.delete(schedule.id) is False


def test_create_validation_errors(store):
    with pytest.raises(ValueError):
        store.create(plan_path="plans/a.md", kind="monthly", run_at="09:00", now=0)
    with pytest.raises(ValueError):
        store.create(plan_path="plans/a.md", kind="daily", run_at="9:00", now=0)
    with pytest.raises(ValueError):
        store.create(plan_path="plans/a.md", kind="daily", run_at="24:00", now=0)
    with pytest.raises(ValueError):
        store.create(plan_path="plans/a.md", kind="weekly", run_at="09:00", now=0)
    with pytest.raises(ValueError):
        store.create(plan_path="plans/a.md", kind="weekly", run_at="09:00",
                     weekdays=(8,), now=0)
    with pytest.raises(ValueError):
        store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                     weekdays=(3,), now=0)
    with pytest.raises(ValueError):
        store.create(plan_path="  ", kind="daily", run_at="09:00", now=0)


def test_update_rejects_bad_fields_and_expired_status(store):
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    with pytest.raises(ValueError):
        store.update(schedule.id, bogus="x")
    with pytest.raises(ValueError):
        store.update(schedule.id, status="expired")
    with pytest.raises(ValueError):
        store.update(schedule.id, status="enabled", run_at="25:00")


def test_update_reenable_recomputes_next_run(store):
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    store.update(schedule.id, status="disabled", now=bangkok_ts("2026-11-25 08:30"))
    updated = store.update(schedule.id, status="enabled", now=bangkok_ts("2026-11-25 10:00"))
    assert updated.status == "enabled"
    assert bangkok_str(updated.next_run) == "2026-11-26 09:00"


def test_update_timing_of_disabled_keeps_disabled(store):
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    store.update(schedule.id, status="disabled", now=bangkok_ts("2026-11-25 08:30"))
    updated = store.update(schedule.id, run_at="10:00", now=bangkok_ts("2026-11-25 08:30"))
    assert updated.status == "disabled"
    assert bangkok_str(updated.next_run) == "2026-11-25 10:00"


def test_update_expired_once_future_time_revives(store):
    schedule = store.create(plan_path="plans/b.md", kind="once", run_at="09:00",
                            now=bangkok_ts("2026-11-25 10:00"))
    assert schedule.status == "expired"
    renamed = store.update(schedule.id, plan_path="plans/c.md",
                           now=bangkok_ts("2026-11-25 10:05"))
    assert renamed.status == "expired" and renamed.plan_path == "plans/c.md"
    revived = store.update(schedule.id, run_at="15:00", now=bangkok_ts("2026-11-25 10:05"))
    assert revived.status == "enabled"
    assert bangkok_str(revived.next_run) == "2026-11-25 15:00"


def test_update_reenable_expired_once_is_rejected(store):
    schedule = store.create(plan_path="plans/b.md", kind="once", run_at="09:00",
                            now=bangkok_ts("2026-11-25 10:00"))
    assert schedule.status == "expired"
    with pytest.raises(ValueError):
        store.update(schedule.id, status="enabled", now=bangkok_ts("2026-11-25 10:05"))


def test_mark_run_once_expires_and_daily_advances(store):
    once = store.create(plan_path="plans/b.md", kind="once", run_at="09:00",
                        now=bangkok_ts("2026-11-25 08:00"))
    done = store.mark_run(once.id, now=bangkok_ts("2026-11-25 09:00"))
    assert done.status == "expired" and done.next_run is None
    assert done.last_run == bangkok_ts("2026-11-25 09:00")
    daily = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                         now=bangkok_ts("2026-11-25 08:00"))
    done = store.mark_run(daily.id, now=bangkok_ts("2026-11-25 09:00"))
    assert done.status == "enabled"
    assert bangkok_str(done.next_run) == "2026-11-26 09:00"


def test_active_tasks_since_uses_real_tasks_table(tmp_path):
    db_path = tmp_path / "scheduler.sqlite3"
    task_store = SQLiteSchedulerStore(db_path)
    task_store.enqueue(ScheduledTask(
        task_id="task-a", role="coder", model_id="model-a", repo_head="abc123",
        branch="agent/task-a", payload_json='{"goal":"fix"}',
        estimated_gpu_seconds=60, priority=1, created_at=bangkok_ts("2026-11-25 09:05"),
    ))
    task_store.close()
    schedules = ScheduleStore(db_path)
    found = schedules.active_tasks_since(bangkok_ts("2026-11-25 09:00"))
    assert [task["task_id"] for task in found] == ["task-a"]
    assert schedules.active_tasks_since(bangkok_ts("2026-11-25 09:06")) == []
    with sqlite3.connect(db_path, timeout=10.0) as db:
        db.execute("UPDATE tasks SET state='COMPLETED' WHERE task_id='task-a'")
    assert schedules.active_tasks_since(bangkok_ts("2026-11-25 09:00")) == []
    schedules.close()


def test_active_tasks_since_without_tasks_table(store):
    assert store.active_tasks_since(0) == []


# ------------------------------------------------------------------ engine

def _engine(store, chat_dir, starter=None, notes=None, **kwargs):
    collected: list[dict] = [] if notes is None else notes
    engine = SchedulerEngine(store, chat_dir=chat_dir,
                             starter=starter if starter is not None else FakeStarter(),
                             note=collected.append, **kwargs)
    return engine, collected


def test_trigger_plan_creates_session_and_prompts(store, chat_dir):
    starter = FakeStarter()
    engine, notes = _engine(store, chat_dir, starter)
    schedule = store.create(plan_path="plans/a.md", kind="once", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    summary = asyncio.run(engine.run_once(now=bangkok_ts("2026-11-25 09:00")))
    assert summary["ran"] == [{"schedule_id": schedule.id, "session_id": "ses-1"}]
    assert starter.sessions == [(str(chat_dir), "Kế hoạch: plans/a.md", "ses-1")]
    assert len(starter.prompts) == 1
    _, prompt = starter.prompts[0]
    assert "plans/a.md" in prompt and "start_task" in prompt
    done = store.get(schedule.id)
    assert done.status == "expired" and done.last_run == bangkok_ts("2026-11-25 09:00")
    assert notes[-1]["kind"] == "schedule_triggered"
    assert notes[-1]["opencode_session_id"] == "ses-1"


def test_overlap_skip_when_leased_task_exists(store, chat_dir, tmp_path):
    starter = FakeStarter()
    engine, notes = _engine(store, chat_dir, starter)
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    ran_at = bangkok_ts("2026-11-25 09:00")
    store.mark_run(schedule.id, now=ran_at)
    # The lead agent split the plan into a task that is still LEASED.
    task_store = SQLiteSchedulerStore(store.path)
    task_store.enqueue(ScheduledTask(
        task_id="task-a", role="coder", model_id="model-a", repo_head="abc123",
        branch="agent/task-a", payload_json='{"goal":"fix"}',
        estimated_gpu_seconds=60, priority=1, created_at=ran_at + 10,
    ))
    with sqlite3.connect(store.path, timeout=10.0) as db:
        db.execute("UPDATE tasks SET state='LEASED' WHERE task_id='task-a'")
    task_store.close()
    force_due(store, schedule.id, next_run=ran_at + 3600)
    summary = asyncio.run(engine.run_once(now=ran_at + 3600))
    assert summary["skipped"] and summary["ran"] == []
    assert starter.sessions == [] and starter.prompts == []
    assert notes[-1]["kind"] == "schedule_overlap_skipped"
    assert notes[-1]["task_ids"] == ["task-a"]
    assert store.get(schedule.id).last_run_task_ids == ("task-a",)


def test_overlap_skip_within_grace_period(store, chat_dir):
    starter = FakeStarter()
    engine, notes = _engine(store, chat_dir, starter)
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    ran_at = bangkok_ts("2026-11-25 09:00")
    store.mark_run(schedule.id, now=ran_at)
    force_due(store, schedule.id, next_run=ran_at + 5 * 60)
    summary = asyncio.run(engine.run_once(now=ran_at + 5 * 60))
    assert summary["skipped"] and starter.sessions == []
    assert "15 phút" in notes[-1]["reason"]


def test_tasks_all_done_runs_again(store, chat_dir):
    starter = FakeStarter()
    engine, _notes = _engine(store, chat_dir, starter)
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    ran_at = bangkok_ts("2026-11-25 09:00")
    store.mark_run(schedule.id, now=ran_at)
    task_store = SQLiteSchedulerStore(store.path)
    task_store.enqueue(ScheduledTask(
        task_id="task-a", role="coder", model_id="model-a", repo_head="abc123",
        branch="agent/task-a", payload_json='{"goal":"fix"}',
        estimated_gpu_seconds=60, priority=1, created_at=ran_at + 10,
    ))
    with sqlite3.connect(store.path, timeout=10.0) as db:
        db.execute("UPDATE tasks SET state='COMPLETED' WHERE task_id='task-a'")
    task_store.close()
    force_due(store, schedule.id, next_run=ran_at + 3600)
    summary = asyncio.run(engine.run_once(now=ran_at + 3600))
    assert len(summary["ran"]) == 1 and starter.sessions


def test_trigger_failure_leaves_schedule_due(store, chat_dir):
    starter = FakeStarter()
    starter.fail = True
    engine, notes = _engine(store, chat_dir, starter)
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    summary = asyncio.run(engine.run_once(now=bangkok_ts("2026-11-25 09:00")))
    assert summary["errors"] and summary["ran"] == []
    assert notes[-1]["kind"] == "schedule_trigger_failed"
    assert store.get(schedule.id).last_run is None  # not advanced: next tick retries
    assert [item.id for item in store.due(bangkok_ts("2026-11-25 09:01"))] == [schedule.id]


def test_job_error_does_not_kill_engine(store, chat_dir):
    starter = FakeStarter()
    engine, notes = _engine(store, chat_dir, starter)
    engine.register_builtin("boom", 60, lambda: 1 / 0)
    schedule = store.create(plan_path="plans/a.md", kind="daily", run_at="09:00",
                            now=bangkok_ts("2026-11-25 08:00"))
    first = asyncio.run(engine.run_once(now=bangkok_ts("2026-11-25 09:00")))
    assert first["ran"] and first["errors"] == []  # interval job only arms on first tick
    second = asyncio.run(engine.run_once(now=bangkok_ts("2026-11-25 09:01")))
    assert any(item.get("job") == "boom" for item in second["errors"])
    third = asyncio.run(engine.run_once(now=bangkok_ts("2026-11-25 09:02")))
    assert any(item.get("job") == "boom" for item in third["errors"])
    assert notes[-1]["kind"] == "builtin_job_failed"


def test_register_builtin_validation(store, chat_dir):
    engine, _notes = _engine(store, chat_dir)
    engine.register_builtin("scan", 300, lambda: None)
    with pytest.raises(ValueError):
        engine.register_builtin("scan", 300, lambda: None)
    with pytest.raises(ValueError):
        engine.register_builtin("bad", 0, lambda: None)
    with pytest.raises(ValueError):
        engine.register_builtin("bad", "soon", lambda: None)


def test_builtin_interval_and_daily_due_fn(store, chat_dir):
    calls: list[str] = []
    engine, _notes = _engine(store, chat_dir)
    engine.register_builtin("scan", 300, lambda: calls.append("scan"))
    engine.register_builtin("report", daily_at_bangkok("00:00"), lambda: calls.append("report"))
    asyncio.run(engine.run_once(now=bangkok_ts("2026-11-25 15:00")))
    assert calls == []  # armed, not due yet
    asyncio.run(engine.run_once(now=bangkok_ts("2026-11-25 15:05")))
    assert calls == ["scan"]  # interval due; daily report waits for 00:00
    asyncio.run(engine.run_once(now=bangkok_ts("2026-11-26 00:00")))
    assert calls == ["scan", "scan", "report"]


def test_run_forever_survives_bad_cycles(store, chat_dir):
    engine, notes = _engine(store, chat_dir, tick_seconds=0.05)
    engine.register_builtin("boom", 0.01, lambda: 1 / 0)

    async def _run():
        task = asyncio.create_task(engine.run_forever())
        await asyncio.sleep(0.3)
        engine.stop()
        await task

    asyncio.run(_run())
    failures = [note for note in notes if note.get("kind") == "builtin_job_failed"]
    assert len(failures) >= 2  # several cycles ran despite the failing job


# ------------------------------------------------------------------ real starter HTTP

def test_urllib_starter_posts_location_and_prompt(tmp_path):
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, payload):
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            seen.append({"path": self.path, "body": body,
                         "auth": self.headers.get("Authorization")})
            if self.path == "/api/session":
                self._reply({"id": "ses-xyz"})
            else:
                self._reply({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        starter = UrllibOpenCodeStarter(
            f"http://127.0.0.1:{server.server_address[1]}", "secret",
        )
        session_id = starter.create_session(str(tmp_path / "chat"), "Kế hoạch: plans/a.md")
        starter.send_prompt(session_id, "Đọc file plan `plans/a.md` và tách thành task qua `start_task`.")
    finally:
        server.shutdown()
        server.server_close()
    assert session_id == "ses-xyz"
    assert seen[0]["path"] == "/api/session"
    assert seen[0]["body"]["location"] == {"directory": str(tmp_path / "chat")}
    assert seen[0]["auth"] == "Basic b3BlbmNvZGU6c2VjcmV0"  # opencode:secret
    assert seen[1]["path"] == "/api/session/ses-xyz/prompt"
    assert "start_task" in seen[1]["body"]["text"]


# ------------------------------------------------------------------ router

class _FakeSession:
    user = type("User", (), {"username": "han"})()
    ip = "127.0.0.1"
    csrf = "csrf"


def _router_client(tmp_path, *, authed=True):
    store = ScheduleStore(tmp_path / "scheduler.sqlite3")
    audits: list[tuple] = []

    def fake_auth(request):
        if not authed:
            raise HTTPException(401, "cần đăng nhập")
        return _FakeSession()

    def fake_audit(session, action, target="", ok=True, detail=None, **kwargs):
        audits.append((action, target))

    app = FastAPI()

    @app.exception_handler(ValueError)
    async def _value_error(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": str(exc)}, status_code=400)

    app.include_router(create_router(
        type("Ctx", (), {"state_dir": tmp_path})(), fake_auth, fake_audit, store=store,
    ))
    client = TestClient(app, raise_server_exceptions=False)
    return client, store, audits


def test_router_crud(tmp_path):
    client, _store, audits = _router_client(tmp_path)
    created = client.post("/api/schedules", json={
        "plan_path": "plans/a.md", "kind": "daily", "run_at": "00:30",
    })
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]
    assert created.json()["status"] == "enabled"
    assert [item["id"] for item in client.get("/api/schedules").json()["schedules"]] == [schedule_id]
    assert client.get(f"/api/schedules/{schedule_id}").json()["plan_path"] == "plans/a.md"
    patched = client.patch(f"/api/schedules/{schedule_id}", json={"status": "disabled"})
    assert patched.status_code == 200 and patched.json()["status"] == "disabled"
    assert ("schedule_create", schedule_id) in audits
    assert ("schedule_update", schedule_id) in audits
    deleted = client.delete(f"/api/schedules/{schedule_id}")
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True, "id": schedule_id}
    assert client.get(f"/api/schedules/{schedule_id}").status_code == 404
    assert client.get("/api/schedules").json() == {"schedules": []}


def test_router_validation_errors(tmp_path):
    client, _store, _audits = _router_client(tmp_path)
    assert client.post("/api/schedules", json={"kind": "daily", "run_at": "09:00"}).status_code == 400
    assert client.post("/api/schedules", json={
        "plan_path": "p.md", "kind": "daily", "run_at": "9:00",
    }).status_code == 400
    created = client.post("/api/schedules", json={
        "plan_path": "p.md", "kind": "daily", "run_at": "09:00",
    })
    schedule_id = created.json()["id"]
    assert client.patch(f"/api/schedules/{schedule_id}", json={"bogus": 1}).status_code == 400
    assert client.patch(f"/api/schedules/{schedule_id}", json={"status": "expired"}).status_code == 400
    assert client.delete("/api/schedules/nope").status_code == 404


def test_router_requires_auth(tmp_path):
    client, _store, _audits = _router_client(tmp_path, authed=False)
    assert client.get("/api/schedules").status_code == 401
    assert client.post("/api/schedules", json={
        "plan_path": "p.md", "kind": "daily", "run_at": "09:00",
    }).status_code == 401
