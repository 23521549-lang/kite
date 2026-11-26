"""Secretary (Gói 4, worker (b)): daily report builder, Bangkok-day grouping, report store,
ntfy summary discipline, secretary folder setup."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from poc.kaggle_worker import secretary as sec
from poc.kaggle_worker.notify import Notifier

DAY = date(2026, 11, 25)


def _ts(year, month, day, hour, minute=0):
    """Epoch of a Bangkok wall time."""
    return datetime(year, month, day, hour, minute, tzinfo=sec.BANGKOK).timestamp()


def test_spend_2330_utc_falls_on_next_bangkok_day():
    # 23:30 UTC 24/11 = 06:30 Bangkok 25/11: it MUST land on the Bangkok day after,
    # never on the UTC day (SpendLedger would bucket it under 24/11).
    utc_2330 = datetime(2026, 11, 24, 23, 30, tzinfo=__import__("zoneinfo").ZoneInfo("UTC")).timestamp()
    assert sec.bangkok_date_of(utc_2330) == date(2026, 11, 25)
    records = [{"worker": "api", "usd": 1.5, "ts": utc_2330}]
    assert sec.spend_for_bangkok_day(records, date(2026, 11, 24)) == (0.0, {})
    assert sec.spend_for_bangkok_day(records, date(2026, 11, 25)) == (1.5, {"api": 1.5})


def test_spend_bangkok_day_bounds_and_malformed_rows_skipped():
    start, end = sec.bangkok_day_bounds(DAY)
    assert end - start == 86400.0
    records = [{"worker": "api", "usd": 2.0, "ts": start},            # 00:00: in
               {"worker": "api", "usd": 3.0, "ts": end},               # next 00:00: out
               {"worker": "pod", "usd": 0.5, "ts": start + 3600},
               {"worker": "api", "usd": "x", "ts": start},             # malformed: skipped
               {"worker": "api", "usd": 9.0, "ts": "soon"},            # malformed: skipped
               {"usd": 9.0, "ts": start}]                             # no worker: skipped
    assert sec.spend_for_bangkok_day(records, DAY) == (2.5, {"api": 2.0, "pod": 0.5})


def test_classify_task_runs_latest_wins_and_one_entry_per_task():
    runs = [{"task_id": "t1", "status": "error", "ts": _ts(2026, 11, 25, 10)},
            {"task_id": "t1", "status": "ok", "ts": _ts(2026, 11, 25, 11)},   # retried, then done
            {"task_id": "t2", "status": "error", "ts": _ts(2026, 11, 25, 12)},
            {"task_id": "t3", "status": "ok", "ts": _ts(2026, 11, 24, 23)},  # previous Bangkok day
            {"task_id": "", "status": "ok", "ts": _ts(2026, 11, 25, 9)},     # no id: skipped
            {"task_id": "t4", "status": "ok", "ts": "later"}]                 # bad ts: skipped
    done, failed = sec.classify_task_runs(runs, DAY)
    assert [r["task_id"] for r in done] == ["t1"]
    assert [r["task_id"] for r in failed] == ["t2"]


def test_build_daily_report_content():
    data = sec.collect_daily_data(
        day=DAY,
        runs=[{"task_id": "t1", "status": "ok", "ts": _ts(2026, 11, 25, 10)},
              {"task_id": "t2", "status": "error", "reason": "tests_failed",
               "ts": _ts(2026, 11, 25, 11)}],
        spend_records=[{"worker": "api", "usd": 1.25, "ts": _ts(2026, 11, 25, 9)}],
        needs_decision_items=[{"kind": "patch_awaiting", "title": "Patch chờ duyệt",
                               "task_id": "t1"}])
    text = sec.build_daily_report(data, day=DAY)
    assert "# Báo cáo ngày 25/11/2026" in text
    assert "Xong: 1" in text and "t1 (xong)" in text
    assert "Hỏng: 1" in text and "t2 (hỏng: tests_failed)" in text
    assert "Tổng chi ngày: $1.2500" in text and "api: $1.2500" in text
    assert "patch_awaiting: 1" in text and "Patch chờ duyệt (task t1)" in text


def test_build_daily_report_empty_day_and_scan_unavailable():
    data = sec.collect_daily_data(day=DAY, needs_decision_available=False)
    text = sec.build_daily_report(data, day=DAY)
    assert "Xong: 0" in text and "Hỏng: 0" in text
    assert "(không có khoản chi nào trong ngày)" in text
    assert "chưa quét được mục cần quyết" in text


def test_save_list_load_report_roundtrip(tmp_path):
    path = sec.save_report(tmp_path, DAY, "# Báo cáo ngày 25/11/2026\n\nNội dung.\n")
    assert path == tmp_path / "secretary" / "reports" / "2026-11-25.md"
    assert path.read_text(encoding="utf-8").startswith("# Báo cáo ngày")
    assert not list(tmp_path.rglob("*.tmp"))  # atomic write leaves no .tmp behind
    assert sec.list_reports(tmp_path) == [{"date": "2026-11-25", "title": "Báo cáo ngày 25/11/2026"}]
    assert sec.load_report(tmp_path, "2026-11-25").startswith("# Báo cáo ngày")
    assert sec.load_report(tmp_path, "2026-11-26") is None
    # Bad dates / traversal attempts never read outside the reports folder.
    assert sec.load_report(tmp_path, "../secret") is None
    assert sec.load_report(tmp_path, "2026-02-30") is None
    assert sec.parse_report_date("25/11/2026") is None
    assert sec.list_reports(tmp_path / "nope") == []


def test_daily_report_message_has_numbers_only_no_code_or_goal():
    data = sec.collect_daily_data(
        day=DAY,
        runs=[{"task_id": "task-abc123", "status": "ok", "ts": _ts(2026, 11, 25, 10)}],
        spend_records=[{"worker": "api", "usd": 2.5, "ts": _ts(2026, 11, 25, 9)}],
        needs_decision_items=[{"kind": "patch_awaiting", "title": "Patch chờ duyệt",
                               "task_id": "task-abc123"}])
    title, message, tags, priority = sec.daily_report_message(DAY, data)
    assert title == "Kite: báo cáo ngày 25/11/2026"
    assert "Task xong: 1, hỏng: 0" in message
    assert "Chi ngày: $2.50" in message
    assert "Patch chờ duyệt/cảnh báo: 1" in message
    # The discipline: no code, no goal text — only counts, ids and amounts.
    for forbidden in ("def ", "import ", "```", "goal", "TODO", "patch diff"):
        assert forbidden not in message
    assert "task-abc123" not in message  # not even the id: counts only


def test_run_daily_report_wires_everything(tmp_path):
    sent = []

    def fake_sender(url, title, message, **kwargs):
        sent.append((title, message))

    notifier = Notifier("https://ntfy.sh/kite-test", sender=fake_sender, background=False)
    posted = []
    result = sec.run_daily_report(
        tmp_path, day=DAY,
        runs=[{"task_id": "t1", "status": "ok", "ts": _ts(2026, 11, 25, 10)}],
        spend_records=[{"worker": "api", "usd": 0.75, "ts": _ts(2026, 11, 25, 9)}],
        scan_fn=lambda: [{"kind": "task_failed", "title": "Task hỏng", "task_id": "t9"}],
        notifier=notifier,
        post_to_session=lambda folder, markdown: posted.append(folder) or True)
    assert result["date"] == "2026-11-25"
    assert Path(result["path"]).is_file()
    assert result["data"]["spend_total_usd"] == 0.75
    assert result["notified"] is True and len(sent) == 1
    assert sent[0][0] == "Kite: báo cáo ngày 25/11/2026"
    assert result["posted"] is True and posted == [tmp_path / "secretary"]
    assert result["needs_decision_available"] is True


def test_run_daily_report_without_scan_or_notifier_still_saves(tmp_path):
    result = sec.run_daily_report(tmp_path, day="2026-11-25", runs=[], spend_records=[])
    assert result["notified"] is False and result["posted"] is False
    assert result["needs_decision_available"] is False  # needs_decision.py not landed yet
    assert "chưa quét được mục cần quyết" in Path(result["path"]).read_text(encoding="utf-8")


def test_daily_report_job_is_the_contract_entry_point(tmp_path):
    assert sec.JOB_NAME == "secretary_daily_report"
    result = sec.daily_report_job(tmp_path, scan_fn=lambda: [],
                                  post_to_session=lambda folder, markdown: False)
    assert result["date"]  # defaults to yesterday (Bangkok); file exists either way
    assert Path(result["path"]).is_file()


def test_run_daily_report_rejects_bad_date_string(tmp_path):
    with pytest.raises(ValueError):
        sec.run_daily_report(tmp_path, day="25/11/2026")


def test_default_report_date_is_yesterday_bangkok():
    now = datetime(2026, 11, 25, 0, 30, tzinfo=sec.BANGKOK)  # 00:30 Bangkok 25/11
    assert sec.default_report_date(now) == date(2026, 11, 24)


def test_resolve_lead_model_follows_chat_setup_rule():
    assert sec.resolve_lead_model(["qwen3.6-fast", "other"]) == "qwen3.6-fast"
    assert sec.resolve_lead_model(["other"]) == "other"
    assert sec.resolve_lead_model(["a", "b"], preferred="b") == "b"
    with pytest.raises(ValueError):
        sec.resolve_lead_model([])
    with pytest.raises(ValueError):
        sec.resolve_lead_model(["a"], preferred="missing")


def test_ensure_secretary_dir_uses_chat_files(tmp_path, monkeypatch):
    import poc.kaggle_worker.mcp_server as mcp

    def fake_chat_files(config, *, lead_model, kite_command, allowed_roots, state_dir=None):
        assert lead_model == "qwen3.6-fast"
        assert str(tmp_path / "secretary") in [str(r) for r in allowed_roots]
        return {"opencode.json": '{"model": "kite/qwen3.6-fast"}\n',
                "AGENTS.md": "# Agent trưởng của Kite\n\nTool hướng dẫn.\n"}

    monkeypatch.setattr(mcp, "chat_files", fake_chat_files)
    monkeypatch.setattr(mcp, "gateway_models", lambda config: ["qwen3.6-fast"])
    folder = sec.ensure_secretary_dir(config=object(), state_dir=tmp_path,
                                      kite_command=["kite", "mcp", "serve"])
    assert folder == tmp_path / "secretary"
    agents = (folder / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# Thư ký của Kite")
    assert "không tự quyết" in agents
    assert "Tool hướng dẫn." in agents  # chat_files content follows the role prompt
    assert (folder / "opencode.json").read_text(encoding="utf-8").startswith('{"model"')
    assert (folder / ".git").is_dir()  # OpenCode needs a Git repo (same rule as chat setup)
    # Second run without force keeps existing files.
    (folder / "AGENTS.md").write_text("tự sửa", encoding="utf-8")
    sec.ensure_secretary_dir(config=object(), state_dir=tmp_path,
                             kite_command=["kite", "mcp", "serve"])
    assert (folder / "AGENTS.md").read_text(encoding="utf-8") == "tự sửa"


def test_secretary_session_id_roundtrip(tmp_path):
    assert sec.load_secretary_session_id(tmp_path) is None
    sec.save_secretary_session_id(tmp_path, "ses_abc123")
    assert sec.load_secretary_session_id(tmp_path) == "ses_abc123"
    assert not list(tmp_path.rglob("*.tmp"))

# ---------------------------------------------------------------------- router (mảnh 4-5)
pytest.importorskip("fastapi")

import json as _json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from poc.kaggle_worker.web.secretary_api import (
    _parse_session_id,
    _session_ids_in_list,
    create_router,
    secretary_chat_allowed,
)


class FakeHTTPResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        if isinstance(self._payload, (dict, list)):
            return _json.dumps(self._payload).encode()
        return str(self._payload or "").encode()


class FakeHTTPClient:
    """Stands in for aiohttp.ClientSession: handler(method, path, kwargs) -> (status, payload)."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list = []

    def request(self, method, url, **kwargs):
        path = url.split("http://127.0.0.1:4096/")[-1]
        self.calls.append((method, path, kwargs))
        status, payload = self.handler(method, path, kwargs)
        return FakeHTTPResponse(status, payload)

    async def close(self):
        pass


