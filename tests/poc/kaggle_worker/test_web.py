"""P3: the web back end. One owner account with password + TOTP, sessions and CSRF, every route behind
the session, the audit log, workers edited through config.yaml, tasks through KiteOps, the event feed."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")

from fastapi.routing import APIRoute, APIWebSocketRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker import cli  # noqa: E402
from poc.kaggle_worker.mcp_server import KiteToolError  # noqa: E402
from poc.kaggle_worker.protocol import ScheduledTask  # noqa: E402
from poc.kaggle_worker.store import SQLiteSchedulerStore  # noqa: E402
from poc.kaggle_worker.web import security as sec  # noqa: E402
from poc.kaggle_worker.web.app import GatewayDown, WebContext, create_app  # noqa: E402
from poc.kaggle_worker.web.db import Store, connect, sqlite_url  # noqa: E402

from tests.poc.kaggle_worker.test_launcher import raw_config  # noqa: E402

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
PASSWORD = "dai-va-kho-doan-123"
NOW = 1_795_400_000.0


class FakeOps:
    """KiteOps as the routes use it; records each call."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.allowed_roots = [Path("/home/han")]

    async def logged(self, tool, method, *args, **kwargs):
        self.calls.append(("logged", tool))
        return await method(*args, **kwargs)

    def resolve(self, task_id):
        if task_id == "missing":
            raise KiteToolError("không có task missing")
        return "chat-1"

    async def list_tasks(self, limit=10):
        return [{"task_id": "chat-1", "state": "LEASED", "task_type": "coding", "goal": "sửa lỗi", "created": "x"},
                {"task_id": "chat-0", "state": "COMPLETED", "task_type": "coding", "goal": "cũ", "created": "x"}]

    async def task_log(self, task_id="", limit=30):
        return {"task_id": self.resolve(task_id), "progress": ["xong chạy test"]}

    async def task_status(self, task_id=""):
        return {"task_id": self.resolve(task_id), "workflow": "COMPLETED",
                "environment_id": "env-1", "chat_session_id": "sess-1",
                "last_apply_error": {"code": "apply_conflict", "head": "abc1234", "failed_files": ["a.py"],
                                     "at": NOW, "task_id": "chat-1"}}

    async def rerun_task(self, task_id=""):
        if task_id == "missing":
            raise KiteToolError("không có task missing")
        self.calls.append(("rerun_task", task_id))
        return {"task_id": "chat-9"}

    async def dismiss_apply_error(self, task_id=""):
        if task_id == "missing":
            raise KiteToolError("không có task missing")
        self.calls.append(("dismiss_apply_error", task_id))
        return {"ok": True, "task_id": task_id}

    async def task_result(self, task_id=""):
        return {"task_id": "chat-1", "ready": True, "status": "READY_FOR_REVIEW"}

    async def start_task(self, repo, goal, task_type="coding", model=None, continue_from="", ignore_uncommitted=False):
        self.calls.append(("start_task", repo, goal))
        return {"task_id": "chat-2", "started": True}

    async def apply_result(self, task_id=""):
        self.calls.append(("apply_result", task_id))
        return {"task_id": "chat-1", "applied": True}

    async def commit_result(self, task_id, message):
        self.calls.append(("commit_result", task_id, message))
        return {"task_id": "chat-1", "committed": True}

    async def cancel_task(self, task_id):
        if task_id == "missing":
            raise KiteToolError("không có task missing")
        return {"task_id": task_id, "cancelled": True}

    async def pause_task(self, task_id):
        return {"task_id": task_id}

    async def resume_task(self, task_id):
        return {"task_id": task_id}

    async def send_instruction(self, task_id, instruction):
        self.calls.append(("send_instruction", task_id, instruction))
        return {"task_id": task_id}

    async def note_task(self, task_id, verdict, reason=""):
        return {"task_id": task_id, "verdict": verdict}

    async def memory_show(self, repo=""):
        return {"chat": ["1. trả lời bằng tiếng Việt"], "repo": repo}

    async def remember(self, text, repo=""):
        return {"saved": "chat"}

    async def memory_forget(self, number, repo=""):
        return {"removed": "x"}

    async def repo_config(self, repo, config=""):
        return {"repo": repo, "config": config}

    async def repo_status(self, repo):
        if "outside" in repo:
            raise KiteToolError(f"{repo} nằm ngoài các thư mục được phép")
        return {"repo": repo, "clean": True}

    async def doctor(self):
        return {"status": "ok"}

    async def update_kite(self, confirm=False):
        return {"behind": 0} if not confirm else {"started": True, "log": "/tmp/update.log"}

    async def cleanup(self, confirm=False, days=7):
        return {"removed": confirm, "freed": "1 GB" if confirm else None}

    async def bench_start(self, confirm=False, label="", suites="code", models="", repo_source="", polyglot="",
                          polyglot_count=0, worker="", network_hosts=None, network_ack=False,
                          bench_root=""):
        return {"started": confirm, "out": f"/bench/{label}"}


class FakeGateway:
    def __init__(self):
        self.calls: list[tuple] = []
        self.down = False

    async def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if self.down:
            raise GatewayDown("ConnectionRefused")
        if path == "/status":
            return {"workers": [{"worker_id": "kaggle-han", "alive": True, "busy": True, "running": 1, "slots": 2,
                                 "loaded_model": "qwen3.6", "version": "abc", "switch": {"on": True, "auto": True},
                                 "launch": {"state": "alive"}}],
                    "queued_jobs": 2, "running_jobs": 1}
        if path == "/kite/browser/token":
            return {"token": "t"}
        return {"ok": True, "path": path, "body": body}


@pytest.fixture
def site(tmp_path):
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("w" * 32 + "\n")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw))
    clock = {"now": NOW}
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")), clock=lambda: clock["now"])
    store.migrate()
    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    store.create_user("han", sec.hash_password(PASSWORD), sealer.seal(SECRET))
    ops, gateway = FakeOps(), FakeGateway()
    ctx = WebContext(config_path, store, sealer, ops_factory=lambda _config: ops, gateway=gateway,
                     clock=lambda: clock["now"], events_every=0.05)
    client = TestClient(create_app(ctx), base_url="https://testserver")
    return {"client": client, "ctx": ctx, "store": store, "ops": ops, "gateway": gateway, "clock": clock,
            "path": tmp_path, "config": config_path}


