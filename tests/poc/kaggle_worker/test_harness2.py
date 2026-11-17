from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import asyncio

import pytest

from poc.kaggle_worker.harness2 import (
    Harness2Error,
    Harness2Runner,
    Harness2Spec,
)
from poc.kaggle_worker.temporal_activities import TemporalKiteActivities
from poc.kaggle_worker.temporal_state import TemporalActivityJournal
from temporalio.testing import ActivityEnvironment


def _harness_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Kite Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "kite@example.invalid"], check=True)
    (path / "pyproject.toml").write_text("[project]\nname='harness2'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "pyproject.toml"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "pin"], check=True, capture_output=True)
    return path


def _spec(evidence_path: Path, **overrides) -> Harness2Spec:
    values = dict(
        benchmark="lab", evidence_path=evidence_path,
        only_domains=("antitrust-competition",), max_tasks=1, seed=17,
    )
    values.update(overrides)
    return Harness2Spec(**values)


def _fake_runner(calls, *, exit_for=None, malformed=False, omit_report=False):
    def run(command, *, cwd, env, capture_output, text, check, timeout):
        args = tuple(command)
        calls.append((args, cwd, env.copy()))
        name = Path(args[0]).name
        if exit_for == name:
            return subprocess.CompletedProcess(command, 9, "", "sensitive token=do-not-log")
        output = Path(env["HARNESS2_RUNS_DIR"]) / "lab"
        output.mkdir(parents=True, exist_ok=True)
        if name == "harness2-judge":
            (output / "judged.json").write_text(
                "not-json" if malformed else json.dumps({"score": 0.75}), encoding="utf-8",
            )
        if name == "harness2-report" and not omit_report:
            (output / "report.md").write_text("evaluation report", encoding="utf-8")
        if name == "harness2":
            (output / "candidate-harness.json").write_text('{"candidate":true}', encoding="utf-8")
        stdout = "" if name == "harness2-report" and omit_report else "private command output"
        return subprocess.CompletedProcess(command, 0, stdout, "")
    return run


def test_evaluation_uses_bounded_sequential_commands_and_records_evidence(tmp_path):
    harness = _harness_repo(tmp_path / "harness")
    evidence = tmp_path / "task-evidence.json"
    evidence.write_text('{"task":"improve harness"}', encoding="utf-8")
    calls = []
    runner = Harness2Runner(
        harness, tmp_path / "evaluations", runner=_fake_runner(calls),
        commands={
            "check": "harness2-check", "run": "harness2",
            "judge": "harness2-judge", "report": "harness2-report",
        },
        base_environment={"MODEL_SECRET": "never-record-this"},
    )

    record = runner.evaluate(_spec(evidence))

    assert [Path(call[0][0]).name for call in calls] == [
        "harness2-check", "harness2", "harness2-judge", "harness2-report",
    ]
    run_args = calls[1][0]
    assert run_args[run_args.index("--k") + 1] == "1"
    assert run_args[run_args.index("--mode") + 1] == "sequential"
    assert run_args[run_args.index("--max-tasks") + 1] == "1"
    assert run_args[run_args.index("--only-domains") + 1] == "antitrust-competition"
    assert calls[1][2]["HARNESS2_RUNS_DIR"].endswith("/runs") or calls[1][2]["HARNESS2_RUNS_DIR"].endswith("\\runs")
    assert record.status == "completed"
    assert record.promotion_decision == "pending"
    assert record.harness_commit
    assert record.candidate_hash
    assert any(path.endswith("judged.json") for path in record.score_paths)
    stored = json.loads(Path(record.record_path).read_text(encoding="utf-8"))
    assert "MODEL_SECRET" not in json.dumps(stored)
    assert str(evidence) in stored["evidence_paths"]


def test_dry_run_only_checks_and_previews_without_judge_or_model_run(tmp_path):
    harness = _harness_repo(tmp_path / "harness")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("safe preview", encoding="utf-8")
    calls = []
    runner = Harness2Runner(harness, tmp_path / "evaluations", runner=_fake_runner(calls))

    record = runner.evaluate(_spec(evidence, dry_run=True))

    assert [Path(call[0][0]).name for call in calls] == ["harness2-check", "harness2"]
    assert "--dry-run" in calls[-1][0]
    assert record.status == "preview"


@pytest.mark.parametrize(
    ("exit_for", "malformed", "omit_report", "message"),
    [
        ("harness2", False, False, "harness2 exited with code 9"),
        (None, True, False, "judged.json is malformed"),
        (None, False, True, "report artifact is missing"),
    ],
)
def test_evaluation_failure_is_recorded_and_redacted(
    tmp_path, exit_for, malformed, omit_report, message,
):
    harness = _harness_repo(tmp_path / "harness")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("evidence", encoding="utf-8")
    calls = []
    runner = Harness2Runner(
        harness, tmp_path / "evaluations",
        runner=_fake_runner(calls, exit_for=exit_for, malformed=malformed, omit_report=omit_report),
    )

    with pytest.raises(Harness2Error, match=message) as error:
        runner.evaluate(_spec(evidence))

    assert "do-not-log" not in str(error.value)
    records = list((tmp_path / "evaluations").glob("*/evaluation.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["status"] == "failed"


def test_candidate_promotion_requires_explicit_reviewer_decision(tmp_path):
    harness = _harness_repo(tmp_path / "harness")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("evidence", encoding="utf-8")
    calls = []
    runner = Harness2Runner(harness, tmp_path / "evaluations", runner=_fake_runner(calls))
    record = runner.evaluate(_spec(evidence))

    with pytest.raises(Harness2Error, match="reviewer"):
        runner.record_promotion(record.evaluation_id, approved=True, reviewer=" ")
    updated = runner.record_promotion(record.evaluation_id, approved=False, reviewer="owner")
    assert updated.promotion_decision == "rejected"
    assert updated.promotion_reviewer == "owner"


def test_same_evaluation_id_resumes_existing_namespace_without_duplicate_completed_run(tmp_path):
    harness = _harness_repo(tmp_path / "harness")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("evidence", encoding="utf-8")
    calls = []
    runner = Harness2Runner(harness, tmp_path / "evaluations", runner=_fake_runner(calls))
    spec = _spec(evidence, evaluation_id="temporal-evaluation-1")

    first = runner.evaluate(spec)
    call_count = len(calls)
    second = runner.evaluate(spec)

    assert second == first
    assert len(calls) == call_count


def test_temporal_activity_recovers_completed_harness_run_without_rerunning_models(tmp_path):
    harness = _harness_repo(tmp_path / "harness")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("evidence", encoding="utf-8")
    calls = []
    output = tmp_path / "evaluations"
    harness_runner = Harness2Runner(harness, output, runner=_fake_runner(calls))

    class Backend:
        async def reconcile_operation(self, *_args):
            raise AssertionError("Harness² recovery must inspect its durable evaluation record")

    async def scenario():
        journal_path = tmp_path / "activity-journal.sqlite3"
        journal = TemporalActivityJournal(journal_path)
        activities = TemporalKiteActivities(Backend(), journal, harness2=harness_runner)
        env = ActivityEnvironment()
        env.info = replace(
            env.info, workflow_id="harness-eval", workflow_run_id="eval-run",
            activity_id="evaluate-1",
        )
        spec = _spec(evidence)
        record = await env.run(activities.evaluate_harness, spec)
        call_count = len(calls)
        op_key = "harness2:eval-run:evaluate-1:evaluate-harness"
        with sqlite3.connect(journal_path) as db:
            db.execute(
                "UPDATE temporal_activity_results SET state='RUNNING',owner_token='dead-worker', "
                "lease_expires_at=0,result_json=NULL WHERE operation_key=?",
                (op_key,),
            )
        resumed = await env.run(activities.evaluate_harness, spec)
        journal.close()
        assert resumed == record
        assert len(calls) == call_count

    asyncio.run(scenario())
