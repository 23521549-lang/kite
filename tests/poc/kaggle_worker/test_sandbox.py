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
                 agent_timeout: bool = False, test_returncodes: list[int] | None = None,
                 review: str = "none", quick_returncodes: list[int] | None = None,
                 check_returncodes: list[int] | None = None, repro_returncodes: list[int] | None = None,
                 quick_fail: str = "FAILED test_calc.py::test_empty - ZeroDivisionError\n1 failed\n") -> None:
        self.calls: list[list[str]] = []
        self.test_returncode = test_returncode
        self.test_returncodes = list(test_returncodes or [])     # one per test run, then test_returncode
        self.setup_returncode = setup_returncode
        self.agent_timeout = agent_timeout
        self.review = review          # what the self-review pass does: none | improve | break
        self.quick_returncodes = list(quick_returncodes or [])
        self.check_returncodes = list(check_returncodes or [])
        self.repro_returncodes = list(repro_returncodes or [])
        self.quick_fail = quick_fail

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
        if kind == "agent" or kind.startswith("fix"):
            if self.agent_timeout:
                raise subprocess.TimeoutExpired(cmd, timeout, output="partial output")
            (workspace / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) if xs else 0\n")
            (workspace / "__pycache__").mkdir(exist_ok=True)
            (workspace / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"junk")
            return subprocess.CompletedProcess(cmd, 0, "\x1b[0m→ \x1b[0mEdit calc.py\nĐã sửa\n", "")
        if kind == "review":
            if self.review == "improve":
                (workspace / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) if xs else 0.0\n")
                (workspace / "test_edge.py").write_text("def test_new_edge():\n    pass\n")
            elif self.review == "break":
                (workspace / "calc.py").write_text("def average(xs):\n    return 1 / 0\n")
                (workspace / "scratch_review.py").write_text("print('tmp')\n")
            return subprocess.CompletedProcess(cmd, 0, "Đã rà soát\n", "")
        if kind == "quick":
            code = self.quick_returncodes.pop(0) if self.quick_returncodes else 0
            output = "1 passed\n" if code == 0 else self.quick_fail
            return subprocess.CompletedProcess(cmd, code, output, "")
        if kind == "repro":                     # the fast path's reproduction test, on the code before the fix
            code = self.repro_returncodes.pop(0) if self.repro_returncodes else 1
            output = "1 passed\n" if code == 0 else "FAILED test_kite_repro.py::test_empty_list - ZeroDivisionError\n"
            return subprocess.CompletedProcess(cmd, code, output, "")
        if kind == "test":
            code = self.test_returncodes.pop(0) if self.test_returncodes else self.test_returncode
            output = "2 passed\n" if code == 0 else "FAILED test_calc.py::test_empty - ZeroDivisionError\n1 failed\n"
            return subprocess.CompletedProcess(cmd, code, output, "")
        if kind == "check":
            code = self.check_returncodes.pop(0) if self.check_returncodes else 0
            output = "" if code == 0 else "calc.py:2:12: F821 Undefined name `total`\nFound 1 error.\n"
            return subprocess.CompletedProcess(cmd, code, output, "")
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
    assert kinds == ["setup", "agent", "test", "review"]
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
    fake = FakeDocker(test_returncode=1)
    _repo, run = run_task(tmp_path, fake, "t2")
    assert run.test_exit_code == 1 and not run.verified
    assert "1 failed" in run.test_output_tail
    # Every fix round was used: agent, fix1, fix2, each followed by a test run.
    kinds = [fake.kind(c) for c in fake.calls if c[:2] != ["docker", "kill"]]
    assert kinds == ["agent", "test", "fix1", "test", "fix2", "test"] and run.fix_rounds_used == 2


def test_failing_tests_go_back_to_the_agent_until_they_pass(tmp_path):
    fake = FakeDocker(test_returncodes=[1, 0])
    _repo, run = run_task(tmp_path, fake, "t9")
    assert run.verified and run.fix_rounds_used == 1 and run.test_exit_code == 0
    fix_prompt = fake.call("fix1")[-1]
    assert "THẤT BẠI" in fix_prompt and "ZeroDivisionError" in fix_prompt and "vòng sửa 1/2" in fix_prompt
    first_prompt = fake.call("agent")[-1]
    assert "Cách làm:" in first_prompt and "calc.py" in first_prompt     # guide + repo context
    log = (tmp_path / "state" / "t9" / "test.log").read_text()
    assert "after agent exit=1" in log and "after fix1 exit=0" in log


def test_no_fix_rounds_when_disabled_or_when_the_agent_failed(tmp_path):
    from dataclasses import replace
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    fake = FakeDocker(test_returncode=1)
    sbx.run_task(replace(SETTINGS, fix_rounds=0), repo=make_repo(tmp_path / "a"), goal="Sửa average",
                 model="qwen3.6", models=["qwen3.6"], state_dir=tmp_path / "state", task_id="t10",
                 runner=fake, log=lambda _m: None)
    assert [fake.kind(c) for c in fake.calls] == ["agent", "test"]
    timeout = FakeDocker(agent_timeout=True, test_returncode=1)
    run = sbx.run_task(SETTINGS, repo=make_repo(tmp_path / "b"), goal="Sửa average", model="qwen3.6",
                       models=["qwen3.6"], state_dir=tmp_path / "state", task_id="t11", runner=timeout,
                       log=lambda _m: None)
    assert run.fix_rounds_used == 0 and run.agent_status == "timeout"


def test_self_review_runs_only_after_tests_pass_and_unchanged_needs_no_retest(tmp_path):
    fake = FakeDocker()
    _repo, run = run_task(tmp_path, fake, "t13")
    assert [fake.kind(c) for c in fake.calls] == ["agent", "test", "review"] and run.review == "unchanged"
    prompt = fake.call("review")[-1]
    assert "RÀ SOÁT" in prompt and "Sửa average" in prompt and "python -m pytest -q" in prompt
    assert value_after(fake.call("review"), "--network") == "kite-sandbox-net"
    failing = FakeDocker(test_returncode=1)
    (tmp_path / "x").mkdir()
    run_task(tmp_path / "x", failing, "t14")
    assert "review" not in [failing.kind(c) for c in failing.calls]           # tests never passed


