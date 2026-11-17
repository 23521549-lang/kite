import asyncio
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from poc.kaggle_worker.integration import ReviewBundle
from poc.kaggle_worker.temporal_workflow import (
    CodingTaskInput,
    CodingTaskWorkflow,
    CodingTaskOutcome,
    CodingRunResult,
    RouteLease,
)


def task_input():
    return CodingTaskInput(
        task_id="task-1",
        goal="fix the parser edge case",
        repo_path="/workspace/repo",
        repo_head="head-abc",
        branch="agent/task-1",
        required_capabilities=("coder",),
        preferred_model_id="model-code",
        estimated_gpu_seconds=600,
    )


def route_lease():
    return RouteLease(
        task_id="task-1",
        lease_id="lease-1",
        owner_id="owner-1",
        provider_id="fake-provider",
        worker_id="worker-1",
        model_id="model-code",
        reserved_gpu_seconds=600,
    )


def test_workflow_runs_checkpoint_review_and_route_settlement_in_order():
    async def scenario():
        events = []

        @activity.defn(name="kite_validate_task")
        async def validate(task: CodingTaskInput) -> None:
            events.append(("validate", task.task_id))

        @activity.defn(name="kite_prepare_workspace")
        async def prepare(task: CodingTaskInput) -> str:
            events.append(("prepare", task.branch))
            return "/workspace/repo/.kite-worktrees/task-1"

        @activity.defn(name="kite_reserve_route")
        async def reserve(task: CodingTaskInput, idempotency_key: str) -> RouteLease | None:
            events.append(("reserve", task.task_id, idempotency_key))
            return route_lease()

        @activity.defn(name="kite_prepare_opencode_session")
        async def prepare_session(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            prior_session_id: str | None,
        ) -> str:
            events.append(("session", prior_session_id or "new"))
            return prior_session_id or "session-1"

        @activity.defn(name="kite_run_opencode")
        async def run_opencode(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            instruction: str, session_id: str,
        ) -> CodingRunResult:
            events.append(("run", route.worker_id, route.model_id, workspace_path, instruction, session_id))
            return CodingRunResult(
                task_id=task.task_id,
                base_repo_head=task.repo_head,
                session_id=session_id,
                patch="diff --git a/parser.py b/parser.py",
                summary="fixed parser edge case",
                changed_files=("parser.py",),
                usage_gpu_seconds=120,
            )

        @activity.defn(name="kite_checkpoint_and_stop")
        async def checkpoint_and_stop(
            task: CodingTaskInput, route: RouteLease, run_result: CodingRunResult,
        ) -> str:
            events.append(("checkpoint", run_result.session_id, route.lease_id))
            return "/state/checkpoints/task-1.json"

        @activity.defn(name="kite_build_review_bundle")
        async def build_review(
            task: CodingTaskInput, route: RouteLease, run_result: CodingRunResult,
            checkpoint_path: str,
        ) -> ReviewBundle:
            events.append(("review", checkpoint_path))
            return ReviewBundle(
                task_id=task.task_id,
                lease_id=route.lease_id,
                worker_id=route.worker_id,
                base_repo_head=run_result.base_repo_head,
                patch=run_result.patch,
                summary=run_result.summary,
                usage_gpu_seconds=run_result.usage_gpu_seconds,
            )

        @activity.defn(name="kite_release_route")
        async def release(
            task: CodingTaskInput, route: RouteLease,
            run_result: CodingRunResult | None, status: str,
        ) -> None:
            events.append(("release", route.lease_id, run_result.usage_gpu_seconds, status))

        activities = [validate, prepare, reserve, prepare_session, run_opencode, checkpoint_and_stop, build_review, release]
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="kite-workflow-test",
                workflows=[CodingTaskWorkflow],
                activities=activities,
            ):
                result = await env.client.execute_workflow(
                    CodingTaskWorkflow.run,
                    task_input(),
                    id="task-1",
                    task_queue="kite-workflow-test",
                    execution_timeout=timedelta(minutes=5),
                )

        assert isinstance(result, CodingTaskOutcome)
        assert result.status == "READY_FOR_REVIEW"
        assert result.checkpoint_path == "/state/checkpoints/task-1.json"
        assert result.review_bundle.task_id == "task-1"
        assert events == [
            ("validate", "task-1"),
            ("prepare", "agent/task-1"),
            ("reserve", "task-1", "task-1:lease:0"),
            ("session", "new"),
            ("run", "worker-1", "model-code", "/workspace/repo/.kite-worktrees/task-1", "", "session-1"),
            ("checkpoint", "session-1", "lease-1"),
            ("review", "/state/checkpoints/task-1.json"),
            ("release", "lease-1", 120, "COMPLETED"),
        ]

    asyncio.run(scenario())


