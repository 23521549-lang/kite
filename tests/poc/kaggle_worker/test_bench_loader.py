"""Bench loader (worker (b)): one folder loads to a validated task, a root
lists its tasks, and load failures name the file/field that failed."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from poc.kaggle_worker import bench_importers as bi
from poc.kaggle_worker import bench_loader as bl
from poc.kaggle_worker.bench_standard import BenchTaskInvalid


def write_task(root: Path, task_id: str, data: dict, *, with_files: bool = True,
               with_tests: bool = True, with_solution: bool = False) -> Path:
    task_dir = root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    if with_files:
        (task_dir / "files").mkdir(exist_ok=True)
        (task_dir / "files" / "stats.py").write_text("def mean(v): return 0\n", encoding="utf-8")
        (task_dir / "files" / "sub").mkdir(exist_ok=True)
        (task_dir / "files" / "sub" / "util.py").write_text("X = 1\n", encoding="utf-8")
    if with_tests:
        (task_dir / "tests").mkdir(exist_ok=True)
        (task_dir / "tests" / "test_stats.py").write_text("def test_mean(): pass\n", encoding="utf-8")
    if with_solution:
        (task_dir / "solution.stats.py").write_text("def mean(v): return 1\n", encoding="utf-8")
    return task_dir


def valid_data(**overrides) -> dict:
    data = {
        "id": "t1-speed",
        "goal": "Sửa hàm mean để test qua.",
        "kind": "code",
        "language": "vi",
        "axes": ["Coding", "Tốc độ"],
        "difficulty": "dễ",
        "timeouts": {"setup": 600, "test": 300},
        "environment": {"setup": "pip install -q -r requirements.txt"},
        "par_seconds": 120,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------- happy path
def test_load_task_dir_happy_path(tmp_path):
    task_dir = write_task(tmp_path, "t1-speed", valid_data(), with_solution=True)
    result = bl.load_task_dir(task_dir)
    assert result.ok and result.task is not None
    assert result.task.id == "t1-speed"
    assert result.task.source_path == task_dir / "task.yaml"
    assert bl.task_dir_of(result.task) == task_dir
    assert result.errors == ()


def test_load_task_or_raise_returns_task(tmp_path):
    task_dir = write_task(tmp_path, "t1-speed", valid_data())
    assert bl.load_task_or_raise(task_dir).id == "t1-speed"


def test_read_dir_files_keeps_relative_paths(tmp_path):
    task_dir = write_task(tmp_path, "t1-speed", valid_data())
    files = bl.read_dir_files(task_dir / "files")
    assert files == {"stats.py": "def mean(v): return 0\n", "sub/util.py": "X = 1\n"}
    assert bl.read_dir_files(task_dir / "tests") == {"test_stats.py": "def test_mean(): pass\n"}
    assert bl.read_dir_files(task_dir / "no-such-dir") == {}


def test_read_solution_files_maps_solution_prefix(tmp_path):
    task_dir = write_task(tmp_path, "t1-speed", valid_data(), with_solution=True)
    assert bl.read_solution_files(task_dir) == {"stats.py": "def mean(v): return 1\n"}
    assert bl.read_solution_files(tmp_path / "no-such-dir") == {}


def test_read_dir_files_rejects_non_utf8(tmp_path):
    task_dir = write_task(tmp_path, "t1-speed", valid_data())
    (task_dir / "files" / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ValueError, match="blob.bin"):
        bl.read_dir_files(task_dir / "files")


# ---------------------------------------------------------------- load errors
def test_missing_task_yaml_is_structured_error_not_exception(tmp_path):
    task_dir = tmp_path / "empty-task"
    task_dir.mkdir()
    result = bl.load_task_dir(task_dir)
    assert not result.ok and result.task is None
    assert len(result.errors) == 1
    assert result.errors[0].field == "task.yaml" and result.errors[0].code == "missing_file"


def test_invalid_task_names_the_field(tmp_path):
    data = valid_data()
    del data["par_seconds"]  # axes khai Tốc độ mà thiếu par_seconds
    task_dir = write_task(tmp_path, "t1-speed", data)
    result = bl.load_task_dir(task_dir)
    assert not result.ok
    assert any(e.field == "par_seconds" and e.code == "missing_required_for_axis"
               for e in result.errors)


def test_load_task_or_raise_raises_structured_error(tmp_path):
    task_dir = write_task(tmp_path, "t1-speed", valid_data(kind="bogus"))
    with pytest.raises(BenchTaskInvalid) as excinfo:
        bl.load_task_or_raise(task_dir)
    assert any(e.field == "kind" for e in excinfo.value.errors)


def test_yaml_syntax_error_is_structured(tmp_path):
    task_dir = tmp_path / "t1-speed"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    result = bl.load_task_dir(task_dir)
    assert not result.ok
    assert result.errors[0].code == "yaml_error"


# ---------------------------------------------------------------- discover
def test_discover_tasks_lists_valid_and_invalid(tmp_path):
    write_task(tmp_path, "good-one", valid_data(id="good-one"))
    write_task(tmp_path, "bad-one", valid_data(id="bad-one", kind="bogus"))
    (tmp_path / "not-a-task").mkdir()
    (tmp_path / "not-a-task" / "README.md").write_text("no task.yaml here\n", encoding="utf-8")
    results = bl.discover_tasks(tmp_path)
    assert [r.task_dir.name for r in results] == ["bad-one", "good-one"]
    by_name = {r.task_dir.name: r for r in results}
    assert by_name["good-one"].ok
    assert not by_name["bad-one"].ok
    assert bl.discover_tasks(tmp_path / "no-such-root") == []


def test_discover_tasks_reports_duplicate_id(tmp_path):
    write_task(tmp_path, "first-copy", valid_data(id="same-id"))
    write_task(tmp_path, "second-copy", valid_data(id="same-id"))
    results = bl.discover_tasks(tmp_path)
    assert len(results) == 2
    assert results[0].ok
    assert not results[1].ok and results[1].task is None
    assert results[1].errors[0].code == "duplicate_id"
    assert "same-id" in results[1].errors[0].message


# ---------------------------------------------------------------- committed fixtures
FIXTURES = Path(__file__).parent / "fixtures" / "bench"


def test_committed_kite_fixtures_load_and_validate():
    cases = [
        ("kite-tb-hello", "terminal", ("Coding",)),
        ("kite-swe-mini", "repo", ("Coding", "Đúng việc")),
    ]
    for name, kind, axes in cases:
        result = bl.load_task_dir(FIXTURES / name)
        assert result.ok, (name, result.errors)
        assert result.task.kind == kind and result.task.axes == axes
        assert result.task.language == "en"


def test_committed_kite_tb_fixture_matches_importer_output(tmp_path):
    """The committed Kite form of tb-hello agrees with what the importer
    writes from the TB source (same id/kind/axes/goal/timeouts)."""
    src = tmp_path / "tb-hello"
    shutil.copytree(FIXTURES / "tb-hello", src)
    imported = bi.import_terminal_bench(src, tmp_path / "out")
    assert imported.ok, imported.reason
    committed = bl.load_task_or_raise(FIXTURES / "kite-tb-hello")
    fresh = bl.load_task_or_raise(imported.dest)
    for field in ("id", "goal", "kind", "language", "axes", "difficulty", "timeouts"):
        assert getattr(fresh, field) == getattr(committed, field), field
    assert fresh.environment == committed.environment
