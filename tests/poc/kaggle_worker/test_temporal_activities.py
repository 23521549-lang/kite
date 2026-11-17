import asyncio
from dataclasses import replace
import sqlite3

from temporalio.testing import ActivityEnvironment

from poc.kaggle_worker.temporal_activities import (
    TemporalKiteActivities,
    activity_idempotency_key,
)
from poc.kaggle_worker.temporal_state import TemporalActivityJournal
from poc.kaggle_worker.temporal_state import RecoveryDecision
from poc.kaggle_worker.temporal_workflow import (
    CodingRunResult,
    CodingTaskInput,
    RouteLease,
)


def test_activity_idempotency_key_is_scoped_to_task_run_and_operation():
    assert activity_idempotency_key("task-a", "run-1", "reserve-route") == (
        "task-a:run-1:reserve-route"
    )
    assert activity_idempotency_key("task-a", "run-2", "reserve-route") != (
        activity_idempotency_key("task-a", "run-1", "reserve-route")
    )


def test_activity_idempotency_key_rejects_empty_components():
    import pytest

    with pytest.raises(ValueError):
        activity_idempotency_key("task-a", "", "reserve-route")


def test_temporal_activity_uses_durable_journal_for_retry_result(tmp_path):
    class Backend:
        def __init__(self):
            self.calls = 0

        async def validate_task(self, task, key):
            self.calls += 1

    async def scenario():
        backend = Backend()
        journal = TemporalActivityJournal(tmp_path / "activity-journal.sqlite3")
        activities = TemporalKiteActivities(backend, journal)
        env = ActivityEnvironment()
        env.info = replace(
            env.info, workflow_id="task-a", workflow_run_id="run-a", activity_id="validate-1"
        )
        task = CodingTaskInput(
            task_id="task-a", goal="goal", repo_path="/repo", repo_head="head",
            branch="agent/task-a", required_capabilities=("coder",),
            preferred_model_id=None, estimated_gpu_seconds=60,
        )

        await env.run(activities.validate_task, task)
        await env.run(activities.validate_task, task)
        journal.close()

        assert backend.calls == 1

    asyncio.run(scenario())


def test_temporal_activity_reconciles_after_crash_before_journal_commit(tmp_path):
    class Backend:
        def __init__(self):
            self.calls = 0
            self.reconciliations = []

        async def validate_task(self, task, key):
            self.calls += 1

        async def reconcile_operation(self, operation_name, key, *context):
            self.reconciliations.append((operation_name, key, context[0].task_id))
            return RecoveryDecision(recovered=True, result=None)

    async def scenario():
        path = tmp_path / "activity-journal.sqlite3"
        backend = Backend()
        journal = TemporalActivityJournal(path)
        activities = TemporalKiteActivities(backend, journal)
        env = ActivityEnvironment()
        env.info = replace(
            env.info, workflow_id="task-a", workflow_run_id="run-a", activity_id="validate-1"
        )
        task = CodingTaskInput(
            task_id="task-a", goal="goal", repo_path="/repo", repo_head="head",
            branch="agent/task-a", required_capabilities=("coder",),
            preferred_model_id=None, estimated_gpu_seconds=60,
        )

        await env.run(activities.validate_task, task)
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE temporal_activity_results SET state='RUNNING',owner_token='dead-worker', "
                "lease_expires_at=0,result_json=NULL"
            )
        await env.run(activities.validate_task, task)
        journal.close()

        assert backend.calls == 1
        assert backend.reconciliations == [
            ("validate-task", "task-a:run-a:validate-1:validate-task", "task-a")
        ]

    asyncio.run(scenario())


