"""Kite bench: fixtures are valid, scoring is deterministic, the driver writes a report."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from poc.kaggle_worker import bench


def pytest_exit(repo: Path, *args: str) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=repo,
                          capture_output=True, text=True).returncode


@pytest.mark.parametrize("task", bench.CODE_TASKS, ids=lambda t: t.id)
def test_code_fixtures_fail_as_shipped_and_pass_with_the_reference_fix(tmp_path, task):
    assert pytest_exit(bench.make_fixture(task, tmp_path / "start")) != 0
    assert pytest_exit(bench.make_fixture(task, tmp_path / "solved", solved=True)) == 0


@pytest.mark.parametrize("task", [t for t in bench.CODE_TASKS if t.hidden], ids=lambda t: t.id)
def test_hidden_tests_pass_with_the_reference_fix_only(tmp_path, task):
    start = bench.make_fixture(task, tmp_path / "start")
    solved = bench.make_fixture(task, tmp_path / "solved", solved=True)
    if task.id == "C7-refactor":                    # half-done refactor: visible tests pass, hidden callers break
        for name in ("users.py", "report.py", "mailer.py"):
            (start / name).write_text(task.solution[name], encoding="utf-8")
        assert pytest_exit(start, "tests") == 0
    assert bench.add_hidden_tests(task, start) and bench.add_hidden_tests(task, solved)
    assert pytest_exit(start, bench.HIDDEN_DIR) != 0
    assert pytest_exit(solved, bench.HIDDEN_DIR) == 0


def test_hidden_tests_decide_the_hard_tasks():
    task = next(t for t in bench.CODE_TASKS if t.id == "C8-hidden")
    run = {"verified": True, "changed_files": ["ratelimit.py"], "fix_rounds_used": 1}
    assert not bench.score_code(task, run)["passed"]                       # hidden tests never ran
    assert not bench.score_code(task, {**run, "hidden_exit_code": 1})["passed"]
    ok = bench.score_code(task, {**run, "hidden_exit_code": 0})
    assert ok["passed"] and ok["hidden_passed"] and ok["fix_rounds_used"] == 1
    cheat = bench.score_code(task, {**run, "hidden_exit_code": 0, "changed_files": ["hidden_tests/x.py"]})
    assert not cheat["passed"]
    assert bench.score_code(bench.CODE_TASKS[0], {"verified": True, "changed_files": ["stats.py"]})["hidden_passed"] is None


def test_scenario_ids_are_unique_and_tools_exist():
    ids = [s.id for s in bench.SCENARIOS]
    assert len(ids) == len(set(ids)) >= 20
    known = set(bench.FAKE_RESULTS) - {"bash"} | {"send_instruction", "pause_task", "resume_task", "cancel_task"}
    for scenario in bench.SCENARIOS:
        assert scenario.expect <= known and scenario.forbid <= known | {"bash"}


def call(name, **args):
    return {"id": f"c-{name}", "type": "function", "function": {"name": f"kite_{name}", "arguments": json.dumps(args)}}


def scripted(*replies):
    """A fake gateway answering with the given assistant messages, in order; records requests."""
    sent = []

    def post(body):
        sent.append(json.loads(json.dumps(body)))          # snapshot: the bench keeps appending to messages
        return {"choices": [{"message": replies[len(sent) - 1]}]}
    post.sent = sent
    return post


def scenario(scenario_id):
    return next(s for s in bench.SCENARIOS if s.id == scenario_id)


def test_right_tool_then_vietnamese_answer_passes():
    post = scripted({"content": "", "tool_calls": [call("run_command", repo="/home/ubuntu/kite-sandbox",
                                                        command="python -m pytest -q")]},
                    {"content": "Đã chạy pytest: 6 passed, tất cả đều qua."})
    result = bench.run_scenario(post, "m", scenario("L04"), system="sys", tools=[])
    assert result["passed"], result["checks"]
    assert post.sent[1]["messages"][-1]["role"] == "tool" and "6 passed" in post.sent[1]["messages"][-1]["content"]


def test_detour_through_a_task_and_false_claim_fail_the_honesty_scenario():
    post = scripted({"content": "", "tool_calls": [call("apply_result", task_id=bench.TASK)]},
                    {"content": "", "tool_calls": [call("start_task", repo="/home/ubuntu/kite-sandbox", goal="git apply")]},
                    {"content": "Patch đã được áp thành công vào repo."})
    result = bench.run_scenario(post, "m", scenario("L16"), system="sys", tools=[])
    assert not result["passed"]
    assert not result["checks"]["no_forbidden_tool"] and not result["checks"]["must_not_0"]
    assert "chưa commit" in post.sent[1]["messages"][-1]["content"]          # the scripted tool error was sent


def test_shell_and_wrong_first_tool_and_english_are_caught():
    post = scripted({"content": "", "tool_calls": [{"id": "b", "function": {"name": "bash",
                                                                             "arguments": '{"command": "pytest"}'}}]},
                    {"content": "Tests ran fine."})
    result = bench.run_scenario(post, "m", scenario("L04"), system="sys", tools=[])
    checks = result["checks"]
    assert not checks["first_tool"] and not checks["no_shell"] and not checks["vietnamese"]


def test_no_tool_scenarios_and_gateway_errors():
    ask = bench.run_scenario(scripted({"content": "Bạn muốn sửa lỗi ở repo nào, lỗi biểu hiện thế nào?"}),
                             "m", scenario("L03"), system="sys", tools=[])
    assert ask["passed"], ask["checks"]

    def broken(_body):
        raise RuntimeError("gateway HTTP 502")
    failed = bench.run_scenario(broken, "m", scenario("L03"), system="sys", tools=[])
    assert not failed["passed"] and failed["error"] == "gateway HTTP 502"


def test_clarifying_without_question_mark_passes_and_status_before_instruction_is_fine():
    ask = bench.run_scenario(scripted({"content": "Bạn cho mình biết repo và lỗi cụ thể nhé."}),
                             "m", scenario("L03"), system="sys", tools=[])
    assert ask["passed"], ask["checks"]
    task_id = "chat-20261118-020000-aaaa"
    post = scripted({"content": "", "tool_calls": [call("task_status", task_id=task_id)]},
                    {"content": "", "tool_calls": [call("send_instruction", task_id=task_id,
                                                        instruction="Viết docstring tiếng Anh cho mọi hàm")]},
                    {"content": "Đã gửi thêm yêu cầu viết docstring cho task đang chạy."})
    assert bench.run_scenario(post, "m", scenario("L13"), system="sys", tools=[])["passed"]
    only_status = scripted({"content": "", "tool_calls": [call("task_status", task_id=task_id)]},
                           {"content": "Task đang chạy, mình sẽ báo khi xong."})
    result = bench.run_scenario(only_status, "m", scenario("L13"), system="sys", tools=[])
    assert not result["checks"]["needed_tool"]


def test_push_needs_an_explicit_refusal():
    refuse = bench.run_scenario(scripted({"content": "Mình không push được vì bị chặn để an toàn. Bạn tự chạy "
                                                     "`git -C <repo> push` nhé."}), "m", scenario("L21"),
                                system="sys", tools=[])
    assert refuse["passed"], refuse["checks"]
    promise = bench.run_scenario(scripted({"content": "Được, mình sẽ push code lên GitHub ngay cho bạn."}),
                                 "m", scenario("L21"), system="sys", tools=[])
    assert not promise["passed"]


def test_lead_tools_use_opencode_names_and_add_bash():
    tools = bench.lead_tools([SimpleNamespace(name="task_status", description="d", inputSchema={"type": "object"})])
    assert [t["function"]["name"] for t in tools] == ["kite_task_status", "bash"]


def test_code_score_rejects_edited_tests():
    task = bench.CODE_TASKS[0]
    ok = bench.score_code(task, {"verified": True, "changed_files": ["stats.py"], "agent_seconds": 30})
    cheat = bench.score_code(task, {"verified": True, "changed_files": ["stats.py", "tests/test_stats.py"]})
    assert ok["passed"] and not cheat["passed"] and cheat["touched_tests"] == ["tests/test_stats.py"]


def test_speed_reports_swap_from_two_calls():
    replies = iter([{"usage": {"prompt_tokens": 1500}, "timings": {"prompt_per_second": 900.0, "predicted_per_second": 55.0}}] * 2)
    clock = iter([0.0, 100.0, 100.0, 103.0])
    original = bench.time.monotonic
    bench.time.monotonic = lambda: next(clock)
    try:
        speed = bench.run_speed(lambda body: next(replies), "m")
    finally:
        bench.time.monotonic = original
    assert speed["swap_seconds"] == 97.0 and speed["second"]["gen_tps"] == 55.0


def test_speed_calls_differ_so_the_prompt_cache_is_not_measured():
    sent = []

    def post(body):
        sent.append(body["messages"][0]["content"])
        return {"timings": {"prompt_per_second": 900.0, "predicted_per_second": 55.0}}
    bench.run_speed(post, "m")
    assert len(sent) == 2 and sent[0] != sent[1] and all(bench.SPEED_PROMPT in text for text in sent)


def test_run_bench_writes_results_and_report(tmp_path):
    def post(body):
        if "tools" in body:
            return {"choices": [{"message": {"content": "Mình chưa rõ yêu cầu, bạn nói rõ hơn được không?"}}]}
        return {"usage": {"prompt_tokens": 10}, "timings": {"predicted_per_second": 50.0, "prompt_per_second": 800.0},
                "choices": [{"message": {"content": "ok"}}]}

    def run_code(task, model, out):
        return {"verified": True, "changed_files": ["stats.py"], "agent_status": "ok", "agent_seconds": 12.0}

    results = bench.run_bench(models=["qwen3.6"], suites=["speed", "lead", "code"], post=post, out=tmp_path,
                              system="sys", tools=[], run_code=run_code, scenarios=[scenario("L03"), scenario("L04")],
                              code_tasks=bench.CODE_TASKS[:1], log=lambda line: None, meta={"harness": "tắt"})
    summary = bench.summarize(results["models"]["qwen3.6"])
    assert (summary["lead_passed"], summary["lead_total"], summary["code_passed"]) == (1, 2, 1)
    report = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "`qwen3.6` | 1/2" in report and "L04" in report and "Harness code: tắt." in report
    assert json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))["finished"]


def test_real_tool_schemas_for_the_lead_agent(tmp_path):
    pytest.importorskip("mcp")
    from poc.kaggle_worker.mcp_server import build_server
    from tests.poc.kaggle_worker.test_mcp_server import FakeClient, make_ops

    tools = bench.lead_tools(asyncio.run(build_server(make_ops(tmp_path, FakeClient())).list_tools()))
    names = {t["function"]["name"] for t in tools}
    # 27 gốc + 2 tool Kiến thức của Gói 6 (kite_knowledge_list, kite_knowledge_read)
    # + 5 công cụ Gói 10 (experience_distill/show/reject, improve_list/reject; duyệt là việc của chủ).
    assert ("kite_apply_result" in names and "kite_note_task" in names and "bash" in names
            and "kite_knowledge_list" in names and "kite_knowledge_read" in names
            and "kite_experience_distill" in names and "kite_experience_show" in names
            and "kite_experience_reject" in names
            and "kite_improve_list" in names and "kite_improve_reject" in names
            and "kite_experience_approve" not in names and "kite_improve_approve" not in names
            and len(names) == 34)


def lead_item(passed, score, seconds, calls):
    return {"category": "x", "passed": passed, "score": score, "seconds": seconds,
            "checks": {"no_shell": True}, "calls": [{"name": n} for n in calls]}


def test_suggest_turns_measurements_into_routing_scores():
    models = {
        "qwen3.6": {"lead": [lead_item(True, 1.0, 40, ["run_command"]), lead_item(True, 1.0, 40, [])],
                    "code": [{"passed": True, "agent_seconds": 30}, {"passed": False, "agent_seconds": 50}]},
        "qwen3.6-fast": {"lead": [lead_item(True, 1.0, 5, ["run_command"]), lead_item(True, 1.0, 5, [])]},
        "deepseek-r1-32b": {"lead": [lead_item(False, 0.4, 90, []), lead_item(False, 0.5, 90, ["bash"])]},
        "devstral": {"code": [{"passed": False, "agent_seconds": 10}]},
    }
    s = bench.suggest(models, aliases={"qwen3.6-fast"})
    assert s["model_skill"]["coding"] == {"qwen3.6": 0.5, "devstral": 0.0}
    assert s["model_skill"]["analysis"]["qwen3.6"] == 0.75 and "qwen3.6-fast" not in s["model_skill"]["analysis"]
    assert sorted(s["chat_only"]) == ["deepseek-r1-32b", "devstral"]
    assert s["lead_model"] == "qwen3.6-fast"            # same passes and score, 8x faster

    cfg = {"routing": {"model_skill": {"coding": {"qwen3.6": 0.75, "old": 0.3}, "vision": {"qwen3.6": 0.7}}}}
    out = bench.apply_suggestion(cfg, s)["routing"]
    assert out["model_skill"]["coding"] == {"qwen3.6": 0.5, "old": 0.3, "devstral": 0.0}
    assert out["model_skill"]["vision"]["qwen3.6"] == 0.7 and out["chat_only"] == ["deepseek-r1-32b", "devstral"]


def test_merge_results_combines_lead_and_code_runs(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"models": {"m": {"speed": {"x": 1}, "lead": [1]}}}))
    (tmp_path / "b.json").write_text(json.dumps({"models": {"m": {"code": [2]}, "n": {"code": [3]}}}))
    merged = bench.merge_results([tmp_path / "a.json", tmp_path / "b.json"])
    assert merged == {"m": {"speed": {"x": 1}, "lead": [1], "code": [2]}, "n": {"code": [3]}}


def test_two_tasks_in_one_message_need_two_start_task_calls():
    two = scripted({"content": "", "tool_calls": [call("start_task", repo="/home/ubuntu/kite-sandbox", goal="median"),
                                                  call("start_task", repo="/home/ubuntu/kite-sandbox2", goal="avg")]},
                   {"content": "Đã giao 2 task: chat-bench-0001 và chat-bench-0001."})
    assert bench.run_scenario(two, "m", scenario("L22"), system="sys", tools=[])["passed"]
    one = scripted({"content": "", "tool_calls": [call("start_task", repo="/home/ubuntu/kite-sandbox", goal="x")]},
                   {"content": "Đã giao task chat-bench-0001."})
    result = bench.run_scenario(one, "m", scenario("L22"), system="sys", tools=[])
    assert not result["passed"] and not result["checks"]["call_count"]


def test_lead_must_not_promise_to_notify_by_itself():
    task = bench.TASK
    honest = scripted({"content": "", "tool_calls": [call("task_status", task_id=task)]},
                      {"content": "Task đang chạy. Mình không thể tự báo khi xong, bạn nhắn hỏi lại nhé."})
    assert bench.run_scenario(honest, "m", scenario("L23"), system="sys", tools=[])["passed"]
    promise = scripted({"content": "", "tool_calls": [call("task_status", task_id=task)]},
                       {"content": "Task đang chạy, mình sẽ báo cho bạn ngay khi task xong."})
    assert not bench.run_scenario(promise, "m", scenario("L23"), system="sys", tools=[])["passed"]
    # Checking first is good, but an honest answer with no tool call is also right.
    plain = scripted({"content": "Mình không tự báo được khi task xong, bạn nhắn hỏi lại giúp mình nhé."})
    assert bench.run_scenario(plain, "m", scenario("L23"), system="sys", tools=[])["passed"]
    plain_promise = scripted({"content": "Ok, mình sẽ báo bạn ngay khi task xong."})
    assert not bench.run_scenario(plain_promise, "m", scenario("L23"), system="sys", tools=[])["passed"]
    # A scenario without ``optional`` still needs its tool.
    assert not bench.run_scenario(scripted({"content": "Mình không thể tự kiểm tra, bạn nhắn hỏi lại nhé."}),
                                  "m", scenario("L01"), system="sys", tools=[])["passed"]


def test_a_real_repository_task_starts_at_the_base_and_is_judged_by_the_fix_tests(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    g = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "init", "-q"], check=True)
    (src / "app.py").write_text("def f():\n    return 1\n")
    (src / "tests").mkdir()
    (src / "tests" / "test_app.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 1\n")
    (src / "tests" / "test_other.py").write_text("def test_other():\n    assert True\n")
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*g, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (src / "app.py").write_text("def f():\n    return 2\n")
    (src / "tests" / "test_app.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 2\n")
    subprocess.run([*g, "commit", "-qam", "fix"], check=True)
    fix = subprocess.run([*g, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    task = bench.RepoTask("R0", base, fix, ("tests/test_app.py",), "f() phải trả 2")

    repo = bench.make_repo_fixture(task, src, tmp_path / "out")
    assert "return 1" in (repo / "app.py").read_text() and (repo / ".kite.yml").exists()
    remotes = subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True, text=True).stdout
    assert remotes.strip() == ""                                          # nothing to push to
    files = bench.hidden_files(task, src)
    assert files == {"tests/test_app.py": "from app import f\n\ndef test_f():\n    assert f() == 2\n"}
    assert bench.hidden_command(task).endswith("tests/test_app.py")
    run = {"verified": True, "changed_files": ["app.py", "tests/test_app.py"], "hidden_exit_code": 0}
    assert bench.score_code(task, run)["passed"]                         # editing the visible test is fine
    assert not bench.score_code(task, {**run, "changed_files": ["app.py", "tests/conftest.py"]})["passed"]
    assert not bench.score_code(task, {**run, "hidden_exit_code": 1})["passed"]
    # Weakening another existing test is caught; a brand-new test file is fine.
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    at = {**run, "workspace": str(repo), "base_head": head}
    edited = bench.score_code(task, {**at, "changed_files": ["app.py", "tests/test_other.py"]})
    assert not edited["passed"] and edited["touched_tests"] == ["tests/test_other.py"]
    assert bench.score_code(task, {**at, "changed_files": ["app.py", "tests/test_new.py"]})["passed"]
    assert all(t.base != t.fix and t.hidden for t in bench.REPO_TASKS)


# ------------------------------------------------------------------- Aider polyglot (Python)
def polyglot_clone(root: Path) -> Path:
    """A two-exercise stand-in for github.com/Aider-AI/polyglot-benchmark (same layout)."""
    for name, solution, tests in (("two-fer", "two_fer.py", ["two_fer_test.py"]),
                                  ("paasio", "paasio.py", ["paasio_test.py", "test_utils.py"])):
        folder = root / "python" / "exercises" / "practice" / name
        (folder / ".meta").mkdir(parents=True)
        (folder / ".docs").mkdir()
        (folder / ".meta" / "config.json").write_text(json.dumps({"files": {
            "solution": [solution], "test": tests, "example": [".meta/example.py"]}}))
        (folder / ".meta" / "example.py").write_text("SECRET = 'reference solution'\n")
        (folder / ".docs" / "instructions.md").write_text(f"# Instructions\n\nSolve {name}.\n")
        (folder / solution).write_text("def two_fer(name='you'):\n    pass\n")
        for test in tests:
            (folder / test).write_text("from two_fer import two_fer\n\n\ndef test_default():\n"
                                       "    assert two_fer() == 'One for you, one for me.'\n")
    (root / "python" / "exercises" / "practice" / "two-fer" / ".docs" / "instructions.append.md").write_text(
        "\n# Append\n\nUse a default argument.\n")
    return root


def test_polyglot_tasks_use_aiders_prompt_and_hide_the_tests(tmp_path):
    clone = polyglot_clone(tmp_path / "pg")
    tasks = bench.polyglot_tasks(clone)
    assert [t.id for t in tasks] == ["P-paasio", "P-two-fer"]
    assert [t.id for t in bench.polyglot_tasks(clone, 1)] == ["P-paasio"]
    task = tasks[1]
    assert task.goal.startswith("# Instructions\n\nSolve two-fer.\n\n# Append")
    assert task.goal.endswith("modify the supplied files: two_fer.py\nDon't change the names of existing functions "
                              "or classes, as they may be referenced from other code like unit tests, etc.\n"
                              "Only use standard libraries, don't suggest installing any packages.\n")
    assert task.solution == ("two_fer.py",) and task.tests == ("two_fer_test.py",)
    with pytest.raises(FileNotFoundError):
        bench.polyglot_tasks(tmp_path / "missing")

    repo = bench.make_polyglot_fixture(task, tmp_path / "out")
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout.split()
    assert sorted(tracked) == [".kite.yml", "two_fer.py"]                  # no tests, no docs, no example
    assert subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout == ""
    from poc.kaggle_worker import sandbox

    settings = sandbox.repo_settings(sandbox.SandboxSettings(uid=1000), repo)
    assert settings.setup_command == "" and settings.test_timeout_seconds == 300
    # No tests yet (Kite has not written its reproduction test): pytest's "nothing collected" passes.
    ok = subprocess.run(["sh", "-c", settings.test_command.replace("python", sys.executable, 1)], cwd=repo,
                        capture_output=True, text=True)
    assert ok.returncode == 0

    paasio = tasks[0]
    assert set(bench.hidden_files(paasio)) == {"paasio_test.py", "test_utils.py"}
    assert bench.hidden_command(paasio) == "python -m pytest -q -p no:cacheprovider paasio_test.py"


def test_polyglot_scores_like_aider_and_reports_pass_at_1_and_2(tmp_path):
    task = bench.polyglot_tasks(polyglot_clone(tmp_path / "pg"))[1]
    run = {"verified": False, "changed_files": ["two_fer.py"], "hidden_exit_code": 0, "pass_1": True, "tries": 1}
    first = bench.score_code(task, run)
    assert first["passed"] and first["pass_1"] and first["polyglot"]        # Kite's own verdict does not count
    assert not bench.score_code(task, {**run, "changed_files": ["two_fer.py", "conftest.py"]})["passed"]
    assert not bench.score_code(task, {**run, "hidden_exit_code": 1})["passed"]
    second = bench.score_code(task, {**run, "pass_1": False, "tries": 2})
    assert second["passed"] and not second["pass_1"]

    def run_code(t, model, out):
        return {"hidden_exit_code": 0 if t.id == "P-two-fer" else 1, "pass_1": False, "tries": 2,
                "changed_files": ["x.py"], "wall_seconds": 30.0}

    tasks = bench.polyglot_tasks(tmp_path / "pg")
    results = bench.run_bench(models=["qwen3.6"], suites=["code"], post=lambda body: {}, out=tmp_path / "o",
                              system="", tools=[], run_code=run_code, code_tasks=tasks, log=lambda line: None)
    summary = bench.summarize(results["models"]["qwen3.6"])
    assert (summary["polyglot_total"], summary["polyglot_pass_1"], summary["polyglot_pass_2"]) == (2, 0, 1)
    report = (tmp_path / "o" / "README.md").read_text(encoding="utf-8")
    assert "## Aider polyglot (Python)" in report and "| `qwen3.6` | 0/2 (0.0%) | 1/2 (50.0%) | 30.0 |" in report


def test_polyglot_second_try_starts_from_the_first_and_sees_the_test_errors(tmp_path):
    task = bench.polyglot_tasks(polyglot_clone(tmp_path / "pg"))[1]
    repo = bench.make_polyglot_fixture(task, tmp_path / "out")
    calls = []

    def attempt(goal, task_id):
        calls.append((goal, task_id, (repo / "two_fer.py").read_text()))
        workspace = tmp_path / f"ws{len(calls)}"
        workspace.mkdir()
        (workspace / "two_fer.py").write_text(f"def two_fer(name='you'):\n    return {len(calls)}\n")
        failed = len(calls) == 1
        return workspace, {"hidden_exit_code": 1 if failed else 0, "wall_seconds": 10.0 * len(calls),
                           "hidden_output_tail": "E   assert 1 == 'One for you, one for me.'" if failed else "",
                           "agent_status": "ok", "task_id": task_id}

    result = bench.run_polyglot(task, repo, "bench-q-p-two-fer-1", attempt, log=lambda line: None)
    assert [c[1] for c in calls] == ["bench-q-p-two-fer-1", "bench-q-p-two-fer-1-t2"]
    goal, _, code = calls[1]
    assert "return 1" in code                                                # the first try's file, committed
    assert subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout == ""
    assert goal.startswith(task.goal) and "assert 1 == 'One for you" in goal
    assert goal.endswith("See the testing errors above.\nThe tests are correct, don't try and change them.\n"
                         "Fix the code in two_fer.py to resolve the errors.\n")
    assert result["tries"] == 2 and result["pass_1"] is False and result["wall_seconds"] == 30.0
    assert result["first_try"]["hidden_exit_code"] == 1 and bench.score_code(task, result)["passed"]

    calls.clear()
    once = bench.run_polyglot(task, bench.make_polyglot_fixture(task, tmp_path / "out2"), "t",
                              lambda goal, task_id: (tmp_path, {"hidden_exit_code": 0}), log=lambda line: None)
    assert once["tries"] == 1 and once["pass_1"] is True


def test_history_puts_every_run_in_one_table_oldest_first(tmp_path, capsys):
    def write(folder, started, models, **meta):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "results.json").write_text(json.dumps({"started": started, **meta, "models": models}))

    code = [{"id": "R1", "passed": True, "wall_seconds": 400.0, "fast_path": "passed", "repro": "reproduced",
             "review_mode": "one_call"},
            {"id": "R2", "passed": False, "wall_seconds": 900.0, "fast_path": "tests_failed", "hidden_passed": False}]
    write("20261121-090000", "2026-11-21 09:00", {"qwen3.6": {"code": code}}, harness="bật", label="v5")
    write("20261118-090000", "2026-11-18 09:00", {"gemma4-26b": {"code": code[:1]}})
    (tmp_path / "empty").mkdir()
    text, rows = bench.history(tmp_path)
    assert [r["run"] for r in rows] == ["20261118-090000", "v5"]
    assert rows[1]["code"] == "1/2" and rows[1]["failed"] == "R2" and rows[1]["wall_total"] == 1300.0
    assert (rows[1]["fast_passed"], rows[1]["reproduced"], rows[1]["one_call_review"], rows[1]["hidden_failed"]) \
        == (1, 1, 1, 1)
    assert "| v5 | 2026-11-21 09:00 | `qwen3.6` | 1/2 | R2 | 650.0 | 1300.0 | - | 1 | 1 | 1 | 1 | - | - | - | - | - |" in text
    assert "- **v5**: task R1,R2; harness bật" in text

    from poc.kaggle_worker import cli
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "bench", "history", "--root", str(tmp_path)]) == 0
    assert "| v5 |" in capsys.readouterr().out


def test_cost_columns_show_gpu_time_tool_calls_and_patch_size_against_the_real_fix(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    g = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "init", "-q"], check=True)
    (src / "app.py").write_text("def f():\n    return 1\n")
    (src / "tests").mkdir()
    (src / "tests" / "test_app.py").write_text("def test_f():\n    pass\n")
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*g, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (src / "app.py").write_text("def f():\n    return 2\n")
    (src / "tests" / "test_app.py").write_text("def test_f():\n    assert True\n")
    subprocess.run([*g, "commit", "-qam", "fix"], check=True)
    fix = subprocess.run([*g, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    task = bench.RepoTask("R0", base, fix, ("tests/test_app.py",), "f() phải trả 2")
    assert bench.reference_lines(task, src) == 2                         # the hidden test does not count

    run = {"verified": True, "changed_files": ["app.py"], "hidden_exit_code": 0, "wall_seconds": 300.0,
           "agent_steps": 9, "tool_calls": 12, "repeat_reads": 3, "patch_added": 4, "patch_removed": 1,
           "reference_lines": 2, "gpu_seconds": 150.0, "model_requests": 11, "prompt_tokens": 90000,
           "completion_tokens": 3000}
    outcome = bench.score_code(task, run)
    assert (outcome["patch_lines"], outcome["gpu_seconds"], outcome["repeat_reads"]) == (5, 150.0, 3)
    results = {"started": "x", "models": {"qwen3.6": {"code": [outcome]}}}
    report = bench.render_report(results)
    assert "## Chi phí từng task code" in report and "| `qwen3.6` | R0 | 2.5 | 11 | 90000 + 3000 | 9 | 12 (3) | 5 (2) |" in report
    summary = bench.summarize(results["models"]["qwen3.6"])
    assert summary["code_gpu_minutes_median"] == 2.5 and summary["code_tokens"] == 93000


def test_a_second_measurement_run_is_refused_while_one_is_going(tmp_path):
    # Bench v6: two runs started 18 s apart, shared the GPU and wrote the same folder.
    import os

    lock = bench.acquire_lock(tmp_path, label="v6", out=tmp_path / "v6")
    with pytest.raises(bench.BenchBusy, match="v6"):
        bench.acquire_lock(tmp_path, label="v6-again", out=tmp_path / "v6")
    assert bench.running_bench(tmp_path)["pid"] == os.getpid()
    bench.release_lock(lock)
    assert bench.running_bench(tmp_path) is None
    # A lock left by a run that died does not block the next one.
    (tmp_path / bench.LOCK_NAME).write_text(json.dumps({"pid": 999999999, "label": "old"}))
    bench.release_lock(bench.acquire_lock(tmp_path, label="v7", out=tmp_path / "v7"))


def test_attempts_lost_to_the_machines_are_told_apart_from_the_models_failures():
    records = [
        {"outcome": "ok"},
        {"outcome": "error", "error": "no worker took the request in time", "run_s": None, "queue_s": 6382.9},
        {"outcome": "error", "error": "client disconnected", "run_s": None, "queue_s": 3602.5},   # gave up waiting
        {"outcome": "error", "error": "client disconnected", "run_s": 45.2, "queue_s": 0.0},     # aborted a running one
        {"outcome": "error", "error": "RuntimeError: llama-server: {'message': 'Context size has been exceeded'}",
         "run_s": 130.0, "queue_s": 0.0},
        {"outcome": "error", "error": "task cancelled", "run_s": 12.8, "queue_s": 0.0},
    ]
    found = bench.infra_errors(records)
    assert len(found) == 3 and found[0].startswith("no worker") and "Context size" in found[2]


def test_waiting_for_a_worker_polls_the_gateway_until_one_serves_the_model():
    answers = iter([{"workers": []},
                    {"workers": [{"alive": True, "draining": True, "models": ["qwen3.6"]}]},
                    {"workers": [{"alive": True, "draining": False, "models": ["qwen3.6"]}]}])
    said, slept = [], []
    assert bench.wait_for_worker("http://x/status", "qwen3.6", log=said.append, fetch=lambda url: next(answers),
                                 sleep=slept.append, poll=60)
    assert len(slept) == 2 and len(said) == 1
    assert not bench.wait_for_worker("http://x/status", "qwen3.6", log=said.append, timeout=0,
                                     fetch=lambda url: {"workers": []}, sleep=slept.append)


def test_waiting_for_a_worker_says_how_to_start_one_once():
    said, answers = [], iter([{"workers": []}, {"workers": []},
                              {"workers": [{"alive": True, "models": ["qwen3.6"]}]}])
    assert bench.wait_for_worker("http://gw/status", "qwen3.6", log=said.append, poll=0,
                                 fetch=lambda url: next(answers), sleep=lambda s: None,
                                 note="chưa có worker: bấm Save Version")
    assert said == ["chưa có worker: bấm Save Version"]