def _fake_ctx(tmp_path, handler):
    client = FakeHTTPClient(handler)
    notify = SimpleNamespace(ntfy=None)
    config = SimpleNamespace(notify=notify)
    return SimpleNamespace(state_dir=tmp_path, config_path=tmp_path / "config.yaml",
                           opencode=lambda: ("http://127.0.0.1:4096", "pw"),
                           http_factory=lambda: client, config=lambda: config), client


def _router_client(tmp_path, handler):
    ctx, client = _fake_ctx(tmp_path, handler)
    audited = []
    app = FastAPI()
    app.include_router(create_router(
        ctx,
        auth=lambda request: SimpleNamespace(user=SimpleNamespace(username="admin"),
                                            ip="127.0.0.1"),
        audit=lambda session, action, target="", ok=True, detail=None:
            audited.append((action, target))))
    return TestClient(app, raise_server_exceptions=False), client, audited


def _seed_secretary_folder(tmp_path):
    folder = tmp_path / "secretary"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "opencode.json").write_text('{"model": "kite/x"}\n', encoding="utf-8")
    return folder


def test_chat_allowed_only_create_prompt_list_message(tmp_path):
    assert secretary_chat_allowed("POST", "api/session", {"agent": "x"}) == {"agent"}
    assert secretary_chat_allowed("POST", "api/session", {}) == {"agent"}
    assert secretary_chat_allowed("GET", "api/session", None) == frozenset()
    assert secretary_chat_allowed("POST", "api/session/ses_1/prompt", {"text": "hi"}) == {"text"}
    assert secretary_chat_allowed("GET", "api/session/ses_1/message", None) == frozenset()
    # Extra body keys are rejected, like app.py's proxy.
    with pytest.raises(HTTPException) as exc:
        secretary_chat_allowed("POST", "api/session/ses_1/prompt", {"text": "hi", "model": "x"})
    assert exc.value.status_code == 400
    # In the OpenCode allowlist but outside the secretary subset: 403.
    with pytest.raises(HTTPException) as exc:
        secretary_chat_allowed("GET", "api/agent", None)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        secretary_chat_allowed("POST", "api/session/ses_1/interrupt", {})
    assert exc.value.status_code == 403
    # Not an OpenCode route at all: 403.
    with pytest.raises(HTTPException) as exc:
        secretary_chat_allowed("POST", "api/session/ses_1/agent", {"agent": "x"})
    assert exc.value.status_code == 403