def code(clock, offset=0):
    return sec.totp_code(SECRET, int(clock["now"] // 30) + offset)


def login(site, *, client=None, password=PASSWORD, offset=0):
    client = client or site["client"]
    return client.post("/api/auth/login", json={"username": "han", "password": password,
                                                "code": code(site["clock"], offset)})


def signed_in(site):
    response = login(site)
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


# ---------------------------------------------------------------- security primitives
def test_totp_matches_rfc_6238_and_a_used_code_cannot_come_back():
    rfc = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"                       # RFC 6238 test key, SHA-1
    assert sec.totp_code(rfc, 59 // 30)[-6:] == "287082" and sec.totp_code(rfc, 1111111109 // 30) == "081804"
    counter = sec.verify_totp(SECRET, sec.totp_code(SECRET, 1000), now=1000 * 30 + 5)
    assert counter == 1000
    assert sec.verify_totp(SECRET, sec.totp_code(SECRET, 1000), now=1000 * 30 + 5, last_counter=1000) is None
    assert sec.verify_totp(SECRET, sec.totp_code(SECRET, 999), now=1000 * 30 + 5) == 999      # 30 s drift
    assert sec.verify_totp(SECRET, sec.totp_code(SECRET, 997), now=1000 * 30 + 5) is None
    assert sec.verify_totp(SECRET, "12345", now=0) is None
    assert sec.otpauth_uri(SECRET, "han").startswith("otpauth://totp/Kite:han?secret=")


def test_passwords_are_argon2_and_the_totp_secret_is_sealed(site):
    user = site["store"].user_by_name("han")
    assert user.password_hash.startswith("$argon2id$") and sec.verify_password(user.password_hash, PASSWORD)
    assert not sec.verify_password(user.password_hash, "wrong") and not sec.verify_password(None, PASSWORD)
    assert SECRET not in user.totp_secret and site["ctx"].sealer.unseal(user.totp_secret) == SECRET
    assert (site["path"] / "secrets" / "web_key").stat().st_mode & 0o777 == 0o600
    assert sec.password_problem("short") and sec.password_problem("aaaaaaaaaaaaaaaa")
    assert sec.password_problem(PASSWORD) is None


# ---------------------------------------------------------------- signing in
def test_every_api_route_needs_a_session(site):
    app, client = site["client"].app, site["client"]
    checked = 0
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/") and route.path not in ("/api/auth/login",
                                                                                               "/api/health"):
            path = route.path.replace("{path:path}", "session").replace("{", "").replace("}", "")
            for method in route.methods:
                response = client.request(method, path)
                assert response.status_code == 401, (method, path, response.status_code)
                checked += 1
        if isinstance(route, APIWebSocketRoute):
            with pytest.raises(Exception):
                with client.websocket_connect(route.path, headers={"origin": "https://testserver"}) as ws:
                    ws.receive_json()
    assert checked > 40
    assert client.get("/api/health").json() == {"ok": True}       # the container's health check, nothing else


def test_a_machine_without_an_account_serves_and_signing_in_says_how_to_make_one(site):
    from sqlalchemy import delete

    from poc.kaggle_worker.web.db import users

    with site["store"].engine.begin() as conn:
        conn.execute(delete(users))
    response = login(site)
    assert response.status_code == 409 and "kite-stack admin" in response.json()["error"]


def test_signing_in_needs_password_and_code_and_sets_a_strict_cookie(site):
    client = site["client"]
    wrong_password = login(site, password="khong-phai-mat-khau")
    wrong_code = client.post("/api/auth/login", json={"username": "han", "password": PASSWORD, "code": "000000"})
    nobody = client.post("/api/auth/login", json={"username": "nobody", "password": PASSWORD, "code": "000000"})
    assert {wrong_password.status_code, wrong_code.status_code, nobody.status_code} == {401}
    assert wrong_password.json() == wrong_code.json() == nobody.json()     # nothing tells which part was wrong
    ok = login(site)
    assert ok.status_code == 200 and ok.json()["username"] == "han"
    cookie = ok.headers["set-cookie"]
    assert cookie.startswith("__Host-kite=") and "HttpOnly" in cookie and "Secure" in cookie
    assert "SameSite=strict" in cookie and "Path=/" in cookie and "Domain" not in cookie
    assert client.get("/api/auth/me").json()["username"] == "han"
    replay = login(site, client=TestClient(client.app, base_url="https://testserver"))
    assert replay.status_code == 401                                        # that code was used
    actions = [(r["action"], r["ok"]) for r in site["store"].audit_rows()]
    assert ("login", True) in actions and ("login", False) in actions


def test_five_wrong_tries_lock_the_account_and_twenty_block_the_address(site):
    for _ in range(5):
        assert login(site, password="sai-mat-khau-roi").status_code == 401
    locked = login(site)
    assert locked.status_code == 429 and "khóa" in locked.json()["error"]
    site["clock"]["now"] += 901
    assert login(site).status_code == 200                                   # unlocked after 15 minutes
    site["clock"]["now"] += 60
    for _ in range(20):
        site["client"].post("/api/auth/login", json={"username": "ai-do", "password": "x", "code": "1"})
    assert login(site, offset=0).status_code == 429


def test_writes_need_the_csrf_token_and_a_same_site_origin(site):
    headers = signed_in(site)
    client = site["client"]
    assert client.post("/api/workers/kaggle-han/stop").status_code == 403
    assert client.post("/api/workers/kaggle-han/stop", headers={"X-CSRF-Token": "x"}).status_code == 403
    assert client.post("/api/workers/kaggle-han/stop",
                       headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    ok = client.post("/api/workers/kaggle-han/stop", headers={**headers, "Origin": "https://testserver"})
    assert ok.status_code == 200 and site["gateway"].calls[-1][1] == "/kite/stop"


def test_sessions_end_after_12_idle_hours_or_7_days_and_on_logout(site):
    headers = signed_in(site)
    client = site["client"]
    site["clock"]["now"] += 11 * 3600
    assert client.get("/api/auth/me").status_code == 200
    site["clock"]["now"] += 12 * 3600 + 1
    assert client.get("/api/auth/me").status_code == 401
    headers = signed_in(site)
    for _ in range(7):
        site["clock"]["now"] += 86400
        client.get("/api/auth/me")
    assert client.get("/api/auth/me").status_code == 401
    headers = signed_in(site)
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_changing_the_password_needs_both_factors_and_signs_other_browsers_out(site):
    headers = signed_in(site)
    other = TestClient(site["client"].app, base_url="https://testserver")
    site["clock"]["now"] += 30
    assert login(site, client=other).status_code == 200
    site["clock"]["now"] += 30
    bad = site["client"].post("/api/auth/password", headers=headers,
                              json={"current": "sai", "new": "mat-khau-moi-rat-dai", "code": code(site["clock"])})
    assert bad.status_code == 403
    weak = site["client"].post("/api/auth/password", headers=headers,
                               json={"current": PASSWORD, "new": "ngan", "code": code(site["clock"])})
    assert weak.status_code == 400
    site["clock"]["now"] += 30
    ok = site["client"].post("/api/auth/password", headers=headers,
                             json={"current": PASSWORD, "new": "mat-khau-moi-rat-dai", "code": code(site["clock"])})
    assert ok.status_code == 200 and ok.json()["other_sessions_closed"] == 1
    assert other.get("/api/auth/me").status_code == 401 and site["client"].get("/api/auth/me").status_code == 200
    site["clock"]["now"] += 30
    assert login(site, client=other, password="mat-khau-moi-rat-dai").status_code == 200


def test_a_new_2fa_key_counts_only_after_its_first_code(site):
    headers = signed_in(site)
    site["clock"]["now"] += 30
    begin = site["client"].post("/api/auth/totp/begin", headers=headers,
                                json={"password": PASSWORD, "code": code(site["clock"])})
    new_secret = begin.json()["secret"]
    assert begin.status_code == 200 and new_secret != SECRET
    assert site["client"].post("/api/auth/totp/confirm", headers=headers, json={"code": "000000"}).status_code == 400
    confirm = site["client"].post("/api/auth/totp/confirm", headers=headers,
                                  json={"code": sec.totp_now(new_secret, site["clock"]["now"])})
    assert confirm.status_code == 200
    assert site["ctx"].sealer.unseal(site["store"].user_by_name("han").totp_secret) == new_secret


def test_answers_carry_strict_headers_and_there_is_no_api_explorer(site):
    page = site["client"].get("/")
    assert page.status_code == 200 and "Đăng nhập" in page.text and "<script src=\"/kite-mini.js\">" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert "swagger" not in site["client"].get(path).text.lower()
    assert site["client"].get("/api/nothing").status_code == 401             # unknown API routes are closed too
    signed_in(site)
    assert site["client"].get("/api/nothing").status_code == 404


# ---------------------------------------------------------------- workers
def test_the_worker_list_merges_config_gateway_and_spend(site):
    signed_in(site)
    (site["path"] / "journal").mkdir()
    (site["path"] / "journal" / "spend.jsonl").write_text(json.dumps(
        {"ts": NOW, "at": "2026-11-23T10:00:00", "worker": "kaggle-han", "usd": 0.5}) + "\n")
    rows = site["client"].get("/api/workers").json()["workers"]
    assert rows[0]["id"] == "kaggle-han" and rows[0]["type"] == "kaggle" and rows[0]["alive"] and rows[0]["busy"]
    assert rows[0]["kaggle"]["kernel"] == "hanle/kite-worker" and rows[0]["kaggle"]["auto_click"] is False
    overview = site["client"].get("/api/overview").json()
    assert overview["queued_jobs"] == 2 and overview["tasks"]["counts"] == {"LEASED": 1, "COMPLETED": 1}
    site["gateway"].down = True
    assert site["client"].get("/api/workers").json()["gateway_up"] is False


def test_adding_an_api_worker_keeps_its_key_in_a_private_file_and_out_of_answers(site):
    headers = signed_in(site)
    client = site["client"]
    bad = client.post("/api/workers", headers=headers, json={"type": "openai", "id": "Bad Id", "models": ["gpt"],
                                                             "api_key": "sk-never-written"})
    assert bad.status_code == 400 and not list((site["path"] / "secrets").glob("api_key_*"))
    added = client.post("/api/workers", headers=headers, json={
        "type": "openai", "id": "openrouter", "base_url": "https://openrouter.ai/api/v1",
        "models": ["gpt=openai/gpt-5-mini"], "api_key": "sk-very-secret", "price_in": 0.25, "price_out": 2,
        "daily_cap": 1})
    assert added.status_code == 200 and added.json()["restart_needed"]
    key = site["path"] / "secrets" / "api_key_openrouter"
    from poc.kaggle_worker.web import workers as wk

    raw_key = key.read_text()
    assert raw_key.startswith(wk.SEALED_PREFIX) and "sk-very-secret" not in raw_key   # sealed at rest
    assert wk.read_secret(key, site["ctx"].sealer) == "sk-very-secret"
    assert key.stat().st_mode & 0o777 == 0o600
    raw = json.loads(site["config"].read_text())
    relay = raw["providers"][-1]["relay"]
    assert relay["upstream"] == {"gpt": "openai/gpt-5-mini"} and relay["caps"] == {"daily_usd": 1}
    rows = client.get("/api/workers").json()["workers"]
    assert any(r["id"] == "openrouter" and r["type"] == "openai" for r in rows)
    edited = client.patch("/api/workers/openrouter", headers=headers, json={"daily_cap": 3, "api_key": "sk-new"})
    assert edited.status_code == 200 and wk.read_secret(key, site["ctx"].sealer) == "sk-new"
    assert client.patch("/api/workers/openrouter", headers=headers, json={"owner": "x"}).status_code == 400
    removed = client.delete("/api/workers/openrouter", headers=headers)
    assert removed.status_code == 200 and not key.exists()
    everything = json.dumps(site["store"].audit_rows()) + json.dumps([added.json(), edited.json(), removed.json()])
    assert "sk-very-secret" not in everything and "sk-new" not in everything
    assert [r["action"] for r in site["store"].audit_rows()][:4] == ["worker_remove", "worker_update",
                                                                    "worker_add", "worker_add"]


def test_a_new_kaggle_worker_gets_a_token_to_paste_into_kaggle_secrets(site):
    headers = signed_in(site)
    added = site["client"].post("/api/workers", headers=headers, json={
        "type": "kaggle", "id": "kaggle-an", "owner": "an", "kernel": "an/kite-worker",
        "kaggle_api_token": "KGAT_an", "via": "browser"})
    assert added.status_code == 200, added.text
    token = added.json()["worker_token"]
    from poc.kaggle_worker.web import workers as wk

    token_file = site["path"] / "secrets" / "worker_token_kaggle-an"
    assert token not in token_file.read_text()                       # sealed at rest, like every web-written key
    assert wk.read_secret(token_file, site["ctx"].sealer) == token
    provider = json.loads(site["config"].read_text())["providers"][-1]
    assert provider["launch"] == {"kernel": "an/kite-worker", "idle_minutes": 30, "token": "per-launch",
                                  "via": "browser"}
    assert provider["model"] == "qwen3.6"                                    # copied from the existing worker
    assert token not in json.dumps(site["store"].audit_rows())


def test_start_and_stop_go_to_the_gateway_by_kind(site):
    headers = signed_in(site)
    client = site["client"]
    client.post("/api/workers", headers=headers, json={"type": "openai", "id": "groq", "models": ["llama"],
                                                       "base_url": "https://api.groq.com/openai/v1", "api_key": "k"})
    client.post("/api/workers/groq/stop", headers=headers)
    client.post("/api/workers/groq/start", headers=headers)
    client.post("/api/workers/kaggle-han/start", headers=headers, json={"model": "qwen3.6"})
    client.post("/api/workers/kaggle-han/auto", headers=headers, json={"on": False})
    calls = [(path, body) for _m, path, body in site["gateway"].calls]
    assert ("/kite/switch", {"worker": "groq", "on": False}) in calls
    assert ("/kite/switch", {"worker": "groq", "on": True}) in calls
    assert ("/kite/wake", {"worker": "kaggle-han", "model": "qwen3.6", "reason": "web", "force": False}) in calls
    assert ("/kite/switch", {"worker": "kaggle-han", "auto": False}) in calls
    assert client.post("/api/workers/nobody/start", headers=headers).status_code == 404
    on = client.post("/api/workers/kaggle-han/auto-click", headers=headers, json={"on": True})
    assert on.json()["auto_click"] is True
    assert client.post("/api/workers/groq/auto-click", headers=headers, json={"on": True}).status_code == 400
    assert {"worker_start", "worker_stop", "worker_autostart", "kaggle_auto_click"} <= {
        r["action"] for r in site["store"].audit_rows()}


# ---------------------------------------------------------------- tasks, repos, journal, system
def test_task_routes_call_kite_and_the_sensitive_ones_are_audited(site):
    headers = signed_in(site)
    client = site["client"]
    assert client.get("/api/tasks").json()["tasks"][0]["task_id"] == "chat-1"
    detail = client.get("/api/tasks/chat-1").json()
    assert detail["status"]["workflow"] == "COMPLETED" and detail["result"]["ready"]
    started = client.post("/api/tasks", headers=headers, json={"repo": "/home/han/app", "goal": "sửa test"})
    assert started.json()["task_id"] == "chat-2"
    client.post("/api/tasks/chat-1/apply", headers=headers)
    client.post("/api/tasks/chat-1/commit", headers=headers, json={"message": "Fix the parser"})
    client.post("/api/tasks/chat-1/instruct", headers=headers, json={"text": "thêm test"})
    missing = client.post("/api/tasks/missing/cancel", headers=headers)
    assert missing.status_code == 400 and "missing" in missing.json()["error"]
    assert ("commit_result", "chat-1", "Fix the parser") in site["ops"].calls
    audited = [(r["action"], r["ok"]) for r in site["store"].audit_rows()]
    assert ("task_start", True) in audited and ("apply_result", True) in audited
    assert ("commit_result", True) in audited and ("cancel_task", False) in audited


def test_task_detail_returns_trace_and_persistent_apply_error(site):
    headers = signed_in(site)
    detail = site["client"].get("/api/tasks/chat-1", headers=headers).json()
    assert detail["environment_id"] == "env-1" and detail["chat_session_id"] == "sess-1"
    assert detail["last_apply_error"] == {"code": "apply_conflict", "head": "abc1234",
                                          "failed_files": ["a.py"], "at": NOW, "task_id": "chat-1"}


def test_task_detail_parses_the_stored_apply_error_string(site):
    # Regression (F1): the scheduler store keeps last_apply_error as a JSON string;
    # the detail endpoint used to hand that raw string over, so the front end's
    # `.code === 'apply_conflict'` gate never matched and the rerun button never showed.
    # The previous test stayed green because FakeOps has no scheduler store, so the
    # row overwrite never ran there.
    db_path = site["path"] / "scheduler.sqlite3"
    store = SQLiteSchedulerStore(db_path)
    store.enqueue(ScheduledTask(task_id="chat-1", role="coding", model_id=None, repo_head="abc1234",
                                branch="main", payload_json='{"goal": "sửa lỗi"}',
                                estimated_gpu_seconds=1, priority=0, created_at=NOW,
                                chat_session_id="sess-1", environment_id="env-1",
                                main_repo="/home/han/app"))
    store.set_last_apply_error("chat-1", {"code": "apply_conflict", "head": "abc1234",
                                          "failed_files": ["a.py"], "at": NOW, "task_id": "chat-1"})
    store.close()
    site["ops"]._store = lambda: SQLiteSchedulerStore(db_path)
    headers = signed_in(site)
    detail = site["client"].get("/api/tasks/chat-1", headers=headers).json()
    assert detail["last_apply_error"] == {"code": "apply_conflict", "head": "abc1234",
                                          "failed_files": ["a.py"], "at": NOW, "task_id": "chat-1"}
    assert detail["environment_id"] == "env-1" and detail["chat_session_id"] == "sess-1"
    # A corrupt stored string degrades to None instead of leaking raw text to the UI.
    con = sqlite3.connect(db_path)
    con.execute("UPDATE tasks SET last_apply_error=? WHERE task_id=?", ("not-json{", "chat-1"))
    con.commit()
    con.close()
    assert site["client"].get("/api/tasks/chat-1", headers=headers).json()["last_apply_error"] is None


def test_rerun_returns_the_new_task_id_and_is_audited(site):
    headers = signed_in(site)
    client = site["client"]
    rerun = client.post("/api/tasks/chat-1/rerun", headers=headers)
    assert rerun.json() == {"task_id": "chat-9"}
    assert ("rerun_task", "chat-1") in site["ops"].calls
    audited = [(r["action"], r["ok"]) for r in site["store"].audit_rows()]
    assert ("rerun_task", True) in audited
    missing = client.post("/api/tasks/missing/rerun", headers=headers)
    assert missing.status_code == 400 and "missing" in missing.json()["error"]
    assert ("rerun_task", False) in [(r["action"], r["ok"]) for r in site["store"].audit_rows()]


def test_rerun_tolerates_the_fallback_shape(site):
    # _continue_patch lỗi thì rerun_task (worker (a)) tự rơi về task mới từ đầu; endpoint
    # chỉ cần trả task_id mới, không quan tâm task mới có phải bản tiếp nối hay không.
    async def fallback(task_id=""):
        return {"task_id": "chat-10", "fallback": True,
                "note": "Lưu ý: patch cũ không gộp được lên HEAD mới nên chạy lại từ đầu."}

    site["ops"].rerun_task = fallback
    headers = signed_in(site)
    assert site["client"].post("/api/tasks/chat-1/rerun", headers=headers).json() == {"task_id": "chat-10"}


def test_dismiss_apply_error_calls_ops_and_is_audited(site):
    headers = signed_in(site)
    client = site["client"]
    dismissed = client.post("/api/tasks/chat-1/apply-error/dismiss", headers=headers)
    assert dismissed.json() == {"ok": True, "task_id": "chat-1"}
    assert ("dismiss_apply_error", "chat-1") in site["ops"].calls
    audited = [(r["action"], r["ok"]) for r in site["store"].audit_rows()]
    assert ("dismiss_apply_error", True) in audited
    missing = client.post("/api/tasks/missing/apply-error/dismiss", headers=headers)
    assert missing.status_code == 400 and "missing" in missing.json()["error"]


def test_structured_tool_error_carries_code_head_and_failed_files(site):
    # ApplyConflictError (worker (a)) mang 3 thuộc tính cấu trúc; lỗi thường thì không.
    async def conflict(task_id=""):
        err = KiteToolError("patch không áp được (repo đã đổi so với lúc chạy task?)")
        err.code = "apply_conflict"
        err.head = "abc1234"
        err.failed_files = ["a.py", "b/c.py"]
        raise err

    site["ops"].apply_result = conflict
    headers = signed_in(site)
    response = site["client"].post("/api/tasks/chat-1/apply", headers=headers)
    assert response.status_code == 400
    assert response.json() == {"error": "patch không áp được (repo đã đổi so với lúc chạy task?)",
                               "code": "apply_conflict", "head": "abc1234",
                               "failed_files": ["a.py", "b/c.py"]}
    plain = site["client"].post("/api/tasks/missing/cancel", headers=headers)
    assert plain.status_code == 400 and set(plain.json()) == {"error"}


def test_repos_must_be_git_repositories_inside_the_allowed_folders(site):
    headers = signed_in(site)
    client = site["client"]
    assert client.post("/api/repos", headers=headers,
                       json={"name": "x", "path": "/outside/x"}).status_code == 400
    assert client.post("/api/repos", headers=headers, json={"name": "bad name", "path": "/home/han/a"}).status_code == 400
    added = client.post("/api/repos", headers=headers, json={"name": "app", "path": "/home/han/app",
                                                             "url": "https://github.com/han/app"})
    assert added.status_code == 200
    listed = client.get("/api/repos").json()
    assert listed["repos"][0]["name"] == "app" and listed["memory"]["chat"]
    assert client.delete(f"/api/repos/{added.json()['id']}", headers=headers).status_code == 200


def test_journal_routes_read_the_journal_files(site):
    signed_in(site)
    journal = site["path"] / "journal"
    journal.mkdir()
    lines = [{"ts": NOW - 100, "at": "2026-11-23T10:00:00", "tag": "chat", "outcome": "ok", "run_s": 3,
              "prompt_tokens": 100, "completion_tokens": 20, "worker": "kaggle-han", "model": "qwen3.6"},
             {"ts": NOW - 50, "at": "2026-11-23T10:01:00", "tag": "chat-1", "outcome": "error", "run_s": 1,
              "worker": "kaggle-han", "model": "qwen3.6"}]
    (journal / "gateway.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    events = site["client"].get("/api/journal/events?file=gateway&limit=10").json()["records"]
    assert [e["outcome"] for e in events] == ["error", "ok"]                # newest first
    assert site["client"].get("/api/journal/events?file=gateway&tag=chat-1").json()["records"][0]["tag"] == "chat-1"
    assert site["client"].get("/api/journal/events?file=secrets").status_code == 400
    day = site["client"].get("/api/journal/daily?days=7").json()["days"][0]
    assert day == {"day": "2026-11-23", "requests": 2, "errors": 1, "prompt_tokens": 100, "completion_tokens": 20,
                   "gpu_seconds": 4.0, "tasks": 0, "tasks_ok": 0, "usd": 0.0}
    assert "gateway" in site["client"].get("/api/journal/summary?days=7").json()


def test_system_routes_restart_only_kite_services_and_not_under_a_running_task(site):
    headers = signed_in(site)
    client = site["client"]
    restarted = []

    async def restart(services):
        restarted.append(services)
        return {"ok": True}

    site["ctx"].restart = restart
    assert client.post("/api/system/restart", headers=headers, json={"services": ["sshd"]}).status_code == 400
    busy = client.post("/api/system/restart", headers=headers, json={"services": ["kite-gateway"]})
    assert busy.status_code == 409 and "chat-1" in busy.json()["error"]
    forced = client.post("/api/system/restart", headers=headers, json={"services": ["kite-gateway"], "force": True})
    assert forced.json()["ok"] and restarted == [["kite-gateway"]]
    assert client.post("/api/system/update", headers=headers).json()["started"]
    assert client.get("/api/system/doctor").json()["status"] == "ok"
    assert client.post("/api/bench/run", headers=headers, json={"label": "v7"}).json()["started"]
    rows = client.get("/api/audit").json()["rows"]
    assert {"restart", "update", "bench_start"} <= {r["action"] for r in rows}


# ---------------------------------------------------------------- live events and the chat proxy
def test_the_event_feed_needs_a_session_and_a_same_site_origin(site):
    signed_in(site)
    client = site["client"]
    cookie = f"__Host-kite={client.cookies.get('__Host-kite')}"
    with pytest.raises(Exception):
        with client.websocket_connect("/api/ws", headers={"origin": "https://evil.example", "cookie": cookie}) as ws:
            ws.receive_json()
    journal = site["path"] / "journal"
    journal.mkdir()
    (journal / "launches.jsonl").write_text("")
    from poc.kaggle_worker.backend import task_slug

    trace = site["path"] / "tasks" / task_slug("chat-1")
    trace.mkdir(parents=True)
    (trace / "trace.jsonl").write_text(json.dumps({"ts": NOW, "event": "step_start", "step": "test"}) + "\n")
    # The test client opens ws:// and so leaves the Secure cookie out; a browser on https (wss) sends it.
    cookie = f"__Host-kite={client.cookies.get('__Host-kite')}"
    with client.websocket_connect("/api/ws", headers={"origin": "https://testserver", "cookie": cookie}) as ws:
        first = ws.receive_json()
        assert first["t"] == "status" and first["gateway_up"]
        tasks = ws.receive_json()
        assert tasks["t"] == "tasks" and tasks["tasks"][0]["step"] == "test" and tasks["tasks"][0]["state"] == "LEASED"
        with (journal / "launches.jsonl").open("a") as stream:
            stream.write(json.dumps({"event": "alive", "worker": "kaggle-han"}) + "\n")
        ws.send_text("ping")
        event = ws.receive_json()
        assert event == {"t": "event", "file": "launches", "record": {"event": "alive", "worker": "kaggle-han"}}


class OpenCodeStub(BaseHTTPRequestHandler):
    """OpenCode 2's server, as far as the Chat tab uses it."""
    seen: list = []

    def _answer(self, status, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        self.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        return body

    def do_GET(self):
        self._record()
        if self.path == "/api/info":
            return self._answer(200, {"version": "2.4.0", "urls": ["http://127.0.0.1"]})
        self._answer(200, {"data": [{"id": "ses_1", "time": {"created": 1, "updated": 5, "busy": 5, "idle": 4}},
                                    {"id": "ses_0", "time": {"created": 1, "updated": 3, "busy": 2, "idle": 3}},
                                    {"id": "ses_9", "parentID": "ses_1", "time": {"created": 1, "updated": 5, "busy": 5}}]})

    def do_POST(self):
        body = self._record()
        self._answer(200, {"data": {"id": "ses_2", "echo": json.loads(body or "{}")}})

    def do_PATCH(self):
        self.do_POST()

    def do_DELETE(self):
        self._record()
        self._answer(204)

    def log_message(self, *args):
        pass


@pytest.fixture
def opencode(site, tmp_path):
    OpenCodeStub.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenCodeStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chat = tmp_path / "kite-chat"
    chat.mkdir()
    (chat / "opencode.json").write_text("{}")
    site["ctx"].chat_dir = chat
    site["ctx"].opencode_locator = lambda: (f"http://127.0.0.1:{server.server_port}", "oc-pass")
    yield {"seen": OpenCodeStub.seen, "chat": chat}
    server.shutdown()


def test_the_chat_goes_to_the_opencode_server_behind_the_session(site, opencode):
    client = site["client"]
    assert client.get("/api/opencode/api/session").status_code == 401           # no session: nothing reaches it
    headers = signed_in(site)
    listed = client.get("/api/opencode/api/session?directory=/home/han&limit=5")
    assert listed.status_code == 200 and [item["id"] for item in listed.json()["data"]] == ["ses_1", "ses_0", "ses_9"]
    assert client.post("/api/opencode/api/session/ses_1/prompt", json={"text": "x"}).status_code == 403   # no CSRF
    sent = client.post("/api/opencode/api/session/ses_1/prompt", headers=headers, json={"text": "chào"})
    assert sent.status_code == 200
    method, path, authorization, body = opencode["seen"][0]
    assert path in ("/api/session?directory=%2Fhome%2Fhan&limit=5", "/api/session?directory=/home/han&limit=5")
    assert authorization.startswith("Basic ")
    assert opencode["seen"][1][:2] == ("POST", "/api/session/ses_1/prompt") and json.loads(opencode["seen"][1][3]) == {"text": "chào"}
    assert any(r["action"] == "chat_message" for r in site["store"].audit_rows())
    info = client.get("/api/chat/info").json()
    assert info["ready"] and info["opencode"] == {"reachable": True, "version": "2.4.0", "error": None}
    assert info["busy"] == 1                       # ses_1 works now; ses_0 is idle; ses_9 is a sub-agent
    assert opencode["seen"][-1][1].startswith("/api/session?directory=")


def test_the_web_reaches_only_the_chat_routes_of_opencode_and_only_with_chat_fields(site, opencode):
    client, headers = site["client"], signed_in(site)
    # Its shell, terminal, files, credentials and session moves are not the Chat tab's.
    for method, path in (("POST", "api/session/ses_1/shell"), ("GET", "api/pty"), ("POST", "api/pty"),
                         ("GET", "api/fs/read/etc/passwd"), ("POST", "api/experimental/fs/write"),
                         ("GET", "api/credential"), ("POST", "api/session/ses_1/move"), ("GET", "doc"),
                         ("GET", "session"), ("POST", "api/session/ses_1/prompt_async")):
        assert client.request(method, f"/api/opencode/{path}", headers=headers, json={}).status_code == 403, path
    # A prompt is text: no host files attached, no permission rules slipped in.
    assert client.post("/api/opencode/api/session/ses_1/prompt", headers=headers,
                       json={"text": "x", "files": [{"path": "/etc/shadow"}]}).status_code == 400
    assert client.post("/api/opencode/api/session", headers=headers,
                       json={"permissions": [{"action": "*", "effect": "allow"}]}).status_code == 400
    assert client.patch("/api/opencode/api/session/ses_1", headers=headers,
                        json={"permissions": []}).status_code == 400
    # "Always allow" outlives the chat folder's rules: once or reject only.
    assert client.post("/api/opencode/api/session/ses_1/permission/per_1/reply", headers=headers,
                       json={"decision": "always"}).status_code == 400
    assert client.post("/api/opencode/api/session/ses_1/permission/per_1/reply", headers=headers,
                       json={"decision": "once"}).status_code == 200
    assert not any("shell" in s[1] or "pty" in s[1] or "/fs/" in s[1] for s in opencode["seen"])
    # A new session is always in Kite's chat folder, whatever the browser says.
    made = client.post("/api/opencode/api/session", headers=headers, json={})
    assert made.json()["data"]["echo"] == {"location": {"directory": str(opencode["chat"])}}
    renamed = client.patch("/api/opencode/api/session/ses_1", headers=headers, json={"title": "Sửa parser"})
    assert renamed.json()["data"]["echo"] == {"title": "Sửa parser"}
    assert client.delete("/api/opencode/api/session/ses_1", headers=headers).status_code == 204
    assert client.post("/api/opencode/api/session/ses_1/interrupt", headers=headers).status_code == 200
    answered = client.post("/api/opencode/api/session/ses_1/form/frm_1/reply", headers=headers,
                           json={"answer": {"q0": "kite"}})
    assert answered.json()["data"]["echo"] == {"answer": {"q0": "kite"}}
    (opencode["chat"] / "opencode.json").unlink()
    assert client.post("/api/opencode/api/session", headers=headers, json={}).status_code == 409


def test_the_opencode_service_is_found_where_opencode_2_writes_it(tmp_path):
    from poc.kaggle_worker.web.app import find_opencode

    home = tmp_path
    assert find_opencode(home, "http://127.0.0.1:4096", "from-secret") == ("http://127.0.0.1:4096", "from-secret")
    state = home / ".local" / "state" / "opencode"
    state.mkdir(parents=True)
    (state / "service.json").write_text(json.dumps({"url": "http://127.0.0.1:49374", "password": "p1"}))
    assert find_opencode(home, "http://127.0.0.1:4096", None) == ("http://127.0.0.1:49374", "p1")
    (state / "service.json").write_text(json.dumps({"url": "http://127.0.0.1:49374/"}))
    config = home / ".config" / "opencode"
    config.mkdir(parents=True)
    (config / "service.json").write_text(json.dumps({"password": "p2"}))
    assert find_opencode(home, "http://127.0.0.1:4096", None) == ("http://127.0.0.1:49374", "p2")
    # Only a loopback address: a file pointing elsewhere is not followed.
    (state / "service.json").write_text(json.dumps({"url": "http://10.0.0.9:49374", "password": "p1"}))
    assert find_opencode(home, "http://127.0.0.1:4096", "s") == ("http://127.0.0.1:4096", "s")
    (state / "service.json").write_text("not json")
    assert find_opencode(home, "http://127.0.0.1:4096", "s") == ("http://127.0.0.1:4096", "s")


def test_the_built_page_is_served_with_cache_rules_and_a_fallback_for_every_tab(site, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "index-abc.js").write_text("console.log(1)")
    (dist / ".kite-source").write_text("hash")
    client = site["client"]
    assert "Kite" in client.get("/").text                     # not built yet: the small login page
    site["ctx"].dist = dist
    page = client.get("/chat/ses_1")
    assert page.status_code == 200 and "id=root" in page.text and page.headers["cache-control"] == "no-cache"
    assert "script-src 'self'" in page.headers["content-security-policy"]
    asset = client.get("/assets/index-abc.js")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/assets/missing.js").status_code == 404
    assert "id=root" in client.get("/.kite-source").text      # dot files are never served
    assert client.get("/../config.json").status_code in (200, 404) and "providers" not in client.get("/../config.json").text


# ---------------------------------------------------------------- kite web admin
def test_kite_web_admin_creates_the_account_once_with_a_confirmed_2fa(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw_config(tmp_path)))
    monkeypatch.setattr(sec, "new_totp_secret", lambda: SECRET)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    monkeypatch.setattr("builtins.input", lambda prompt="": sec.totp_now(SECRET))
    assert cli.main(["--config", str(path), "web", "admin", "--username", "han"]) == 0
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")))
    user = store.user_by_name("han")
    assert user is not None and sec.verify_password(user.password_hash, PASSWORD)
    assert SECRET in capsys.readouterr().err                                  # shown once, on this machine
    assert cli.main(["--config", str(path), "web", "admin"]) != 0              # a second account: refused
    monkeypatch.setattr("builtins.input", lambda prompt="": "000000")
    assert cli.main(["--config", str(path), "web", "admin", "--reset"]) != 0   # wrong code: nothing changes
    assert store.user_by_name("han").password_hash == user.password_hash


# ---------------------------------------------------------------- the same tables on Postgres
@pytest.mark.skipif(not (shutil.which("initdb") or Path("/usr/lib/postgresql/16/bin/initdb").exists()),
                    reason="no Postgres binaries on this machine")
def test_the_tables_work_on_postgres(tmp_path):
    pytest.importorskip("psycopg")
    bin_dir = Path(shutil.which("initdb")).parent if shutil.which("initdb") else Path("/usr/lib/postgresql/16/bin")
    data, sock = tmp_path / "pg", tmp_path / "sock"
    sock.mkdir()
    user = {} if os.geteuid() else {"user": "nobody"}
    if user:
        pytest.skip("initdb refuses to run as root")
    subprocess.run([str(bin_dir / "initdb"), "-D", str(data), "-U", "kite", "--auth=trust"], check=True,
                   capture_output=True)
    port = 55432
    subprocess.run([str(bin_dir / "pg_ctl"), "-D", str(data), "-o", f"-p {port} -k {sock} -c listen_addresses=''",
                    "-w", "start"], check=True, capture_output=True)
    try:
        subprocess.run([str(bin_dir / "createdb"), "-h", str(sock), "-p", str(port), "-U", "kite", "kite_web"],
                       check=True, capture_output=True)
        store = Store(connect(f"postgresql+psycopg://kite@/kite_web?host={sock}&port={port}"))
        assert store.migrate() == 1 and store.migrate() == 1
        uid = store.create_user("han", "h", "s")
        store.add_session("sid", uid, "csrf", "1.2.3.4", "ua")
        store.attempt("1.2.3.4", "han", False)
        store.audit("han", "1.2.3.4", "login", "han", True, {"x": 1})
        store.set_setting("theme", {"dark": True})
        store.set_setting("theme", {"dark": False})
        assert store.session("sid").csrf == "csrf" and store.failures_from("1.2.3.4", 60) == 1
        assert store.audit_rows()[0]["detail"] == {"x": 1} and store.setting("theme") == {"dark": False}
        store.engine.dispose()
    finally:
        subprocess.run([str(bin_dir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)


class ModelsStub(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.headers.get("Authorization") == "Bearer sk-good"
        body = json.dumps({"data": [{"id": "gpt-up"}]} if ok else {"error": "bad key"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_testing_a_key_lists_the_apis_models_and_saves_nothing(site):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        headers = signed_in(site)
        base = f"http://127.0.0.1:{server.server_port}/v1"
        good = site["client"].post("/api/providers/test", headers=headers,
                                   json={"type": "openai", "base_url": base, "api_key": "sk-good"}).json()
        bad = site["client"].post("/api/providers/test", headers=headers,
                                  json={"type": "openai", "base_url": base, "api_key": "sk-bad"}).json()
    finally:
        server.shutdown()
    assert good["ok"] and good["models"] == ["gpt-up"]
    assert not bad["ok"] and "401" in bad["detail"]
    assert not list((site["path"] / "secrets").glob("api_key_*"))
    types = site["client"].get("/api/provider-types").json()["types"]
    assert [t["type"] for t in types] == ["kaggle", "openai", "anthropic", "runpod"]


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._text = payload if isinstance(payload, str) else json.dumps(payload)

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """aiohttp-shaped stand-in for test_connection: canned answers keyed by (method, url suffix)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _respond(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (verb, suffix), response in self.routes.items():
            if verb == method and url.endswith(suffix):
                return response
        return _FakeResponse(404, {"error": "no route"})

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)


def _probe(kind, routes, **body):
    from poc.kaggle_worker.web import workers as wk

    session = _FakeSession(routes)
    body.setdefault("api_key", "sk-good")
    return asyncio.run(wk.test_connection(body, session_factory=lambda: session)), session


_LISTED = _FakeResponse(200, {"data": [{"id": "m1"}, {"id": "m2"}]})


def test_a_connection_test_now_calls_the_model_and_sees_usage():
    result, session = _probe("openai", {("GET", "/models"): _LISTED,
                                        ("POST", "/chat/completions"): _FakeResponse(
                                            200, {"usage": {"prompt_tokens": 1, "completion_tokens": 1}})},
                             type="openai", base_url="https://api.example/v1", models=["m1"])
    assert result["ok"] and result["model_tested"] == "m1" and result["model_ok"]
    assert result["usage_reported"] is True
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    post = next(call for call in session.calls if call[0] == "POST")
    assert post[2]["json"]["model"] == "m1" and post[2]["json"]["max_tokens"] == 1


def test_a_connection_test_warns_when_the_api_reports_no_usage():
    result, _ = _probe("openai", {("GET", "/models"): _LISTED,
                                  ("POST", "/chat/completions"): _FakeResponse(200, {"choices": []})},
                       type="openai", base_url="https://api.example/v1", models=["m1"])
    assert result["ok"] and result["model_ok"] and result["usage_reported"] is False


def test_a_failed_chat_call_keeps_the_successful_listing():
    result, _ = _probe("openai", {("GET", "/models"): _LISTED,
                                  ("POST", "/chat/completions"): _FakeResponse(500, {"error": "boom"})},
                       type="openai", base_url="https://api.example/v1", models=["m1"])
    assert result["ok"] and result["models"] == ["m1", "m2"]
    assert result["model_ok"] is False and "500" in result["chat_detail"]


def test_a_connection_test_uses_the_first_listed_model_and_the_apis_own_id_for_an_alias():
    routes = {("GET", "/models"): _LISTED,
              ("POST", "/chat/completions"): _FakeResponse(200, {"usage": {"total_tokens": 2}})}
    listed, _ = _probe("openai", routes, type="openai", base_url="https://api.example/v1")
    assert listed["model_tested"] == "m1"
    aliased, session = _probe("openai", routes, type="openai", base_url="https://api.example/v1",
                              models=["chat=m2"])
    assert aliased["model_tested"] == "m2"
    assert next(call for call in session.calls if call[0] == "POST")[2]["json"]["model"] == "m2"


def test_a_connection_test_probes_anthropic_with_a_messages_call():
    result, session = _probe("anthropic", {("GET", "/v1/models"): _LISTED,
                                           ("POST", "/v1/messages"): _FakeResponse(
                                               200, {"usage": {"input_tokens": 1, "output_tokens": 1}})},
                             type="anthropic", models=["claude-test"])
    assert result["ok"] and result["model_tested"] == "claude-test" and result["model_ok"]
    assert result["usage_reported"] is True and result["latency_ms"] >= 0
    post = next(call for call in session.calls if call[0] == "POST")
    assert post[2]["json"]["max_tokens"] == 1 and post[2]["headers"]["x-api-key"] == "sk-good"


def test_the_other_tabs_answer(site):
    headers = signed_in(site)
    client = site["client"]
    journal = site["path"] / "journal"
    journal.mkdir()
    (journal / "spend.jsonl").write_text(
        json.dumps({"ts": NOW, "at": "2026-11-23T09:00:00", "worker": "openrouter", "usd": 0.25}) + "\n")
    assert client.get("/api/spend").json()["days"] == [{"day": "2026-11-23", "workers": {"openrouter": 0.25},
                                                        "usd": 0.25}]
    assert client.get("/api/repos/memory").json()["chat"]
    assert client.post("/api/repos/memory", headers=headers, json={"text": "luôn viết test"}).json()["saved"] == "chat"
    assert client.put("/api/repos/config", headers=headers,
                      json={"repo": "/home/han/app", "config": "test: pytest -q"}).status_code == 200
    assert client.get("/api/system/cleanup").json()["removed"] is False
    assert client.post("/api/system/cleanup", headers=headers, json={"days": 3}).json()["freed"] == "1 GB"
    settings = client.get("/api/settings").json()
    assert settings["gateway"]["port"] == 8787 and settings["version"]["commit"]
    assert client.post("/api/settings/ntfy/test", headers=headers).status_code == 400    # not set up
    assert "runs" in client.get("/api/bench/history").json()
    site["gateway"].down = True
    cookie = f"__Host-kite={client.cookies.get('__Host-kite')}"
    with pytest.raises(Exception):
        with client.websocket_connect("/api/browser/ws", headers={"origin": "https://testserver",
                                                                  "cookie": cookie}) as ws:
            ws.receive_text()
    assert {"memory_add", "repo_config", "cleanup"} <= {r["action"] for r in site["store"].audit_rows()}


# ---------------------------------------------------------------- phase 6: re-auth, recovery, sealing
def test_reauth_guesses_lock_the_account_just_like_signin_guesses(site):
    headers = signed_in(site)
    client = site["client"]
    for _ in range(5):
        site["clock"]["now"] += 30
        bad = client.post("/api/auth/password", headers=headers,
                          json={"current": "sai-mat-khau", "new": "mat-khau-moi-rat-dai",
                                "code": code(site["clock"])})
        assert bad.status_code == 403
    # The account is locked now: re-auth refuses outright and even a correct sign-in is refused.
    site["clock"]["now"] += 30
    assert client.post("/api/auth/password", headers=headers,
                       json={"current": PASSWORD, "new": "mat-khau-moi-rat-dai",
                             "code": code(site["clock"])}).status_code == 429
    assert login(site, client=TestClient(client.app, base_url="https://testserver")).status_code == 429
    assert "lockout" in [r["action"] for r in site["store"].audit_rows()]
    site["clock"]["now"] += 901
    assert login(site, client=TestClient(client.app, base_url="https://testserver")).status_code == 200


def test_a_lock_that_expires_starts_the_failure_count_from_zero(site):
    for _ in range(5):
        assert login(site, password="sai-mat-khau-roi").status_code == 401
    assert login(site).status_code == 429
    site["clock"]["now"] += 901
    assert login(site, password="sai-mat-khau-roi").status_code == 401    # one wrong try…
    assert login(site).status_code == 200                                # …does not re-lock at once


def test_a_recovery_code_signs_in_once_and_only_once(site):
    codes = sec.new_recovery_codes()
    user = site["store"].user_by_name("han")
    site["store"].replace_recovery_codes(user.id, [sec.recovery_digest(c) for c in codes])
    client = site["client"]
    ok = client.post("/api/auth/login", json={"username": "han", "password": PASSWORD, "code": codes[0]})
    assert ok.status_code == 200
    assert site["store"].recovery_codes_left(user.id) == 7
    assert "login_recovery" in [r["action"] for r in site["store"].audit_rows()]
    again = TestClient(client.app, base_url="https://testserver").post(
        "/api/auth/login", json={"username": "han", "password": PASSWORD, "code": codes[0]})
    assert again.status_code == 401                                       # spent: never a second time
    sloppy = TestClient(client.app, base_url="https://testserver").post(
        "/api/auth/login", json={"username": "han", "password": PASSWORD, "code": codes[1].lower()})
    assert sloppy.status_code == 200                                      # dashes and case do not matter
    wrong_password = TestClient(client.app, base_url="https://testserver").post(
        "/api/auth/login", json={"username": "han", "password": "sai-mat-khau", "code": codes[2]})
    assert wrong_password.status_code == 401
    assert site["store"].recovery_codes_left(user.id) == 6                # a wrong password spends nothing


def test_setting_up_2fa_again_hands_out_fresh_recovery_codes(site):
    headers = signed_in(site)
    site["clock"]["now"] += 30
    begin = site["client"].post("/api/auth/totp/begin", headers=headers,
                                json={"password": PASSWORD, "code": code(site["clock"])})
    new_secret = begin.json()["secret"]
    confirm = site["client"].post("/api/auth/totp/confirm", headers=headers,
                                  json={"code": sec.totp_now(new_secret, site["clock"]["now"])})
    assert confirm.status_code == 200 and len(confirm.json()["recovery_codes"]) == 8
    fresh = TestClient(site["client"].app, base_url="https://testserver")
    ok = fresh.post("/api/auth/login", json={"username": "han", "password": PASSWORD,
                                             "code": confirm.json()["recovery_codes"][0]})
    assert ok.status_code == 200


def test_a_session_that_moves_browser_must_reauth_before_writing(site):
    headers = signed_in(site)
    client = site["client"]
    moved = {**headers, "User-Agent": "Mozilla/5.0 (some other browser)"}
    assert client.get("/api/auth/me", headers=moved).status_code == 200          # reads keep working
    blocked = client.post("/api/workers/kaggle-han/stop", headers=moved)
    assert blocked.status_code == 403 and "2FA" in blocked.json()["error"]
    site["clock"]["now"] += 30
    reauth = client.post("/api/auth/totp/begin", headers=moved,
                         json={"password": PASSWORD, "code": code(site["clock"])})
    assert reauth.status_code == 200                                             # re-auth adopts the browser
    assert client.post("/api/workers/kaggle-han/stop", headers=moved).status_code == 200


def test_secret_files_seal_and_legacy_files_migrate_on_read(tmp_path):
    from poc.kaggle_worker.web import workers as wk

    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    path = tmp_path / "secrets" / "api_key_x"
    wk.write_secret(path, "sk-live", sealer=sealer)
    assert path.read_text().startswith(wk.SEALED_PREFIX) and "sk-live" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert wk.read_secret(path, sealer) == "sk-live"
    with pytest.raises(Exception):
        wk.read_secret(path)                                                     # sealed needs its key
    legacy = tmp_path / "secrets" / "api_key_old"
    wk.write_secret(legacy, "sk-plain")                                          # the CLI's plaintext form
    assert wk.read_secret(legacy, sealer) == "sk-plain"
    assert legacy.read_text().startswith(wk.SEALED_PREFIX)                       # migrated by that read
    assert wk.read_secret(legacy, sealer) == "sk-plain"


def test_insecure_cookies_are_refused_off_loopback(tmp_path):
    from poc.kaggle_worker.config import ConfigError

    with pytest.raises(ConfigError):
        cli._refuse_insecure_cookies("0.0.0.0", True)
    cli._refuse_insecure_cookies("127.0.0.1", True)
    cli._refuse_insecure_cookies("0.0.0.0", False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw_config(tmp_path)))
    assert cli.main(["--config", str(path), "web", "serve", "--host", "0.0.0.0",
                     "--insecure-cookies"]) != 0


def test_web_key_export_round_trips_and_a_swapped_key_is_caught(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw_config(tmp_path)))
    monkeypatch.setattr(sec, "new_totp_secret", lambda: SECRET)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    monkeypatch.setattr("builtins.input", lambda prompt="": sec.totp_now(SECRET))
    assert cli.main(["--config", str(path), "web", "admin", "--username", "han"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(path), "web", "admin", "--export-key"]) == 0
    blob = capsys.readouterr().err.splitlines()[-1].strip()
    key_file_text = (tmp_path / "secrets" / "web_key").read_text().strip()
    assert sec.unseal_with_passphrase(blob, PASSWORD) == key_file_text
    with pytest.raises(ValueError):
        sec.unseal_with_passphrase(blob, "mat-khau-sai-hoan-toan")
    # The key file vanishes (a restore, a wiped folder): a fresh key is silently minted by the Sealer,
    # and the canary check is what turns that into a loud refusal instead of a dead 2FA.
    (tmp_path / "secrets" / "web_key").unlink()
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")))
    from poc.kaggle_worker.config import ConfigError

    with pytest.raises(ConfigError):
        cli._check_sealer_canary(store, sec.Sealer(tmp_path / "secrets" / "web_key"))
    assert cli.main(["--config", str(path), "web", "serve"]) != 0
