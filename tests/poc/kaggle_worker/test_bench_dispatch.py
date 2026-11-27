"""Dispatch by v1 kind (worker (b)): each kind takes its existing path, and a
kind with no runnable path fails with an explicit error — never silently."""

from __future__ import annotations

from pathlib import Path

import yaml

from poc.kaggle_worker import bench
from poc.kaggle_worker import bench_loader as bl
from poc.kaggle_worker.bench_standard import BenchTask


def write_task(root: Path, task_id: str, data: dict, *, files=None, tests=None,
               solution=None) -> Path:
    task_dir = root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    if files is not None:
        (task_dir / "files").mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            target = task_dir / "files" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    if tests is not None:
        (task_dir / "tests").mkdir(parents=True, exist_ok=True)
        for name, content in tests.items():
            target = task_dir / "tests" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    if solution is not None:
        for name, content in solution.items():
            (task_dir / f"solution.{name}").write_text(content, encoding="utf-8")
    return task_dir


def base_data(**overrides) -> dict:
    data = {
        "id": "t1",
        "goal": "Sửa hàm mean để test qua.",
        "kind": "code",
        "language": "vi",
        "axes": ["Coding"],
        "difficulty": "dễ",
        "timeouts": {"setup": 600, "test": 300},
        "environment": {},
    }
    data.update(overrides)
    return data


def code_task_dir(tmp_path: Path, task_id: str = "t1-code", kind: str = "code") -> Path:
    return write_task(
        tmp_path, task_id, base_data(id=task_id, kind=kind),
        files={"stats.py": "def mean(v): return 0\n"},
        tests={"test_stats.py": "def test_mean(): pass\n"},
        solution={"stats.py": "def mean(v): return 1\n"})


def passing_run() -> dict:
    """A run dict as a successful code run reports it (verified + hidden ok)."""
    return {"verified": True, "hidden_exit_code": 0, "changed_files": [],
            "agent_status": "ok", "wall_seconds": 12.5}


def fake_post(final: str):
    calls = []

    def post(body: dict) -> dict:
        calls.append(body)
        return {"choices": [{"message": {"content": final, "tool_calls": []}}]}

    post.calls = calls  # type: ignore[attr-defined]
    return post


# ---------------------------------------------------------------- conversions
def test_task_to_code_task_reads_files_tests_solution(tmp_path):
    task_dir = code_task_dir(tmp_path)
    task = bl.load_task_or_raise(task_dir)
    code_task = bench.task_to_code_task(task, task_dir)
    assert isinstance(code_task, bench.CodeTask)
    assert code_task.files == {"stats.py": "def mean(v): return 0\n"}
    assert code_task.hidden == {"test_stats.py": "def test_mean(): pass\n"}
    assert code_task.solution == {"stats.py": "def mean(v): return 1\n"}


def test_task_to_scenario_mirrors_block(tmp_path):
    task_dir = write_task(tmp_path, "s1", base_data(
        id="s1", kind="scenario", axes=["Suy luận"],
        scenario={"prompt": "Giao task đi", "expect": ["start_task"],
                  "category": "giao việc", "must": ["chat-bench-0001"],
                  "forbid": ["bash"]}), files={})
    task = bl.load_task_or_raise(task_dir)
    scenario = bench.task_to_scenario(task)
    assert isinstance(scenario, bench.Scenario)
    assert (scenario.id, scenario.category, scenario.prompt) == ("s1", "giao việc", "Giao task đi")
    assert scenario.expect == {"start_task"} and scenario.forbid == {"bash"}
    assert scenario.must == ["chat-bench-0001"]