def test_parse_session_id_shapes():
    assert _parse_session_id({"id": "a"}) == "a"
    assert _parse_session_id({"data": {"id": "b"}}) == "b"
    assert _parse_session_id({"sessionID": "c"}) == "c"
    assert _parse_session_id({"data": [{"id": "d"}]}) is None
    assert _session_ids_in_list({"data": [{"id": "a"}, {"id": "b"}]}) == {"a", "b"}


def test_reports_endpoints(tmp_path):
    sec.save_report(tmp_path, DAY, "# Báo cáo ngày 25/11/2026\n\nNội dung.\n")
    client, _, _ = _router_client(tmp_path, lambda m, p, k: (404, {}))
    response = client.get("/api/secretary/reports")
    assert response.status_code == 200
    assert response.json() == {"reports": [{"date": "2026-11-25",
                                            "title": "Báo cáo ngày 25/11/2026"}]}
    response = client.get("/api/secretary/reports/2026-11-25")
    assert response.status_code == 200
    assert response.json()["markdown"].startswith("# Báo cáo ngày")
    assert client.get("/api/secretary/reports/2026-11-26").status_code == 404
    assert client.get("/api/secretary/reports/25-11-2026").status_code == 400


def test_chat_create_forces_secretary_directory(tmp_path):
    folder = _seed_secretary_folder(tmp_path)

    def handler(method, path, kwargs):
        assert method == "POST" and path == "api/session"
        body = _json.loads(kwargs["data"].decode())
        assert body["location"] == {"directory": str(folder)}  # never the main chat dir
        return 200, {"data": {"id": "ses_new"}}

    client, http, _ = _router_client(tmp_path, handler)
    response = client.post("/api/secretary/chat", json={})
    assert response.status_code == 200
    assert response.json() == {"data": {"id": "ses_new"}}


