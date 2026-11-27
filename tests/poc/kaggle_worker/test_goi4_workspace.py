"""Gói 4 (worker (c)): the Workspace API — queue, needs-decision (read-only), review proxy.

The router is mounted on a bare FastAPI app here with a fake auth/audit and a fake context;
the OpenCode side is a small stub server in the style of test_web.py's OpenCodeStub.
"""

from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker.backend import task_slug  # noqa: E402
from poc.kaggle_worker.journal import Journal  # noqa: E402
from poc.kaggle_worker.protocol import OwnerPolicy, ScheduledTask, WorkerProfile  # noqa: E402
from poc.kaggle_worker.relay import SpendLedger  # noqa: E402
from poc.kaggle_worker.store import SQLiteSchedulerStore  # noqa: E402
from poc.kaggle_worker.web.workspace_api import create_router  # noqa: E402


class OpenCodeStub(BaseHTTPRequestHandler):
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
        self._answer(200, {"data": [{"id": "ses_1", "time": {"created": 1, "updated": 5}}]})

    def do_POST(self):
        body = self._record()
        self._answer(200, {"data": {"id": "ses_2", "echo": json.loads(body or "{}")}})

    def do_DELETE(self):
        self._record()
        self._answer(204)

    def log_message(self, *args):
        pass


def sample_task(**changes):
    data = dict(task_id="task-1", role="coder", model_id="model-a", repo_head="abc123",
                branch="agent/task-1", payload_json=json.dumps({"goal": "sửa parser"}),
                estimated_gpu_seconds=60, priority=1, created_at=time.time() - 100)
    data.update(changes)
    return ScheduledTask(**data)