def test_long_opencode_activity_renews_route_and_emits_temporal_heartbeats(tmp_path):
    class Backend:
        def __init__(self):
            self.heartbeats = []
            self.finish = asyncio.Event()

        async def run_opencode(
            self, task, route, workspace_path, instruction, session_id, key, heartbeat,
        ):
            await self.finish.wait()
            return CodingRunResult(
                task_id=task.task_id, base_repo_head=task.repo_head, session_id=session_id,
                patch="patch", summary="done", changed_files=("a.py",), usage_gpu_seconds=5,
            )

        async def heartbeat_route(self, task, route, key):
            self.heartbeats.append(key)
            if len(self.heartbeats) == 2:
                self.finish.set()

    async def scenario():
        backend = Backend()
        journal = TemporalActivityJournal(tmp_path / "activity-journal.sqlite3")
        activities = TemporalKiteActivities(
            backend, journal, route_heartbeat_interval_seconds=0.01,
        )
        env = ActivityEnvironment()
        env.info = replace(
            env.info, workflow_id="task-a", workflow_run_id="run-heartbeat",
            activity_id="run-1", attempt=4,
        )
        temporal_heartbeats = []
        env.on_heartbeat = lambda *details: temporal_heartbeats.append(details)
        task = CodingTaskInput(
            task_id="task-a", goal="goal", repo_path="/repo", repo_head="head",
            branch="agent/task-a", required_capabilities=("coder",),
            preferred_model_id="model-a", estimated_gpu_seconds=60,
        )
        route = RouteLease(
            task_id="task-a", lease_id="lease-a", owner_id="owner-a",
            provider_id="provider-a", worker_id="worker-a", model_id="model-a",
            reserved_gpu_seconds=60,
        )

        result = await env.run(
            activities.run_opencode, task, route, "/repo/.worktree", "fix it", "session-a",
        )
        journal.close()

        assert result.session_id == "session-a"
        assert len(backend.heartbeats) == 2
        assert backend.heartbeats[0].endswith("attempt:4:route-heartbeat:1")
        assert backend.heartbeats[1].endswith("attempt:4:route-heartbeat:2")
        assert len(temporal_heartbeats) >= 3

    asyncio.run(scenario())


def test_two_workflow_runs_cannot_write_the_same_task_at_once(tmp_path):
    class Backend:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def run_opencode(
            self, task, route, workspace_path, instruction, session_id, key, heartbeat,
        ):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.08)
            self.active -= 1
            return CodingRunResult(
                task_id=task.task_id, base_repo_head=task.repo_head, session_id=session_id,
                patch="patch", summary="done", changed_files=("a.py",), usage_gpu_seconds=5,
            )

        async def heartbeat_route(self, task, route, key):
            pass

    async def scenario():
        backend = Backend()
        journal = TemporalActivityJournal(
            tmp_path / "activity-journal.sqlite3", execution_lease_seconds=2,
            poll_interval_seconds=0.005,
        )
        activities = TemporalKiteActivities(
            backend, journal, route_heartbeat_interval_seconds=0.02,
        )
        task = CodingTaskInput(
            task_id="task-a", goal="goal", repo_path="/repo", repo_head="head",
            branch="agent/task-a", required_capabilities=("coder",),
            preferred_model_id="model-a", estimated_gpu_seconds=60,
        )
        route = RouteLease(
            task_id="task-a", lease_id="lease-a", owner_id="owner-a",
            provider_id="provider-a", worker_id="worker-a", model_id="model-a",
            reserved_gpu_seconds=60,
        )
        environments = [ActivityEnvironment(), ActivityEnvironment()]
        waiting_heartbeats = []
        for number, env in enumerate(environments):
            env.info = replace(
                env.info, workflow_id="task-a", workflow_run_id=f"run-{number}",
                activity_id="run-opencode",
            )
            env.on_heartbeat = lambda *details: waiting_heartbeats.extend(details)
        await asyncio.gather(*(
            env.run(
                activities.run_opencode, task, route, "/repo/.worktree", "fix it", "session-a",
            )
            for env in environments
        ))
        journal.close()
        assert backend.max_active == 1
        assert any(
            isinstance(detail, dict) and detail.get("state") == "WAITING_FOR_SINGLE_WRITER"
            for detail in waiting_heartbeats
        )

    asyncio.run(scenario())


