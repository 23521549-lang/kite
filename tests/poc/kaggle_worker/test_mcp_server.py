"""Kite MCP tools for the Vietnamese lead agent: fake Temporal client, real store and Git repos."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from poc.kaggle_worker.config import validate_config
from poc.kaggle_worker.integration import ReviewBundle
from poc.kaggle_worker.mcp_server import KiteOps, KiteToolError, chat_files
from poc.kaggle_worker.store import SQLiteSchedulerStore
from poc.kaggle_worker.temporal_workflow import CodingTaskOutcome


def _raw_config(tmp_path: Path) -> dict:
    return {
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
    }


def make_config(tmp_path: Path):
    return validate_config(_raw_config(tmp_path))


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


def finished_task(tmp_path: Path, client: FakeClient, ops: KiteOps, repo: Path, head: str) -> str:
    """A COMPLETED task whose patch changes calc.py and adds notes.md."""
    from poc.kaggle_worker.backend import task_slug

    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    (repo / "calc.py").write_text("x = 2\n")
    (repo / "notes.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    patch = subprocess.run(["git", "-C", str(repo), "diff", "--cached"], capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard"], check=True)
    patch_path = tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(patch)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = CodingTaskOutcome(task_id, "READY_FOR_REVIEW", str(checkpoint), ReviewBundle(
        task_id, "lease", "kaggle-han", head, patch, "Kite test run: PASSED", 20))
    return task_id


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_commit_result_commits_only_the_applied_patch_files(tmp_path):
    repo, head = make_repo(tmp_path)
    for key, value in (("user.name", "t"), ("user.email", "t@e.com")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    with pytest.raises(KiteToolError, match="chưa được áp"):
        asyncio.run(ops.commit_result(task_id, "Add median"))
    asyncio.run(ops.apply_result(task_id))
    (repo / "other.txt").write_text("not part of the patch\n")
    with pytest.raises(KiteToolError, match="trống"):
        asyncio.run(ops.commit_result(task_id, "  "))
    committed = asyncio.run(ops.commit_result(task_id, "Add median"))
    assert committed["committed"] and not committed["pushed"] and sorted(committed["files"]) == ["calc.py", "notes.md"]
    assert git_out(repo, "log", "-1", "--format=%s") == "Add median"
    assert sorted(git_out(repo, "show", "--name-only", "--format=", "HEAD").split()) == ["calc.py", "notes.md"]
    assert git_out(repo, "status", "--short") == "?? other.txt"


def test_commit_result_refuses_when_other_files_are_staged(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    asyncio.run(ops.apply_result(task_id))
    (repo / "staged.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "staged.txt"], check=True)
    with pytest.raises(KiteToolError, match="staged.txt"):
        asyncio.run(ops.commit_result(task_id, "Add median"))
    assert git_out(repo, "rev-parse", "HEAD") == head


def test_repo_status_is_read_only(tmp_path):
    repo, head = make_repo(tmp_path)
    (repo / "calc.py").write_text("x = 3\n")
    status = asyncio.run(make_ops(tmp_path, FakeClient()).repo_status(str(repo)))
    assert status["head"] == head and "calc.py" in status["changes"] and "init" in status["recent_commits"]
    assert (repo / "calc.py").read_text() == "x = 3\n"


def test_run_command_uses_an_offline_container_on_a_copy(tmp_path):
    from poc.kaggle_worker.sandbox import SandboxSettings

    repo, _head = make_repo(tmp_path, "proj")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN")
    (repo / "link").symlink_to(secret)
    (repo / "node_modules").mkdir()
    seen = {}

    def runner(args, *, timeout=None):
        work = Path(args[args.index("-w") - 1].split(":")[0])
        seen.update(args=args, timeout=timeout, files=sorted(p.name for p in work.iterdir()),
                    link_is_link=(work / "link").is_symlink())
        (work / "calc.py").write_text("changed in the container\n")
        return subprocess.CompletedProcess(args, 0, "\x1b[32m1 passed\x1b[0m\n" + "y" * 9000, "")

    ops = KiteOps(make_config(tmp_path), db_path=tmp_path / "db.sqlite3", state_dir=tmp_path / "state",
                  allowed_roots=[tmp_path], runner=runner, sandbox_settings=SandboxSettings(uid=1000))
    result = asyncio.run(ops.run_command(str(repo), "python -m pytest -q", timeout_seconds=99999))
    args = seen["args"]
    assert args[args.index("--network") + 1] == "none" and "--cap-drop" in args
    assert args[-3:] == ["sh", "-c", "python -m pytest -q"] and seen["timeout"] == 600
    assert "node_modules" not in seen["files"] and ".git" in seen["files"] and seen["link_is_link"]
    assert result["exit_code"] == 0 and result["truncated"] and len(result["output"]) == 8000
    assert (repo / "calc.py").read_text() == "x = 1\n"           # the real repository is untouched
    assert list((tmp_path / "state" / "exec").iterdir()) == []    # the copy is deleted
    with pytest.raises(KiteToolError):
        asyncio.run(ops.run_command(str(repo), "   "))
    with pytest.raises(KiteToolError, match="ngoài"):
        asyncio.run(ops.run_command("/etc", "ls"))


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
    permission = config["permission"]
    assert (permission["edit"], permission["webfetch"], permission["external_directory"]) == ("deny",) * 3
    assert permission["bash"]["*"] == "ask"
    assert list(permission["bash"])[0] == "*"            # OpenCode: the last matching rule wins
    for dangerous in ("sudo *", "docker *", "curl *", "git push*", "*secrets*"):
        assert permission["bash"][dangerous] == "deny"
    assert config["mcp"]["kite"]["command"] == ["/k/kite", "--config", "/c.yaml", "mcp", "serve"]
    assert config["provider"]["kite"]["options"]["baseURL"] == "http://127.0.0.1:8787/v1"
    assert "tiếng Việt" in files["AGENTS.md"] and "`/home/u`" in files["AGENTS.md"]


def test_context_window_per_model_reaches_opencode(tmp_path):
    from poc.kaggle_worker.config import ConfigError, model_contexts, sandbox_settings_from_config
    from poc.kaggle_worker.sandbox import agent_config

    raw = _raw_config(tmp_path)
    raw["gateway"]["model_context"] = {"qwen3.6": 65536}
    raw["sandbox"] = {"gateway_host": "172.19.0.1"}
    config = validate_config(raw)
    # Aliases follow their base model; models not listed keep 32768.
    assert model_contexts(config, ["qwen3.6", "devstral", "qwen3.6-fast"]) == {
        "qwen3.6": 65536, "devstral": 32768, "qwen3.6-fast": 65536}
    files = chat_files(config, lead_model="qwen3.6", kite_command=["kite"], allowed_roots=[Path("/home/u")])
    models = json.loads(files["opencode.json"])["provider"]["kite"]["models"]
    assert models["qwen3.6"]["limit"]["context"] == 65536 and models["devstral"]["limit"]["context"] == 32768
    sandbox = agent_config(sandbox_settings_from_config(config), model="qwen3.6", models=["qwen3.6", "devstral"])
    limits = {m: spec["limit"]["context"] for m, spec in sandbox["provider"]["kite"]["models"].items()}
    assert limits == {"qwen3.6": 65536, "devstral": 32768}
    for bad in ({"qwen3.6": 1024}, {"qwen3.6": 10 ** 6}, {"qwen3.6": "64k"}, {"bad name!": 65536}):
        raw["gateway"]["model_context"] = bad
        with pytest.raises(ConfigError):
            validate_config(raw)


def test_mcp_server_exposes_every_tool(tmp_path):
    pytest.importorskip("mcp")
    from poc.kaggle_worker.mcp_server import build_server

    server = build_server(make_ops(tmp_path, FakeClient()))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"start_task", "task_status", "task_result", "apply_result", "commit_result", "repo_status",
                     "run_command", "send_instruction", "pause_task",
                     "resume_task", "cancel_task", "list_tasks", "worker_status"}


def fake_process(proc: Path, pid: int, argv: list[str], namespace: str = "pid:[1]") -> None:
    entry = proc / str(pid)
    (entry / "ns").mkdir(parents=True)
    (entry / "ns" / "pid").symlink_to(namespace)
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def test_reload_stops_the_opencode_daemon_and_old_mcp_servers_only(tmp_path):
    from poc.kaggle_worker.mcp_server import stop_stale_chat_processes

    proc, config = tmp_path / "proc", tmp_path / "config.yaml"
    (proc / "self" / "ns").mkdir(parents=True)
    (proc / "self" / "ns" / "pid").symlink_to("pid:[1]")
    kite = "/home/u/kite/.venv/bin/kite"
    fake_process(proc, 10, ["/home/u/.opencode/bin/opencode", "serve", "--service"])
    fake_process(proc, 11, ["/usr/bin/python", kite, "--config", str(config), "mcp", "serve", "--allow-root", "/home/u"])
    fake_process(proc, 12, ["/usr/bin/python", kite, "--config", "/other.yaml", "mcp", "serve"])  # another config
    fake_process(proc, 13, ["/usr/local/bin/opencode", "run", "-m", "kite/qwen3.6", "fix"])      # TUI-less agent
    fake_process(proc, 14, ["/usr/local/bin/opencode", "serve"], namespace="pid:[999]")         # inside a container
    fake_process(proc, 15, ["/home/u/.opencode/bin/opencode"])                                   # the TUI itself
    killed = []
    stopped = stop_stale_chat_processes(config, proc=proc, uid=os.getuid(), kill=lambda pid, sig: killed.append(pid))
    assert sorted(killed) == [10, 11] and sorted(item["pid"] for item in stopped) == [10, 11]

    def gone(pid, sig):
        raise ProcessLookupError
    assert stop_stale_chat_processes(config, proc=proc, uid=os.getuid(), kill=gone) == []
    assert stop_stale_chat_processes(config, proc=proc, uid=os.getuid() + 1, kill=gone) == []


def test_chat_only_models_never_run_tasks(tmp_path):
    from dataclasses import replace

    from poc.kaggle_worker.config import model_policy_from_config, validate_config
    from poc.kaggle_worker.routing import ModelPolicy

    with pytest.raises(Exception):
        validate_config({**_raw_config(tmp_path), "routing": {"chat_only": "devstral"}})
    config = validate_config({**_raw_config(tmp_path), "routing": {"chat_only": ["devstral", "devstral"]}})
    assert config.routing.chat_only == ("devstral",)
    assert model_policy_from_config(config).excluded == frozenset({"devstral"})
    ops = make_ops(tmp_path, FakeClient())
    ops.config = config
    repo, _head = make_repo(tmp_path)
    assert "devstral" not in ops.base_models()
    with pytest.raises(KiteToolError):
        asyncio.run(ops.start_task(str(repo), "x", model="devstral"))


def test_lead_agent_tool_calls_are_journaled_with_results_and_errors(tmp_path):
    from poc.kaggle_worker.journal import read_jsonl

    repo, _head = make_repo(tmp_path)
    ops = make_ops(tmp_path, FakeClient())
    started = asyncio.run(ops.logged("start_task", ops.start_task, str(repo), "Sửa calc.py", "coding", None))
    with pytest.raises(KiteToolError):
        asyncio.run(ops.logged("commit_result", ops.commit_result, started["task_id"], ""))
    ok, failed = read_jsonl(tmp_path / "state" / "journal" / "actions.jsonl")
    assert ok["tool"] == "start_task" and ok["ok"] and ok["task_id"] == started["task_id"]
    assert ok["args"]["goal"] == "Sửa calc.py" and ok["args"]["task_type"] == "coding"
    assert failed["tool"] == "commit_result" and not failed["ok"] and "trống" in failed["error"]
    assert failed["task_id"] == started["task_id"]


def test_chat_and_sandbox_requests_carry_a_task_tag(tmp_path):
    from poc.kaggle_worker.sandbox import SandboxSettings, agent_config

    files = chat_files(make_config(tmp_path), lead_model="qwen3.6", kite_command=["kite"], allowed_roots=[tmp_path])
    assert json.loads(files["opencode.json"])["provider"]["kite"]["options"]["headers"] == {"X-Kite-Task": "chat"}
    config = agent_config(SandboxSettings(), model="qwen3.6", models=["qwen3.6"], task_tag="chat-1")
    assert config["provider"]["kite"]["options"]["headers"] == {"X-Kite-Task": "chat-1"}
    assert "headers" not in agent_config(SandboxSettings(), model="qwen3.6", models=["qwen3.6"])["provider"]["kite"]["options"]
