"""Bench network mode (Gói 7, worker (c)): tier-2 egress proxy + per-task plumbing.

* The proxy forwards only allowlisted hosts (unit + live proxy subprocess, no Docker).
* The proxy container joins the internal network at start and the bridge only via
  ``docker network connect`` (fake runner records the exact commands).
* The sandbox plumbing hands the proxy to the agent phase only, as a per-task copy
  (``dataclasses.replace``): test/check keep ``network="none"`` and no proxy env, and
  the next ordinary task after a networked bench stays offline.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from poc.kaggle_worker import egress_proxy
from poc.kaggle_worker import sandbox as sbx
from poc.kaggle_worker.egress_proxy import EgressProxyError, is_allowed_host, start_egress_proxy


# ------------------------------------------------------------------ allowlist rule
@pytest.mark.parametrize("host,allow,expected", [
    ("api.example.com", ["api.example.com"], True),
    ("API.EXAMPLE.COM", ["api.example.com"], True),
    ("api.example.com:443", ["api.example.com"], True),
    ("sub.api.example.com", ["api.example.com"], False),   # no wildcard, no subdomain
    ("api.example.com.evil.com", ["api.example.com"], False),
    ("other.example.com", ["api.example.com"], False),
    ("", ["api.example.com"], False),
    (None, ["api.example.com"], False),
])
def test_is_allowed_host_is_an_exact_match(host, allow, expected):
    assert is_allowed_host(host, allow) is expected


def test_proxy_env_keeps_gateway_traffic_off_the_proxy():
    env = dict(sbx.proxy_env("http://egress-proxy:3128", "http://172.19.0.1:8787/v1"))
    assert env["HTTP_PROXY"] == "http://egress-proxy:3128"
    assert env["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert "172.19.0.1" in env["NO_PROXY"] and "127.0.0.1" in env["NO_PROXY"]


# ------------------------------------------------------------------ proxy lifecycle (fake Docker)
class FakeRunner:
    def __init__(self, *, connect_ok: bool = True, run_ok: bool = True):
        self.calls: list[list[str]] = []
        self.connect_ok, self.run_ok = connect_ok, run_ok

    def __call__(self, cmd, *, timeout=None):
        self.calls.append(list(cmd))
        if cmd[1:3] == ["network", "connect"]:
            code = 0 if self.connect_ok else 1
            return subprocess.CompletedProcess(cmd, code, "", "no such network" if code else "")
        if cmd[1:3] == ["run", "-d"]:
            return subprocess.CompletedProcess(cmd, 0 if self.run_ok else 1, "abc123\n",
                                               "" if self.run_ok else "cannot start")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_start_egress_proxy_joins_internal_first_then_bridge(tmp_path):
    fake = FakeRunner()
    settings = sbx.SandboxSettings(uid=1000)
    proxy = start_egress_proxy(state_dir=tmp_path, settings=settings,
                               hosts=["api.example.com", "pypi.org"], run_id="bench-1", runner=fake)
    assert proxy.proxy_url == "http://egress-proxy:3128"
    assert proxy.name == "kite-egress-bench-1"
    assert (proxy.run_dir / "proxy.py").read_text(encoding="utf-8")
    assert json.loads((proxy.run_dir / "allowlist.json").read_text(encoding="utf-8")) == [
        "api.example.com", "pypi.org"]
    assert proxy.log_path.exists()
    run_cmd, connect_cmd = fake.calls[0], fake.calls[1]
    assert run_cmd[1:4] == ["run", "-d", "--rm"]
    assert run_cmd[run_cmd.index("--network") + 1] == "kite-sandbox-net"
    assert "bridge" not in run_cmd                                    # tier 1 is out
    assert run_cmd[run_cmd.index("--network-alias") + 1] == "egress-proxy"
    assert run_cmd[-1] == "/proxy/log/egress.log" and settings.image in run_cmd
    assert connect_cmd == ["docker", "network", "connect", "bridge", "kite-egress-bench-1"]
    proxy.stop()
    assert fake.calls[-1] == ["docker", "rm", "-f", "kite-egress-bench-1"]


def test_start_egress_proxy_refuses_an_invalid_allowlist(tmp_path):
    with pytest.raises(EgressProxyError):
        start_egress_proxy(state_dir=tmp_path, settings=sbx.SandboxSettings(uid=1000),
                           hosts=["*.example.com"], run_id="bench-1", runner=FakeRunner())
    with pytest.raises(EgressProxyError):
        start_egress_proxy(state_dir=tmp_path, settings=sbx.SandboxSettings(uid=1000),
                           hosts=["1.2.3.4"], run_id="bench-1", runner=FakeRunner())
    with pytest.raises(EgressProxyError):
        start_egress_proxy(state_dir=tmp_path, settings=sbx.SandboxSettings(uid=1000),
                           hosts=[], run_id="bench-1", runner=FakeRunner())


def test_start_egress_proxy_cleans_up_when_bridge_connect_fails(tmp_path):
    fake = FakeRunner(connect_ok=False)
    with pytest.raises(EgressProxyError, match="bridge"):
        start_egress_proxy(state_dir=tmp_path, settings=sbx.SandboxSettings(uid=1000),
                           hosts=["api.example.com"], run_id="bench-1", runner=fake)
    assert fake.calls[-1] == ["docker", "rm", "-f", "kite-egress-bench-1"]


# ------------------------------------------------------------------ live proxy (no Docker)
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, *_args):
        pass


def _wait_listening(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"nothing listening on {port}")


def test_proxy_script_forwards_only_allowlisted_hosts(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hello)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    srv_port = server.server_address[1]
    script = tmp_path / "proxy.py"
    script.write_text(egress_proxy.PROXY_SCRIPT, encoding="utf-8")
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps(["localhost"]), encoding="utf-8")
    log = tmp_path / "egress.log"
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(script), "--port", str(port),
                             "--allowlist", str(allowlist), "--log", str(log)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_listening(port)

        def request(raw: bytes) -> bytes:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                sock.sendall(raw)
                sock.settimeout(10)
                chunks = []
                try:
                    while True:
                        data = sock.recv(65536)
                        if not data:
                            break
                        chunks.append(data)
                except socket.timeout:
                    pass
                return b"".join(chunks)

        # CONNECT tunnel to the allowed host: the local server answers through it.
        tunneled = request(f"CONNECT localhost:{srv_port} HTTP/1.1\r\n"
                           f"Host: localhost:{srv_port}\r\n\r\n"
                           "GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
        assert b"200 Connection Established" in tunneled and b"hello" in tunneled
        # Absolute-form HTTP to the allowed host: forwarded as well.
        plain = request(f"GET http://localhost:{srv_port}/ HTTP/1.1\r\n"
                        "Host: localhost\r\nConnection: close\r\n\r\n".encode())
        assert b"200 OK" in plain and b"hello" in plain
        # A host outside the allowlist is refused without any upstream connection.
        denied = request(b"CONNECT not-allowed.example:443 HTTP/1.1\r\nHost: not-allowed.example\r\n\r\n")
        assert b"403 Forbidden" in denied
        assert b"hello" not in denied
        logged = log.read_text(encoding="utf-8")
        assert "ALLOW localhost" in logged and "DENY not-allowed.example" in logged
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server.shutdown()


# ------------------------------------------------------------------ sandbox plumbing (fake Docker)
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


def _make_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) - 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _env(cmd: list[str], key: str) -> str | None:
    for i, item in enumerate(cmd):
        if item == "-e" and cmd[i + 1].startswith(f"{key}="):
            return cmd[i + 1].split("=", 1)[1]
    return None


class _RecordingDocker:
    """Every container call succeeds; the agent fixes the fixture; calls are recorded."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:2] == ["docker", "kill"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        name = cmd[cmd.index("--name") + 1]
        kind = name.split("-")[1]
        workspace = Path(cmd[cmd.index("-v") + 1].rsplit(":/work", 1)[0])
        if kind == "agent":
            (workspace / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) if xs else 0\n")
        output = "1 passed\n" if kind == "test" else ""
        return subprocess.CompletedProcess(cmd, 0, output, "")

    def calls_of(self, kind: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "docker" and "--name" in c
                and c[c.index("--name") + 1].startswith(f"kite-{kind}-")]


