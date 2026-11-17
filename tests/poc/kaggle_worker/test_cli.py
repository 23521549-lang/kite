from pathlib import Path
import json
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from poc.kaggle_worker.cli import main
except ModuleNotFoundError:
    main = None


def invoke(args, capsys):
    assert main is not None, "CLI module has not been implemented"
    result = main(args)
    captured = capsys.readouterr()
    return result, captured.out, captured.err


def test_task_add_list_show_and_cancel_persist(tmp_path, capsys):
    db = tmp_path / "state" / "tasks.sqlite3"
    common = ["--db", str(db)]

    result, out, err = invoke(common + [
        "task", "add", "--task-id", "task-cli", "--goal", "Fix parser edge case",
        "--repo-head", "head-42", "--branch", "agent/task-cli", "--estimate-gpu-seconds", "300",
    ], capsys)
    created = json.loads(out)
    assert result == 0 and not err
    assert created["task_id"] == "task-cli"
    assert created["state"] == "QUEUED"

    result, out, _ = invoke(common + ["task", "list"], capsys)
    assert result == 0
    assert [item["task_id"] for item in json.loads(out)] == ["task-cli"]

    result, out, _ = invoke(common + ["task", "show", "task-cli"], capsys)
    assert result == 0
    shown = json.loads(out)
    assert shown["repo_head"] == "head-42"
    assert json.loads(shown["payload_json"])["goal"] == "Fix parser edge case"

    result, out, err = invoke(common + ["task", "cancel", "task-cli"], capsys)
    assert result == 0 and not err
    assert json.loads(out)["state"] == "CANCELLED"


def test_task_add_requires_pinned_head_and_branch(tmp_path, capsys):
    result, _, err = invoke([
        "--db", str(tmp_path / "tasks.sqlite3"), "task", "add", "--goal", "do work",
    ], capsys)
    assert result == 2
    assert "required" in err.lower()


def test_review_command_validates_worker_result_and_prints_patch(tmp_path, capsys):
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps({
        "task_id": "t1", "lease_id": "l1", "worker_id": "w1",
        "base_repo_head": "h1", "patch": "diff --git a/x b/x",
        "summary": "fix", "usage_gpu_seconds": 4,
    }), encoding="utf-8")
    args = ["--db", str(tmp_path / "tasks.sqlite3"), "review", "result", str(result_file),
            "--expected-head", "h1", "--expected-task", "t1", "--expected-lease", "l1",
            "--expected-worker", "w1"]
    result, out, err = invoke(args, capsys)
    bundle = json.loads(out)
    assert result == 0 and not err
    assert bundle["patch"] == "diff --git a/x b/x"
    assert bundle["summary"] == "fix"
    assert not (tmp_path / "x").exists()


def test_review_command_rejects_stale_head(tmp_path, capsys):
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps({
        "task_id": "t1", "lease_id": "l1", "worker_id": "w1",
        "base_repo_head": "old", "patch": "diff", "summary": "fix", "usage_gpu_seconds": 4,
    }), encoding="utf-8")
    args = ["--db", str(tmp_path / "tasks.sqlite3"), "review", "result", str(result_file),
            "--expected-head", "new", "--expected-task", "t1", "--expected-lease", "l1",
            "--expected-worker", "w1"]
    result, out, err = invoke(args, capsys)
    assert result == 1
    assert out == ""
    assert "HEAD" in err


def write_config(path):
    import yaml
    path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "kite"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": str(path.parent / "harness2"), "cli": "harness2"},
        "providers": [{"id": "kaggle-main", "adapter": "kaggle", "model": "qwen/coder-7b",
                       "capabilities": ["coding"], "quota": {"gpu_seconds": 3600, "concurrency": 1},
                       "secrets": {"token": "env:KAGGLE_TOKEN"}}],
    }), encoding="utf-8")


def test_provider_validate_and_refresh_are_offline_and_never_enable(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    write_config(config)
    result, out, err = invoke(["--config", str(config), "provider", "validate"], capsys)
    assert result == 0 and not err
    assert json.loads(out)["valid"] is True
    assert "KAGGLE_TOKEN" not in out

    result, out, err = invoke(["--config", str(config), "provider", "refresh"], capsys)
    assert result == 0 and not err
    refreshed = json.loads(out)
    assert refreshed["refreshed"] == []
    assert refreshed["unknown"] == ["kaggle-main"]
    assert refreshed["enabled"] == []


def test_kaggle_provider_cannot_be_enabled(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    write_config(config)
    result, out, err = invoke(["--config", str(config), "provider", "enable", "kaggle-main"], capsys)
    assert result == 1 and not out
    assert "validation-only" in err


def test_setup_captures_secret_without_echo_and_creates_private_file(tmp_path, capsys, monkeypatch):
    import os
    import poc.kaggle_worker.cli as cli
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "never-print-this-secret")
    config = tmp_path / "state" / "config.yaml"
    result, out, err = invoke(["--config", str(config), "setup", "--secret-name", "KAGGLE_TOKEN"], capsys)
    assert result == 0 and not err
    assert "never-print-this-secret" not in out
    secret_file = config.parent / "secrets" / "kaggle_token"
    assert secret_file.read_text(encoding="utf-8").strip() == "never-print-this-secret"
    if os.name == "posix":
        assert secret_file.stat().st_mode & 0o777 == 0o600


def test_registered_future_adapter_refreshes_only_configured_provider_and_enable_is_explicit(tmp_path, capsys):
    import yaml
    from poc.kaggle_worker.config import ProviderAdapter, register_provider_adapter, unregister_provider_adapter
    calls = []
    adapter_id = "fake-cloud"
    register_provider_adapter(ProviderAdapter(
        adapter_id, validate=lambda provider: calls.append(("validate", provider.id)),
        refresh=lambda provider: (calls.append(("refresh", provider.id)) or {"healthy": True}),
    ))
    try:
        config = tmp_path / "config.yaml"
        raw = {
            "schema_version": 1,
            "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "kite"},
            "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
            "harness2": {"checkout": "harness2", "cli": "harness2"},
            "providers": [{"id": "cloud-main", "adapter": adapter_id, "model": "vendor/model",
                           "capabilities": ["coding"], "quota": {"gpu_seconds": 500, "concurrency": 1},
                           "secrets": {"api_key": "env:CLOUD_TOKEN"}}],
        }
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        result, out, err = invoke(["--config", str(config), "provider", "refresh"], capsys)
        assert result == 0 and not err
        result_json = json.loads(out)
        assert result_json["refreshed"] == [{"provider_id": "cloud-main", "observation": {"healthy": True}}]
        assert result_json["enabled"] == []
        assert calls == [("validate", "cloud-main"), ("refresh", "cloud-main")]

        result, out, err = invoke(["--config", str(config), "provider", "enable", "cloud-main"], capsys)
        assert result == 0 and not err and json.loads(out)["enabled"] is True
        assert yaml.safe_load(config.read_text(encoding="utf-8"))["providers"][0]["enabled"] is True
    finally:
        unregister_provider_adapter(adapter_id)
