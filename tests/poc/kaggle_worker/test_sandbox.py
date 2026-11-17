"""Sandbox tests with a fake Docker: hardening flags, clone isolation, patch, verification, config."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from poc.kaggle_worker import sandbox as sbx
from poc.kaggle_worker.config import ConfigError, sandbox_settings_from_config, validate_config

SETTINGS = sbx.SandboxSettings(uid=1000)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) - 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


class FakeDocker:
    """Stands in for `docker`: the agent edits the mounted clone, the test run returns a code."""

    def __init__(self, *, test_returncode: int = 0, agent_timeout: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.test_returncode = test_returncode
        self.agent_timeout = agent_timeout

    def __call__(self, cmd, *, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:2] == ["docker", "kill"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        workspace = Path(cmd[cmd.index("-v") + 1].rsplit(":/work", 1)[0])
        after_image = cmd[cmd.index(SETTINGS.image) + 1:]
        if after_image[:2] == ["opencode", "run"]:
            if self.agent_timeout:
                raise subprocess.TimeoutExpired(cmd, timeout, output="partial output")
            (workspace / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) if xs else 0\n")
            (workspace / "__pycache__").mkdir(exist_ok=True)
            (workspace / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"junk")
            return subprocess.CompletedProcess(cmd, 0, "Đã sửa calc.py\n", "")
        if after_image[:2] == ["sh", "-c"]:
            output = "2 passed\n" if self.test_returncode == 0 else "1 failed\n"
            return subprocess.CompletedProcess(cmd, self.test_returncode, output, "")
        raise AssertionError(f"unexpected docker call: {cmd}")


def run_task(tmp_path: Path, fake: FakeDocker, task_id: str):
    repo = make_repo(tmp_path)
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6",
                       models=["qwen3.6", "qwen3.6-fast"], small_model="qwen3.6-fast",
                       state_dir=tmp_path / "state", task_id=task_id, runner=fake, log=lambda _m: None)
    return repo, run


def test_agent_container_is_hardened_and_isolated(tmp_path):
    args = sbx.docker_run_args(SETTINGS, name="kite-agent-t1", workspace=tmp_path,
                               network="kite-sandbox-net", config_file=tmp_path / "opencode.json")
    joined = " ".join(args)
    assert args[args.index("--network") + 1] == "kite-sandbox-net"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in args
    assert args[args.index("--user") + 1] == "1000:1000"
    assert f"{tmp_path.resolve()}:/work" in args
    assert "--privileged" not in args and "/var/lib/kite" not in joined and "docker.sock" not in joined
    assert args[-1] == SETTINGS.image


def test_run_task_collects_patch_and_verifies_with_its_own_test_run(tmp_path):
    fake = FakeDocker()
    repo, run = run_task(tmp_path, fake, "t1")
    assert run.agent_status == "ok" and run.test_exit_code == 0 and run.verified
    assert run.changed_files == ["calc.py"]
    patch = Path(run.patch_path).read_text()
    assert "if xs else 0" in patch and "__pycache__" not in patch
    assert "- 1" in (repo / "calc.py").read_text()                     # original repo untouched
    assert sbx._git(Path(run.workspace), "remote") == ""               # nothing to push to
    agent_call = next(c for c in fake.calls if "opencode" in c)
    assert agent_call[agent_call.index("-m") + 1] == "kite/qwen3.6"
    test_call = fake.calls[-1]
    assert test_call[test_call.index("--network") + 1] == "none"       # tests run fully offline
    config = json.loads((tmp_path / "state" / "t1" / "opencode.json").read_text())
    assert config["provider"]["kite"]["options"]["baseURL"] == "http://172.19.0.1:8787/v1"
    assert config["permission"]["bash"] == "allow" and config["small_model"] == "kite/qwen3.6-fast"
    result = json.loads((tmp_path / "state" / "t1" / "result.json").read_text())
    assert result["verified"] is True


def test_failing_tests_are_reported_and_not_verified(tmp_path):
    _repo, run = run_task(tmp_path, FakeDocker(test_returncode=1), "t2")
    assert run.test_exit_code == 1 and not run.verified
    assert "1 failed" in run.test_output_tail


def test_agent_timeout_kills_the_container(tmp_path):
    fake = FakeDocker(agent_timeout=True)
    _repo, run = run_task(tmp_path, fake, "t3")
    assert run.agent_status == "timeout" and not run.verified
    assert ["docker", "kill", "kite-agent-t3"] in fake.calls


def test_dirty_repository_and_bad_ids_are_rejected(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(sbx.SandboxError):
        sbx.run_task(SETTINGS, repo=repo, goal="x", model="qwen3.6", models=["qwen3.6"],
                     state_dir=tmp_path / "state", task_id="Bad_ID", runner=FakeDocker(), log=lambda _m: None)
    (repo / "calc.py").write_text("changed\n")
    with pytest.raises(sbx.SandboxError):
        sbx.run_task(SETTINGS, repo=repo, goal="x", model="qwen3.6", models=["qwen3.6"],
                     state_dir=tmp_path / "state", task_id="t4", runner=FakeDocker(), log=lambda _m: None)


def test_doctor_reports_every_isolation_check(tmp_path):
    def fake(cmd, *, timeout=None):
        cmd = list(cmd)
        if cmd[1:3] == ["version", "--format"]:
            return subprocess.CompletedProcess(cmd, 0, "29.0\n", "")
        if cmd[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        if cmd[1:3] == ["network", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, "true\n", "")
        if cmd[-2:] == ["opencode", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, "2.3.0\n", "")
        if "python" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({
                "gateway": 200, "internet": "URLError", "host_secrets_visible": False, "uid": 1000}) + "\n", "")
        raise AssertionError(cmd)

    checks = sbx.doctor(SETTINGS, runner=fake)
    assert set(checks) == {"docker", "image", "network_internal", "gateway_reachable",
                           "internet_blocked", "host_secrets_hidden", "non_root", "opencode"}
    assert all(check["ok"] for check in checks.values())


def _config(**sandbox):
    return {
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "q"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": [],
        "sandbox": sandbox,
    }


def test_sandbox_config_validation_and_settings():
    settings = sandbox_settings_from_config(validate_config(_config(gateway_host="172.19.0.1", memory="1g")))
    assert settings.gateway_url == "http://172.19.0.1:8787/v1" and settings.memory == "1g"
    for bad in ({"gateway_host": "127.0.0.1"}, {"gateway_host": "8.8.8.8"}, {"network": "host"},
                {"memory": "lots"}, {"docker_command": []}):
        with pytest.raises(ConfigError):
            validate_config(_config(**bad))