_SETTINGS = sbx.SandboxSettings(uid=1000, fast_path=False, self_review=False,
                                code_map=False, quick_tests=False)


def _run(tmp_path: Path, fake: _RecordingDocker, task_id: str = "sbx-1", **kwargs):
    repo = _make_repo(tmp_path / task_id)
    return sbx.run_task(_SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6",
                        models=["qwen3.6"], state_dir=tmp_path / "state", task_id=task_id,
                        runner=fake, log=lambda _m: None, **kwargs)


def test_agent_phase_gets_the_proxy_and_test_phase_does_not(tmp_path):
    fake = _RecordingDocker()
    _run(tmp_path, fake, egress_proxy_url="http://egress-proxy:3128")
    agent = fake.calls_of("agent")
    assert agent, "agent container never ran"
    assert _env(agent[0], "HTTP_PROXY") == "http://egress-proxy:3128"
    assert _env(agent[0], "HTTPS_PROXY") == "http://egress-proxy:3128"
    assert "172.19.0.1" in (_env(agent[0], "NO_PROXY") or "")
    assert agent[0][agent[0].index("--network") + 1] == "kite-sandbox-net"   # still internal
    for test_cmd in fake.calls_of("test"):
        assert test_cmd[test_cmd.index("--network") + 1] == "none"
        assert _env(test_cmd, "HTTP_PROXY") is None