def test_task_to_repo_task_uses_base_and_mirrored_tests(tmp_path):
    task_dir = write_task(tmp_path, "r1", base_data(
        id="r1", kind="repo", repo="sample/calc", base_commit="abc1234"),
        tests={"tests/test_median.py": "def test_median(): pass\n"})
    task = bl.load_task_or_raise(task_dir)
    repo_task = bench.task_to_repo_task(task, task_dir)
    assert isinstance(repo_task, bench.RepoTask)
    assert repo_task.base == "abc1234"
    assert repo_task.hidden == ("tests/test_median.py",)
    # fix is the documented placeholder (v1 has no fix SHA); hidden tests
    # come from the folder, never from `git show fix:`.
    assert repo_task.fix == "abc1234"


# ---------------------------------------------------------------- dispatch
def test_dispatch_code_goes_through_run_code_and_score_code(tmp_path):
    task_dir = code_task_dir(tmp_path)
    task = bl.load_task_or_raise(task_dir)
    seen = {}

    def run_code(code_task, model, out):
        seen["task"] = code_task
        seen["model"] = model
        return passing_run()

    outcome = bench.dispatch_v1_task(task, model="m1", post=fake_post(""),
                                     system="", tools=[], run_code=run_code,
                                     out=tmp_path / "out", log=lambda line: None)
    assert isinstance(seen["task"], bench.CodeTask) and seen["model"] == "m1"
    assert outcome["passed"] is True and outcome["id"] == "t1-code"
    assert outcome["kind"] == "code" and outcome["axes"] == ["Coding"]


def test_dispatch_terminal_uses_the_code_path(tmp_path):
    task_dir = code_task_dir(tmp_path, task_id="t1-term", kind="terminal")
    task = bl.load_task_or_raise(task_dir)
    seen = {}

    def run_code(code_task, model, out):
        seen["task"] = code_task
        return passing_run()

    outcome = bench.dispatch_v1_task(task, model="m1", post=fake_post(""),
                                     system="", tools=[], run_code=run_code,
                                     out=tmp_path / "out", log=lambda line: None)
    assert isinstance(seen["task"], bench.CodeTask)
    assert outcome["passed"] is True and outcome["kind"] == "terminal"


def test_dispatch_repo_goes_through_repo_task(tmp_path):
    task_dir = write_task(tmp_path, "r1", base_data(
        id="r1", kind="repo", repo="sample/calc", base_commit="abc1234"),
        tests={"tests/test_median.py": "def test_median(): pass\n"})
    task = bl.load_task_or_raise(task_dir)
    seen = {}

    def run_code(repo_task, model, out):
        seen["task"] = repo_task
        return passing_run()

    outcome = bench.dispatch_v1_task(task, model="m1", post=fake_post(""),
                                     system="", tools=[], run_code=run_code,
                                     out=tmp_path / "out", log=lambda line: None)
    assert isinstance(seen["task"], bench.RepoTask)
    assert seen["task"].hidden == ("tests/test_median.py",)
    assert outcome["passed"] is True
    # The folder's hidden tests ride along for the caller to inject.
    assert outcome["hidden_files"] == {"tests/test_median.py": "def test_median(): pass\n"}


def test_dispatch_scenario_runs_the_real_lead_with_scripted_results(tmp_path):
    task_dir = write_task(tmp_path, "s1", base_data(
        id="s1", kind="scenario", axes=["Suy luận"],
        scenario={"prompt": "Trả lời hello.", "expect": [],
                  "must": ["hello"]}), files={})
    task = bl.load_task_or_raise(task_dir)
    post = fake_post("Xin chào, kết quả là hello")

    def run_code(*args):
        raise AssertionError("scenario must not call run_code")

    outcome = bench.dispatch_v1_task(task, model="m1", post=post,
                                     system="Hệ thống", tools=[], run_code=run_code,
                                     out=tmp_path / "out", log=lambda line: None)
    assert len(post.calls) == 1  # the real lead was called once
    assert outcome["passed"] is True
    assert outcome["score"] == 1.0 and outcome["score_100"] == 100.0