@pytest.fixture
def site(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "scheduler.sqlite3"
    store = SQLiteSchedulerStore(db_path)
    store.register_owner_policy(OwnerPolicy(owner_id="owner-a", owner_authorized=True,
                                            policy_approved=True, max_concurrent_sessions=1,
                                            quota_remaining_seconds=900, quota_observed_at=100.0))
    store.register_worker(WorkerProfile(worker_id="worker-a", owner_id="owner-a", provider_id="fake",
                                        roles=("coder",), model_ids=("model-a",), enabled=True,
                                        provider_ready=True, status_observed_at=100.0, max_concurrency=1))
    store.enqueue(sample_task(task_id="task-1"))
    store.enqueue(sample_task(task_id="task-2", created_at=time.time() - 3600,
                              payload_json=json.dumps({"goal": "task lỗi"})))
    store.close()
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE tasks SET state='COMPLETED' WHERE task_id='task-1'")
        db.execute("UPDATE tasks SET state='FAILED', last_error='hết GPU' WHERE task_id='task-2'")
        db.commit()
    patch_dir = state_dir / "tasks" / task_slug("task-1")
    patch_dir.mkdir(parents=True)
    (patch_dir / "result.patch").write_text(
        "diff --git a/parser.py b/parser.py\n--- a/parser.py\n+++ b/parser.py\n@@ -1 +1 @@\n-x\n+y\n",
        encoding="utf-8")
    chat_dir = state_dir / "chat"
    chat_dir.mkdir()
    (chat_dir / "opencode.json").write_text('{"model": "kite/qwen"}\n', encoding="utf-8")

    OpenCodeStub.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenCodeStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class FakeOps:
        def __init__(self):
            self.db_path = db_path

    class FakeCtx:
        def __init__(self):
            self.state_dir = state_dir
            self.chat_dir = chat_dir
            self.http_factory = None
            self._ledger = SpendLedger([], clock=time.time)

        def journal(self):
            return Journal(state_dir / "journal")

        def ledger(self):
            return self._ledger

        def config(self):
            return SimpleNamespace(
                notify=SimpleNamespace(near_cap_pct=80),
                providers=[SimpleNamespace(
                    effective_worker_id="w-relay",
                    relay=SimpleNamespace(daily_cap=10.0, monthly_cap=100.0))])

        def ops(self):
            return FakeOps()

        def opencode(self):
            return f"http://127.0.0.1:{server.server_port}", "oc-pass"

    ctx = FakeCtx()
    audit_calls: list[tuple] = []

    def auth(request):
        return SimpleNamespace(username="han", ip="127.0.0.1")

    def audit(session, action, target="", ok=True, detail=None, **kwargs):
        audit_calls.append((action, target))

    app = FastAPI()
    app.include_router(create_router(ctx, auth, audit))
    client = TestClient(app)
    yield {"client": client, "ctx": ctx, "audit": audit_calls, "seen": OpenCodeStub.seen,
           "state_dir": state_dir}
    server.shutdown()


def test_queue_lists_only_patch_awaiting(site):
    items = site["client"].get("/api/workspace/queue").json()["items"]
    assert [i["key"] for i in items] == ["patch:task-1"]
    assert items[0]["title"] == "sửa parser" and items[0]["link"] == "/tasks/task-1"


def test_needs_decision_reads_everything_and_sends_no_ntfy(site):
    items = site["client"].get("/api/workspace/needs-decision").json()["items"]
    assert [i["key"] for i in items] == ["patch:task-1", "failed:task-2"]
    assert not (site["state_dir"] / "needs_decision" / "notified.json").exists()


def test_review_create_writes_folder_and_forces_its_directory(site):
    response = site["client"].post("/api/workspace/review/task-1/chat")
    assert response.status_code == 200, response.text
    review_dir = site["state_dir"] / "review" / task_slug("task-1")
    assert (review_dir / "opencode.json").read_text(encoding="utf-8") == '{"model": "kite/qwen"}\n'
    agents = (review_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "CHỈ TƯ VẤN" in agents and "sửa parser" in agents and "parser.py" in agents
    method, path, authorization, body = site["seen"][0]
    assert (method, path) == ("POST", "/api/session")
    assert authorization.startswith("Basic ")
    assert json.loads(body)["location"] == {"directory": str(review_dir)}
    assert ("review_chat_create", "task-1") in site["audit"]


def test_review_list_is_scoped_to_the_review_folder(site):
    site["client"].post("/api/workspace/review/task-1/chat")
    response = site["client"].get("/api/workspace/review/task-1/chat/api/session",
                                  params={"directory": "/elsewhere", "limit": 5})
    assert response.status_code == 200
    path = site["seen"][-1][1]
    review_dir = str(site["state_dir"] / "review" / task_slug("task-1"))
    assert "directory=" in path and "/elsewhere" not in path
    assert review_dir.replace("/", "%2F") in path or review_dir in path


def test_review_proxy_blocks_shell_and_extra_fields(site):
    site["client"].post("/api/workspace/review/task-1/chat")
    assert site["client"].post("/api/workspace/review/task-1/chat/api/session/ses_1/shell",
                               json={}).status_code == 403
    assert site["client"].post("/api/workspace/review/task-1/chat/api/session/ses_1/prompt",
                               json={"text": "x", "files": [{"path": "/etc/shadow"}]}).status_code == 400
    sent = site["client"].post("/api/workspace/review/task-1/chat/api/session/ses_1/prompt",
                               json={"text": "patch này ổn không?"})
    assert sent.status_code == 200
    assert json.loads(site["seen"][-1][3]) == {"text": "patch này ổn không?"}
    assert ("review_chat", "task-1") in site["audit"]


def test_review_routes_are_subset_of_opencode_routes():
    """Every review-proxy operation must also be open to the Chat tab (app.py's allowlist)."""
    from poc.kaggle_worker.web.app import OPENCODE_ROUTES
    from poc.kaggle_worker.web.workspace_api import REVIEW_ROUTES_SPEC

    def allowed(routes, method, path):
        return any(method in methods and pattern.fullmatch(path) for methods, pattern, _ in routes)

    import re

    for methods, pattern, _keys in REVIEW_ROUTES_SPEC:
        sample = pattern.replace("{S}", "ses_1")
        assert re.fullmatch(r"[A-Za-z0-9_/-]+", sample), pattern
        for method in methods.split():
            assert allowed(OPENCODE_ROUTES, method, sample), (method, pattern)


def test_review_proxy_allows_only_review_routes(site):
    site["client"].post("/api/workspace/review/task-1/chat")
    base = "/api/workspace/review/task-1/chat"
    for method, path in (
            ("PATCH", f"{base}/api/session/ses_1"),
            ("DELETE", f"{base}/api/session/ses_1"),
            ("POST", f"{base}/api/session/ses_1/interrupt"),
            ("POST", f"{base}/api/session/ses_1/agent"),
            ("GET", f"{base}/api/session/ses_1/permission"),
            ("GET", f"{base}/api/session/ses_1/form"),
            ("POST", f"{base}/api/session/ses_1/permission/per_1/reply"),
            ("POST", f"{base}/api/session/ses_1/form/frm_1/reply"),
            ("GET", f"{base}/api/info"),
    ):
        assert site["client"].request(method, path, json={}).status_code == 403, (method, path)
    # The four allowed operations still work.
    assert site["client"].get(f"{base}/api/session").status_code == 200
    assert site["client"].get(f"{base}/api/session/ses_1/message").status_code == 200


def test_review_message_needs_session_in_review_dir(site):
    site["client"].post("/api/workspace/review/task-1/chat")
    base = "/api/workspace/review/task-1/chat"
    # ses_1 is what the stub lists under the review folder; ses_9 is not.
    assert site["client"].get(f"{base}/api/session/ses_1/message").status_code == 200
    assert site["client"].get(f"{base}/api/session/ses_9/message").status_code == 404
    assert site["client"].post(f"{base}/api/session/ses_9/prompt", json={"text": "x"}).status_code == 404
    assert site["client"].post(f"{base}/api/session/ses_1/prompt", json={"text": "x"}).status_code == 200


def test_review_create_404s_without_task_or_patch(site):
    assert site["client"].post("/api/workspace/review/missing/chat").status_code == 404
    assert site["client"].post("/api/workspace/review/task-2/chat").status_code == 404
    assert site["client"].get("/api/workspace/review/task-1/chat/api/session").status_code == 404