def test_self_review_changes_are_kept_when_tests_still_pass(tmp_path):
    fake = FakeDocker(review="improve")
    _repo, run = run_task(tmp_path, fake, "t15")
    # The review added test_edge.py: it runs first (quick), then the full suite.
    assert [fake.kind(c) for c in fake.calls] == ["agent", "test", "review", "quick", "test"]
    assert run.review == "kept" and run.verified and (run.quick_test_runs, run.full_test_runs) == (1, 2)
    assert sorted(run.changed_files) == ["calc.py", "test_edge.py"]
    assert "else 0.0" in Path(run.patch_path).read_text()


def test_self_review_that_breaks_tests_is_rolled_back(tmp_path):
    fake = FakeDocker(review="break", test_returncodes=[0, 1])
    _repo, run = run_task(tmp_path, fake, "t16")
    assert run.review == "reverted" and run.verified and run.test_exit_code == 0
    assert run.changed_files == ["calc.py"]
    patch = Path(run.patch_path).read_text()
    assert "if xs else 0" in patch and "1 / 0" not in patch and "scratch_review" not in patch
    assert "after review exit=1" in (tmp_path / "state" / "t16" / "test.log").read_text()


def test_self_review_off_for_non_coding_tasks_and_when_disabled(tmp_path):
    from dataclasses import replace
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
    fake = FakeDocker(review="improve")
    sbx.run_task(SETTINGS, repo=make_repo(tmp_path / "a"), goal="Phân tích calc.py", model="qwen3.6",
                 models=["qwen3.6"], state_dir=tmp_path / "state", task_id="t17", task_type="analysis",
                 runner=fake, log=lambda _m: None)
    off = FakeDocker(review="improve")
    run = sbx.run_task(replace(SETTINGS, self_review=False), repo=make_repo(tmp_path / "b"), goal="Sửa average",
                       model="qwen3.6", models=["qwen3.6"], state_dir=tmp_path / "state", task_id="t18",
                       runner=off, log=lambda _m: None)
    assert "review" not in [fake.kind(c) for c in fake.calls] + [off.kind(c) for c in off.calls]
    assert run.review == "skipped"


def test_run_without_context_for_the_baseline(tmp_path):
    (tmp_path / "a").mkdir()
    fake = FakeDocker()
    sbx.run_task(SETTINGS, repo=make_repo(tmp_path / "a"), goal="Sửa average trong calc.py", model="qwen3.6",
                 models=["qwen3.6"], state_dir=tmp_path / "state", task_id="t12", with_context=False,
                 runner=fake, log=lambda _m: None)
    assert "Ngữ cảnh repo" not in fake.call("agent")[-1]


def test_repo_context_lists_files_and_shows_the_ones_the_goal_names(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "README.md").write_text("# Calc\nTiny calculator.\n")
    (repo / "other.py").write_text("SECRET_LOOKING = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "more")
    context = sbx.repo_context(repo, "Sửa hàm average trong calc.py")
    assert "calc.py" in context and "return sum(xs)" in context and "Tiny calculator" in context
    assert "other.py" in context and "SECRET_LOOKING" not in context      # listed, not shown
    assert len(sbx.repo_context(repo, "x", max_chars=10)) < 400
    prompt = sbx.agent_prompt("Phân tích calc.py", "", task_type="analysis", context=context)
    assert "file:dòng" in prompt and "Kiểm tra lại bằng lệnh" not in prompt


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
                {"setup_command": 3}, {"self_review": "yes"}, {"deps_cache": "on"}, {"quick_tests": 1}):
        with pytest.raises(ConfigError):
            validate_config(_config(**bad))


def test_require_worker_fails_fast_without_a_live_worker():
    seen = []
    status = {"workers": [{"worker_id": "kaggle-han", "alive": False, "draining": False,
                           "models": ["qwen3.6", "devstral"]}]}

    def fetch(url):
        seen.append(url)
        return status

    with pytest.raises(sbx.SandboxError, match="no live worker"):
        sbx.require_worker(SETTINGS, "qwen3.6", fetch=fetch)
    assert seen[0] == "http://172.19.0.1:8787/status"
    status["workers"][0]["alive"] = True
    assert sbx.require_worker(SETTINGS, "qwen3.6", fetch=fetch)["worker_id"] == "kaggle-han"
    with pytest.raises(sbx.SandboxError):
        sbx.require_worker(SETTINGS, "gemma4", fetch=fetch)      # model nobody serves
    status["workers"][0]["draining"] = True
    with pytest.raises(sbx.SandboxError):
        sbx.require_worker(SETTINGS, "qwen3.6", fetch=fetch)     # worker is shutting down

    def unreachable(url):
        raise OSError("connection refused")

    with pytest.raises(sbx.SandboxError, match="cannot reach the gateway"):
        sbx.require_worker(SETTINGS, "qwen3.6", fetch=unreachable)


def test_a_repository_declares_its_own_setup_and_test_commands(tmp_path):
    from dataclasses import replace

    work = tmp_path / "w"
    work.mkdir()
    assert sbx.repo_settings(SETTINGS, work) is SETTINGS                 # no .kite.yml: operator defaults
    (work / ".kite.yml").write_text("setup: pip install -r svc/requirements.txt\n"
                                    "test: PYTHONPATH=. python -m pytest -q svc/tests\ntest_timeout: 900\n")
    mine = sbx.repo_settings(SETTINGS, work)
    assert (mine.setup_command, mine.test_command, mine.test_timeout_seconds) == (
        "pip install -r svc/requirements.txt", "PYTHONPATH=. python -m pytest -q svc/tests", 900)
    assert mine.setup_timeout_seconds == SETTINGS.setup_timeout_seconds
    assert sbx.repo_settings(replace(SETTINGS, repo_config=False), work).test_command == SETTINGS.test_command
    for bad in ("test: [1, 2]\n", "test_timeout: 5\n", "network: bridge\n", "test: 'unterminated\n", "- x\n"):
        (work / ".kite.yml").write_text(bad)
        with pytest.raises(sbx.SandboxError):
            sbx.repo_settings(SETTINGS, work)


