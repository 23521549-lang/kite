import asyncio
import sqlite3

import pytest

from poc.kaggle_worker.temporal_state import (
    JournalError,
    RecoveryDecision,
    TemporalActivityJournal,
)
from poc.kaggle_worker.temporal_workflow import CodingRunResult


def test_completed_operation_result_survives_journal_reopen(tmp_path):
    async def scenario():
        path = tmp_path / "temporal-state.sqlite3"
        first = TemporalActivityJournal(path)
        calls = 0

        async def operation(key):
            nonlocal calls
            calls += 1
            return CodingRunResult(
                task_id="task-1", base_repo_head="head-1", session_id="session-1",
                patch="patch", summary="done", changed_files=("src/a.py",),
                usage_gpu_seconds=12,
            )

        result = await first.run("task-1:run-1:activity-3:run-opencode", operation,
                                 result_type=CodingRunResult)
        first.close()

        reopened = TemporalActivityJournal(path)

        async def must_not_run(key):
            raise AssertionError("completed activity should be served from the durable journal")

        recovered = await reopened.run(
            "task-1:run-1:activity-3:run-opencode", must_not_run,
            result_type=CodingRunResult,
        )
        reopened.close()

        assert calls == 1
        assert recovered == result
        assert recovered.changed_files == ("src/a.py",)

    asyncio.run(scenario())


def test_concurrent_calls_with_same_key_execute_backend_once(tmp_path):
    async def scenario():
        path = tmp_path / "temporal-state.sqlite3"
        journal_a = TemporalActivityJournal(path)
        journal_b = TemporalActivityJournal(path)
        calls = 0

        async def operation(key):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.15)
            return "session-1"

        results = await asyncio.gather(
            journal_a.run("same-key", operation, result_type=str),
            journal_b.run("same-key", operation, result_type=str),
        )
        journal_a.close()
        journal_b.close()

        assert results == ["session-1", "session-1"]
        assert calls == 1

    asyncio.run(scenario())


def test_failed_operation_can_retry_and_persists_success(tmp_path):
    async def scenario():
        journal = TemporalActivityJournal(tmp_path / "temporal-state.sqlite3")
        calls = 0

        async def operation(key):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary backend failure")
            return "checkpoint.json"

        with pytest.raises(RuntimeError, match="temporary backend failure"):
            await journal.run("task:run:activity:checkpoint", operation, result_type=str)
        async def reconcile(operation_key):
            return RecoveryDecision(recovered=False)

        assert await journal.run(
            "task:run:activity:checkpoint", operation, result_type=str, recover=reconcile,
        ) == "checkpoint.json"
        assert calls == 2
        journal.close()

    asyncio.run(scenario())


def test_interrupted_operation_reconciles_before_replaying_side_effect(tmp_path):
    async def scenario():
        path = tmp_path / "temporal-state.sqlite3"
        journal = TemporalActivityJournal(path)
        key = "task:run:activity:reserve"

        async def original_operation(operation_key):
            return "lease-42"

        assert await journal.run(key, original_operation, result_type=str) == "lease-42"
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE temporal_activity_results SET state='RUNNING',owner_token='dead-worker', "
                "lease_expires_at=0,result_json=NULL WHERE operation_key=?",
                (key,),
            )

        replayed = 0
        reconciled = []

        async def must_not_repeat(operation_key):
            nonlocal replayed
            replayed += 1
            raise AssertionError("journal must reconcile before retrying the side effect")

        async def reconcile(operation_key):
            reconciled.append(operation_key)
            return RecoveryDecision(recovered=True, result="lease-42")

        result = await journal.run(
            key, must_not_repeat, result_type=str, recover=reconcile,
        )
        journal.close()

        assert result == "lease-42"
        assert reconciled == [key]
        assert replayed == 0

    asyncio.run(scenario())


def test_recovery_can_confirm_side_effect_absent_then_execute_once(tmp_path):
    async def scenario():
        path = tmp_path / "temporal-state.sqlite3"
        journal = TemporalActivityJournal(path)
        key = "task:run:activity:prepare-session"
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO temporal_activity_results "
                "(operation_key,request_fingerprint,state,owner_token,lease_expires_at,result_json,updated_at) "
                "VALUES (?, ?, 'FAILED', NULL, NULL, NULL, 0)",
                (key, key),
            )
        calls = 0

        async def operation(operation_key):
            nonlocal calls
            calls += 1
            return "session-new"

        async def reconcile(operation_key):
            return RecoveryDecision(recovered=False)

        assert await journal.run(
            key, operation, result_type=str, recover=reconcile,
        ) == "session-new"
        assert calls == 1
        journal.close()

    asyncio.run(scenario())


def test_same_operation_key_rejects_different_request_fingerprint(tmp_path):
    async def scenario():
        journal = TemporalActivityJournal(tmp_path / "temporal-state.sqlite3")
        async def operation(key):
            return "first"

        await journal.run(
            "same-key", operation, result_type=str, request_fingerprint="request-a"
        )
        with pytest.raises(JournalError, match="different request"):
            await journal.run(
                "same-key", operation, result_type=str, request_fingerprint="request-b"
            )
        journal.close()

    asyncio.run(scenario())
