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
    assert "kite_apply_result" in names and "kite_note_task" in names and "bash" in names and len(names) == 15


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
