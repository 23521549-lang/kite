"""Importer Terminal-Bench (worker (b)): the field map holds, tests are kept
verbatim, and anything unmappable is skipped with an explicit reason."""

from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

import pytest
import yaml

from poc.kaggle_worker import bench_importers as bi
from poc.kaggle_worker import bench_loader as bl

FIXTURES = Path(__file__).parent / "fixtures" / "bench"


def copy_tb_source(tmp_path: Path, name: str = "tb-hello") -> Path:
    src = tmp_path / name
    shutil.copytree(FIXTURES / "tb-hello", src)
    return src


# ---------------------------------------------------------------- SWE-bench
def _git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def make_sample_checkout(tmp_path: Path) -> tuple[Path, str]:
    """A tiny git repo at its base commit: calc.py with mean() only, plus a
    passing test for mean. Returns (repo path, base SHA)."""
    repo = tmp_path / "sample-calc"
    repo.mkdir()
    (repo / "calc.py").write_text(
        "def mean(values):\n    return sum(values) / len(values)\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import mean\n\n\ndef test_mean():\n    assert mean([2, 4]) == 3\n",
        encoding="utf-8")
    _git("init", "-q", cwd=repo)
    _git("-c", "user.name=kite-bench", "-c", "user.email=bench@kite.local",
         "add", "-A", cwd=repo)
    _git("-c", "user.name=kite-bench", "-c", "user.email=bench@kite.local",
         "commit", "-qm", "base: mean only", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return repo, sha


def load_swe_instance(tmp_path: Path, *, base_commit: str) -> dict:
    instance = json.loads((FIXTURES / "swe-mini-instance.json").read_text(encoding="utf-8"))
    instance["base_commit"] = base_commit
    return instance


def test_diff_files_and_is_test_path():
    patch = ("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
             "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n")
    assert bi._diff_files(patch) == ["calc.py", "tests/test_x.py"]
    assert bi._diff_files("diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n") == []
    assert bi._is_test_path("tests/test_x.py") and bi._is_test_path("pkg/test_util.py")
    assert bi._is_test_path("pkg/foo_test.py") and not bi._is_test_path("calc.py")


def test_swebench_to_task_data_maps_fields():
    instance = {"instance_id": "sample__calc-1", "repo": "sample/calc",
                "base_commit": "abc1234", "problem_statement": " Add median. ",
                "FAIL_TO_PASS": ["tests/test_median.py::test_median"], "PASS_TO_PASS": []}
    data = bi.swebench_to_task_data(instance, timeouts={"setup": 600, "test": 300},
                                    expected_files=["calc.py"])
    assert data["id"] == "sample__calc-1" and data["kind"] == "repo"
    assert data["goal"] == "Add median." and data["language"] == "en"
    assert data["axes"] == ["Coding", "Đúng việc"]
    assert data["expected_files"] == ["calc.py"]
    assert data["repo"] == "sample/calc" and data["base_commit"] == "abc1234"
    assert data["difficulty"] == bi.SWE_DIFFICULTY_UNRATED
    assert data["swe_bench"]["FAIL_TO_PASS"] == ["tests/test_median.py::test_median"]
    assert bi.swebench_to_task_data(instance, timeouts={"setup": 600},
                                    expected_files=[])["axes"] == ["Coding"]


def test_import_swebench_sample_maps_fields(tmp_path):
    repo, sha = make_sample_checkout(tmp_path)
    instance = load_swe_instance(tmp_path, base_commit=sha)
    result = bi.import_swebench(instance, tmp_path / "out",
                                timeouts={"setup": 600, "test": 300}, repo_checkout=repo)
    assert result.ok, result.reason
    assert result.task_id == "sample__calc-1" and result.source == "swe-bench"
    assert any("environment trống" in w for w in result.warnings)
    task, errors = bl.load_task_file(result.dest / "task.yaml")
    assert errors == [] and task is not None
    assert task.kind == "repo" and task.language == "en"
    assert task.repo == "sample/calc" and task.base_commit == sha
    assert task.axes == ("Coding", "Đúng việc")
    assert task.expected_files == ("calc.py",)
    assert task.difficulty == bi.SWE_DIFFICULTY_UNRATED
    assert task.timeouts == {"setup": 600, "test": 300}
    assert task.environment == {}
    # tests/ mirrors the repo tree for kind=repo: the hidden test paths
    # ARE repo-relative paths (what RepoTask.hidden carries).
    hidden = (result.dest / "tests" / "tests" / "test_median.py").read_text(encoding="utf-8")
    assert "def test_median" in hidden and "median([3, 1, 2])" in hidden
    raw = yaml.safe_load((result.dest / "task.yaml").read_text(encoding="utf-8"))
    assert raw["swe_bench"]["FAIL_TO_PASS"] == ["tests/test_median.py::test_median"]
    assert (result.dest / "swe_bench_patch.diff").read_text(encoding="utf-8") == instance["patch"]
    assert (result.dest / "swe_bench_test_patch.diff").read_text(encoding="utf-8") == instance["test_patch"]


def test_imported_swebench_sample_passes_with_gold_patch(tmp_path):
    """The sample is real: base + test patch fails, base + test patch +
    gold patch passes."""
    repo, sha = make_sample_checkout(tmp_path)
    instance = load_swe_instance(tmp_path, base_commit=sha)
    result = bi.import_swebench(instance, tmp_path / "out",
                                timeouts={"setup": 600, "test": 300}, repo_checkout=repo)
    assert result.ok, result.reason
    work = tmp_path / "work"
    shutil.copytree(repo, work)
    _git("checkout", "-q", sha, cwd=work)
    for name in ("swe_bench_test_patch.diff", "swe_bench_patch.diff"):
        patch_file = tmp_path / name
        patch_file.write_text((result.dest / name).read_text(encoding="utf-8"), encoding="utf-8")
        _git("apply", str(patch_file), cwd=work)
    run = subprocess.run(["python", "-m", "pytest", "-q", "tests"],
                         cwd=work, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr


def test_import_swebench_skips_without_checkout(tmp_path):
    instance = load_swe_instance(tmp_path, base_commit="abc1234")
    result = bi.import_swebench(instance, tmp_path / "out",
                                timeouts={"setup": 600, "test": 300}, repo_checkout=None)
    assert result.skipped and "checkout" in result.reason


def test_import_swebench_skips_missing_field(tmp_path):
    repo, sha = make_sample_checkout(tmp_path)
    instance = load_swe_instance(tmp_path, base_commit=sha)
    del instance["problem_statement"]
    result = bi.import_swebench(instance, tmp_path / "out",
                                timeouts={"setup": 600, "test": 300}, repo_checkout=repo)
    assert result.skipped and "problem_statement" in result.reason


def test_import_swebench_skips_unappliable_test_patch(tmp_path):
    repo, sha = make_sample_checkout(tmp_path)
    instance = load_swe_instance(tmp_path, base_commit=sha)
    instance["test_patch"] = instance["test_patch"].replace("test_median", "test_nope")
    # The patch still applies (new file with another name) — corrupt it instead.
    instance["test_patch"] = "this is not a patch\n"
    result = bi.import_swebench(instance, tmp_path / "out",
                                timeouts={"setup": 600, "test": 300}, repo_checkout=repo)
    assert result.skipped
    assert "test_patch" in result.reason


def test_import_swebench_requires_timeouts_from_caller(tmp_path):
    repo, sha = make_sample_checkout(tmp_path)
    instance = load_swe_instance(tmp_path, base_commit=sha)
    with pytest.raises(ValueError, match="timeouts"):
        bi.import_swebench(instance, tmp_path / "out", timeouts={}, repo_checkout=repo)
    with pytest.raises(ValueError, match="timeouts"):
        bi.import_swebench(instance, tmp_path / "out",
                           timeouts={"test": -1}, repo_checkout=repo)


def test_import_swebench_test_only_patch_scores_coding_only(tmp_path):
    repo, sha = make_sample_checkout(tmp_path)
    instance = load_swe_instance(tmp_path, base_commit=sha)
    instance["patch"] = ("diff --git a/tests/test_calc.py b/tests/test_calc.py\n"
                         "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n"
                         "@@ -1,4 +1,5 @@\n from calc import mean\n+\n \n \n def test_mean():\n")
    result = bi.import_swebench(instance, tmp_path / "out",
                                timeouts={"setup": 600, "test": 300}, repo_checkout=repo)
    assert result.ok, result.reason
    task = bl.load_task_or_raise(result.dest)
    assert task.axes == ("Coding",) and task.expected_files is None
    assert any("chỉ chấm trục Coding" in w for w in result.warnings)
    src = copy_tb_source(tmp_path)
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.ok, result.reason
    assert result.task_id == "tb-hello"
    assert result.source == "terminal-bench"
    task, errors = bl.load_task_file(result.dest / "task.yaml")
    assert errors == [] and task is not None
    # Field map (see bench_importers module docstring):
    assert task.kind == "terminal"
    assert task.language == "en"
    assert task.goal.startswith("Create a file named answer.txt")
    assert task.axes == ("Coding",)
    assert task.difficulty == "easy"
    assert task.timeouts == {"agent": 120, "test": 60}
    assert task.environment == {"dockerfile": "Dockerfile"}
    raw = yaml.safe_load((result.dest / "task.yaml").read_text(encoding="utf-8"))
    assert raw["terminal_bench"]["category"] == "scripting"
    assert raw["terminal_bench"]["parser_name"] == "pytest"
    assert raw["terminal_bench"]["tags"] == ["scripting", "files"]


def test_import_tb_keeps_tests_verbatim_and_dockerfile(tmp_path):
    src = copy_tb_source(tmp_path)
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.ok, result.reason
    dest = result.dest
    assert (dest / "tests" / "test_outputs.py").read_text(encoding="utf-8") == \
        (src / "tests" / "test_outputs.py").read_text(encoding="utf-8")
    assert (dest / "Dockerfile").read_text(encoding="utf-8") == \
        (src / "Dockerfile").read_text(encoding="utf-8")
    assert (dest / "run-tests.sh").read_text(encoding="utf-8") == \
        (src / "run-tests.sh").read_text(encoding="utf-8")
    # files/ exists but is EMPTY: the TB starting state lives in the image.
    assert (dest / "files").is_dir() and not any((dest / "files").iterdir())
    # solution.sh is a script, not a file patch: kept as reference only.
    assert (dest / "tb-solution" / "solution.sh").read_text(encoding="utf-8") == \
        (src / "solution.sh").read_text(encoding="utf-8")
    assert not list(dest.glob("solution.*"))


def test_imported_tb_task_passes_with_reference_solution(tmp_path):
    """The sample is real: solve.sh makes the hidden tests pass."""
    import subprocess
    src = copy_tb_source(tmp_path)
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.ok, result.reason
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["sh", str(src / "solution.sh")], cwd=work, check=True)
    run = subprocess.run(["python", "-m", "pytest", "-q",
                          str(result.dest / "tests" / "test_outputs.py")],
                         cwd=work, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr


def test_import_tb_uses_tb_timeout_defaults_with_warning(tmp_path):
    src = copy_tb_source(tmp_path)
    data = yaml.safe_load((src / "task.yaml").read_text(encoding="utf-8"))
    del data["max_agent_timeout_sec"]
    del data["max_test_timeout_sec"]
    (src / "task.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.ok, result.reason
    task = bl.load_task_or_raise(result.dest)
    assert task.timeouts == {"agent": bi.TB_DEFAULT_AGENT_TIMEOUT_SEC,
                             "test": bi.TB_DEFAULT_TEST_TIMEOUT_SEC}
    assert any("mặc định của Terminal-Bench" in w for w in result.warnings)


def test_import_tb_task_id_override(tmp_path):
    src = copy_tb_source(tmp_path)
    result = bi.import_terminal_bench(src, tmp_path / "out", task_id="my-tb-task")
    assert result.ok, result.reason
    assert result.task_id == "my-tb-task"
    assert (tmp_path / "out" / "my-tb-task").is_dir()


# ---------------------------------------------------------------- skips
def test_import_tb_skips_when_not_a_tb_task(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = bi.import_terminal_bench(empty, tmp_path / "out")
    assert result.skipped and result.task_id == "empty"
    assert "task.yaml" in result.reason


def test_import_tb_skips_without_instruction(tmp_path):
    src = copy_tb_source(tmp_path)
    data = yaml.safe_load((src / "task.yaml").read_text(encoding="utf-8"))
    del data["instruction"]
    (src / "task.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.skipped
    assert "instruction" in result.reason


def test_import_tb_skips_non_pytest_parser(tmp_path):
    src = copy_tb_source(tmp_path)
    data = yaml.safe_load((src / "task.yaml").read_text(encoding="utf-8"))
    data["parser_name"] = "swebench"
    (src / "task.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.skipped
    assert "swebench" in result.reason and "pytest" in result.reason


def test_import_tb_skips_without_tests_or_dockerfile(tmp_path):
    src = copy_tb_source(tmp_path)
    shutil.rmtree(src / "tests")
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.skipped and "tests/" in result.reason

    src = copy_tb_source(tmp_path, "tb-hello2")
    (src / "Dockerfile").unlink()
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.skipped and "Dockerfile" in result.reason


def test_import_tb_skips_bad_timeout(tmp_path):
    src = copy_tb_source(tmp_path)
    data = yaml.safe_load((src / "task.yaml").read_text(encoding="utf-8"))
    data["max_test_timeout_sec"] = -5
    (src / "task.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    result = bi.import_terminal_bench(src, tmp_path / "out")
    assert result.skipped and "max_test_timeout_sec" in result.reason


def test_import_tb_skips_existing_dest(tmp_path):
    src = copy_tb_source(tmp_path)
    first = bi.import_terminal_bench(src, tmp_path / "out")
    assert first.ok, first.reason
    second = bi.import_terminal_bench(src, tmp_path / "out")
    assert second.skipped and "đã tồn tại" in second.reason


# ---------------------------------------------------------------- instance input (F5: CLI + web share this)
def test_parse_swebench_instances_single_object_array_and_jsonl():
    one = json.dumps({"instance_id": "a__1", "repo": "x/y"})
    assert bi.parse_swebench_instances(one) == [{"instance_id": "a__1", "repo": "x/y"}]
    assert bi.parse_swebench_instances(f"[{one}, {json.dumps({'instance_id': 'b__2'})}]")[1]["instance_id"] == "b__2"
    assert bi.parse_swebench_instances(one + "\n" + json.dumps({"instance_id": "b__2"}) + "\n")[0]["instance_id"] == "a__1"


def test_parse_swebench_instances_rejects_bad_input():
    with pytest.raises(ValueError, match="trống"):
        bi.parse_swebench_instances("   ")
    with pytest.raises(ValueError, match="lỗi cú pháp"):
        bi.parse_swebench_instances("not json at all")
    with pytest.raises(ValueError, match="JSON object"):
        bi.parse_swebench_instances(json.dumps(["a", "b"]))


def test_pick_swebench_instance_rules():
    only = [{"instance_id": "a__1"}]
    assert bi.pick_swebench_instance(only)["instance_id"] == "a__1"
    two = only + [{"instance_id": "b__2"}]
    assert bi.pick_swebench_instance(two, "b__2")["instance_id"] == "b__2"
    with pytest.raises(ValueError, match="--instance-id"):
        bi.pick_swebench_instance(two)
    with pytest.raises(ValueError, match="không có instance 'zzz'"):
        bi.pick_swebench_instance(two, "zzz")
    with pytest.raises(ValueError, match="không có instance"):
        bi.pick_swebench_instance([])


def test_load_swebench_instance_file_picks_by_id(tmp_path):
    path = tmp_path / "instances.jsonl"
    path.write_text(json.dumps({"instance_id": "a__1"}) + "\n" + json.dumps({"instance_id": "b__2"}) + "\n",
                    encoding="utf-8")
    assert bi.load_swebench_instance_file(path, "b__2")["instance_id"] == "b__2"
    with pytest.raises(ValueError, match="không đọc được file instance"):
        bi.load_swebench_instance_file(tmp_path / "no-such.json")