def test_run_task_uses_the_repository_test_command(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".kite.yml").write_text("test: python -m pytest -q -k average\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "kite config")
    fake = FakeDocker()
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="t20", runner=fake, log=lambda _m: None)
    assert fake.call("test")[-1] == "python -m pytest -q -k average"
    assert run.test_command == "python -m pytest -q -k average"
    assert "python -m pytest -q -k average" in fake.call("agent")[-1]   # the agent is told the same command


class DepsOnlyDocker(FakeDocker):
    """Setup installs into /deps only (like `pip install -r requirements.txt`), never into the clone."""

    def __call__(self, cmd, *, timeout=None):
        cmd = list(cmd)
        if cmd[:2] != ["docker", "kill"] and self.kind(cmd) == "setup":
            self.calls.append(cmd)
            mount = next(v for v in cmd if v.endswith(":/deps"))
            Path(mount.rsplit(":/deps", 1)[0], "python").mkdir(parents=True, exist_ok=True)
            Path(mount.rsplit(":/deps", 1)[0], "python", "lib.py").write_text("installed\n")
            return subprocess.CompletedProcess(cmd, 0, "installed\n", "")
        return super().__call__(cmd, timeout=timeout)


def test_deps_key_follows_the_dependency_files_only(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "svc").mkdir()
    (repo / "svc" / "requirements.txt").write_text("requests==2.0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "deps")
    key = sbx.deps_key(SETTINGS, repo, "pip install -r svc/requirements.txt")
    assert key and sbx.deps_key(SETTINGS, repo, "") is None
    assert sbx.deps_key(SETTINGS, repo, "pip install -r svc/requirements.txt -q") != key     # other command
    (repo / "calc.py").write_text("x = 1\n")
    git(repo, "commit", "-qam", "code only")
    assert sbx.deps_key(SETTINGS, repo, "pip install -r svc/requirements.txt") == key
    (repo / "svc" / "requirements.txt").write_text("requests==2.1\n")
    git(repo, "commit", "-qam", "bump")
    assert sbx.deps_key(SETTINGS, repo, "pip install -r svc/requirements.txt") != key


def test_setup_is_installed_once_and_reused_read_only_by_later_tasks(tmp_path):
    cache = tmp_path / "deps-cache"
    repo = make_repo(tmp_path)
    first = DepsOnlyDocker()
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="d1", runner=first, log=lambda _m: None,
                       setup_command="pip install -r requirements.txt", deps_cache=cache)
    assert run.verified and not run.setup_cached and "setup" in [first.kind(c) for c in first.calls]
    (stored,) = [p for p in cache.iterdir() if not p.name.startswith(".")]
    assert (stored / "python" / "lib.py").exists() and (tmp_path / "state" / "d1" / "deps").is_symlink()
    second = DepsOnlyDocker()
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="d2", runner=second, log=lambda _m: None,
                       setup_command="pip install -r requirements.txt", deps_cache=cache)
    assert run.verified and run.setup_cached and run.setup_exit_code == 0
    assert "setup" not in [second.kind(c) for c in second.calls]
    mount = next(v for v in second.call("agent") if ":/deps" in v)
    assert mount == f"{stored.resolve()}:/deps:ro"                      # shared, so nobody may change it
    # A setup that also writes into the clone (node_modules, generated code) is never cached.
    writes = FakeDocker()
    (tmp_path / "w").mkdir()
    sbx.run_task(SETTINGS, repo=make_repo(tmp_path / "w"), goal="Sửa average", model="qwen3.6",
                 models=["qwen3.6"], state_dir=tmp_path / "state", task_id="d3", runner=writes,
                 log=lambda _m: None, setup_command="npm ci", deps_cache=cache)
    assert len([p for p in cache.iterdir() if not p.name.startswith(".")]) == 1
    assert not (tmp_path / "state" / "d3" / "deps").is_symlink()


def test_deps_are_copied_into_a_cache_on_another_disk(tmp_path, monkeypatch):
    import errno, os
    deps = tmp_path / "task" / "deps"
    (deps / "python").mkdir(parents=True)
    (deps / "python" / "lib.py").write_text("installed\n")
    real_rename = os.rename

    def rename(src, dst):
        if Path(src) == deps:                       # moving /deps out of the task dir crosses disks
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr(sbx.os, "rename", rename)
    assert sbx.store_deps(tmp_path / "cache", "k1", deps)
    stored = tmp_path / "cache" / "k1"
    assert (stored / "python" / "lib.py").read_text() == "installed\n" and (stored / ".kite-ready").is_file()
    assert deps.is_symlink() and deps.resolve() == stored.resolve()
    assert [p.name for p in (tmp_path / "cache").iterdir()] == ["k1"]          # no staging left behind
    other = tmp_path / "task2" / "deps"
    (other / "python").mkdir(parents=True)
    assert not sbx.store_deps(tmp_path / "cache", "k1", other)                  # already stored
    assert other.is_dir() and not other.is_symlink()


def test_old_dependency_sets_are_pruned(tmp_path):
    import os
    for i in range(8):
        ready = tmp_path / f"k{i}" / ".kite-ready"
        ready.parent.mkdir()
        ready.write_text("x")
        os.utime(ready, (1000 + i, 1000 + i))       # long idle; k7 is the most recently used
    sbx.prune_deps(tmp_path, keep=6)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"k{i}" for i in range(2, 8)]


