"""Code map (graphify graph -> prompt text): parsing, lookups, callers, impact, repo context, run_task."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from poc.kaggle_worker import codemap
from poc.kaggle_worker import sandbox as sbx

CALC = ("def average(xs):\n    return sum(xs) / len(xs) - 1\n\n\n"
        "class Stats:\n    def mean(self, xs):\n        return average(xs)\n")
MAIN = ("from calc import average, Stats\n\ndef report(xs):\n    print(average(xs))\n"
        "    s = Stats()\n    return s.mean(xs)\n")
TEST = "from calc import average\n\ndef test_empty():\n    assert average([]) == 0\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    for name, text in {"calc.py": CALC, "main.py": MAIN, "test_calc.py": TEST}.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def graph_data() -> dict:
    """What graphify writes for make_repo (trimmed to the fields Kite reads)."""
    def node(i, label, path, line, **extra):
        return {"id": i, "label": label, "file_type": "code", "source_file": path, "source_location": f"L{line}", **extra}

    def link(a, b, relation, path, line):
        return {"source": a, "target": b, "relation": relation, "source_file": path, "source_location": f"L{line}"}

    return {"nodes": [
        node("calc", "calc.py", "calc.py", 1), node("calc_average", "average()", "calc.py", 1, _callable=True),
        node("calc_stats", "Stats", "calc.py", 5, _callable_class=True), node("calc_stats_mean", ".mean()", "calc.py", 6),
        node("main", "main.py", "main.py", 1), node("main_report", "report()", "main.py", 3),
        node("test_calc", "test_calc.py", "test_calc.py", 1), node("test_empty", "test_empty()", "test_calc.py", 3),
        {"id": "why", "label": "Mean of xs, 0 for an empty list.", "file_type": "rationale",
         "source_file": "calc.py", "source_location": "L2"},
    ], "links": [
        link("calc", "calc_average", "contains", "calc.py", 1), link("calc", "calc_stats", "contains", "calc.py", 5),
        link("calc_stats", "calc_stats_mean", "method", "calc.py", 6),
        link("main", "main_report", "contains", "main.py", 3), link("main", "calc", "imports_from", "main.py", 1),
        link("main_report", "calc_average", "calls", "main.py", 4), link("main_report", "calc_stats", "calls", "main.py", 5),
        link("test_calc", "test_empty", "contains", "test_calc.py", 3),
        link("test_empty", "calc_average", "calls", "test_calc.py", 4),
        link("why", "calc_average", "rationale_for", "calc.py", 2),
    ]}


def write_graph(tmp_path: Path) -> Path:
    path = tmp_path / "graph" / "graphify-out" / "graph.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(graph_data()))
    return path


def test_graph_symbols_parents_callers_and_instance_calls(tmp_path):
    repo = make_repo(tmp_path)
    graph = codemap.CodeGraph(graph_data(), repo)
    assert {s.kind for s in graph.lookup("average")} == {"function"}
    assert graph.lookup("calc.py")[0].kind == "module" and graph.lookup("Stats")[0].kind == "class"
    mean = graph.lookup("Stats.mean")[0]
    assert graph.qualified(mean) == "Stats.mean()" and graph.lookup("Other.mean") == []
    text = "\n".join(graph.describe(graph.lookup("average")[0]))
    assert "average() — định nghĩa ở calc.py:1" in text and "Mean of xs" in text
    assert "main.py:4 (report())" in text and "test dùng: test_calc.py:4 (test_empty())" in text
    # s.mean(xs) is an edge to the class only; the scan of files that use Stats finds the call.
    assert "main.py:6 (report())" in "\n".join(graph.describe(mean))
    assert "L1 average() · L5 class Stats · L6 Stats.mean()" == graph.outline("calc.py")
    assert codemap.CodeGraph.load(tmp_path / "missing.json") is None


def test_names_in_a_vietnamese_goal():
    goal = ("Sửa TelemetryEventsTable.query_since trong services/backend/core/tables.py để đọc hết các trang "
            "(LastEvaluatedKey), thêm hằng `MAX_QUERY_PAGES`; không đổi hàm list_tenants.")
    assert codemap.goal_names(goal) == ["TelemetryEventsTable.query_since", "LastEvaluatedKey",
                                        "MAX_QUERY_PAGES", "list_tenants"]


def test_goal_map_impact_and_failing_targets(tmp_path):
    repo = make_repo(tmp_path)
    graph = codemap.CodeGraph(graph_data(), repo)
    text, found = codemap.goal_map(graph, "Sửa average và Stats.mean", [], workspace=repo)
    assert [s.name for s in found] == ["mean"] and "main.py:6" in text
    # A plain word (average) is used only when nothing code-like and no file pointed anywhere.
    text, found = codemap.goal_map(graph, "Sửa average cho list rỗng", [], workspace=repo)
    assert [s.name for s in found] == ["average"] and "main.py:4" in text
    assert codemap.goal_map(graph, "Sửa average trong calc.py", ["calc.py"], workspace=repo)[1] == []
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "calc.py").write_text(CALC.replace("- 1", "if xs else 0"))
    assert codemap.changed_lines(repo, base) == {"calc.py": [2]}
    impact = codemap.impact(graph, repo, base)
    assert impact.startswith("- average()") and "main.py:4 (report())" in impact and "Stats" not in impact
    output = "FAILED test_calc.py::test_empty - ZeroDivisionError\n1 failed\n"
    assert codemap.failing_targets(graph, output) == "- test_empty gọi: average() (calc.py:1)"


def test_build_is_local_reused_and_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    seen = {}

    def runner(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        out = Path(cmd[cmd.index("--out") + 1]) / "graphify-out"
        out.mkdir(parents=True)
        (out / "graph.json").write_text("{}")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    path = codemap.build(tmp_path / "work", tmp_path / "g", runner=runner, cli=["graphify"])
    assert path == tmp_path / "g" / "graphify-out" / "graph.json"
    assert seen["cmd"][:3] == ["graphify", "extract", str(tmp_path / "work")] and "--code-only" in seen["cmd"]
    assert "OPENAI_API_KEY" not in seen["env"]
    assert codemap.build(tmp_path / "work", tmp_path / "g", runner=None, cli=["graphify"]) == path   # reused
    assert codemap.build(tmp_path / "work", tmp_path / "h") is None              # no graphify installed
    failing = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom")
    assert codemap.build(tmp_path / "work", tmp_path / "i", runner=failing, cli=["graphify"]) is None


def test_repo_context_with_a_map_shows_definitions_with_line_numbers(tmp_path):
    repo = make_repo(tmp_path)
    graph = codemap.CodeGraph(graph_data(), repo)
    context = sbx.repo_context(repo, "Sửa Stats.mean cho đúng", graph=graph)
    assert "Bản đồ code" in context and "Stats.mean() — định nghĩa ở calc.py:6" in context
    assert "----- calc.py dòng 6-7: Stats.mean() -----" in context and "    7|         return average(xs)" in context
    plain = sbx.repo_context(repo, "Sửa Stats.mean cho đúng")             # no graph: git grep finds it
    assert "Bản đồ code" not in plain and "calc.py dòng 6-7: Stats.mean" in plain


def test_large_repositories_get_a_folder_summary_and_the_relevant_files_first(tmp_path):
    repo = make_repo(tmp_path)
    for i in range(30):
        (repo / "assets").mkdir(exist_ok=True)
        (repo / "assets" / f"font{i}.woff2").write_bytes(b"x")
        (repo / "docs" / f"d{i:02}").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / f"d{i:02}" / "a.md").write_text("doc\n")
    (repo / "pkg" / "core").mkdir(parents=True)
    (repo / "pkg" / "core" / "engine.py").write_text("def run_engine():\n    return 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "more")
    context = sbx.repo_context(repo, "Sửa run_engine", max_files=10)
    assert "30 file font/ảnh/lock không liệt kê" in context and "font1.woff2" not in context
    assert "Thư mục (số file):" in context and "  docs/d00/ (1)" in context
    listing = context.split("Các file trong repo", 1)[1].split("-----", 1)[0]
    assert "pkg/core/engine.py" in listing and "calc.py" in listing and "docs/d29/a.md" not in listing
    assert "pkg/core/engine.py dòng 1-2: run_engine" in context


def test_failure_digest_keeps_the_first_traceback_and_the_summary():
    output = ("." * 500 + "F\n" + "=" * 10 + " FAILURES " + "=" * 10 + "\n" + "_ test_a _\n    assert 1 == 2\nE   first\n"
              + "x" * 5000 + "\n" + "=" * 10 + " short test summary info " + "=" * 10 + "\n"
              + "FAILED t.py::test_a\nFAILED t.py::test_b\n2 failed\n")
    digest = sbx.failure_digest(output, 1200)
    assert "E   first" in digest and "FAILED t.py::test_b" in digest and len(digest) < 1300
    assert sbx.failure_digest("1 failed\nboom\n", 100) == "1 failed\nboom\n"
    prompt = sbx.fix_prompt("g", "pytest", output, 1, 2, "- test_a gọi: f() (a.py:1)")
    assert "Test thất bại gọi tới" in prompt and prompt.index("```") < prompt.index("test_a gọi")
    assert "bắt đầu từ danh sách" in sbx.review_prompt("g", "pytest", "- f() — ...")
    assert "bắt đầu từ danh sách" not in sbx.review_prompt("g", "pytest")


def test_run_task_uses_the_map_in_the_first_prompt_and_the_review(tmp_path, monkeypatch):
    from test_sandbox import FakeDocker, SETTINGS

    graph_file = write_graph(tmp_path)
    monkeypatch.setattr(codemap, "build", lambda workspace, out_dir, **kw: graph_file)
    repo = make_repo(tmp_path)
    fake = FakeDocker()
    run = sbx.run_task(SETTINGS, repo=repo, goal="Sửa average", model="qwen3.6", models=["qwen3.6"],
                       state_dir=tmp_path / "state", task_id="m1", runner=fake, log=lambda _m: None)
    assert run.verified
    assert "Bản đồ code" in fake.call("agent")[-1] and "main.py:4 (report())" in fake.call("agent")[-1]
    review = fake.call("review")[-1]
    assert "những chỗ đang dùng nó" in review and "main.py:4 (report())" in review
    from dataclasses import replace
    off = FakeDocker()
    sbx.run_task(replace(SETTINGS, code_map=False), repo=repo, goal="Sửa average", model="qwen3.6",
                 models=["qwen3.6"], state_dir=tmp_path / "state", task_id="m2", runner=off, log=lambda _m: None)
    assert "Bản đồ code" not in off.call("agent")[-1] and "những chỗ đang dùng nó" not in off.call("review")[-1]


@pytest.mark.graphify
def test_real_graphify_maps_the_callers(tmp_path):
    if codemap.graphify_cli() is None:
        pytest.skip("graphify is not installed (pip install graphifyy)")
    repo = make_repo(tmp_path)
    graph = codemap.CodeGraph.load(codemap.build(repo, tmp_path / "g"), repo)
    assert graph is not None
    text = "\n".join(graph.describe(graph.lookup("average")[0]))
    assert "main.py:4" in text and "test_calc.py:4" in text
    assert subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout == ""
