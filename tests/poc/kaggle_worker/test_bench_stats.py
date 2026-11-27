"""Error bars for the bench (bench_stats) and `kite bench compare` on runs as they are on disk."""

from __future__ import annotations

import json
import math

import pytest

from poc.kaggle_worker import bench, bench_stats, cli


def test_mcnemar_exact_matches_the_binomial_by_hand():
    # 10 disagreeing tasks, 8 for A and 2 for B: p = 2 * (C(10,0) + C(10,1) + C(10,2)) / 2^10.
    assert bench_stats.mcnemar_exact(8, 2) == pytest.approx(2 * (1 + 10 + 45) / 1024)
    assert bench_stats.mcnemar_exact(2, 8) == bench_stats.mcnemar_exact(8, 2)
    assert bench_stats.mcnemar_exact(0, 0) == 1.0 and bench_stats.mcnemar_exact(3, 3) == 1.0
    assert bench_stats.mcnemar_exact(10, 0) == pytest.approx(2 / 1024)


def test_wilson_interval_and_standard_error():
    low, high = bench_stats.wilson(17, 34)                      # 50% of 34: about ±16 points
    assert low == pytest.approx(0.3407, abs=1e-3) and high == pytest.approx(0.6593, abs=1e-3)
    low, high = bench_stats.wilson(0, 34)
    assert low == 0.0 and 0.09 < high < 0.11                    # not a zero-width interval at 0/n
    assert bench_stats.wilson(0, 0) is None
    mean, se = bench_stats.mean_se([0, 100, 100, 100])
    assert mean == 75 and se == pytest.approx(math.sqrt(2500 / 4))
    assert bench_stats.mean_se([42]) == (42, None) and bench_stats.mean_se([]) == (None, None)


def test_paired_uses_mcnemar_for_one_run_and_the_task_cluster_for_repeats():
    same = {"P-a": 1.0, "P-b": 0.0, "P-c": 1.0}
    identical = bench_stats.paired(same, same)
    assert identical["mcnemar_p"] == 1.0 and not identical["significant"] and identical["diff"] == 0
    a = {f"P-{i}": 0.0 for i in range(10)} | {"P-x": 1.0}
    b = {f"P-{i}": 1.0 for i in range(10)} | {"P-y": 1.0}
    one_run = bench_stats.paired(a, b)
    assert one_run["tasks"] == 10 and one_run["only_in_a"] == ["P-x"] and one_run["only_in_b"] == ["P-y"]
    assert one_run["mcnemar_p"] == pytest.approx(2 / 1024) and one_run["significant"]
    repeated = bench_stats.paired({"t1": 0.5, "t2": 0.5, "t3": 0.0, "t4": 1.0},
                                  {"t1": 1.0, "t2": 0.5, "t3": 0.5, "t4": 0.5})
    assert repeated["mcnemar_p"] is None                         # rates over repeats: the interval decides
    assert repeated["diff"] == pytest.approx(0.125) and repeated["diff_ci95"][0] < 0 < repeated["diff_ci95"][1]
    assert not repeated["significant"]
    clear = bench_stats.paired({"t1": 0.0, "t2": 0.5, "t3": 0.0}, {"t1": 1.0, "t2": 1.0, "t3": 0.5})
    assert clear["diff_ci95"][0] > 0 and clear["significant"]


def write_run(root, name, models, **meta):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "results.json").write_text(json.dumps({"started": "2026-11-22 09:00", **meta, "models": models}),
                                         encoding="utf-8")
    return folder


def polyglot(passed: dict[str, bool], first: dict[str, bool] | None = None) -> dict:
    return {"code": [{"id": task, "polyglot": True, "passed": ok, "pass_1": (first or passed)[task]}
                     for task, ok in passed.items()]}


def test_compare_old_runs_task_by_task_without_running_anything(tmp_path, capsys):
    tasks = [f"P-{i:02d}" for i in range(34)]
    v6 = write_run(tmp_path, "v6", {"qwen3.6": polyglot({t: i < 14 for i, t in enumerate(tasks)})}, label="v6")
    v6b = write_run(tmp_path, "v6b", {"qwen3.6": polyglot({t: i < 17 for i, t in enumerate(tasks)})}, label="v6b")
    text, result = bench.compare([v6], [v6b])
    assert result["tasks"] == 34 and result["b_better"] == ["P-14", "P-15", "P-16"] and result["a_better"] == []
    assert result["mcnemar_p"] == pytest.approx(0.25) and not result["significant"]
    assert "chưa đủ để nói hai cấu hình khác nhau" in text and "p = 0.250" in text and "+8.8 điểm" in text

    # Repeats: two runs on each side, the rate of a task is its mean over them.
    v6c = write_run(tmp_path, "v6c", {"qwen3.6": polyglot({t: i < 15 for i, t in enumerate(tasks)})})
    _, repeated = bench.compare([v6, v6c], [v6b, v6b])
    assert repeated["rate_a"] == pytest.approx(14.5 / 34) and repeated["mcnemar_p"] is None

    # Two models in one run: name them; pass@1 instead of pass@2.
    both = write_run(tmp_path, "both", {"qwen3.6": polyglot({"P-a": True}, {"P-a": False}),
                                        "gemma4-26b": polyglot({"P-a": True})})
    with pytest.raises(ValueError, match="chọn model"):
        bench.compare([both], [both])
    _, first = bench.compare([both], [both], model_a="qwen3.6", model_b="gemma4-26b", metric="pass_1")
    assert first["b_better"] == ["P-a"]

    assert cli.main(["--config", str(tmp_path / "config.yaml"), "bench", "compare", str(v6), "--vs", str(v6b),
                     "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["b"]["runs"] == ["v6b"]


def test_history_and_the_run_report_show_the_interval(tmp_path):
    write_run(tmp_path, "v6", {"qwen3.6": polyglot({"P-a": True, "P-b": False})}, label="v6")
    text, rows = bench.history(tmp_path)
    assert rows[0]["code"] == "1/2" and rows[0]["code_ci"] == "1/2 (9–91%)"
    assert "| v6 | 2026-11-22 09:00 | `qwen3.6` | 1/2 (9–91%) |" in text
    assert "1/2 (9–91%) → 1/2 (9–91%)" in text
    report = bench.render_report({"models": {"qwen3.6": polyglot({"P-a": True, "P-b": False})}})
    assert "1/2 (50.0%, CI 95% 9–91%)" in report