def test_quick_test_template_comes_from_kite_yml_or_a_plain_pytest_command(tmp_path):
    from dataclasses import replace
    assert sbx.quick_template(SETTINGS) == "python -m pytest -q {tests}"
    assert sbx.quick_template(SETTINGS, 'PYTHONPATH=. python -m pytest -q -k "not slow"').endswith('"not slow" {tests}')
    for command in ("python -m pytest -q tests", "pytest && echo ok", "npm test", ""):
        assert sbx.quick_template(SETTINGS, command) == ""                  # cannot narrow these safely
    assert sbx.quick_template(replace(SETTINGS, quick_tests=False)) == ""
    repo = make_repo(tmp_path)
    (repo / ".kite.yml").write_text("test: python -m pytest -q svc/tests\nquick_test: python -m pytest -q {tests}\n")
    settings = sbx.repo_settings(SETTINGS, repo)
    assert sbx.quick_template(settings) == "python -m pytest -q {tests}"
    (repo / ".kite.yml").write_text("quick_test: python -m pytest -q\n")
    with pytest.raises(sbx.SandboxError, match="tests"):
        sbx.repo_settings(SETTINGS, repo)


def test_select_tests_takes_failing_written_and_related_tests(tmp_path):
    from poc.kaggle_worker import codemap
    from test_codemap import graph_data, make_repo as make_map_repo

    repo = make_map_repo(tmp_path)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert sbx.select_tests(repo, base) == []
    (repo / "calc.py").write_text((repo / "calc.py").read_text().replace("- 1", "if xs else 0"))
    (repo / "test_new.py").write_text("def test_new():\n    pass\n")
    output = "FAILED test_calc.py::test_empty - AssertionError\nFAILED gone.py::test_x - E\n"
    assert sbx.select_tests(repo, base, last_output=output) == ["test_calc.py::test_empty", "test_new.py"]
    graph = codemap.CodeGraph(graph_data(), repo)
    assert sbx.select_tests(repo, base, graph) == ["test_new.py", "test_calc.py"]     # test_calc uses average()
    assert sbx.select_tests(repo, base, last_output=output, limit=1) == []         # too many: run everything
    assert sbx.quick_test_command("pytest -q {tests}", ["a b.py", "t.py::x"]) == "pytest -q 'a b.py' t.py::x"


def test_a_failing_quick_run_goes_straight_to_the_next_fix_round(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "test_calc.py").write_text("from calc import average\n\ndef test_empty():\n    assert average([]) == 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "tests")
    fake = FakeDocker(test_returncodes=[1], quick_returncodes=[1, 0])
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="q1", runner=fake, log=lambda _m: None)
    assert [fake.kind(c) for c in fake.calls] == ["agent", "test", "fix1", "quick", "fix2", "quick", "test", "review"]
    assert run.verified and (run.quick_test_runs, run.full_test_runs, run.fix_rounds_used) == (2, 2, 2)
    fix2 = [c for c in fake.calls if fake.kind(c) == "fix2"][0][-1]
    assert "`python -m pytest -q test_calc.py::test_empty` (không mạng)" in fix2       # what failed, and how
    assert "chạy riêng các file test liên quan" in fix2 and "<file test>" in fake.call("agent")[-1]
    log = (tmp_path / "state" / "q1" / "test.log").read_text()
    assert "quick after fix1 exit=1" in log and "quick after fix2 exit=0" in log and "after fix2 exit=0" in log


LOOP = "".join("> build · gemma4-26b\n→ Read schemas/admin.py\n> build · gemma4-26b\n← Write repro.py\n"
               "Wrote file successfully: repro.py\n> build · gemma4-26b\n$ python3 repro.py\n"
               f"Result: [name=None] in 0.{i}s\n" for i in range(6))


def test_stuck_watch_catches_a_repeating_block_but_not_progress():
    watch = sbx.StuckWatch()
    reason = ""
    for line in LOOP.splitlines():
        reason = watch.feed(line) or reason
    assert "lặp lại 4 lần cùng 3 bước" in reason and "→ Read schemas/admin.py" in reason
    assert len(watch.steps) == 12                                   # stopped at the fourth round, not later
    progress = "".join(f"> build · m\n← Edit calc.py\n> build · m\n$ pytest\n{i} failed\n" for i in range(6, 0, -1))
    assert sbx.StuckWatch().scan(progress) == ""                    # results change: still working
    assert sbx.StuckWatch().scan("> build · m\n$ ls\n" * 3) == ""     # three repeats are not enough
    assert sbx.StuckWatch().scan("> build · m\n$ ls\n" * 4).startswith("lặp lại 4 lần cùng 1 bước")


def test_a_stuck_agent_is_stopped_and_the_next_round_is_told_why(tmp_path):
    class Looping(FakeDocker):
        def __call__(self, cmd, *, timeout=None):
            if cmd[:2] != ["docker", "kill"] and self.kind(list(cmd)) == "agent":
                super().__call__(cmd, timeout=timeout)              # the edit happened, then it looped
                return subprocess.CompletedProcess(cmd, 0, LOOP, "")
            return super().__call__(cmd, timeout=timeout)

    fake = Looping(test_returncodes=[1, 0])
    _, run = run_task(tmp_path, fake, "stuck1")
    assert run.stuck_stops == 1 and run.fix_rounds_used == 1 and run.verified
    fix = fake.call("fix1")[-1]
    assert fix.startswith("Sửa average\nLần trước Kite đã dừng bạn vì lặp lại 4 lần")
    agent_log = (tmp_path / "state" / "stuck1" / "agent.log").read_text()
    assert "===== agent exit=125 =====" in agent_log and "[kite] stopped: lặp lại" in agent_log
    # Stuck but the fix was already in and tests pass: that still counts, and the review runs.
    fake = Looping(test_returncodes=[0])
    (tmp_path / "b").mkdir()
    _, run = run_task(tmp_path / "b", fake, "stuck2")
    assert run.agent_status == "stuck" and run.verified and "review" in [fake.kind(c) for c in fake.calls]


