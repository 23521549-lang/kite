"""Sandbox tests with a fake Docker: hardening, clone isolation, setup step, patch, verification, config."""

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


def value_after(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


class FakeDocker:
    """Stands in for `docker`: setup may create files, the agent edits the clone, tests return a code."""

    def __init__(self, *, test_returncode: int = 0, setup_returncode: int = 0,
                 agent_timeout: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.test_returncode = test_returncode
        self.setup_returncode = setup_returncode
        self.agent_timeout = agent_timeout

    def kind(self, cmd: list[str]) -> str:
        return value_after(cmd, "--name").split("-")[1]          # kite-<kind>-<task>

    def __call__(self, cmd, *, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:2] == ["docker", "kill"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        workspace = Path(value_after(cmd, "-v").rsplit(":/work", 1)[0])
        kind = self.kind(cmd)
        if kind == "setup":
            (workspace / "generated_by_setup.txt").write_text("artefact\n")
            return subprocess.CompletedProcess(cmd, self.setup_returncode, "installed\n", "")
        if kind == "agent":
            if self.agent_timeout:
                raise subprocess.TimeoutExpired(cmd, timeout, output="partial output")
            (workspace / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) if xs else 0\n")
            (workspace / "__pycache__").mkdir(exist_ok=True)
            (workspace / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"junk")
            return subprocess.CompletedProcess(cmd, 0, "\x1b[0m→ \x1b[0mEdit calc.py\nĐã sửa\n", "")
        if kind == "test":
            output = "2 passed\n" if self.test_returncode == 0 else "1 failed\n"
            return subprocess.CompletedProcess(cmd, self.test_returncode, output, "")
        raise AssertionError(f"unexpected docker call: {cmd}")

    def call(self, kind: str) -> list[str]:
        return next(c for c in self.calls if c[:2] != ["docker", "kill"] and self.kind(c) == kind)


def run_task(tmp_path: Path, fake: FakeDocker, task_id: str, **kwargs):
    repo = make_repo(tmp_path)
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6",
                       models=["qwen3.6", "qwen3.6-fast"], small_model="qwen3.6-fast",
                       state_dir=tmp_path / "state", task_id=task_id, runner=fake,
                       log=lambda _m: None, **kwargs)
    return repo, run


def test_agent_container_is_hardened_and_isolated(tmp_path):
    args = sbx.docker_run_args(SETTINGS, name="kite-agent-t1", workspace=tmp_path,
                               network="kite-sandbox-net", config_file=tmp_path / "opencode.json")
    joined = " ".join(args)
    assert value_after(args, "--network") == "kite-sandbox-net"
    assert value_after(args, "--cap-drop") == "ALL"
    assert "no-new-privileges" in args
    assert value_after(args, "--user") == "1000:1000"
    assert f"{tmp_path.resolve()}:/work" in args
    assert "--privileged" not in args and "/var/lib/kite" not in joined and "docker.sock" not in joined
    assert args[-1] == SETTINGS.image


def test_run_task_collects_patch_and_verifies_with_its_own_test_run(tmp_path):
    fake = FakeDocker()
    repo, run = run_task(tmp_path, fake, "t1")
    assert run.agent_status == "ok" and run.test_exit_code == 0 and run.verified
    assert run.setup_exit_code is None and run.patch_base == run.base_head
    assert run.changed_files == ["calc.py"]
    patch = Path(run.patch_path).read_text()
    assert "if xs else 0" in patch and "__pycache__" not in patch
    assert "- 1" in (repo / "calc.py").read_text()                     # original repo untouched
    assert sbx._git(Path(run.workspace), "remote") == ""               # nothing to push to
    assert "\x1b" not in run.agent_output_tail and "Edit calc.py" in run.agent_output_tail
    agent = fake.call("agent")
    assert value_after(agent, "-m") == "kite/qwen3.6"
    assert value_after(fake.call("test"), "--network") == "none"       # tests run fully offline
    config = json.loads((tmp_path / "state" / "t1" / "opencode.json").read_text())
    assert config["provider"]["kite"]["options"]["baseURL"] == "http://172.19.0.1:8787/v1"
    assert config["permission"]["bash"] == "allow" and config["small_model"] == "kite/qwen3.6-fast"
    assert json.loads((tmp_path / "state" / "t1" / "result.json").read_text())["verified"] is True


def test_setup_runs_first_with_network_and_its_files_stay_out_of_the_patch(tmp_path):
    fake = FakeDocker()
    _repo, run = run_task(tmp_path, fake, "t5", setup_command="pip install -r requirements.txt")
    kinds = [fake.kind(c) for c in fake.calls]
    assert kinds == ["setup", "agent", "test"]
    setup, agent, test = fake.call("setup"), fake.call("agent"), fake.call("test")
    assert value_after(setup, "--network") == "bridge"
    assert value_after(agent, "--network") == "kite-sandbox-net" and value_after(test, "--network") == "none"
    deps = str((tmp_path / "state" / "t5" / "deps").resolve()) + ":/deps"
    for call in (setup, agent, test):
        assert deps in call and "PIP_USER=1" in call and "PYTHONUSERBASE=/deps/python" in call
    assert setup[-3:] == ["sh", "-c", "pip install -r requirements.txt"]
    assert run.setup_exit_code == 0 and run.patch_base != run.base_head and run.verified
    assert run.changed_files == ["calc.py"]                           # setup artefact excluded
    assert "generated_by_setup" not in Path(run.patch_path).read_text()


def test_failed_setup_stops_before_the_agent_uses_any_gpu(tmp_path):
    fake = FakeDocker(setup_returncode=1)
    _repo, run = run_task(tmp_path, fake, "t6", setup_command="pip install nope")
    assert [fake.kind(c) for c in fake.calls] == ["setup"]
    assert run.agent_status == "skipped" and not run.verified
    assert json.loads((tmp_path / "state" / "t6" / "result.json").read_text())["setup_exit_code"] == 1


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
    settings = sandbox_settings_from_config(validate_config(_config(
        gateway_host="172.19.0.1", memory="1g", setup_command="pip install -r requirements.txt")))
    assert settings.gateway_url == "http://172.19.0.1:8787/v1" and settings.memory == "1g"
    assert settings.setup_command == "pip install -r requirements.txt" and settings.setup_network == "bridge"
    for bad in ({"gateway_host": "127.0.0.1"}, {"gateway_host": "8.8.8.8"}, {"network": "host"},
                {"memory": "lots"}, {"docker_command": []}, {"setup_network": "host"},
                {"setup_command": 3}):
        with pytest.raises(ConfigError):
            validate_config(_config(**bad))