def test_chat_without_secretary_folder_is_409(tmp_path):
    client, _, _ = _router_client(tmp_path, lambda m, p, k: (200, {}))
    assert client.post("/api/secretary/chat", json={}).status_code == 409
    assert client.get("/api/secretary/chat/api/session").status_code == 409


def test_chat_prompt_and_message_only_inside_secretary_folder(tmp_path):
    _seed_secretary_folder(tmp_path)

    def handler(method, path, kwargs):
        if method == "GET" and path == "api/session":
            return 200, {"data": [{"id": "ses_1"}]}
        if method == "GET" and path.endswith("/message"):
            return 200, {"data": [{"id": "m1", "info": {"role": "user"},
                                   "parts": [{"type": "text", "text": "hi"}]}]}
        return 200, {}

    client, _, _ = _router_client(tmp_path, handler)
    # A session id from elsewhere (not listed in the secretary folder) is refused.
    assert client.post("/api/secretary/chat/api/session/ses_other/prompt",
                       json={"text": "xin chào"}).status_code == 404
    assert client.get("/api/secretary/chat/api/session/ses_other/message").status_code == 404
    # A session listed inside the folder goes through.
    response = client.post("/api/secretary/chat/api/session/ses_1/prompt",
                           json={"text": "xin chào"})
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    response = client.get("/api/secretary/chat/api/session/ses_1/message")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "m1"


