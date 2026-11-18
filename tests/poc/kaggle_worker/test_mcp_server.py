"""Kite MCP tools for the Vietnamese lead agent: fake Temporal client, real store and Git repos."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from poc.kaggle_worker.config import validate_config
from poc.kaggle_worker.integration import ReviewBundle
from poc.kaggle_worker.mcp_server import KiteOps, KiteToolError, chat_files
from poc.kaggle_worker.store import SQLiteSchedulerStore
from poc.kaggle_worker.temporal_workflow import CodingTaskOutcome


def make_config(tmp_path: Path):
    return validate_config({
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "kite-coding-tasks"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": [{
            "id": "kaggle-han", "adapter": "pull", "enabled": True, "owner": "han",
            "model": "qwen3.6", "models": ["devstral"], "capabilities": ["coding"],
            "quota": {"gpu_seconds": 108000, "concurrency": 1},
            "secrets": {"worker_token": f"file:{tmp_path / 'token'}"},
        }],
        "gateway": {"aliases": {"qwen3.6-fast": {"model": "qwen3.6", "params": {}}}},
    })


def make_repo(root: Path, name: str = "repo") -> tuple[Path, str]:
    repo = root / name
    repo.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com", *a],
                                    check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    (repo / "calc.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    return repo, run("rev-parse", "HEAD")


class FakeHandle:
    def __init__(self, client, workflow_id):
        self.client, self.workflow_id = client, workflow_id

    async def describe(self):
        if self.workflow_id not in self.client.state:
            raise RuntimeError("workflow not found")
        return SimpleNamespace(status=SimpleNamespace(name=self.client.state[self.workflow_id]))

    async def query(self, _query):
        return "CODING"

    async def signal(self, signal, *args):
        self.client.signals.append((self.workflow_id, signal.__name__, args))

    async def result(self):
        return self.client.outcomes[self.workflow_id]


class FakeClient:
    def __init__(self):
        self.started, self.signals, self.state, self.outcomes = [], [], {}, {}

    async def start_workflow(self, _run, task_input, *, id, task_queue):
        self.started.append((task_input, id, task_queue))
        self.state[id] = "RUNNING"

    def get_workflow_handle(self, workflow_id):
        return FakeHandle(self, workflow_id)

    def get_workflow_handle_for(self, _run, workflow_id):
        return FakeHandle(self, workflow_id)


def make_ops(tmp_path: Path, client: FakeClient, roots=None, status=None) -> KiteOps:
    async def connect(_endpoint):
        return client

    return KiteOps(make_config(tmp_path), db_path=tmp_path / "state" / "scheduler.sqlite3",
                   state_dir=tmp_path / "state", allowed_roots=roots or [tmp_path], connect=connect,
                   gateway_status=lambda: status or {"workers": []})


def test_start_task_queues_and_starts_a_workflow_at_the_repo_head(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    started = asyncio.run(ops.start_task(str(repo), "Phân tích calc.py", task_type="analysis"))
    task_input, workflow_id, queue = client.started[0]
    assert started["task_id"] == workflow_id and workflow_id.startswith("chat-")
    assert (task_input.repo_head, task_input.task_type, queue) == (head, "analysis", "kite-coding-tasks")
    with SQLiteSchedulerStore(tmp_path / "state" / "scheduler.sqlite3") as store:
        queued = store.get_task(workflow_id)
    assert queued["state"] == "QUEUED" and queued["task_type"] == "analysis"
    for bad in ({"task_type": "hack"}, {"model": "qwen3.6-fast"}, {"model": "gpt-9"}):
        with pytest.raises(KiteToolError):
            asyncio.run(ops.start_task(str(repo), "x", **bad))
    with pytest.raises(KiteToolError):
        asyncio.run(ops.start_task(str(repo), "   "))


def test_repositories_must_be_git_and_inside_the_allowed_folders(tmp_path):
    repo, _head = make_repo(tmp_path)
    (tmp_path / "allowed" / "plain").mkdir(parents=True)
    ops = make_ops(tmp_path, FakeClient(), roots=[tmp_path / "allowed"])
    with pytest.raises(KiteToolError, match="ngoài"):
        asyncio.run(ops.start_task(str(repo), "x"))
    with pytest.raises(KiteToolError, match="Git"):
        asyncio.run(ops.start_task(str(tmp_path / "allowed" / "plain"), "x"))


def test_instruction_pause_resume_and_cancel_become_workflow_signals(tmp_path):
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    asyncio.run(ops.send_instruction("t1", " Thêm docstring "))
    asyncio.run(ops.pause_task("t1"))
    asyncio.run(ops.resume_task("t1"))
    asyncio.run(ops.cancel_task("t1"))
    assert client.signals == [("t1", "resume_with_instruction", ("Thêm docstring",)), ("t1", "pause", ()),
                              ("t1", "resume_with_instruction", ("",)), ("t1", "cancel", ())]
    with pytest.raises(KiteToolError):
        asyncio.run(ops.send_instruction("t1", "  "))


def test_status_and_result_report_progress_then_the_review_bundle(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    status = asyncio.run(ops.task_status(task_id))
    assert (status["workflow"], status["step"], status["queue_state"]) == ("RUNNING", "CODING", "QUEUED")
    assert asyncio.run(ops.task_result(task_id))["ready"] is False

    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = CodingTaskOutcome(task_id, "READY_FOR_REVIEW", str(checkpoint), ReviewBundle(
        task_id, "lease", "kaggle-han", head, "+" * 7000, "Kite test run: PASSED", 20))
    result = asyncio.run(ops.task_result(task_id))
    assert result["ready"] and "PASSED" in result["summary"]
    assert len(result["patch"]) < 7000 and "còn 1000 ký tự" in result["patch"]
    assert result["apply_with"].startswith(f"git -C {repo} apply ")
    with pytest.raises(KiteToolError):
        asyncio.run(ops.task_status("unknown-task"))


def test_apply_result_applies_the_patch_without_committing(tmp_path):
    from poc.kaggle_worker.backend import task_slug

    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    with pytest.raises(KiteToolError, match="chưa xong"):
        asyncio.run(ops.apply_result(task_id))
    (repo / "calc.py").write_text("x = 2\n")
    patch = subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "calc.py"], check=True)
    patch_path = tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(patch)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = CodingTaskOutcome(task_id, "READY_FOR_REVIEW", str(checkpoint), ReviewBundle(
        task_id, "lease", "kaggle-han", head, patch, "Kite test run: PASSED", 20))
    applied = asyncio.run(ops.apply_result(task_id))
    assert applied["files"] == ["calc.py"] and applied["committed"] is False
    assert (repo / "calc.py").read_text() == "x = 2\n"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip() == head
    with pytest.raises(KiteToolError, match="chưa commit"):
        asyncio.run(ops.apply_result(task_id))


def test_list_tasks_newest_first_and_worker_status(tmp_path):
    repo, _head = make_repo(tmp_path)
    ops = make_ops(tmp_path, FakeClient(), status={"workers": [{"worker_id": "kaggle-han", "alive": True}]})
    first = asyncio.run(ops.start_task(str(repo), "Việc một"))["task_id"]
    asyncio.run(asyncio.sleep(1.1))
    second = asyncio.run(ops.start_task(str(repo), "Việc hai"))["task_id"]
    listed = asyncio.run(ops.list_tasks())
    assert [t["task_id"] for t in listed] == [second, first] and listed[0]["goal"] == "Việc hai"
    assert asyncio.run(ops.worker_status())["workers"][0]["alive"] is True


def test_chat_files_lock_the_lead_agent_down_and_speak_vietnamese(tmp_path):
    files = chat_files(make_config(tmp_path), lead_model="qwen3.6-fast",
                       kite_command=["/k/kite", "--config", "/c.yaml", "mcp", "serve"], allowed_roots=[Path("/home/u")])
    config = json.loads(files["opencode.json"])
    assert config["model"] == "kite/qwen3.6-fast"
    assert config["permission"] == {"edit": "deny", "bash": "deny", "webfetch": "deny", "external_directory": "deny"}
    assert config["mcp"]["kite"]["command"] == ["/k/kite", "--config", "/c.yaml", "mcp", "serve"]
    assert config["provider"]["kite"]["options"]["baseURL"] == "http://127.0.0.1:8787/v1"
    assert "tiếng Việt" in files["AGENTS.md"] and "`/home/u`" in files["AGENTS.md"]


def test_mcp_server_exposes_every_tool(tmp_path):
    pytest.importorskip("mcp")
    from poc.kaggle_worker.mcp_server import build_server

    server = build_server(make_ops(tmp_path, FakeClient()))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"start_task", "task_status", "task_result", "apply_result", "send_instruction", "pause_task",
                     "resume_task", "cancel_task", "list_tasks", "worker_status"}