def test_streaming_runner_stops_a_looping_process_right_away(tmp_path):
    import sys
    import time
    from dataclasses import replace

    marker = f"kite-stuck-{tmp_path.name}"
    script = tmp_path / f"{marker}.py"
    script.write_text(f"import sys, time\nsys.stdout.write({LOOP!r})\nsys.stdout.flush()\ntime.sleep(60)\n")
    settings = replace(SETTINGS, docker_command=("sh", "-c", f"pkill -f {marker}", "docker"))
    started = time.monotonic()
    code, output = sbx._run_step(settings, sbx.default_runner, name="kite-agent-x", timeout=50,
                                 args=[sys.executable, str(script)], watch=sbx.StuckWatch())
    assert code == 125 and "[kite] stopped: lặp lại 4 lần" in output and time.monotonic() - started < 20
    script.write_text("import time\nprint('> build · m', flush=True)\ntime.sleep(60)\n")
    code, output = sbx._run_step(settings, sbx.default_runner, name="kite-agent-x", timeout=2,
                                 args=[sys.executable, str(script)], watch=sbx.StuckWatch())
    assert code == 124 and "step timed out" in output


def checked_repo(tmp_path: Path, check: str = "ruff check --select F82 {py_files}") -> Path:
    repo = make_repo(tmp_path)
    (repo / ".kite.yml").write_text(f"test: python -m pytest -q\ncheck: {check}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "kite.yml")
    return repo


def test_check_command_comes_from_kite_yml_and_fills_in_the_changed_files(tmp_path):
    repo = checked_repo(tmp_path)
    assert sbx.repo_settings(SETTINGS, repo).check_command == "ruff check --select F82 {py_files}"
    (repo / ".kite.yml").write_text("check: ruff check {tests}\n")
    with pytest.raises(sbx.SandboxError, match="py_files"):
        sbx.repo_settings(SETTINGS, repo)
    files = ["calc.py", "docs/a b.md", "pkg/types.pyi"]
    assert sbx.check_command("ruff check {py_files}", files) == "ruff check calc.py pkg/types.pyi"
    assert sbx.check_command("prettier --check {files}", files) == "prettier --check calc.py 'docs/a b.md' pkg/types.pyi"
    assert sbx.check_command("ruff check {py_files}", ["README.md"]) == ""          # nothing to check: skipped
    assert sbx.check_command("mypy .", []) == "mypy ." and sbx.check_command("", files) == ""
    assert "ruff check" in sbx.suggest_repo_config(["calc.py"], "[tool.ruff]\nline-length = 100\n")


def test_changed_scratch_and_broken_files_are_found(tmp_path):
    repo = make_repo(tmp_path)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) if xs else 0\n")
    (repo / "tools").mkdir()
    (repo / "tools" / "report.py").write_text("def report(:\n    pass\n")
    (repo / "reproduce_bug.py").write_text("print(1)\n")
    (repo / "calc.py.orig").write_text("old\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_debug_mode.py").write_text("def test_x():\n    pass\n")
    files = sbx.changed_files(repo, base)
    assert set(files) == {"calc.py", "tools/report.py", "reproduce_bug.py", "calc.py.orig", "tests/test_debug_mode.py"}
    assert sbx.scratch_files(repo, base) == ["calc.py.orig", "reproduce_bug.py"]     # a real test is not scratch
    problems = sbx.syntax_errors(repo, files)
    assert problems.startswith("tools/report.py:1: SyntaxError") and "calc.py:" not in problems
    assert sbx.syntax_command(problems) == "python -m py_compile tools/report.py"


def test_a_failing_check_goes_back_to_the_agent_like_a_failing_test(tmp_path):
    repo = checked_repo(tmp_path)
    fake = FakeDocker(check_returncodes=[1, 0])
    logs: list[str] = []
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="c1", runner=fake, log=logs.append)
    assert [fake.kind(c) for c in fake.calls] == ["agent", "test", "check", "fix1", "test", "check", "review"]
    assert run.verified and run.check_exit_code == 0 and run.check_runs == 2 and run.fix_rounds_used == 1
    assert fake.call("check")[-1] == "ruff check --select F82 calc.py"            # only the changed file
    assert value_after(fake.call("check"), "--network") == "none"
    fix = fake.call("fix1")[-1]
    assert "Test đã QUA, nhưng Kite chạy kiểm tra `ruff check --select F82 calc.py`" in fix
    assert "F821 Undefined name `total`" in fix and "không thêm `# noqa`" in fix
    assert "Kite còn kiểm tra bằng `ruff check --select F82 <file .py đã sửa>`" in fake.call("agent")[-1]
    review = fake.call("review")[-1]
    assert "và `ruff check --select F82 <file .py đã sửa>`" in review and "Danh sách rà soát:" in review
    assert "Bảo mật: không ghi cứng mật khẩu" in review
    assert any("test qua nhưng kiểm tra lỗi (exit 1)" in line for line in logs)
    # Never fixed within the rounds: not verified, and no review on top of a failing check.
    fake = FakeDocker(check_returncodes=[1, 1, 1])
    (tmp_path / "b").mkdir()
    run = sbx.run_task(SETTINGS, repo=checked_repo(tmp_path / "b"), goal="Sửa average", model="qwen3.6",
                       models=["qwen3.6"], state_dir=tmp_path / "state", task_id="c2", runner=fake,
                       log=lambda _m: None)
    assert not run.verified and run.check_exit_code == 1 and run.fix_rounds_used == 2
    assert "review" not in [fake.kind(c) for c in fake.calls]
    assert "F821" in run.check_output_tail and run.to_dict()["verified"] is False


def test_a_review_that_breaks_the_check_is_rolled_back(tmp_path):
    fake = FakeDocker(review="improve", check_returncodes=[0, 1])
    logs: list[str] = []
    run = sbx.run_task(SETTINGS, repo=checked_repo(tmp_path), goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="c4", runner=fake, log=logs.append)
    assert [fake.kind(c) for c in fake.calls][-4:] == ["review", "quick", "test", "check"]   # it added a test
    assert run.review == "reverted" and run.verified and run.check_exit_code == 0
    assert "else 0.0" not in (tmp_path / "state" / "c4" / "result.patch").read_text()
    assert any("kiểm tra 1): trả lại bản trước" in line for line in logs)