def test_report_run_saves_posts_and_reports_counts(tmp_path, monkeypatch):
    _seed_secretary_folder(tmp_path)
    # Journal with one done run + one spend row on Bangkok 25/11.
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "tasks.jsonl").write_text(
        _json.dumps({"task_id": "t1", "status": "ok", "ts": _ts(2026, 11, 25, 10)}) + "\n",
        encoding="utf-8")
    (journal_dir / "spend.jsonl").write_text(
        _json.dumps({"worker": "api", "usd": 1.5, "ts": _ts(2026, 11, 25, 9)}) + "\n",
        encoding="utf-8")

    def handler(method, path, kwargs):
        if method == "POST" and path == "api/session":
            return 200, {"id": "ses_1"}
        if method == "POST" and path.endswith("/prompt"):
            body = _json.loads(kwargs["data"].decode())
            assert body["text"].startswith("# Báo cáo ngày 25/11/2026")
            return 200, {}
        raise AssertionError(f"unexpected OpenCode call {method} {path}")

    client, http, audited = _router_client(tmp_path, handler)
    monkeypatch.setattr("poc.kaggle_worker.secretary.ensure_secretary_dir",
                        lambda **kwargs: tmp_path / "secretary")
    response = client.post("/api/secretary/report/run", json={"date": "2026-11-25"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["date"] == "2026-11-25"
    assert payload["done"] == 1 and payload["failed"] == 0
    assert payload["spend_total_usd"] == 1.5
    assert payload["notified"] is False  # no ntfy configured in the fake ctx
    assert payload["posted"]["posted"] is True
    assert payload["posted"]["session_id"] == "ses_1"
    assert ("secretary_report_run", "2026-11-25") in audited
    assert (tmp_path / "secretary" / "reports" / "2026-11-25.md").is_file()
    assert (tmp_path / "secretary" / "session.json").is_file()
    # The prompt really went to the secretary session, not elsewhere.
    prompt_calls = [c for c in http.calls if c[1].endswith("/prompt")]
    assert len(prompt_calls) == 1 and "ses_1" in prompt_calls[0][1]


def test_report_run_uses_real_needs_decision_scan(tmp_path, monkeypatch):
    """End-to-end with worker (c)'s real scan: a COMPLETED task with result.patch shows up
    in the report's patch-awaiting count (via _scan_fn_for, no stub)."""
    import sqlite3

    from poc.kaggle_worker.backend import task_slug
    from poc.kaggle_worker.store import SQLiteSchedulerStore

    _seed_secretary_folder(tmp_path)
    store = SQLiteSchedulerStore(tmp_path / "scheduler.sqlite3")
    store.close()
    with sqlite3.connect(tmp_path / "scheduler.sqlite3") as db:
        db.execute(
            "INSERT INTO tasks (task_id, role, model_id, repo_head, branch, payload_json,"
            " estimated_gpu_seconds, priority, created_at, state, attempts,"
            " task_type, required_capabilities_json) VALUES"
            " ('task-abc', 'code', NULL, 'deadbeef', 'main', ?, 60, 5, ?, 'COMPLETED', 1,"
            " 'coding', '[]')",
            (_json.dumps({"goal": "Sửa lỗi đăng nhập"}), _ts(2026, 11, 25, 8)))
    patch_dir = tmp_path / "tasks" / task_slug("task-abc")
    patch_dir.mkdir(parents=True)
    (patch_dir / "result.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")

    def handler(method, path, kwargs):
        if method == "POST" and path == "api/session":
            return 200, {"id": "ses_1"}
        if method == "POST" and path.endswith("/prompt"):
            return 200, {}
        raise AssertionError(f"unexpected OpenCode call {method} {path}")

    client, _, _ = _router_client(tmp_path, handler)
    monkeypatch.setattr("poc.kaggle_worker.secretary.ensure_secretary_dir",
                        lambda **kwargs: tmp_path / "secretary")
    response = client.post("/api/secretary/report/run", json={"date": "2026-11-25"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["needs_decision_available"] is True
    assert payload["needs_decision"] == 1
    text = (tmp_path / "secretary" / "reports" / "2026-11-25.md").read_text(encoding="utf-8")
    assert "patch_awaiting: 1" in text
