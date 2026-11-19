"""Journal: append-only records, summaries and per-task views."""

from __future__ import annotations

import json
from pathlib import Path

from poc.kaggle_worker import cli
from poc.kaggle_worker.journal import Journal, append_jsonl, clean_tag, read_jsonl, show_task, summarize


def test_append_and_read_survive_broken_lines_and_unwritable_paths(tmp_path, capsys):
    path = tmp_path / "j" / "x.jsonl"
    assert append_jsonl(path, {"a": 1}, clock=lambda: 100.0)
    with path.open("a") as stream:
        stream.write("not json\n[1, 2]\n")
    append_jsonl(path, {"b": "tiếng Việt"})
    records = read_jsonl(path)
    assert [r.get("a") or r.get("b") for r in records] == [1, "tiếng Việt"] and records[0]["ts"] == 100.0
    assert read_jsonl(tmp_path / "missing.jsonl") == []
    blocked = tmp_path / "file"
    blocked.write_text("")
    assert not append_jsonl(blocked / "x.jsonl", {"a": 1})
    assert not append_jsonl(blocked / "x.jsonl", {"a": 2})
    assert capsys.readouterr().err.count("cannot write") == 1          # one warning, not one per line
    assert clean_tag(" chat ") == "chat" and clean_tag("x y") is None and clean_tag("") is None


def make_journal(tmp_path: Path) -> Journal:
    clock = iter(range(1000, 2000)).__next__
    journal = Journal(tmp_path / "journal", clock=lambda: float(clock()))
    journal.write("tasks", {"task_id": "t1", "task_type": "coding", "model": "gemma4-26b", "agent_status": "ok",
                            "seconds": 200, "fix_rounds": 0, "review": "kept", "test_exit": 0, "verified": True,
                            "changed_files": ["a.py"]})
    journal.write("tasks", {"task_id": "t2", "task_type": "coding", "model": "qwen3.6", "agent_status": "ok",
                            "seconds": 100, "fix_rounds": 2, "review": "skipped", "test_exit": 1, "verified": False,
                            "changed_files": ["b.py"]})
    journal.write("tasks", {"task_id": "t3", "model": "qwen3.6", "status": "error", "error": "docker died"})
    for tag, run_s, model in (("t1", 30.0, "gemma4-26b"), ("t1", 50.0, "gemma4-26b"), ("chat", 5.0, "gemma4-26b"),
                              ("bench-x", 2.0, "qwen3.6")):
        journal.write("gateway", {"tag": tag, "model": model, "outcome": "ok", "run_s": run_s, "queue_s": 1.0,
                                  "prompt_tokens": 1000, "completion_tokens": 100, "gen_tps": 49.0})
    journal.write("gateway", {"tag": "t2", "model": "qwen3.6", "outcome": "error", "error": "job timed out"})
    journal.write("actions", {"tool": "apply_result", "ok": True, "task_id": "t1", "args": {"task_id": "t1"}})
    journal.write("actions", {"tool": "commit_result", "ok": False, "task_id": "t1", "args": {"task_id": "t1"}})
    journal.write("notes", {"task_id": "t1", "verdict": "good", "text": "đúng ý"})
    return journal


def test_summary_gives_rates_reasons_and_gateway_health(tmp_path):
    report = summarize(make_journal(tmp_path))
    tasks = report["tasks"]
    assert tasks["runs"] == 3
    assert tasks["reasons"] == {"verified": 1, "tests_failed": 1, "error": 1}
    assert tasks["by_model"]["gemma4-26b"]["verified"] == 1 and tasks["by_model"]["qwen3.6"]["runs"] == 2
    assert tasks["applied"] == 1 and tasks["committed"] == 0 and tasks["user_notes"] == {"good": 1}
    assert {f["task_id"] for f in tasks["recent_failures"]} == {"t2", "t3"}
    assert report["lead_agent"]["tool_errors"] == {"commit_result": 1}
    gateway = report["gateway"]
    assert gateway["by_source"] == {"task": 3, "chat": 1, "bench": 1}
    assert gateway["by_model"]["gemma4-26b"]["prompt_tokens"] == 3000 and gateway["errors"] == {"job timed out": 1}
    assert summarize(make_journal(tmp_path / "again"), since=5000.0)["tasks"]["runs"] == 0


def test_show_task_collects_everything_about_one_task(tmp_path):
    journal = make_journal(tmp_path)
    trace = tmp_path / "trace.jsonl"
    append_jsonl(trace, {"event": "step", "step": "agent", "seconds": 120})
    view = show_task(journal, "t1", trace)
    assert len(view["runs"]) == 1 and view["trace"][0]["step"] == "agent"
    assert view["requests"]["count"] == 2 and view["requests"]["model_seconds"] == 80.0
    assert [a["tool"] for a in view["actions"]] == ["apply_result", "commit_result"]
    assert view["notes"][0]["text"] == "đúng ý"


def test_journal_cli_note_summary_and_show(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("")
    assert cli.main(["--config", str(config), "journal", "note", "t9", "--bad", "sai đặc tả"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["saved"] == {"task_id": "t9", "verdict": "bad", "text": "sai đặc tả"}
    assert cli.main(["--config", str(config), "journal", "summary", "--days", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"]["user_notes"] == {"bad": 1}
    assert cli.main(["--config", str(config), "journal", "show", "t9"]) == 0
    assert json.loads(capsys.readouterr().out)["notes"][0]["verdict"] == "bad"
