"""Durable Temporal workflow for an isolated Kite coding task."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .integration import ReviewBundle


@dataclass(frozen=True)
class CodingTaskInput:
    task_id: str
    goal: str
    repo_path: str
    repo_head: str
    branch: str
    required_capabilities: tuple[str, ...]
    preferred_model_id: str | None
    estimated_gpu_seconds: int
    task_type: str = "coding"
    role: str = "coder"
    # Set only when a long wait for a worker continues the workflow as new: the workspace was
    # already validated and prepared by the earlier run, so the new run reuses it.
    prepared_workspace: str | None = None
    route_wait_count: int = 0


@dataclass(frozen=True)
class RouteLease:
    task_id: str
    lease_id: str
    owner_id: str
    provider_id: str
    worker_id: str
    model_id: str
    reserved_gpu_seconds: int


@dataclass(frozen=True)
class CodingRunResult:
    task_id: str
    base_repo_head: str
    session_id: str
    patch: str
    summary: str
    changed_files: tuple[str, ...]
    usage_gpu_seconds: int


@dataclass(frozen=True)
class CodingTaskOutcome:
    task_id: str
    status: str
    checkpoint_path: str | None
    review_bundle: ReviewBundle | None


# A waiting workflow re-checks for a worker with backoff (15 s doubling to 5 min) or as soon as
# the ``worker_available`` signal arrives. Each check adds history events, so after this many
# checks the workflow continues as new to stay far below Temporal's 51,200-event limit.
MAX_ROUTE_CHECKS_PER_RUN = 200
ROUTE_RECHECK_BASE_SECONDS = 15
ROUTE_RECHECK_MAX_SECONDS = 300


@workflow.defn(name="KiteCodingTaskWorkflow")
class CodingTaskWorkflow:
    """Route, execute, checkpoint, and return a review-only result."""

    def __init__(self) -> None:
        self._paused = False
        self._cancelled = False
        self._rejected = False
        self._pending_instruction = ""
        self._status = "CREATED"
        self._checkpoint_path: str | None = None
        self._worker_hint = False

    @workflow.signal
    def pause(self) -> None:
        self._paused = True
        self._status = "PAUSE_REQUESTED"

    @workflow.signal
    def resume_with_instruction(self, instruction: str = "") -> None:
        if instruction.strip():
            self._pending_instruction = instruction.strip()
        self._paused = False

    @workflow.signal
    def cancel(self) -> None:
        self._cancelled = True
        self._paused = False

    @workflow.signal
    def reject(self) -> None:
        """Stop this coding attempt after its current checkpoint is durable."""
        self._rejected = True
        self._paused = False

    @workflow.signal
    def worker_available(self) -> None:
        """Wake a task that is waiting for a worker (sent when a worker heartbeats as ready)."""
        self._worker_hint = True

    @workflow.query
    def current_status(self) -> str:
        return self._status

    async def _wait_if_paused(self) -> None:
        if self._paused:
            self._status = "PAUSED"
            await workflow.wait_condition(
                lambda: not self._paused or self._cancelled or self._rejected
            )

    async def _execute(
        self,
        name: str,
        *args: object,
        timeout: timedelta,
        result_type: type | None = None,
    ) -> object:
        return await workflow.execute_activity(
            name,
            args=list(args),
            result_type=result_type,
            start_to_close_timeout=timeout,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=3,
            ),
        )

    async def _run_coding_activity(
        self,
        task: CodingTaskInput,
        route: RouteLease,
        workspace_path: str,
        instruction: str,
        session_id: str,
    ) -> CodingRunResult:
        run_handle = workflow.start_activity(
            "kite_run_opencode",
            args=[task, route, workspace_path, instruction, session_id],
            start_to_close_timeout=timedelta(hours=6),
            heartbeat_timeout=timedelta(minutes=2),
            result_type=CodingRunResult,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=3,
            ),
        )
        control_wait = asyncio.create_task(workflow.wait_condition(
            lambda: self._paused or self._cancelled or self._rejected,
        ))
        done, _ = await workflow.wait(
            [run_handle, control_wait], return_when=asyncio.FIRST_COMPLETED,
        )
        try:
            if control_wait in done and not run_handle.done():
                await self._execute(
                    "kite_interrupt_opencode", task, route, session_id,
                    timeout=timedelta(minutes=2),
                )
            return await run_handle
        finally:
            if not control_wait.done():
                control_wait.cancel()
                await workflow.wait([control_wait])

    @workflow.run
    async def run(self, task: CodingTaskInput) -> CodingTaskOutcome:
        self._status = "VALIDATING"
        await self._wait_if_paused()
        if self._cancelled:
            self._status = "CANCELLED"
            return CodingTaskOutcome(task.task_id, "CANCELLED", None, None)
        if self._rejected:
            self._status = "REJECTED"
            return CodingTaskOutcome(task.task_id, "REJECTED", None, None)

        if task.prepared_workspace:
            workspace_path = task.prepared_workspace
        else:
            await self._execute("kite_validate_task", task, timeout=timedelta(minutes=1))
            workspace_path = await self._execute(
                "kite_prepare_workspace", task, timeout=timedelta(minutes=2)
            )

        route_attempt = 0
        route: RouteLease | None = None
        while route is None:
            await self._wait_if_paused()
            if self._cancelled:
                self._status = "CANCELLED"
                return CodingTaskOutcome(task.task_id, "CANCELLED", None, None)
            if self._rejected:
                self._status = "REJECTED"
                return CodingTaskOutcome(task.task_id, "REJECTED", None, None)
            self._status = "WAITING_FOR_WORKER"
            route = await self._execute(
                "kite_reserve_route",
                task,
                f"{task.task_id}:lease:{route_attempt}",
                timeout=timedelta(minutes=1),
                result_type=RouteLease,
            )
            if route is None:
                route_attempt += 1
                if (route_attempt >= MAX_ROUTE_CHECKS_PER_RUN
                        or workflow.info().is_continue_as_new_suggested()):
                    workflow.continue_as_new(replace(
                        task,
                        prepared_workspace=workspace_path,
                        route_wait_count=task.route_wait_count + route_attempt,
                    ))
                self._worker_hint = False
                delay = min(
                    ROUTE_RECHECK_MAX_SECONDS,
                    ROUTE_RECHECK_BASE_SECONDS * 2 ** min(route_attempt - 1, 5),
                )
                try:
                    await workflow.wait_condition(
                        lambda: self._worker_hint or self._paused or self._cancelled or self._rejected,
                        timeout=timedelta(seconds=delay),
                    )
                except asyncio.TimeoutError:
                    pass

        previous_session_id: str | None = None
        run_result: CodingRunResult | None = None
        while True:
            await self._wait_if_paused()
            if self._cancelled:
                await self._execute(
                    "kite_release_route", task, route, run_result, "CANCELLED",
                    timeout=timedelta(minutes=1),
                )
                self._status = "CANCELLED"
                return CodingTaskOutcome(task.task_id, "CANCELLED", self._checkpoint_path, None)
            if self._rejected:
                await self._execute(
                    "kite_release_route", task, route, run_result, "REJECTED",
                    timeout=timedelta(minutes=1),
                )
                self._status = "REJECTED"
                return CodingTaskOutcome(task.task_id, "REJECTED", self._checkpoint_path, None)

            instruction = self._pending_instruction
            self._pending_instruction = ""
            session_id = await self._execute(
                "kite_prepare_opencode_session",
                task,
                route,
                workspace_path,
                previous_session_id,
                timeout=timedelta(minutes=1),
                result_type=str,
            )
            self._status = "CODING"
            run_result = await self._run_coding_activity(
                task, route, workspace_path, instruction, session_id,
            )
            self._status = "CHECKPOINTING"
            self._checkpoint_path = await self._execute(
                "kite_checkpoint_and_stop", task, route, run_result,
                timeout=timedelta(minutes=2),
            )
            previous_session_id = run_result.session_id
            await self._wait_if_paused()
            if self._cancelled:
                await self._execute(
                    "kite_release_route", task, route, run_result, "CANCELLED",
                    timeout=timedelta(minutes=1),
                )
                self._status = "CANCELLED"
                return CodingTaskOutcome(task.task_id, "CANCELLED", self._checkpoint_path, None)
            if self._rejected:
                await self._execute(
                    "kite_release_route", task, route, run_result, "REJECTED",
                    timeout=timedelta(minutes=1),
                )
                self._status = "REJECTED"
                return CodingTaskOutcome(task.task_id, "REJECTED", self._checkpoint_path, None)
            if not self._pending_instruction:
                break

        self._status = "BUILDING_REVIEW"
        review_bundle = await self._execute(
            "kite_build_review_bundle", task, route, run_result, self._checkpoint_path,
            timeout=timedelta(minutes=1),
            result_type=ReviewBundle,
        )
        await self._execute(
            "kite_release_route", task, route, run_result, "COMPLETED",
            timeout=timedelta(minutes=1),
        )
        self._status = "READY_FOR_REVIEW"
        return CodingTaskOutcome(task.task_id, self._status, self._checkpoint_path, review_bundle)