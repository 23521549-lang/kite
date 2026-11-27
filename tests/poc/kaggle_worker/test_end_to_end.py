"""Local-only contract test for route -> OpenCode -> checkpoint -> review."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sqlite3
import threading
from urllib.parse import urlparse, parse_qs

import pytest
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from loopback_port import free_loopback_port
from poc.kaggle_worker.opencode import OpenCodeClient, OpenCodeTaskAdapter
from poc.kaggle_worker.routing import RoutingRequest, WorkerLoad, select_worker
from poc.kaggle_worker.protocol import OwnerPolicy, WorkerProfile
from poc.kaggle_worker.temporal_activities import TemporalKiteActivities
from poc.kaggle_worker.temporal_state import RecoveryDecision, TemporalActivityJournal
from poc.kaggle_worker.temporal_workflow import (
    CodingRunResult, CodingTaskInput, CodingTaskWorkflow, RouteLease,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


class FakeOpenCodeServer:
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.active = set()
        self.prompts = 0
        self.max_active = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, status, value=None, content_type="application/json"):
                body = b"" if value is None else (value if isinstance(value, bytes) else json.dumps(value).encode())
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/api/session/active":
                    return self.send(200, [{"id": item} for item in owner.active])
                if path.startswith("/api/session/") and "/message/" not in path:
                    sid = path.rsplit("/", 1)[-1]
                    return self.send(200, owner.sessions[sid]) if sid in owner.sessions else self.send(404, {})
                if "/message/" in path:
                    _, _, _, sid, _, mid = path.split("/")
                    item = owner.messages.get((sid, mid))
                    return self.send(200, item) if item else self.send(404, {})
                if "/log" in path:
                    sid = path.split("/")[4]
                    after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
                    if not any(key[0] == sid for key in owner.messages):
                        return self.send(200, b"", "text/event-stream")
                    events = [
                        {"sequence": 1, "event": {"type": "session.text.delta", "properties": {"text": "Changed value."}}},
                        {"sequence": 2, "event": {"type": "session.execution.succeeded", "properties": {}}},
                    ]
                    payload = b"".join(
                        b"id: " + str(item["sequence"]).encode() + b"\ndata: " + json.dumps(item).encode() + b"\n\n"
                        for item in events if item["sequence"] > after
                    )
                    return self.send(200, payload, "text/event-stream")
                return self.send(404, {})

            def do_POST(self):
                path = urlparse(self.path).path
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size)) if size else {}
                if path == "/api/session":
                    owner.sessions[body["id"]] = body
                    return self.send(200, body)
                if path.endswith("/model"):
                    return self.send(200, {})
                if path.endswith("/prompt"):
                    sid = path.split("/")[3]
                    owner.messages[(sid, body["id"])] = body
                    owner.prompts += 1
                    owner.active.add(sid)
                    owner.max_active = max(owner.max_active, len(owner.active))
                    workdir = Path(owner.sessions[sid]["location"]["directory"])
                    (workdir / "src.py").write_text("value = 2\n", encoding="utf-8")
                    owner.active.discard(sid)
                    return self.send(200, {"accepted": True})
                if path.endswith("/interrupt"):
                    owner.active.discard(path.split("/")[3])
                    return self.send(200, {"interrupted": True})
                return self.send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class FakeKiteBackend:
    def __init__(self, root: Path, data: Path, task: CodingTaskInput, remote: Path):
        self.root = root
        self.data = data
        self.task = task
        self.remote = remote
        self.lease = None
        self.route_decisions = []
        self.reservations = 0
        self.heartbeats = 0
        self.settlements = set()
        self.settlement_count = 0
        self.checkpoint_path = data / "checkpoint.json"
        self.release_crash_once = False

    async def reconcile_operation(self, operation_name, key, *context):
        if operation_name == "release-route:COMPLETED" and key in self.settlements:
            return RecoveryDecision(recovered=True, result=None)
        return RecoveryDecision(recovered=False)

    async def validate_task(self, task, key):
        assert git(self.root, "rev-parse", "HEAD") == task.repo_head

    async def prepare_workspace(self, task, key):
        workspace = self.data / "worktree"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-b", task.branch,
                        str(workspace), task.repo_head], check=True, capture_output=True)
        return str(workspace)

    async def reserve_route(self, task, key):
        now = 1000
        workers = [
            WorkerProfile("worker-other", "owner-other", "provider-other", ("reviewer",),
                          ("model-other",), True, True, now, 1),
            WorkerProfile("worker-fit", "owner-fit", "provider-fit", ("coder",),
                          ("model-fit",), True, True, now, 1),
        ]
        owners = {
            "owner-other": OwnerPolicy("owner-other", True, True, 1, 30, now),
            "owner-fit": OwnerPolicy("owner-fit", True, True, 1, 1200, now),
        }
        loads = {item.worker_id: WorkerLoad(0, 0, 0) for item in workers}
        decision = select_worker(
            RoutingRequest("coder", task.preferred_model_id, task.estimated_gpu_seconds),
            workers, owners, loads, now=now,
        )
        assert decision is not None
        self.route_decisions.append(decision)
        self.reservations += 1
        self.lease = RouteLease(task.task_id, "lease-e2e", decision.owner_id,
                                decision.provider_id, decision.worker_id,
                                decision.model_id, task.estimated_gpu_seconds)
        return self.lease

    async def heartbeat_route(self, task, route, key):
        self.heartbeats += 1

    async def prepare_opencode_session(self, *args):
        raise AssertionError("OpenCode adapter should own session preparation")

    async def run_opencode(self, *args):
        raise AssertionError("OpenCode adapter should own model execution")

    async def interrupt_opencode(self, *args):
        pass

    async def checkpoint_and_stop(self, task, route, result, key):
        data = {"task_id": task.task_id, "head": task.repo_head,
                "session_id": result.session_id, "patch": result.patch,
                "route": route.worker_id}
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.write_text(json.dumps(data), encoding="utf-8")
        return str(self.checkpoint_path)

    async def build_review_bundle(self, task, route, result, checkpoint_path, key):
        from poc.kaggle_worker.integration import ReviewBundle
        return ReviewBundle(task.task_id, route.lease_id, route.worker_id,
                            result.base_repo_head, result.patch, result.summary,
                            result.usage_gpu_seconds)

    async def release_route(self, task, route, result, outcome, key):
        if key in self.settlements:
            return
        self.settlements.add(key)
        self.settlement_count += 1
        if self.release_crash_once:
            self.release_crash_once = False
            raise RuntimeError("simulated worker loss after durable quota settlement")


def prepare_remote_repo(base: Path):
    source = base / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    git(source, "config", "user.name", "Kite E2E")
    git(source, "config", "user.email", "kite-e2e@example.invalid")
    (source / "src.py").write_text("value = 1\n", encoding="utf-8")
    git(source, "add", "src.py")
    subprocess.run(["git", "-C", str(source), "commit", "-m", "base"], check=True, capture_output=True)
    head = git(source, "rev-parse", "HEAD")
    remote = base / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    shutil.copytree(source / ".git" / "objects", remote / "objects", dirs_exist_ok=True)
    default_branch = git(source, "branch", "--show-current")
    subprocess.run(["git", "--git-dir", str(remote), "update-ref", f"refs/heads/{default_branch}", head],
                   check=True, capture_output=True)
    git(source, "remote", "add", "origin", str(remote))
    return source, remote, head, git(remote, "rev-parse", f"refs/heads/{default_branch}")


def test_temporal_e2e_routes_runs_opencode_and_returns_review_only_patch(tmp_path):
    source, remote, head, remote_head = prepare_remote_repo(tmp_path)
    task = CodingTaskInput("e2e-1", "Change value to two", str(source), head,
                           "agent/e2e-1", ("coder",), "model-fit", 60)
    backend = FakeKiteBackend(source, tmp_path / "state", task, remote)
    fake_server = FakeOpenCodeServer()

    async def scenario():
        journal = TemporalActivityJournal(tmp_path / "state" / "activities.sqlite3")
        activities = TemporalKiteActivities(
            backend, journal,
            opencode=OpenCodeTaskAdapter(OpenCodeClient(fake_server.url), poll_interval_seconds=0.01),
            route_heartbeat_interval_seconds=0.02,
        )
        methods = [activities.validate_task, activities.prepare_workspace,
                   activities.reserve_route, activities.prepare_opencode_session,
                   activities.run_opencode, activities.interrupt_opencode,
                   activities.checkpoint_and_stop, activities.build_review_bundle,
                   activities.release_route]
        async with await WorkflowEnvironment.start_time_skipping(port=free_loopback_port()) as env:
            async with Worker(env.client, task_queue="kite-e2e", workflows=[CodingTaskWorkflow], activities=methods):
                outcome = await env.client.execute_workflow(
                    CodingTaskWorkflow.run, task, id=task.task_id, task_queue="kite-e2e",
                )
        journal.close()
        return outcome

    try:
        outcome = asyncio.run(scenario())
        assert outcome.status == "READY_FOR_REVIEW"
        assert outcome.review_bundle is not None
        assert outcome.review_bundle.worker_id == "worker-fit"
        assert "value = 2" in outcome.review_bundle.patch
        assert backend.route_decisions[0].provider_id == "provider-fit"
        assert backend.reservations == 1
        assert backend.settlement_count == 1
        assert fake_server.prompts == 1
        assert fake_server.max_active == 1
        assert git(source, "rev-parse", "HEAD") == head
        assert git(source, "status", "--porcelain") == ""
        remote_ref = f"refs/heads/{git(source, 'branch', '--show-current')}"
        assert git(remote, "rev-parse", remote_ref) == remote_head
        assert (source / "src.py").read_text(encoding="utf-8") == "value = 1\n"
        assert json.loads(backend.checkpoint_path.read_text(encoding="utf-8"))["head"] == head
        assert "value = 2" in (tmp_path / "state" / "worktree" / "src.py").read_text(encoding="utf-8")
    finally:
        fake_server.close()


def test_new_activity_worker_recovers_quota_settlement_after_checkpoint(tmp_path):
    source, remote, head, _ = prepare_remote_repo(tmp_path)
    task = CodingTaskInput("recover-e2e", "goal", str(source), head,
                           "agent/recover-e2e", ("coder",), "model-fit", 60)
    backend = FakeKiteBackend(source, tmp_path / "state", task, remote)
    backend.lease = RouteLease(task.task_id, "lease-recover", "owner-fit", "provider-fit",
                               "worker-fit", "model-fit", 60)
    result = CodingRunResult(
        task.task_id, head, "session-1", "diff --git a/src.py b/src.py\n", "done", ("src.py",), 0,
    )
    backend.release_crash_once = True
    path = tmp_path / "state" / "activity-journal.sqlite3"

    async def scenario():
        await backend.checkpoint_and_stop(task, backend.lease, result, "checkpoint-before-restart")
        first_journal = TemporalActivityJournal(path)
        first = TemporalKiteActivities(backend, first_journal)
        env = ActivityEnvironment()
        env.info = replace(env.info, workflow_id=task.task_id, workflow_run_id="same-run",
                           activity_id="release-completed")
        with pytest.raises(RuntimeError, match="quota settlement"):
            await env.run(first.release_route, task, backend.lease, result, "COMPLETED")
        first_journal.close()
        with sqlite3.connect(path) as db:
            db.execute("UPDATE temporal_activity_results SET state='RUNNING', owner_token='dead', lease_expires_at=0")

        restarted_journal = TemporalActivityJournal(path)
        restarted = TemporalKiteActivities(backend, restarted_journal)
        await env.run(restarted.release_route, task, backend.lease, result, "COMPLETED")
        restarted_journal.close()

    asyncio.run(scenario())
    assert backend.checkpoint_path.exists()
    assert backend.settlement_count == 1
    assert len(backend.settlements) == 1
