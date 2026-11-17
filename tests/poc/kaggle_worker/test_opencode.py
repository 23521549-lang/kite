from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
import json
import subprocess
import sqlite3
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from poc.kaggle_worker.opencode import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeTaskAdapter,
    wait_for_terminal,
)
from poc.kaggle_worker.temporal_workflow import CodingTaskInput, RouteLease
from poc.kaggle_worker.temporal_activities import TemporalKiteActivities
from poc.kaggle_worker.temporal_state import TemporalActivityJournal
from temporalio.testing import ActivityEnvironment


class FakeOpenCode:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.sessions: dict[str, dict] = {}
        self.messages: dict[tuple[str, str], dict] = {}
        self.active_sessions: set[str] = set()
        self.fail_prompt = False
        self.terminal_event = "session.execution.succeeded"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status: int, payload: object | None = None):
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path
                owner.requests.append(("GET", self.path, None))
                if path == "/api/info":
                    return self._send(200, {"ready": True})
                if path == "/api/session/active":
                    return self._send(200, [{"id": sid} for sid in sorted(owner.active_sessions)])
                if path.startswith("/api/session/") and path.count("/") == 3:
                    session_id = path.rsplit("/", 1)[-1]
                    if session_id not in owner.sessions:
                        return self._send(404, {"error": "not found"})
                    return self._send(200, owner.sessions[session_id])
                if "/message/" in path:
                    parts = path.split("/")
                    message = owner.messages.get((parts[3], parts[5]))
                    return self._send(404, {"error": "not found"}) if message is None else self._send(200, message)
                if "/api/experimental/session/" in path and path.endswith("/log"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    session_id = path.split("/")[4]
                    query = parse_qs(urlparse(self.path).query)
                    after = int(query.get("after", ["0"])[0])
                    has_prompt = any(key[0] == session_id for key in owner.messages)
                    events = [
                        {"sequence": 1, "event": {"type": "session.text.delta", "properties": {"text": "Done"}}},
                        {"sequence": 2, "event": {"type": owner.terminal_event, "properties": {}}},
                    ]
                    for event in events:
                        if not has_prompt or event["sequence"] <= after:
                            continue
                        blob = json.dumps(event).encode()
                        self.wfile.write(b"id: " + str(event["sequence"]).encode() + b"\ndata: " + blob + b"\n\n")
                        self.wfile.flush()
                    return
                return self._send(404, {"error": "unknown path"})

            def do_POST(self):
                path = urlparse(self.path).path
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size)) if size else None
                owner.requests.append(("POST", self.path, body))
                if path == "/api/session":
                    sid = body["id"]
                    owner.sessions.setdefault(sid, body)
                    return self._send(200, owner.sessions[sid])
                if path.endswith("/model"):
                    return self._send(200)
                if path.endswith("/prompt"):
                    if owner.fail_prompt:
                        return self._send(409, {"error": "session busy"})
                    parts = path.split("/")
                    owner.messages[(parts[3], body["id"])] = body
                    owner.active_sessions.add(parts[3])
                    return self._send(200, {"accepted": True})
                if path.endswith("/interrupt"):
                    owner.active_sessions.discard(path.split("/")[3])
                    return self._send(200, {"interrupted": True})
                return self._send(404, {"error": "unknown path"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def fake_opencode():
    fake = FakeOpenCode()
    try:
        yield fake
    finally:
        fake.close()


def test_client_health_and_reuses_deterministic_session_with_routed_model(fake_opencode, tmp_path):
    client = OpenCodeClient(fake_opencode.base_url)
    assert client.health() == {"ready": True}
    first = client.ensure_session(
        str(tmp_path), provider_id="kaggle", model_id="model-a", operation_key="task-1:session",
    )
    second = client.ensure_session(
        str(tmp_path), provider_id="kaggle", model_id="model-a", operation_key="task-1:session",
    )
    assert first == second
    creates = [request for request in fake_opencode.requests if request[1] == "/api/session"]
    assert len(creates) == 1
    assert creates[0][2]["model"] == {"providerID": "kaggle", "modelID": "model-a"}


def test_client_submits_idempotent_prompt_and_reads_durable_events(fake_opencode, tmp_path):
    client = OpenCodeClient(fake_opencode.base_url)
    session_id = client.ensure_session(
        str(tmp_path), provider_id="provider", model_id="model", operation_key="op-session",
    )
    client.submit_prompt(session_id, "Fix the bug", operation_key="op-prompt")
    client.submit_prompt(session_id, "Fix the bug", operation_key="op-prompt")
    events = list(client.read_events(session_id, after=0))
    assert [event.sequence for event in events] == [1, 2]
    assert events[-1].type == "session.execution.succeeded"
    prompts = [request for request in fake_opencode.requests if request[1].endswith("/prompt")]
    assert len(prompts) == 1
    assert prompts[0][2]["text"] == "Fix the bug"
    assert prompts[0][2]["id"].startswith("msg_")


def test_client_waits_for_completion_and_heartbeats_event_sequence(fake_opencode, tmp_path):
    client = OpenCodeClient(fake_opencode.base_url)
    session_id = client.ensure_session(
        str(tmp_path), provider_id="provider", model_id="model", operation_key="op-session",
    )
    client.submit_prompt(session_id, "Run", operation_key="wait-prompt")
    heartbeats = []
    events, cursor = wait_for_terminal(
        client, session_id, timeout_seconds=1, heartbeat=heartbeats.append,
    )
    assert events[-1].type == "session.execution.succeeded"
    assert cursor == 2
    assert any(item.get("event_type") == "session.execution.succeeded" for item in heartbeats)


def test_client_surfaces_provider_conflict_and_can_interrupt(fake_opencode, tmp_path):
    client = OpenCodeClient(fake_opencode.base_url)
    session_id = client.ensure_session(
        str(tmp_path), provider_id="provider", model_id="model", operation_key="op-session",
    )
    fake_opencode.fail_prompt = True
    with pytest.raises(OpenCodeError, match="409"):
        client.submit_prompt(session_id, "Run", operation_key="op-prompt")
    assert client.interrupt(session_id) == {"interrupted": True}


def test_client_waits_for_terminal_interruption_event(fake_opencode, tmp_path):
    client = OpenCodeClient(fake_opencode.base_url)
    session_id = client.ensure_session(
        str(tmp_path), provider_id="provider", model_id="model", operation_key="op-session",
    )
    fake_opencode.terminal_event = "session.execution.interrupted"
    client.submit_prompt(session_id, "Run", operation_key="interrupt-prompt")
    response, events, cursor = client.interrupt_and_wait(
        session_id, timeout_seconds=1, heartbeat=lambda _details: None,
    )
    assert response == {"interrupted": True}
    assert events[-1].type == "session.execution.interrupted"
    assert cursor == 2


def test_client_rejects_non_loopback_endpoint_without_explicit_opt_in():
    with pytest.raises(ValueError, match="loopback"):
        OpenCodeClient("http://opencode.internal:4096")


def test_client_requires_https_and_auth_for_explicit_remote_endpoint():
    with pytest.raises(ValueError, match="HTTPS"):
        OpenCodeClient("http://opencode.internal:4096", allow_remote=True)
    with pytest.raises(ValueError, match="authentication"):
        OpenCodeClient("https://opencode.internal:4096", allow_remote=True)


def test_task_adapter_runs_in_isolated_worktree_and_returns_review_patch(fake_opencode, tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Kite Test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "kite@example.invalid"], check=True)
    (workspace / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.py"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-m", "base"], check=True, capture_output=True)
    base = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    (workspace / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (workspace / "new.py").write_text("created = True\n", encoding="utf-8")
    task = CodingTaskInput(
        task_id="task-adapter", goal="Change value to 2", repo_path=str(workspace),
        repo_head=base, branch="kite/task-adapter", required_capabilities=("coder",),
        preferred_model_id="model-a", estimated_gpu_seconds=60,
    )
    route = RouteLease(
        task_id=task.task_id, lease_id="lease-a", owner_id="owner-a",
        provider_id="provider-a", worker_id="worker-a", model_id="model-a",
        reserved_gpu_seconds=60,
    )

    async def scenario():
        adapter = OpenCodeTaskAdapter(OpenCodeClient(fake_opencode.base_url))
        session_id = await adapter.prepare_session(task, route, str(workspace), None, "prep-key")
        heartbeats = []
        result = await adapter.run(
            task, route, str(workspace), "", session_id, "run-key", heartbeats.append,
        )
        assert result.task_id == task.task_id
        assert result.base_repo_head == base
        assert result.session_id == session_id
        assert result.changed_files == ("new.py", "tracked.py")
        assert "value = 2" in result.patch
        assert "created = True" in result.patch
        assert result.summary == "Done"
        assert any(item.get("event_type") == "session.execution.succeeded" for item in heartbeats)
        apply_check = tmp_path / "apply-check"
        subprocess.run(
            ["git", "clone", "--quiet", str(workspace), str(apply_check)],
            check=True, capture_output=True,
        )
        checked = subprocess.run(
            ["git", "-C", str(apply_check), "apply", "--check", "-"],
            input=result.patch, text=True, capture_output=True,
        )
        assert checked.returncode == 0, checked.stderr
        prompt = next(req[2] for req in fake_opencode.requests if req[1].endswith("/prompt"))
        assert prompt["text"] == task.goal

    import asyncio
    asyncio.run(scenario())


def test_task_adapter_preserves_prior_session_while_switching_routed_model(fake_opencode, tmp_path):
    task = CodingTaskInput(
        task_id="task-resume", goal="continue", repo_path=str(tmp_path), repo_head="head",
        branch="kite/task-resume", required_capabilities=("coder",),
        preferred_model_id=None, estimated_gpu_seconds=10,
    )
    first_route = RouteLease(
        task_id=task.task_id, lease_id="lease-1", owner_id="owner-a",
        provider_id="provider-a", worker_id="worker-a", model_id="model-a",
        reserved_gpu_seconds=10,
    )
    second_route = RouteLease(
        task_id=task.task_id, lease_id="lease-2", owner_id="owner-b",
        provider_id="provider-b", worker_id="worker-b", model_id="model-b",
        reserved_gpu_seconds=10,
    )

    async def scenario():
        adapter = OpenCodeTaskAdapter(OpenCodeClient(fake_opencode.base_url))
        session = await adapter.prepare_session(task, first_route, str(tmp_path), None, "prepare-1")
        resumed = await adapter.prepare_session(
            task, second_route, str(tmp_path), session, "prepare-2",
        )
        assert resumed == session
        creates = [request for request in fake_opencode.requests if request[1] == "/api/session"]
        model_updates = [
            request[2] for request in fake_opencode.requests
            if urlparse(request[1]).path.endswith("/model")
        ]
        assert len(creates) == 1
        assert model_updates[-1]["model"] == {"providerID": "provider-b", "modelID": "model-b"}

    import asyncio
    asyncio.run(scenario())


def test_temporal_activities_use_injected_opencode_adapter(fake_opencode, tmp_path):
    workspace = tmp_path / "activity-worktree"
    workspace.mkdir()
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Kite Test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "kite@example.invalid"], check=True)
    (workspace / "a.py").write_text("a = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-m", "base"], check=True, capture_output=True)
    base = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    (workspace / "a.py").write_text("a = 2\n", encoding="utf-8")
    task = CodingTaskInput(
        task_id="task-wired", goal="Update a", repo_path=str(workspace), repo_head=base,
        branch="kite/task-wired", required_capabilities=("coder",),
        preferred_model_id="model-a", estimated_gpu_seconds=30,
    )
    route = RouteLease(
        task_id=task.task_id, lease_id="lease-wired", owner_id="owner-a",
        provider_id="provider-a", worker_id="worker-a", model_id="model-a",
        reserved_gpu_seconds=30,
    )

    class Backend:
        events = []

        async def reconcile_operation(self, *_args):
            raise AssertionError("OpenCode operations must use the injected adapter")

        async def heartbeat_route(self, *_args):
            pass

        async def prepare_opencode_session(self, *_args):
            raise AssertionError("session preparation must use the injected adapter")

        async def run_opencode(self, *_args):
            raise AssertionError("session execution must use the injected adapter")

        async def checkpoint_and_stop(self, task, route, result, key):
            self.events.append("checkpoint")
            return "/state/task-wired.json"

    async def scenario():
        journal = TemporalActivityJournal(tmp_path / "wired-journal.sqlite3")
        activities = TemporalKiteActivities(
            Backend(), journal, opencode=OpenCodeTaskAdapter(
                OpenCodeClient(fake_opencode.base_url), poll_interval_seconds=0.01,
            ), route_heartbeat_interval_seconds=0.2,
        )
        env = ActivityEnvironment()
        env.info = replace(
            env.info, workflow_id=task.task_id, workflow_run_id="wired-run",
            activity_id="prepare-session",
        )
        session_id = await env.run(
            activities.prepare_opencode_session, task, route, str(workspace), None,
        )
        env.info = replace(env.info, activity_id="run-session")
        result = await env.run(
            activities.run_opencode, task, route, str(workspace), "", session_id,
        )
        operation_key = "task-wired:wired-run:run-session:run-opencode"
        with sqlite3.connect(tmp_path / "wired-journal.sqlite3") as db:
            db.execute(
                "UPDATE temporal_activity_results SET state='RUNNING',owner_token='dead-worker', "
                "lease_expires_at=0,result_json=NULL WHERE operation_key=?",
                (operation_key,),
            )
        env.info = replace(
            env.info, attempt=2,
            heartbeat_details=({
                "operation_key": operation_key, "session_id": session_id,
                "start_sequence": 0, "event_sequence": 1,
            },),
        )
        resumed_result = await env.run(
            activities.run_opencode, task, route, str(workspace), "", session_id,
        )
        env.info = replace(env.info, activity_id="checkpoint-session")
        checkpoint = await env.run(activities.checkpoint_and_stop, task, route, resumed_result)
        journal.close()
        assert result.session_id == session_id
        assert "a = 2" in result.patch
        assert resumed_result == result
        assert len([req for req in fake_opencode.requests if req[1].endswith("/prompt")]) == 1
        assert checkpoint == "/state/task-wired.json"
        assert Backend.events == ["checkpoint"]
        assert not fake_opencode.active_sessions
        checkpoint_index = next(
            index for index, item in enumerate(fake_opencode.requests)
            if item[0] == "POST" and item[1] == "/api/session"
        )
        stop_index = next(
            index for index, item in enumerate(fake_opencode.requests)
            if item[0] == "POST" and urlparse(item[1]).path.endswith("/interrupt")
        )
        assert checkpoint_index < stop_index

    import asyncio
    asyncio.run(scenario())