def test_workflow_waits_for_a_route_and_rechecks_before_running():
    async def scenario():
        reserve_count = 0
        events = []

        @activity.defn(name="kite_validate_task")
        async def validate(task: CodingTaskInput) -> None:
            return None

        @activity.defn(name="kite_prepare_workspace")
        async def prepare(task: CodingTaskInput) -> str:
            return "/workspace/repo/.kite-worktrees/task-1"

        @activity.defn(name="kite_reserve_route")
        async def reserve(task: CodingTaskInput, idempotency_key: str) -> RouteLease | None:
            nonlocal reserve_count
            reserve_count += 1
            if reserve_count == 1:
                return None
            return route_lease()

        @activity.defn(name="kite_prepare_opencode_session")
        async def prepare_session(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            prior_session_id: str | None,
        ) -> str:
            return prior_session_id or "session-1"

        @activity.defn(name="kite_run_opencode")
        async def run_opencode(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            instruction: str, session_id: str,
        ) -> CodingRunResult:
            events.append("run")
            return CodingRunResult(
                task_id=task.task_id, base_repo_head=task.repo_head, session_id=session_id,
                patch="patch", summary="done", changed_files=("parser.py",), usage_gpu_seconds=60,
            )

        @activity.defn(name="kite_checkpoint_and_stop")
        async def checkpoint_and_stop(
            task: CodingTaskInput, route: RouteLease, run_result: CodingRunResult,
        ) -> str:
            return "/state/task-1.json"

        @activity.defn(name="kite_build_review_bundle")
        async def build_review(
            task: CodingTaskInput, route: RouteLease, run_result: CodingRunResult,
            checkpoint_path: str,
        ) -> ReviewBundle:
            return ReviewBundle(
                task_id=task.task_id, lease_id=route.lease_id, worker_id=route.worker_id,
                base_repo_head=run_result.base_repo_head, patch=run_result.patch,
                summary=run_result.summary, usage_gpu_seconds=run_result.usage_gpu_seconds,
            )

        @activity.defn(name="kite_release_route")
        async def release(
            task: CodingTaskInput, route: RouteLease,
            run_result: CodingRunResult | None, status: str,
        ) -> None:
            events.append("release")

        activities = [validate, prepare, reserve, prepare_session, run_opencode, checkpoint_and_stop, build_review, release]
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue="kite-route-wait-test",
                workflows=[CodingTaskWorkflow], activities=activities,
            ):
                result = await env.client.execute_workflow(
                    CodingTaskWorkflow.run, task_input(), id="task-wait-for-route",
                    task_queue="kite-route-wait-test", execution_timeout=timedelta(minutes=5),
                )

        assert reserve_count == 2
        assert events == ["run", "release"]
        assert result.status == "READY_FOR_REVIEW"

    asyncio.run(scenario())


