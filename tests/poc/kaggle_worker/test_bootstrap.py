from pathlib import Path
import os

import pytest

from poc.kaggle_worker.bootstrap import BootstrapError, bootstrap


def repo_fixture(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='kite'\n", encoding="utf-8")
    (repo / "deploy" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    return repo


def bootstrap_args(repo, data_dir, **overrides):
    args = {"system": "Linux", "version": "24.04", "distro": "ubuntu", "git": True,
            "python312": True, "docker": True, "compose": True,
            "available_bytes": 40 * 1024**3, "opencode_installed": True, "dry_run": True}
    args.update(overrides)
    return args


@pytest.mark.parametrize("overrides", [
    {"system": "Windows"}, {"version": "20.04"}, {"distro": "debian"},
    {"git": False}, {"python312": False}, {"docker": False}, {"compose": False},
    {"available_bytes": 2 * 1024**3},
])
def test_rejects_unsupported_host_or_missing_prerequisite(tmp_path, overrides):
    repo = repo_fixture(tmp_path)
    data = tmp_path / "persistent"
    with pytest.raises(BootstrapError):
        bootstrap(repo, data, **bootstrap_args(repo, data, **overrides))


def test_requires_absolute_persistent_state_path(tmp_path):
    repo = repo_fixture(tmp_path)
    with pytest.raises(BootstrapError, match="absolute"):
        bootstrap(repo, Path("relative-state"), **bootstrap_args(repo, Path("relative-state")))


def test_refuses_state_path_file_and_nonempty_unmanaged_harness_path(tmp_path):
    repo = repo_fixture(tmp_path)
    data = tmp_path / "ebs"
    (data / "kite").mkdir(parents=True)
    (data / "kite" / "harness2").mkdir()
    (data / "kite" / "harness2" / "keep.txt").write_text("state", encoding="utf-8")
    with pytest.raises(BootstrapError, match="Refusing|refusing"):
        bootstrap(repo, data, **bootstrap_args(repo, data))

    (data / "kite" / "harness2" / "keep.txt").unlink()
    (data / "kite" / "harness2").rmdir()
    (data / "kite" / "harness2").write_text("not-a-directory", encoding="utf-8")
    with pytest.raises(BootstrapError, match="not a directory"):
        bootstrap(repo, data, **bootstrap_args(repo, data))


def test_dry_run_has_no_filesystem_or_provider_side_effects(tmp_path):
    repo = repo_fixture(tmp_path)
    data = tmp_path / "ebs"
    commands = bootstrap(repo, data, **bootstrap_args(repo, data))
    assert commands
    assert not data.exists()
    assert not (repo / "deploy" / ".env").exists()
    assert any("opencode.ai/v2/install" in " ".join(c) for c in commands) is False
    assert not any("kaggle" in " ".join(c).lower() for c in commands)
    compose = next(c for c in commands if c[:3] == ["docker", "compose", "--env-file"])
    assert compose[-3:] == ["postgres", "admin-tools", "temporal"]
    uninstalled = bootstrap(repo, data, **bootstrap_args(repo, data, opencode_installed=False))
    assert any("https://opencode.ai/v2/install" in " ".join(c) for c in uninstalled)


def test_bootstrap_is_idempotent_and_keeps_provider_disabled(tmp_path):
    repo = repo_fixture(tmp_path)
    data = tmp_path / "ebs"
    seen = []

    def fake_runner(command, cwd):
        seen.append(command)
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir()

    args = bootstrap_args(repo, data, dry_run=False)
    bootstrap(repo, data, command_runner=fake_runner, **args)
    config_path = data / "kite" / "config.yaml"
    original = config_path.read_text(encoding="utf-8")
    assert "providers: []" in original
    env_path = repo / "deploy" / ".env"
    assert env_path.exists()
    if os.name == "posix":
        assert env_path.stat().st_mode & 0o777 == 0o600

    seen.clear()
    bootstrap(repo, data, command_runner=fake_runner, **args)
    assert config_path.read_text(encoding="utf-8") == original
    assert not any(c[:2] == ["git", "clone"] for c in seen)
    assert sum(c[:2] == ["docker", "compose"] for c in seen) == 1