def test_a_check_tool_that_is_not_installed_is_not_the_agents_job(tmp_path):
    fake = FakeDocker(check_returncodes=[127])
    logs: list[str] = []
    run = sbx.run_task(SETTINGS, repo=checked_repo(tmp_path), goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="c3", runner=fake, log=logs.append)
    assert [fake.kind(c) for c in fake.calls] == ["agent", "test", "check"]
    assert not run.verified and run.check_exit_code == 127 and run.fix_rounds_used == 0
    assert any("setup đã cài công cụ đó chưa" in line for line in logs)


def test_a_syntax_error_outside_the_tests_is_caught_without_a_check(tmp_path):
    class Broken(FakeDocker):
        def __call__(self, cmd, *, timeout=None):
            result = super().__call__(cmd, timeout=timeout)
            if cmd[:2] != ["docker", "kill"] and self.kind(list(cmd)) == "agent":
                workspace = Path(value_after(list(cmd), "-v").rsplit(":/work", 1)[0])
                (workspace / "cli.py").write_text("def main(:\n    pass\n")      # not imported by any test
            return result

    fake = Broken()
    _, run = run_task(tmp_path, fake, "s1")
    assert [fake.kind(c) for c in fake.calls][:3] == ["agent", "test", "fix1"]
    assert "`python -m py_compile cli.py`" in fake.call("fix1")[-1] and "SyntaxError" in fake.call("fix1")[-1]
    assert run.check_exit_code == 1 and not run.verified          # the fake fix rounds never repair cli.py
    assert (tmp_path / "state" / "s1" / "test.log").read_text().count("===== syntax after") == 3


def test_scratch_files_are_flagged_for_the_review_and_in_the_result(tmp_path):
    class Messy(FakeDocker):
        def __call__(self, cmd, *, timeout=None):
            result = super().__call__(cmd, timeout=timeout)
            if cmd[:2] != ["docker", "kill"] and self.kind(list(cmd)) == "agent":
                workspace = Path(value_after(list(cmd), "-v").rsplit(":/work", 1)[0])
                (workspace / "repro_average.py").write_text("print(1)\n")
            return result

    fake = Messy()
    logs: list[str] = []
    repo = make_repo(tmp_path)
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="m1", runner=fake, log=logs.append)
    assert run.verified and run.scratch_files == ["repro_average.py"]
    assert "File mới trông như file thử: repro_average.py" in fake.call("review")[-1]
    assert any("có file trông như file thử: repro_average.py" in line for line in logs)


FIX = """Sửa như sau.

calc.py
<<<<<<< SEARCH
    return sum(xs) / len(xs) - 1
=======
    return sum(xs) / len(xs) if xs else 0
>>>>>>> REPLACE
"""


def test_edit_blocks_apply_all_or_nothing_and_stay_inside_the_repo(tmp_path):
    from poc.kaggle_worker import editblocks as eb

    (tmp_path / "a.py").write_text("x = 1\nmax = 10\n\nclass A:\n    def f(self):\n        return 1\n")
    blocks = eb.parse("```python\na.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n```\n"
                      "<<<<<<< SEARCH\ndef f(self):\n    return 1\n=======\ndef f(self):\n    return 2\n"
                      ">>>>>>> REPLACE\n\nnew/t.py\n<<<<<<< SEARCH\n=======\nok = True\n>>>>>>> REPLACE\n")
    assert [b.path for b in blocks] == ["a.py", "a.py", "new/t.py"]          # second block reuses the file
    assert eb.apply(tmp_path, blocks) == ["a.py", "new/t.py"]
    assert (tmp_path / "a.py").read_text() == "x = 2\nmax = 10\n\nclass A:\n    def f(self):\n        return 2\n"
    for bad, why in (("a.py\n<<<<<<< SEARCH\nnope\n=======\ny\n>>>>>>> REPLACE", "not in the file"),
                     ("../x.py\n<<<<<<< SEARCH\n=======\ny\n>>>>>>> REPLACE", "inside the repository"),
                     ("a.py\n<<<<<<< SEARCH\n=======\ny\n>>>>>>> REPLACE", "file exists"),
                     ("a.py\n<<<<<<< SEARCH\nx = 2\n=======\nx = 3\n>>>>>>> REPLACE\n"
                      "a.py\n<<<<<<< SEARCH\nmissing\n=======\nz\n>>>>>>> REPLACE", "not in the file")):
        with pytest.raises(eb.EditError, match=why):
            eb.apply(tmp_path, eb.parse(bad))
    assert "x = 2" in (tmp_path / "a.py").read_text()                       # nothing half-applied
    (tmp_path / "d.py").write_text("a = 1\nb = 2\na = 1\n")
    with pytest.raises(eb.EditError, match="more than one place"):
        eb.apply(tmp_path, eb.parse("d.py\n<<<<<<< SEARCH\na = 1\n=======\na = 0\n>>>>>>> REPLACE"))