def test_pause_checkpoints_before_resume_instruction_continues_opencode_session():
    async def scenario():
        entered_run = asyncio.Event()
        release_first_run = asyncio.Event()
        run_calls = []
        prepared_sessions = []
        checkpointed_sessions = []

        @activity.defn(name="kite_validate_task")
        async def validate(task: CodingTaskInput) -> None:
            return None

        @activity.defn(name="kite_prepare_workspace")
        async def prepare(task: CodingTaskInput) -> str:
            return "/workspace/repo/.kite-worktrees/task-1"

        @activity.defn(name="kite_reserve_route")
        async def reserve(task: CodingTaskInput, idempotency_key: str) -> RouteLease | None:
            return route_lease()

        @activity.defn(name="kite_prepare_opencode_session")
        async def prepare_session(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            prior_session_id: str | None,
        ) -> str:
            prepared_sessions.append(prior_session_id)
            return prior_session_id or "session-1"

        @activity.defn(name="kite_run_opencode")
        async def run_opencode(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            instruction: str, session_id: str,
        ) -> CodingRunResult:
            run_calls.append((instruction, session_id))
            if len(run_calls) == 1:
                entered_run.set()
                await release_first_run.wait()
            return CodingRunResult(
                task_id=task.task_id, base_repo_head=task.repo_head,
                session_id=session_id, patch="patch", summary="done",
                changed_files=("parser.py",), usage_gpu_seconds=60,
            )

        @activity.defn(name="kite_interrupt_opencode")
        async def interrupt_opencode(
            task: CodingTaskInput, route: RouteLease, session_id: str,
        ) -> None:
            release_first_run.set()

        @activity.defn(name="kite_checkpoint_and_stop")
        async def checkpoint_and_stop(
            task: CodingTaskInput, route: RouteLease, run_result: CodingRunResult,
        ) -> str:
            checkpointed_sessions.append(run_result.session_id)
            return f"/state/{run_result.session_id}.json"

        @activity.defn(name="kite_build_review_bundle")
        async def build_review(
            task: CodingTaskInput, route: RouteLease, run_result: CodingRunResult,
            checkpoint_path: str,
        ) -> ReviewBundle:
            return ReviewBundle(
                task_id=task.task_id, lease_id=route.lease_id, worker_id=route.worker_id,
                base_repo_head=run_result.base_repo_head, patch=run_result.patch,
                summary=run_result.summary, usage_gpu_seconds=run_result.usage_gpu_seconds,
            )

        @activity.defn(name="kite_release_route")
        async def release(
            task: CodingTaskInput, route: RouteLease,
            run_result: CodingRunResult | None, status: str,
        ) -> None:
            return None

        activities = [validate, prepare, reserve, prepare_session, run_opencode, interrupt_opencode, checkpoint_and_stop, build_review, release]
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue="kite-pause-test",
                workflows=[CodingTaskWorkflow], activities=activities,
            ):
                handle = await env.client.start_workflow(
                    CodingTaskWorkflow.run, task_input(), id="task-pause",
                    task_queue="kite-pause-test", execution_timeout=timedelta(minutes=5),
                )
                await asyncio.wait_for(entered_run.wait(), timeout=10)
                await handle.signal(CodingTaskWorkflow.pause)

                for _ in range(100):
                    if await handle.query(CodingTaskWorkflow.current_status) == "PAUSED":
                        break
                    await asyncio.sleep(0.01)
                assert await handle.query(CodingTaskWorkflow.current_status) == "PAUSED"
                assert checkpointed_sessions == ["session-1"]

                await handle.signal(CodingTaskWorkflow.resume_with_instruction, "also handle blank input")
                result = await handle.result()

        assert run_calls == [("", "session-1"), ("also handle blank input", "session-1")]
        assert prepared_sessions == [None, "session-1"]
        assert checkpointed_sessions == ["session-1", "session-1"]
        assert result.checkpoint_path == "/state/session-1.json"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("signal_name", "expected_status"),
    [("reject", "REJECTED"), ("cancel", "CANCELLED")],
)
def test_reject_or_cancel_checkpoints_before_releasing_provider(signal_name, expected_status):
    async def scenario():
        entered_run = asyncio.Event()
        finish_run = asyncio.Event()
        events = []

        @activity.defn(name="kite_validate_task")
        async def validate(task: CodingTaskInput) -> None:
            return None

        @activity.defn(name="kite_prepare_workspace")
        async def prepare(task: CodingTaskInput) -> str:
            return "/workspace/task-1"

        @activity.defn(name="kite_reserve_route")
        async def reserve(task: CodingTaskInput, key: str) -> RouteLease | None:
            return route_lease()

        @activity.defn(name="kite_prepare_opencode_session")
        async def prepare_session(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            prior_session_id: str | None,
        ) -> str:
            return prior_session_id or "session-stop"

        @activity.defn(name="kite_run_opencode")
        async def run_opencode(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            instruction: str, session_id: str,
        ) -> CodingRunResult:
            entered_run.set()
            await finish_run.wait()
            return CodingRunResult(
                task_id=task.task_id, base_repo_head=task.repo_head, session_id=session_id,
                patch="patch", summary="done", changed_files=("parser.py",),
                usage_gpu_seconds=30,
            )

        @activity.defn(name="kite_interrupt_opencode")
        async def interrupt_opencode(
            task: CodingTaskInput, route: RouteLease, session_id: str,
        ) -> None:
            events.append(("interrupt", session_id))
            finish_run.set()

        @activity.defn(name="kite_checkpoint_and_stop")
        async def checkpoint_and_stop(
            task: CodingTaskInput, route: RouteLease, result: CodingRunResult,
        ) -> str:
            events.append(("checkpoint", result.session_id))
            return "/state/session-stop.json"

        @activity.defn(name="kite_release_route")
        async def release(
            task: CodingTaskInput, route: RouteLease,
            result: CodingRunResult | None, outcome: str,
        ) -> None:
            events.append(("release", outcome))

        activities = [validate, prepare, reserve, prepare_session, run_opencode, interrupt_opencode, checkpoint_and_stop, release]
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue=f"kite-stop-{signal_name}",
                workflows=[CodingTaskWorkflow], activities=activities,
            ):
                handle = await env.client.start_workflow(
                    CodingTaskWorkflow.run, task_input(), id=f"task-{signal_name}",
                    task_queue=f"kite-stop-{signal_name}",
                    execution_timeout=timedelta(minutes=5),
                )
                await asyncio.wait_for(entered_run.wait(), timeout=10)
                await handle.signal(signal_name)
                result = await handle.result()

        assert result.status == expected_status
        assert events == [
            ("interrupt", "session-stop"), ("checkpoint", "session-stop"),
            ("release", expected_status),
        ]

    asyncio.run(scenario())


