"""CLI `kite bench import` (F5): both importers usable from the command line,
Terminal-Bench first, SWE-bench second; a skip is reported, not silent."""

from __future__ import annotations

from pathlib import Path
import json
import shutil
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from poc.kaggle_worker import bench_loader as bl  # noqa: E402
from poc.kaggle_worker.cli import main  # noqa: E402
from tests.poc.kaggle_worker.test_bench_importers import (  # noqa: E402
    load_swe_instance, make_sample_checkout)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "bench"


def invoke(args, capsys):
    result = main(args)
    captured = capsys.readouterr()
    return result, captured.out, captured.err


def config_args(tmp_path: Path) -> list[str]:
    return ["--config", str(tmp_path / "config.yaml")]


def test_import_terminal_bench_cli_end_to_end(tmp_path, capsys):
    src = tmp_path / "tb-hello"
    shutil.copytree(FIXTURES_DIR / "tb-hello", src)
    code, out, err = invoke(config_args(tmp_path) + ["bench", "import", "terminal-bench", str(src)], capsys)
    assert code == 0, err
    result = json.loads(out)
    assert result["ok"] and result["task_id"] == "tb-hello" and result["source"] == "terminal-bench"
    assert not result["skipped"] and result["reason"] is None
    dest = Path(result["dest"])
    assert dest == tmp_path / "bench-tasks" / "tb-hello"
    loaded = bl.load_task_dir(dest)
    assert loaded.ok, loaded.errors
    assert loaded.task.kind == "terminal" and loaded.task.axes == ("Coding",)
    # The imported task is visible to the bench run's own discovery.
    assert [r.task.id for r in bl.discover_tasks(tmp_path / "bench-tasks") if r.task] == ["tb-hello"]


def test_import_terminal_bench_cli_second_import_skips(tmp_path, capsys):
    src = tmp_path / "tb-hello"
    shutil.copytree(FIXTURES_DIR / "tb-hello", src)
    assert invoke(config_args(tmp_path) + ["bench", "import", "terminal-bench", str(src)], capsys)[0] == 0
    code, out, _ = invoke(config_args(tmp_path) + ["bench", "import", "terminal-bench", str(src)], capsys)
    assert code == 1
    result = json.loads(out)
    assert result["skipped"] and "đã tồn tại" in result["reason"]


def test_import_terminal_bench_cli_task_id_override(tmp_path, capsys):
    src = tmp_path / "tb-hello"
    shutil.copytree(FIXTURES_DIR / "tb-hello", src)
    code, out, err = invoke(config_args(tmp_path) + [
        "bench", "import", "terminal-bench", str(src), "--task-id", "hello-renamed"], capsys)
    assert code == 0, err
    assert json.loads(out)["task_id"] == "hello-renamed"
    assert bl.load_task_dir(tmp_path / "bench-tasks" / "hello-renamed").ok


def test_import_swebench_cli_end_to_end(tmp_path, capsys):
    repo, sha = make_sample_checkout(tmp_path)
    instance = load_swe_instance(tmp_path, base_commit=sha)
    instance_file = tmp_path / "instance.json"
    instance_file.write_text(json.dumps(instance), encoding="utf-8")
    code, out, err = invoke(config_args(tmp_path) + [
        "bench", "import", "swe-bench", str(instance_file),
        "--repo-checkout", str(repo)], capsys)
    assert code == 0, err
    result = json.loads(out)
    assert result["ok"] and result["task_id"] == "sample__calc-1" and result["source"] == "swe-bench"
    loaded = bl.load_task_dir(Path(result["dest"]))
    assert loaded.ok, loaded.errors
    assert loaded.task.kind == "repo" and loaded.task.axes == ("Coding", "Đúng việc")
    assert loaded.task.timeouts == {"agent": 360.0, "test": 60.0}


def test_import_swebench_cli_missing_checkout_and_bad_json(tmp_path, capsys):
    instance_file = tmp_path / "instance.json"
    instance_file.write_text(json.dumps({"instance_id": "x__1"}), encoding="utf-8")
    code, _, err = invoke(config_args(tmp_path) + [
        "bench", "import", "swe-bench", str(instance_file),
        "--repo-checkout", str(tmp_path / "no-such-repo")], capsys)
    assert code == 1 and "--repo-checkout" in err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    repo, _ = make_sample_checkout(tmp_path)
    code, _, err = invoke(config_args(tmp_path) + [
        "bench", "import", "swe-bench", str(bad), "--repo-checkout", str(repo)], capsys)
    assert code == 1 and "lỗi cú pháp" in err


def test_import_swebench_cli_rejects_non_positive_timeout(tmp_path, capsys):
    repo, sha = make_sample_checkout(tmp_path)
    instance = load_swe_instance(tmp_path, base_commit=sha)
    instance_file = tmp_path / "instance.json"
    instance_file.write_text(json.dumps(instance), encoding="utf-8")
    code, _, err = invoke(config_args(tmp_path) + [
        "bench", "import", "swe-bench", str(instance_file),
        "--repo-checkout", str(repo), "--agent-timeout", "0"], capsys)
    assert code == 1 and "agent-timeout" in err