def test_fast_context_shows_the_named_code_verbatim(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "svc").mkdir()
    (repo / "svc" / "base.py").write_text("class Table:\n    def get(self, key):\n        return key\n")
    (repo / "svc" / "models.py").write_text("from svc.base import Table\n\n\nclass Models(Table):\n"
                                            "    name = 'models'\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "svc")
    context = sbx.fast_context(repo, "Sửa average trong calc.py cho list rỗng", None)
    assert "### calc.py (toàn bộ file, 2 dòng)" in context and "return sum(xs) / len(xs) - 1" in context
    assert "|" not in context.split("`````")[1]                              # no line numbers to copy wrongly
    context = sbx.fast_context(repo, "Thêm Models.get_metadata() để đọc metadata", None)
    assert "svc/models.py, dòng 4-5" in context and "lớp cha Table" in context and "def get(self, key)" in context
    assert sbx.fast_context(repo, "Làm cho nó tốt hơn", None) == ""         # nothing located: agent explores


REPRO = """test_kite_repro.py
<<<<<<< SEARCH
=======
from calc import average


def test_empty_list_gives_zero():
    assert average([]) == 0
>>>>>>> REPLACE
"""


def answers(*texts):
    """An asker that gives these answers in turn (and records the prompts)."""
    queue, prompts = list(texts), []

    def ask(prompt):
        prompts.append(prompt)
        return queue.pop(0)
    ask.prompts = prompts
    return ask


def test_the_fast_path_fixes_a_task_with_one_model_call_and_skips_the_agent(tmp_path):
    fake = FakeDocker()
    logs: list[str] = []
    repo = make_repo(tmp_path)
    ask = answers(REPRO, FIX, "ỔN")
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average trong calc.py cho list rỗng", model="qwen3.6",
                       models=["qwen3.6"], state_dir=tmp_path / "state", task_id="f1", runner=fake,
                       log=logs.append, asker=ask)
    prompts = ask.prompts
    # The reproduction test fails on the old code, then the fix passes it and the whole suite:
    # no agent session, no review session.
    assert [fake.kind(c) for c in fake.calls] == ["repro", "quick", "test"]
    assert fake.call("repro")[-1] == "python -m pytest -q test_kite_repro.py"
    assert fake.call("quick")[-1] == "python -m pytest -q test_kite_repro.py"
    assert run.verified and run.fast_path == "passed" and run.repro == "reproduced" and run.agent_status == "ok"
    assert (run.review, run.review_mode) == ("unchanged", "one_call") and len(prompts) == 3
    assert "TRƯỚC khi sửa" in prompts[0] and "test_kite_repro.py" in prompts[0]
    assert "test_empty_list_gives_zero" not in prompts[1]                 # the fix never sees the test
    assert "RÀ SOÁT cuối" in prompts[2] and "+    return sum(xs) / len(xs) if xs else 0" in prompts[2]
    patch = (tmp_path / "state" / "f1" / "result.patch").read_text()
    assert "test_empty_list_gives_zero" in patch                          # the test ships with the fix
    assert run.changed_files == ["calc.py", "test_kite_repro.py"] and "if xs else 0" in patch
    assert "<<<<<<< SEARCH" in prompts[1] and "return sum(xs) / len(xs) - 1" in prompts[1]
    assert any("đường nhanh: passed" in line and "bỏ qua agent" in line for line in logs)
    agent_log = (tmp_path / "state" / "f1" / "agent.log").read_text()
    assert "===== fast repro reproduced =====" in agent_log and "===== fast passed =====" in agent_log


def test_the_one_call_review_can_correct_a_fast_fix_but_never_break_it(tmp_path):
    better = ("calc.py\n<<<<<<< SEARCH\n    return sum(xs) / len(xs) if xs else 0\n=======\n"
              "    return sum(xs) / len(xs) if xs else 0.0\n>>>>>>> REPLACE\n")
    fake = FakeDocker()
    _, run = run_task(tmp_path, fake, "r1", asker=answers(REPRO, FIX, better))
    assert [fake.kind(c) for c in fake.calls] == ["repro", "quick", "test", "quick", "test"]
    assert run.review == "kept" and run.verified
    assert "else 0.0" in (tmp_path / "state" / "r1" / "result.patch").read_text()
    # The correction breaks the tests: the fast fix as it was is kept.
    (tmp_path / "b").mkdir()
    fake = FakeDocker(test_returncodes=[0, 1])
    _, run = run_task(tmp_path / "b", fake, "r2", asker=answers(REPRO, FIX, better))
    assert run.review == "reverted" and run.verified and "else 0.0" not in (tmp_path / "b" / "state" / "r2" / "result.patch").read_text()
    # The review cannot be applied or asked: nothing changes, the fast fix stands.
    (tmp_path / "c").mkdir()
    fake = FakeDocker()
    _, run = run_task(tmp_path / "c", fake, "r3",
                      asker=answers(REPRO, FIX, "calc.py\n<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE"))
    assert run.review == "unchanged" and run.verified and [fake.kind(c) for c in fake.calls] == ["repro", "quick", "test"]
    from dataclasses import replace
    (tmp_path / "d").mkdir()
    fake = FakeDocker()
    repo = make_repo(tmp_path / "d")
    run = sbx.run_task(replace(SETTINGS, fast_review=False), repo=repo, goal="Sửa average", model="qwen3.6",
                       models=["qwen3.6"], state_dir=tmp_path / "d" / "state", task_id="r4", runner=fake,
                       log=lambda _m: None, asker=answers(REPRO, FIX))
    assert run.review_mode == "agent" and [fake.kind(c) for c in fake.calls] == ["repro", "quick", "test", "review"]


def test_without_a_reproduction_the_fast_fix_gets_the_agent_review(tmp_path):
    # The test passes on the old code too: it proves nothing, so a reading-only review is not enough.
    fake = FakeDocker(repro_returncodes=[0])
    _, run = run_task(tmp_path, fake, "p1", asker=answers(REPRO, FIX))
    assert (run.fast_path, run.repro, run.review_mode) == ("passed", "not_reproduced", "agent") and run.verified
    assert [fake.kind(c) for c in fake.calls] == ["repro", "quick", "test", "review"]
    # No test written at all (the model answered something else): the same.
    (tmp_path / "b").mkdir()
    fake = FakeDocker()
    _, run = run_task(tmp_path / "b", fake, "p2", asker=answers("Không viết được.", FIX))
    assert (run.fast_path, run.repro, run.review_mode) == ("passed", "none", "agent")
    assert [fake.kind(c) for c in fake.calls] == ["test", "review"]


def test_a_fix_that_disagrees_with_the_reproduction_test_goes_to_the_agent(tmp_path):
    fake = FakeDocker(quick_returncodes=[1, 0],
                      quick_fail="FAILED test_kite_repro.py::test_empty_list_gives_zero - assert 1 == 0\n1 failed\n")
    logs: list[str] = []
    repo = make_repo(tmp_path)
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average trong calc.py cho list rỗng", model="qwen3.6",
                       models=["qwen3.6"], state_dir=tmp_path / "state", task_id="d1", runner=fake,
                       log=logs.append, asker=answers(REPRO, FIX))
    assert run.fast_path == "repro_failed" and run.repro == "reproduced" and run.verified
    assert [fake.kind(c) for c in fake.calls][:4] == ["repro", "quick", "agent", "test"]
    agent = fake.call("agent")[-1]
    assert "test tái hiện lỗi trên bản sửa" in agent and "test_empty_list_gives_zero" in agent
    assert "yêu cầu gốc mới là chuẩn" in agent and "if xs else 0" in agent          # both the test and the fix
    work = tmp_path / "state" / "d1" / "work"
    assert not (work / "test_kite_repro.py").exists() or "test_kite_repro.py" in run.changed_files
    assert any("đường nhanh: repro_failed, test tái hiện: reproduced" in line for line in logs)


