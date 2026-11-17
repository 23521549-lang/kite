"""Quota-aware orchestration of one provider worker at a time."""

from __future__ import annotations

import threading
import time
from typing import Callable

from .protocol import CheckpointRef, ModelRequest, ModelResponse, WorkerProvider, WorkerStatus


class WorkerError(RuntimeError):
    """Base error that keeps the task identity needed for recovery."""

    def __init__(self, message: str, *, task_id: str | None = None, session_id: str | None = None):
        super().__init__(message)
        self.task_id = task_id
        self.session_id = session_id


class WorkerStartTimeout(WorkerError):
    """Raised when the provider does not become ready before its deadline."""


class WorkerFailed(WorkerError):
    """Raised when a worker reports a terminal or failed state."""


class WorkerController:
    """Serializes model requests through a lazily started worker session."""

    def __init__(
        self,
        provider: WorkerProvider,
        *,
        checkpoint_writer: Callable[[str, str], CheckpointRef] | None = None,
        startup_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._provider = provider
        self._checkpoint_writer = checkpoint_writer
        self._startup_timeout_seconds = startup_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._operation_lock = threading.RLock()
        self._task_id: str | None = None
        self._session_id: str | None = None
        self._last_activity_at: float | None = None

    @property
    def active_task_id(self) -> str | None:
        with self._operation_lock:
            return self._task_id

    @property
    def active_session_id(self) -> str | None:
        with self._operation_lock:
            return self._session_id

    @property
    def status(self) -> WorkerStatus:
        with self._operation_lock:
            if self._session_id is None:
                return WorkerStatus.STOPPED
            return self._provider.status(self._session_id)

    def run_request(self, task_id: str, request: ModelRequest) -> ModelResponse:
        """Serialize requests and lazily start/reuse the active task's worker."""
        with self._operation_lock:
            return self._run_request(task_id, request)

    def _run_request(self, task_id: str, request: ModelRequest) -> ModelResponse:
        self._ensure_task(task_id)
        if self._session_id is None:
            try:
                session_id = self._provider.start(task_id)
            except Exception as error:
                raise WorkerFailed(
                    f"Worker failed to start for task {task_id}: {error}", task_id=task_id
                ) from error
            if not session_id:
                raise WorkerFailed("Provider returned an empty session id", task_id=task_id)
            self._task_id = task_id
            self._session_id = session_id

        self._wait_until_ready(task_id, self._session_id)
        try:
            response = self._provider.invoke(self._session_id, request)
        except Exception as error:
            raise WorkerFailed(
                f"Worker invocation failed for task {task_id}: {error}",
                task_id=task_id,
                session_id=self._session_id,
            ) from error
        finally:
            self._last_activity_at = self._monotonic()
        return response

    def pause(self, task_id: str, reason: str) -> CheckpointRef:
        """Serialize pause, persist task progress, then stop its worker."""
        with self._operation_lock:
            return self._pause(task_id, reason)

    def _pause(self, task_id: str, reason: str) -> CheckpointRef:
        if self._checkpoint_writer is None:
            raise WorkerError("A checkpoint writer is required before pausing", task_id=task_id)
        self._ensure_task(task_id)
        checkpoint = self._checkpoint_writer(task_id, reason)
        if self._session_id is not None:
            self._stop_active()
        return checkpoint

    def complete(self, task_id: str) -> None:
        """Stop the task's worker if active; repeated completion is harmless."""
        with self._operation_lock:
            self._stop_for_task(task_id)

    def cancel(self, task_id: str) -> None:
        """Stop the task's worker if active; repeated cancellation is harmless."""
        with self._operation_lock:
            self._stop_for_task(task_id)

    def stop_if_idle(self, idle_timeout_seconds: float) -> bool:
        """Checkpoint and stop an active worker after the configured idle period."""
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        with self._operation_lock:
            if self._session_id is None or self._task_id is None or self._last_activity_at is None:
                return False
            if self._monotonic() - self._last_activity_at < idle_timeout_seconds:
                return False
            self._pause(self._task_id, "idle timeout")
            return True

    def _ensure_task(self, task_id: str) -> None:
        if self._task_id is not None and self._task_id != task_id:
            raise WorkerError(
                f"Worker is already assigned to task {self._task_id}",
                task_id=task_id,
                session_id=self._session_id,
            )

    def _wait_until_ready(self, task_id: str, session_id: str) -> None:
        deadline = self._monotonic() + self._startup_timeout_seconds
        while True:
            try:
                status = self._provider.status(session_id)
            except Exception as error:
                self._stop_after_start_failure(session_id)
                raise WorkerFailed(
                    f"Could not check worker status for task {task_id}: {error}",
                    task_id=task_id,
                    session_id=session_id,
                ) from error

            if status is WorkerStatus.READY:
                return
            if status in (WorkerStatus.FAILED, WorkerStatus.STOPPED):
                self._stop_after_start_failure(session_id)
                raise WorkerFailed(
                    f"Worker entered {status.value} while starting task {task_id}",
                    task_id=task_id,
                    session_id=session_id,
                )

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                self._stop_after_start_failure(session_id)
                raise WorkerStartTimeout(
                    f"Worker did not become ready within {self._startup_timeout_seconds:g}s",
                    task_id=task_id,
                    session_id=session_id,
                )
            self._sleep(min(self._poll_interval_seconds, remaining))

    def _stop_after_start_failure(self, session_id: str) -> None:
        try:
            self._provider.stop(session_id)
        except Exception:
            # Keep the session identity so a caller can retry cleanup.
            return
        self._clear_session()

    def _stop_for_task(self, task_id: str) -> None:
        self._ensure_task(task_id)
        if self._session_id is not None:
            self._stop_active()

    def _stop_active(self) -> None:
        assert self._session_id is not None
        self._provider.stop(self._session_id)
        self._clear_session()

    def _clear_session(self) -> None:
        self._session_id = None
        self._task_id = None
        self._last_activity_at = None