def test_retryable_opencode_failure_retries_same_route_then_checkpoints():
    async def scenario():
        run_attempts = 0
        reservations = []
        events = []

        @activity.defn(name="kite_validate_task")
        async def validate(task: CodingTaskInput) -> None:
            return None

        @activity.defn(name="kite_prepare_workspace")
        async def prepare(task: CodingTaskInput) -> str:
            return "/workspace/task-1"

        @activity.defn(name="kite_reserve_route")
        async def reserve(task: CodingTaskInput, key: str) -> RouteLease | None:
            reservations.append(key)
            return route_lease()

        @activity.defn(name="kite_prepare_opencode_session")
        async def prepare_session(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            prior_session_id: str | None,
        ) -> str:
            return prior_session_id or "session-retry"

        @activity.defn(name="kite_run_opencode")
        async def run_opencode(
            task: CodingTaskInput, route: RouteLease, workspace_path: str,
            instruction: str, session_id: str,
        ) -> CodingRunResult:
            nonlocal run_attempts
            run_attempts += 1
            events.append(("run", run_attempts))
            if run_attempts == 1:
                raise ApplicationError("temporary provider error")
            return CodingRunResult(
                task_id=task.task_id, base_repo_head=task.repo_head, session_id=session_id,
                patch="patch", summary="done", changed_files=("parser.py",),
                usage_gpu_seconds=45,
            )

        @activity.defn(name="kite_checkpoint_and_stop")
        async def checkpoint_and_stop(
            task: CodingTaskInput, route: RouteLease, result: CodingRunResult,
        ) -> str:
            events.append(("checkpoint", result.session_id))
            return "/state/session-retry.json"

        @activity.defn(name="kite_build_review_bundle")
        async def build_review(
            task: CodingTaskInput, route: RouteLease, result: CodingRunResult,
            checkpoint_path: str,
        ) -> ReviewBundle:
            return ReviewBundle(
                task_id=task.task_id, lease_id=route.lease_id, worker_id=route.worker_id,
                base_repo_head=result.base_repo_head, patch=result.patch,
                summary=result.summary, usage_gpu_seconds=result.usage_gpu_seconds,
            )

        @activity.defn(name="kite_release_route")
        async def release(
            task: CodingTaskInput, route: RouteLease,
            result: CodingRunResult | None, outcome: str,
        ) -> None:
            events.append(("release", outcome))

        activities = [
            validate, prepare, reserve, prepare_session, run_opencode, checkpoint_and_stop,
            build_review, release,
        ]
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue="kite-retry-test",
                workflows=[CodingTaskWorkflow], activities=activities,
            ):
                result = await env.client.execute_workflow(
                    CodingTaskWorkflow.run, task_input(), id="task-retry",
                    task_queue="kite-retry-test", execution_timeout=timedelta(minutes=5),
                )

        assert result.status == "READY_FOR_REVIEW"
        assert run_attempts == 2
        assert len(reservations) == 1
        assert events == [
            ("run", 1), ("run", 2), ("checkpoint", "session-retry"),
            ("release", "COMPLETED"),
        ]

    asyncio.run(scenario())


def test_nonretryable_validation_error_stops_before_route_reservation():
    async def scenario():
        events = []

        @activity.defn(name="kite_validate_task")
        async def validate(task: CodingTaskInput) -> None:
            raise ApplicationError("invalid task", non_retryable=True)

        @activity.defn(name="kite_prepare_workspace")
        async def prepare(task: CodingTaskInput) -> str:
            events.append("prepare")
            return "/workspace/task-1"

        @activity.defn(name="kite_reserve_route")
        async def reserve(task: CodingTaskInput, key: str) -> RouteLease | None:
            events.append("reserve")
            return route_lease()

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue="kite-nonretry-test",
                workflows=[CodingTaskWorkflow], activities=[validate, prepare, reserve],
            ):
                handle = await env.client.start_workflow(
                    CodingTaskWorkflow.run, task_input(), id="task-nonretry",
                    task_queue="kite-nonretry-test", execution_timeout=timedelta(minutes=5),
                )
                with pytest.raises(WorkflowFailureError):
                    await handle.result()

        assert events == []

    asyncio.run(scenario())