def test_the_reproduction_test_goes_next_to_the_tests_of_the_named_code(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "svc" / "tests").mkdir(parents=True)
    (repo / "svc" / "app.py").write_text("def handler():\n    return 1\n")
    (repo / "svc" / "tests" / "test_app.py").write_text("def test_x():\n    pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "svc")
    assert sbx.repro_path(repo, "Sửa handler trong svc/app.py", None) == "svc/tests/test_kite_repro.py"
    (repo / "svc" / "tests" / "test_kite_repro.py").write_text("")
    assert sbx.repro_path(repo, "Sửa handler trong svc/app.py", None) == "svc/tests/test_kite_repro_2.py"
    assert sbx.repro_path(repo, "Sửa average", None) == "test_kite_repro.py"


def test_a_fast_fix_that_fails_the_tests_is_dropped_and_the_agent_is_told(tmp_path):
    fake = FakeDocker(test_returncodes=[1, 0])
    _, run = run_task(tmp_path, fake, "f2", asker=lambda prompt: FIX)
    assert [fake.kind(c) for c in fake.calls][:3] == ["test", "agent", "test"]
    assert run.fast_path == "tests_failed" and run.verified and run.fix_rounds_used == 0
    agent = fake.call("agent")[-1]
    assert "Kite đã thử một bản sửa nhanh" in agent and "test của Kite lỗi" in agent and "if xs else 0" in agent
    assert "ZeroDivisionError" in agent                                       # what the tests said
    # The clone went back before the agent ran: the agent's own edit is what the patch holds.
    assert run.changed_files == ["calc.py"]


def test_the_fast_path_steps_aside_when_it_cannot_help(tmp_path):
    for task_id, answer, status in (("f3", "CẦN_AGENT: cần xem thêm file cấu hình", "declined"),
                                    ("f4", "calc.py\n<<<<<<< SEARCH\nno such line\n=======\nx\n>>>>>>> REPLACE",
                                     "not_applied")):
        (tmp_path / task_id).mkdir()
        fake = FakeDocker()
        _, run = run_task(tmp_path / task_id, fake, task_id, asker=lambda prompt, a=answer: a)
        assert run.fast_path == status and fake.kind(fake.calls[0]) == "agent" and run.verified
        assert ("không áp được" in fake.call("agent")[-1]) == (status == "not_applied")

    def down(prompt):
        raise sbx.SandboxError("gateway: connection refused")

    (tmp_path / "f7").mkdir()
    repo = make_repo(tmp_path / "f7")
    (repo / "test_calc.py").write_text("def test_x():\n    assert False\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "tests")
    fake = FakeDocker()
    cheat = "test_calc.py\n<<<<<<< SEARCH\n    assert False\n=======\n    assert True\n>>>>>>> REPLACE\n"
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average trong calc.py", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state7", task_id="f7", runner=fake, log=lambda _m: None,
                       asker=lambda p: FIX + cheat)
    assert run.fast_path == "not_applied" and "sửa test có sẵn" in fake.call("agent")[-1]
    assert "assert False" in (tmp_path / "state7" / "f7" / "work" / "test_calc.py").read_text()

    (tmp_path / "f5").mkdir()
    fake = FakeDocker()
    _, run = run_task(tmp_path / "f5", fake, "f5", asker=down)
    assert run.fast_path == "error" and run.verified and fake.kind(fake.calls[0]) == "agent"
    from dataclasses import replace
    (tmp_path / "f6").mkdir()
    fake = FakeDocker()
    called = []
    repo = make_repo(tmp_path / "f6")
    run = sbx.run_task(replace(SETTINGS, fast_path=False), repo=repo, goal="Sửa average", model="qwen3.6",
                       models=["qwen3.6"], state_dir=tmp_path / "state6", task_id="f6", runner=fake,
                       log=lambda _m: None, asker=lambda p: called.append(p) or FIX)
    assert run.fast_path == "off" and not called


@pytest.mark.gateway_asker
def test_gateway_asker_sends_one_tagged_request(monkeypatch):
    import json as _json
    real = sbx.gateway_asker
    seen = {}

    class Response:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def read(self): return self.body

    def urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()), body=_json.loads(request.data))
        return Response(_json.dumps({"choices": [{"message": {"content": FIX}}]}).encode())

    from dataclasses import replace
    monkeypatch.setattr(sbx.urllib.request, "urlopen", urlopen)
    ask = real(replace(SETTINGS, host_gateway_url="http://127.0.0.1:8787/v1"), "qwen3.6", tag="t-1", pin="kaggle-han")
    assert ask("hi") == FIX and seen["url"] == "http://127.0.0.1:8787/v1/chat/completions"
    assert seen["headers"]["X-kite-task"] == "t-1" and seen["headers"]["X-kite-worker"] == "kaggle-han"
    assert seen["body"]["model"] == "qwen3.6" and seen["body"]["max_tokens"] == sbx.FAST_MAX_TOKENS


def test_changed_existing_tests_are_flagged_for_the_review(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "test_calc.py").write_text("def test_empty():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "tests")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "test_calc.py").write_text("def test_empty():\n    pass\n")
    (repo / "test_new.py").write_text("def test_new():\n    pass\n")
    git(repo, "add", "-A")
    assert sbx.edited_tests(repo, base) == ["test_calc.py"]          # new tests are fine
    prompt = sbx.review_prompt("g", "pytest", edited=["test_calc.py"])
    assert "Bạn đã sửa hoặc xóa test có sẵn: test_calc.py" in prompt and "trả file test về như cũ" in prompt
    assert "Bạn đã sửa hoặc xóa" not in sbx.review_prompt("g", "pytest")
