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


def pytest_exit(repo: Path) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=repo,
                          capture_output=True, text=True).returncode


@pytest.mark.parametrize("task", bench.CODE_TASKS, ids=lambda t: t.id)
def test_code_fixtures_fail_as_shipped_and_pass_with_the_reference_fix(tmp_path, task):
    assert pytest_exit(bench.make_fixture(task, tmp_path / "start")) != 0
    assert pytest_exit(bench.make_fixture(task, tmp_path / "solved", solved=True)) == 0


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
                              code_tasks=bench.CODE_TASKS[:1], log=lambda line: None)
    summary = bench.summarize(results["models"]["qwen3.6"])
    assert (summary["lead_passed"], summary["lead_total"], summary["code_passed"]) == (1, 2, 1)
    report = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "`qwen3.6` | 1/2" in report and "L04" in report
    assert json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))["finished"]


def test_real_tool_schemas_for_the_lead_agent(tmp_path):
    pytest.importorskip("mcp")
    from poc.kaggle_worker.mcp_server import build_server
    from tests.poc.kaggle_worker.test_mcp_server import FakeClient, make_ops

    tools = bench.lead_tools(asyncio.run(build_server(make_ops(tmp_path, FakeClient())).list_tools()))
    names = {t["function"]["name"] for t in tools}
    assert "kite_apply_result" in names and "bash" in names and len(names) == 14