def test_ordinary_task_after_a_networked_bench_stays_offline(tmp_path):
    fake = _RecordingDocker()
    _run(tmp_path, fake, "sbx-1", egress_proxy_url="http://egress-proxy:3128")
    assert _SETTINGS.egress_proxy_url == ""                    # the shared settings were not mutated
    fake.calls.clear()
    _run(tmp_path, fake, "sbx-2")                              # the very next task, no override
    assert all(_env(cmd, "HTTP_PROXY") is None for cmd in fake.calls)
    assert all(_env(cmd, "HTTPS_PROXY") is None for cmd in fake.calls)


# ------------------------------------------------------------------ network approval (mảnh 4)
def _network_task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / "bench-tasks" / "t1-net"
    (task_dir / "files").mkdir(parents=True)
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "files" / "app.py").write_text("x = 1\n")
    (task_dir / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    (task_dir / "task.yaml").write_text(
        "id: t1-net\ngoal: Gọi api.example.com\nkind: code\nlanguage: vi\naxes: [Coding]\n"
        "difficulty: dễ\ntimeouts: {setup: 60, test: 60}\n"
        "environment:\n  network: {hosts: [api.example.com]}\n", encoding="utf-8")
    return task_dir


def test_declared_network_hosts_reads_task_yaml(tmp_path):
    from poc.kaggle_worker.mcp_server import declared_network_hosts

    _network_task_dir(tmp_path)
    assert declared_network_hosts(tmp_path / "state", tmp_path / "bench-tasks") == {
        "t1-net": ["api.example.com"]}
    assert declared_network_hosts(tmp_path / "state") == {}    # no <state>/bench-tasks yet


def test_check_network_request_needs_ack_and_a_declared_host(tmp_path):
    from poc.kaggle_worker.mcp_server import check_network_request

    state = tmp_path / "state"
    assert check_network_request(state_dir=state, hosts=[], ack=False) == {"mode": "offline", "hosts": []}
    with pytest.raises(ValueError, match="xác nhận"):
        check_network_request(state_dir=state, hosts=["api.example.com"], ack=False)
    with pytest.raises(ValueError, match="khai báo"):
        check_network_request(state_dir=state, hosts=["evil.example.com"], ack=True,
                              bench_root=tmp_path / "bench-tasks",
                              declared={"t1-net": ["api.example.com"]})
    _network_task_dir(tmp_path)
    stamp = check_network_request(state_dir=state, hosts=["api.example.com"], ack=True,
                                  bench_root=tmp_path / "bench-tasks")
    assert stamp == {"mode": "allowlist", "hosts": ["api.example.com"]}
    with pytest.raises(ValueError, match="wildcard|\\*"):
        check_network_request(state_dir=state, hosts=["*.example.com"], ack=True)


# ------------------------------------------------------------------ v1 hidden tests (worker (b) follow-up)
def test_v1_hidden_tests_come_from_the_task_folder_not_from_git(tmp_path):
    from poc.kaggle_worker import bench, bench_loader
    from poc.kaggle_worker.cli import v1_hidden_test_files

    task_dir = tmp_path / "bench-tasks" / "r1"
    (task_dir / "tests" / "tests").mkdir(parents=True)
    (task_dir / "files").mkdir(parents=True)
    (task_dir / "files" / "calc.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n")
    (task_dir / "tests" / "tests" / "test_median.py").write_text("def test_median(): pass\n")
    (task_dir / "task.yaml").write_text(
        "id: r1\ngoal: Thêm median\nkind: repo\nlanguage: en\naxes: [Coding, Đúng việc]\n"
        "difficulty: dễ\ntimeouts: {setup: 600, test: 300}\nenvironment: {}\n"
        "repo: sample/calc\nbase_commit: abc1234\nexpected_files: [calc.py]\n", encoding="utf-8")
    bench_task = bench_loader.load_task_or_raise(task_dir)
    repo_task = bench.task_to_repo_task(bench_task, task_dir)
    assert repo_task.fix == repo_task.base == "abc1234"       # the v1 placeholder
    files = v1_hidden_test_files(repo_task, {"r1": bench_task})
    assert files == {"tests/test_median.py": "def test_median(): pass\n"}
    # The old path would fail here: `git show abc1234:tests/test_median.py` -- the fix
    # added that file, so it does not exist at the base commit. That is why the branch
    # above exists; prove it instead of assuming it.
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    with pytest.raises(subprocess.CalledProcessError):
        bench.hidden_files(repo_task, repo)
    # Non-v1 tasks keep the old path: a legacy RepoTask and a CodeTask both give None.
    legacy = bench.RepoTask("R1", "base0001", "fix0002", ("tests/test_x.py",), "goal")
    assert v1_hidden_test_files(legacy, {"r1": bench_task}) is None
    code_task = bench.CodeTask("c1", "goal", {}, {}, hidden={"test_a.py": "x"})
    assert v1_hidden_test_files(code_task, {"r1": bench_task}) is None


# ------------------------------------------------------------------ bench_start network approval
def _ops_config(tmp_path: Path) -> dict:
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


class _FakeClient:
    async def start_workflow(self, *_args, **_kwargs):
        raise AssertionError("no task should start here")


def _make_ops(tmp_path: Path):
    from poc.kaggle_worker.config import validate_config
    from poc.kaggle_worker.mcp_server import KiteOps

    spawned: list[list[str]] = []

    async def connect(_endpoint):
        return _FakeClient()

    ops = KiteOps(validate_config(_ops_config(tmp_path)),
                  db_path=tmp_path / "state" / "scheduler.sqlite3",
                  state_dir=tmp_path / "state", allowed_roots=[tmp_path], connect=connect,
                  gateway_status=lambda: {"workers": []},
                  spawn=lambda cmd, log: spawned.append(cmd))
    return ops, spawned


def test_bench_start_network_needs_owner_ack_and_a_declared_host(tmp_path):
    import asyncio

    from poc.kaggle_worker.mcp_server import KiteToolError

    ops, spawned = _make_ops(tmp_path)
    _network_task_dir(tmp_path)
    bench_root = tmp_path / "bench-tasks"
    with pytest.raises(KiteToolError, match="xác nhận"):
        asyncio.run(ops.bench_start(confirm=True, label="n1", network_hosts=["api.example.com"],
                                    bench_root=str(bench_root)))
    with pytest.raises(KiteToolError, match="khai báo"):
        asyncio.run(ops.bench_start(confirm=True, label="n1", network_hosts=["evil.example.com"],
                                    network_ack=True, bench_root=str(bench_root)))
    started = asyncio.run(ops.logged(
        "bench_start", ops.bench_start, confirm=True, label="n1",
        network_hosts=["api.example.com"], network_ack=True, bench_root=str(bench_root)))
    assert started["started"] is True
    assert started["network"] == {"mode": "allowlist", "hosts": ["api.example.com"]}
    assert started["gate_set"] is None                    # a networked run never joins the gate set
    assert "--network-hosts" in spawned[0] and "api.example.com" in spawned[0]
    assert "--network-ack" in spawned[0]
    journal = ops.journal.read("actions")                 # the approval decision is journaled
    assert any(r.get("tool") == "bench_start"
               and (r.get("args") or {}).get("network_ack") is True for r in journal)


def test_bench_start_offline_stays_in_the_gate_set(tmp_path):
    import asyncio

    ops, spawned = _make_ops(tmp_path)
    started = asyncio.run(ops.bench_start(confirm=True, label="o1"))
    assert started["network"] == {"mode": "offline", "hosts": []}
    assert started["gate_set"] == "gate-v1"
    assert "--network-hosts" not in spawned[0]


# ------------------------------------------------------------------ criteria stamp (mảnh 4)
def _v1_task(tmp_path: Path, task_id: str, kind: str, axes, **extra) -> object:
    from poc.kaggle_worker import bench_loader

    task_dir = tmp_path / "bench-tasks" / task_id
    (task_dir / "files").mkdir(parents=True)
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "files" / "app.py").write_text("x = 1\n")
    (task_dir / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    lines = [f"id: {task_id}", "goal: Bài kiểm tra", f"kind: {kind}", "language: vi",
             f"axes: [{', '.join(axes)}]", "difficulty: dễ", "timeouts: {setup: 60, test: 60}",
             "environment: {}"]
    for key, value in extra.items():
        lines.append(f"{key}: {value}")
    (task_dir / "task.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bench_loader.load_task_or_raise(task_dir)


def _criteria_results() -> dict:
    return {"started": "2026-11-25 21:00", "models": {"qwen3.6": {"code": [], "v1": [
        {"id": "t1", "kind": "code", "axes": ["Coding", "Tốc độ"], "passed": True,
         "wall_seconds": 50.0, "gpu_seconds": 120.0, "task_id": "bench-qwen36-t1-000000"},
        {"id": "t2", "kind": "code", "axes": ["Phân tích", "Đúng việc"], "passed": True,
         "wall_seconds": 60.0, "gpu_seconds": 60.0, "task_id": "bench-qwen36-t2-000000"},
        {"id": "s1", "kind": "scenario", "axes": ["Suy luận"], "passed": True,
         "score": 0.8, "score_100": 80.0},
        {"id": "t4", "kind": "code", "axes": ["Trí nhớ"], "passed": True,
         "wall_seconds": 40.0, "gpu_seconds": 60.0, "task_id": "bench-qwen36-t4-000000"},
    ]}}}


def test_write_bench_criteria_stamps_overall_and_excludes_network_runs(tmp_path):
    from poc.kaggle_worker import bench_criteria as bc
    from poc.kaggle_worker.cli import write_bench_criteria

    state = tmp_path / "state"
    v1_tasks = {
        "t1": _v1_task(tmp_path, "t1", "code", ["Coding", "Tốc độ"], par_seconds=100),
        "t2": _v1_task(tmp_path, "t2", "code", ["Phân tích", "Đúng việc"],
                       expected_files="[app.py]"),
        "s1": _v1_task(tmp_path, "s1", "scenario", ["Suy luận"],
                       scenario='{prompt: "Trả lời.", expect: []}'),
        "t4": _v1_task(tmp_path, "t4", "code", ["Trí nhớ"], lesson_ref="lessons/x.md#y"),
    }
    out1 = state / "bench" / "run1"
    out1.mkdir(parents=True)
    first = write_bench_criteria(_criteria_results(), out1, state_dir=state,
                                 network={"mode": "offline", "hosts": []}, gate_set="gate-v1",
                                 v1_tasks=v1_tasks)
    record = first["runs"][0]
    assert record["criteria_version"] == "v1" and record["gate_set"] == "gate-v1"
    assert record["network"] == {"mode": "offline", "hosts": []}
    assert record["axes"]["Coding"] == 100.0 and record["axes"]["Tốc độ"] == 100.0
    assert record["axes"]["Suy luận"] == 80.0 and record["axes"]["Trí nhớ"] == 100.0
    assert record["axes"]["Ổn định"] == 100.0
    assert record["axes"]["Chi phí"] is None          # first run of the gate set: no baseline
    assert record["overall"] is None                  # ...so no overall yet (spec §6.2)
    assert record["cost_basis_gpu_minutes_median"] == 1.0
    # One task per axis here: no standard error yet; Ổn định has its three sandbox tasks.
    assert record["axes_se"]["Coding"] is None and record["axes_se"]["Ổn định"] == 0.0
    assert first["official"]["official"] is False     # fewer than 3 runs
    # Second run: the first run is the cost baseline (1.0 min/task vs 1.0) -> Chi phí 100.
    out2 = state / "bench" / "run2"
    out2.mkdir(parents=True)
    second = write_bench_criteria(_criteria_results(), out2, state_dir=state,
                                  network={"mode": "offline", "hosts": []}, gate_set="gate-v1",
                                  v1_tasks=v1_tasks)
    record2 = second["runs"][0]
    assert record2["axes"]["Chi phí"] == 100.0
    assert record2["overall"] == 97.5
    assert record2["level"]["level"] == 5 and record2["level"]["name_vi"] == "Chuyên gia"
    # Only runs with an overall count: run 1 has none (no cost baseline yet).
    assert second["official"]["runs"] == 1 and second["official"]["overall"] == 97.5
    # A networked run is stamped allowlist + gate_set None and never joins the official set.
    out3 = state / "bench" / "run3"
    out3.mkdir(parents=True)
    third = write_bench_criteria(_criteria_results(), out3, state_dir=state,
                                 network={"mode": "allowlist", "hosts": ["api.example.com"]},
                                 gate_set=None, v1_tasks=v1_tasks)
    record3 = third["runs"][0]
    assert record3["network"] == {"mode": "allowlist", "hosts": ["api.example.com"]}
    assert record3["gate_set"] is None and record3["level_unstable"] is None
    assert third["official"]["runs"] == 1       # the networked run did not join
    assert bc.radar_data(record2["axes"])[0] == {"axis": "Coding", "key": "coding", "score": 100.0}