def test_dispatch_qa_fails_explicitly_without_calling_any_runner(tmp_path):
    task_dir = write_task(tmp_path, "q1", base_data(id="q1", kind="qa"), files={})
    task = bl.load_task_or_raise(task_dir)

    def run_code(*args):
        raise AssertionError("qa must not call run_code")

    outcome = bench.dispatch_v1_task(task, model="m1", post=fake_post(""),
                                     system="", tools=[], run_code=run_code,
                                     out=tmp_path / "out", log=lambda line: None)
    assert outcome["passed"] is False
    assert "Q9-phụ" in outcome["error"] and "qa" in outcome["error"]


def test_dispatch_unknown_kind_fails_explicitly(tmp_path):
    task = BenchTask(id="x1", goal="g", kind="bogus", language="vi", axes=("Coding",),
                     difficulty="dễ", timeouts={"test": 60}, environment={})
    outcome = bench.dispatch_v1_task(task, model="m1", post=fake_post(""),
                                     system="", tools=[], run_code=lambda *a: {},
                                     out=tmp_path / "out", task_dir=tmp_path,
                                     log=lambda line: None)
    assert outcome["passed"] is False and "bogus" in outcome["error"]


def test_dispatch_code_without_run_code_fails_explicitly(tmp_path):
    task = bl.load_task_or_raise(code_task_dir(tmp_path))
    outcome = bench.dispatch_v1_task(task, model="m1", post=fake_post(""),
                                     system="", tools=[], run_code=None,
                                     out=tmp_path / "out", log=lambda line: None)
    assert outcome["passed"] is False and "run_code" in outcome["error"]


def test_dispatch_code_without_task_dir_fails_explicitly(tmp_path):
    task = BenchTask(id="t1", goal="g", kind="code", language="vi", axes=("Coding",),
                     difficulty="dễ", timeouts={"test": 60}, environment={})
    assert task.source_path is None
    outcome = bench.dispatch_v1_task(task, model="m1", post=fake_post(""),
                                     system="", tools=[],
                                     run_code=lambda *a: passing_run(),
                                     out=tmp_path / "out", log=lambda line: None)
    assert outcome["passed"] is False and "source_path" in outcome["error"]


def test_dispatch_runner_exception_becomes_failed_outcome(tmp_path):
    task = bl.load_task_or_raise(code_task_dir(tmp_path))

    def run_code(*args):
        raise RuntimeError("worker mất")

    outcome = bench.dispatch_v1_task(task, model="m1", post=fake_post(""),
                                     system="", tools=[], run_code=run_code,
                                     out=tmp_path / "out", log=lambda line: None)
    assert outcome["passed"] is False and outcome["error"] == "worker mất"


# ---------------------------------------------------------------- run_bench
def test_run_bench_with_v1_tasks_and_without(tmp_path):
    code_task = bl.load_task_or_raise(code_task_dir(tmp_path))
    scenario_task = bl.load_task_or_raise(write_task(
        tmp_path, "s1", base_data(id="s1", kind="scenario", axes=["Suy luận"],
                                  scenario={"prompt": "Trả lời hello.", "expect": [],
                                            "must": ["hello"]}), files={}))
    post = fake_post("Xin chào, kết quả là hello")
    out = tmp_path / "bench-out"
    results = bench.run_bench(
        models=["m1"], suites=[], post=post, out=out, system="", tools=[],
        run_code=lambda task, model, out_dir: passing_run(),
        v1_tasks=[code_task, scenario_task], log=lambda line: None)
    entries = results["models"]["m1"]["v1"]
    assert [e["id"] for e in entries] == ["t1-code", "s1"]
    assert all(e["passed"] for e in entries)
    assert (out / "results.json").is_file()

    # Old callers (no v1_tasks) see exactly the old behaviour: no "v1" key.
    out2 = tmp_path / "bench-out2"
    results2 = bench.run_bench(models=["m1"], suites=[], post=post, out=out2,
                               system="", tools=[], log=lambda line: None)
    assert "v1" not in results2["models"]["m1"]
