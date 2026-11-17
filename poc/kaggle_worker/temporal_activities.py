"""Temporal activity contracts and stable idempotency-key helpers.

Concrete provider, workspace and persistence adapters are injected by the
runtime. Each adapter method must persist the supplied key with its side effect
so a Temporal retry returns the original result instead of repeating it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Protocol, TypeVar

from temporalio import activity

from .integration import ReviewBundle
from .harness2 import Harness2EvaluationRecord, Harness2Runner, Harness2Spec
from .opencode import OpenCodeTaskAdapter
from .temporal_state import RecoveryDecision, TemporalActivityJournal
from .temporal_workflow import CodingRunResult, CodingTaskInput, RouteLease

T = TypeVar("T")


def activity_idempotency_key(task_id: str, workflow_run_id: str, operation: str) -> str:
    """Build a stable operation key that is unique within a workflow run."""
    parts = (task_id, workflow_run_id, operation)
    if any(not isinstance(part, str) or not part.strip() for part in parts):
        raise ValueError("task_id, workflow_run_id, and operation must be non-empty strings")
    return ":".join(part.strip() for part in parts)


class KiteActivityBackend(Protocol):
    """Side-effect adapter with a durable replay contract.

    Implementations must persist each operation key with its result so retries
    return the original lease/session/checkpoint and settle usage once. Route
    reservation and session identity must be durable before returning. Session
    execution must acquire a single-writer guard, resume that same session on
    retry, and call ``heartbeat`` with resumable progress; checkpointing must
    finish before a session is stopped or its route is released.
    """

    async def reconcile_operation(
        self, operation_name: str, operation_key: str, *operation_context: object,
    ) -> RecoveryDecision: ...

    async def validate_task(self, task: CodingTaskInput, key: str) -> None: ...
    async def prepare_workspace(self, task: CodingTaskInput, key: str) -> str: ...
    async def reserve_route(self, task: CodingTaskInput, key: str) -> RouteLease | None: ...
    async def prepare_opencode_session(
        self, task: CodingTaskInput, route: RouteLease, workspace_path: str,
        prior_session_id: str | None, key: str,
    ) -> str: ...
    async def heartbeat_route(
        self, task: CodingTaskInput, route: RouteLease, key: str,
    ) -> None: ...
    async def run_opencode(
        self, task: CodingTaskInput, route: RouteLease, workspace_path: str,
        instruction: str, session_id: str, key: str,
        heartbeat: Callable[[dict[str, object]], None],
    ) -> CodingRunResult: ...
    async def interrupt_opencode(
        self, task: CodingTaskInput, route: RouteLease, session_id: str, key: str,
    ) -> None: ...
    async def checkpoint_and_stop(
        self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult, key: str,
    ) -> str: ...
    async def build_review_bundle(
        self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult,
        checkpoint_path: str, key: str,
    ) -> ReviewBundle: ...
    async def release_route(
        self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult | None,
        outcome: str, key: str,
    ) -> None: ...


class TemporalKiteActivities:
    """Registerable activity methods backed by injected Kite adapters."""

    def __init__(
        self,
        backend: KiteActivityBackend,
        journal: TemporalActivityJournal,
        *,
        opencode: OpenCodeTaskAdapter | None = None,
        harness2: Harness2Runner | None = None,
        route_heartbeat_interval_seconds: float = 30.0,
    ) -> None:
        if route_heartbeat_interval_seconds <= 0:
            raise ValueError("route_heartbeat_interval_seconds must be positive")
        self.backend = backend
        self.journal = journal
        self.opencode = opencode
        self.harness2 = harness2
        self.route_heartbeat_interval_seconds = route_heartbeat_interval_seconds

    @staticmethod
    def _key(task_id: str, operation: str) -> str:
        info = activity.info()
        return activity_idempotency_key(
            task_id, info.workflow_run_id, f"{info.activity_id}:{operation}"
        )

    @classmethod
    def _fingerprint(cls, operation_name: str, context: tuple[object, ...]) -> str:
        def jsonable(value: object) -> object:
            if is_dataclass(value) and not isinstance(value, type):
                return jsonable(asdict(value))
            if isinstance(value, dict):
                return {str(key): jsonable(item) for key, item in sorted(value.items())}
            if isinstance(value, (tuple, list)):
                return [jsonable(item) for item in value]
            if isinstance(value, Path):
                return str(value)
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            raise TypeError(f"unsupported activity request value: {type(value).__name__}")

        payload = json.dumps(
            {"operation": operation_name, "context": jsonable(context)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _run_journaled(
        self,
        operation_name: str,
        key: str,
        identity: tuple[object, ...],
        operation: Callable[[str], Awaitable[T]],
        result_type: type[T],
        heartbeat_details: dict[str, object] | None = None,
    ) -> T:
        async def reconcile(operation_key: str) -> RecoveryDecision:
            if self.opencode and operation_name in {
                "prepare-opencode-session", "run-opencode", "interrupt-opencode",
                "stop-opencode-session",
            }:
                return await self.opencode.reconcile_operation(
                    operation_name, operation_key, *identity,
                )
            if self.harness2 and operation_name == "evaluate-harness":
                return await self.harness2.reconcile_operation(operation_key, identity[0])
            return await self.backend.reconcile_operation(
                operation_name, operation_key, *identity,
            )

        return await self.journal.run(
            key,
            operation,
            result_type=result_type,
            request_fingerprint=self._fingerprint(operation_name, identity),
            recover=reconcile,
            on_wait=lambda: activity.heartbeat({
                "state": "WAITING", "operation_key": key,
                "operation": operation_name,
                **(heartbeat_details or {}),
            }),
        )

    async def _run_with_route_heartbeats(
        self,
        task: CodingTaskInput,
        route: RouteLease,
        workspace_path: str,
        instruction: str,
        session_id: str,
        operation_key: str,
        attempt_number: int,
        resume_details: dict[str, object] | None = None,
    ) -> CodingRunResult:
        heartbeat_number = 0
        progress = dict(resume_details or {})

        def emit_heartbeat(details: dict[str, object]) -> None:
            progress.update(details)
            activity.heartbeat(dict(progress))

        async def renew_route() -> None:
            nonlocal heartbeat_number
            while True:
                await asyncio.sleep(self.route_heartbeat_interval_seconds)
                heartbeat_number += 1
                await self.backend.heartbeat_route(
                    task, route,
                    f"{operation_key}:attempt:{attempt_number}:route-heartbeat:{heartbeat_number}",
                )
                emit_heartbeat({
                    "task_id": task.task_id,
                    "route_lease_id": route.lease_id,
                    "session_id": session_id,
                    "operation_key": operation_key,
                    "heartbeat_number": heartbeat_number,
                    "state": "RUNNING",
                })

        if self.opencode:
            run_implementation = self.opencode.run(
                task, route, workspace_path, instruction, session_id,
                operation_key, emit_heartbeat, resume_details=resume_details,
            )
        else:
            run_implementation = self.backend.run_opencode(
                task, route, workspace_path, instruction, session_id,
                operation_key, emit_heartbeat,
            )
        run_task = asyncio.create_task(run_implementation)
        heartbeat_task = asyncio.create_task(renew_route())
        try:
            done, _ = await asyncio.wait(
                (run_task, heartbeat_task), return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
                if error is not None:
                    raise error
                raise RuntimeError("route heartbeat loop stopped unexpectedly")
            return await run_task
        finally:
            for task_to_stop in (run_task, heartbeat_task):
                if not task_to_stop.done():
                    task_to_stop.cancel()
            await asyncio.gather(run_task, heartbeat_task, return_exceptions=True)

    @activity.defn(name="kite_validate_task")
    async def validate_task(self, task: CodingTaskInput) -> None:
        key = self._key(task.task_id, "validate-task")
        await self._run_journaled(
            "validate-task", key, (task,),
            lambda operation_key: self.backend.validate_task(task, operation_key),
            type(None),
        )

    @activity.defn(name="kite_prepare_workspace")
    async def prepare_workspace(self, task: CodingTaskInput) -> str:
        key = self._key(task.task_id, "prepare-workspace")
        return await self._run_journaled(
            "prepare-workspace", key, (task,),
            lambda operation_key: self.backend.prepare_workspace(task, operation_key), str,
        )

    @activity.defn(name="kite_reserve_route")
    async def reserve_route(self, task: CodingTaskInput, attempt_key: str) -> RouteLease | None:
        key = self._key(task.task_id, f"reserve-route:{attempt_key.rsplit(':', 1)[-1]}")
        return await self._run_journaled(
            "reserve-route", key, (task, attempt_key),
            lambda operation_key: self.backend.reserve_route(task, operation_key), RouteLease,
        )

    @activity.defn(name="kite_prepare_opencode_session")
    async def prepare_opencode_session(
        self, task: CodingTaskInput, route: RouteLease, workspace_path: str,
        prior_session_id: str | None,
    ) -> str:
        key = self._key(task.task_id, "prepare-opencode-session")
        return await self._run_journaled(
            "prepare-opencode-session", key,
            (task, route, workspace_path, prior_session_id),
            (lambda operation_key: self.opencode.prepare_session(
                task, route, workspace_path, prior_session_id, operation_key,
            )) if self.opencode else (
                lambda operation_key: self.backend.prepare_opencode_session(
                    task, route, workspace_path, prior_session_id, operation_key,
                )
            ),
            str,
        )

    @activity.defn(name="kite_run_opencode")
    async def run_opencode(
        self, task: CodingTaskInput, route: RouteLease, workspace_path: str,
        instruction: str, session_id: str,
    ) -> CodingRunResult:
        key = self._key(task.task_id, "run-opencode")
        info = activity.info()
        attempt_number = info.attempt
        resume_details = next((
            item for item in reversed(info.heartbeat_details)
            if isinstance(item, dict) and item.get("operation_key") in (None, self._key(task.task_id, "run-opencode"))
        ), None)
        heartbeat = activity.heartbeat
        heartbeat({
            "task_id": task.task_id,
            "route_lease_id": route.lease_id,
            "session_id": session_id,
            "operation_key": key,
            "state": "RUNNING",
        })

        async def exclusive_run() -> CodingRunResult:
            return await self.journal.run_single_writer(
                f"coding-task:{task.task_id}",
                lambda: self._run_with_route_heartbeats(
                    task, route, workspace_path, instruction, session_id, key,
                    attempt_number, resume_details,
                ),
                on_wait=lambda: heartbeat({
                    "task_id": task.task_id,
                    "route_lease_id": route.lease_id,
                    "session_id": session_id,
                    "operation_key": key,
                    "state": "WAITING_FOR_SINGLE_WRITER",
                }),
            )

        return await self._run_journaled(
            "run-opencode", key,
            (task, route, workspace_path, instruction, session_id),
            lambda _operation_key: exclusive_run(),
            CodingRunResult,
            heartbeat_details={
                "task_id": task.task_id,
                "route_lease_id": route.lease_id,
                "session_id": session_id,
            },
        )

    @activity.defn(name="kite_checkpoint_and_stop")
    async def checkpoint_and_stop(
        self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult,
    ) -> str:
        key = self._key(task.task_id, "checkpoint-and-stop")
        checkpoint_path = await self._run_journaled(
            "checkpoint-and-stop", key, (task, route, result),
            lambda operation_key: self.backend.checkpoint_and_stop(
                task, route, result, operation_key,
            ),
            str,
        )
        if self.opencode:
            stop_key = self._key(task.task_id, "stop-opencode-session")
            await self._run_journaled(
                "stop-opencode-session", stop_key,
                (task, route, result, result.session_id),
                lambda _operation_key: self.opencode.stop_session(
                    result.session_id, activity.heartbeat,
                ),
                type(None),
            )
        return checkpoint_path

    @activity.defn(name="kite_interrupt_opencode")
    async def interrupt_opencode(
        self, task: CodingTaskInput, route: RouteLease, session_id: str,
    ) -> None:
        key = self._key(task.task_id, f"interrupt-opencode:{session_id}")
        identity = (task, route, session_id)
        if self.opencode:
            operation = lambda _operation_key: self.opencode.stop_session(
                session_id, activity.heartbeat,
            )
        else:
            operation = lambda operation_key: self.backend.interrupt_opencode(
                task, route, session_id, operation_key,
            )
        await self._run_journaled(
            "interrupt-opencode", key, identity, operation, type(None),
            heartbeat_details={"task_id": task.task_id, "session_id": session_id},
        )

    @activity.defn(name="kite_evaluate_harness")
    async def evaluate_harness(self, spec: Harness2Spec) -> Harness2EvaluationRecord:
        if not self.harness2:
            raise RuntimeError("Harness² runner is not configured")
        key = self._key("harness2", "evaluate-harness")
        evaluation_id = spec.evaluation_id or f"eval_{hashlib.sha256(key.encode()).hexdigest()[:32]}"
        stable_spec = replace(spec, evaluation_id=evaluation_id)
        return await self._run_journaled(
            "evaluate-harness", key, (stable_spec,),
            lambda _operation_key: asyncio.to_thread(self.harness2.evaluate, stable_spec),
            Harness2EvaluationRecord,
            heartbeat_details={"evaluation_id": evaluation_id, "state": "RUNNING"},
        )

    @activity.defn(name="kite_build_review_bundle")
    async def build_review_bundle(
        self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult,
        checkpoint_path: str,
    ) -> ReviewBundle:
        key = self._key(task.task_id, "build-review-bundle")
        return await self._run_journaled(
            "build-review-bundle", key, (task, route, result, checkpoint_path),
            lambda operation_key: self.backend.build_review_bundle(
                task, route, result, checkpoint_path, operation_key,
            ),
            ReviewBundle,
        )

    @activity.defn(name="kite_release_route")
    async def release_route(
        self, task: CodingTaskInput, route: RouteLease, result: CodingRunResult | None,
        outcome: str,
    ) -> None:
        key = self._key(task.task_id, f"release-route:{outcome}")
        await self._run_journaled(
            f"release-route:{outcome}", key, (task, route, result, outcome),
            lambda operation_key: self.backend.release_route(
                task, route, result, outcome, operation_key,
            ),
            type(None),
        )
