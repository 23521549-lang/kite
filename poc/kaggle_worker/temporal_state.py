"""Durable result journal for retry-safe Temporal activity adapters."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, fields, is_dataclass
import json
from pathlib import Path
import sqlite3
import time
from collections.abc import Callable
from typing import Any, TypeVar, get_origin, get_type_hints
import uuid

T = TypeVar("T")


class JournalError(RuntimeError):
    """The activity journal could not safely complete an operation."""


@dataclass(frozen=True)
class RecoveryDecision:
    """Tell the journal whether reconciliation found a completed side effect."""

    recovered: bool
    result: Any = None


class TemporalActivityJournal:
    """Persist activity results and serialize concurrent execution by key.

    A crashed operation becomes claimable after its execution lease expires.
    Its adapter is always retried with the same key and must reconcile any
    external side effect before attempting it again.
    """

    def __init__(
        self,
        path: Path,
        *,
        execution_lease_seconds: float = 60.0,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        if execution_lease_seconds <= 1:
            raise ValueError("execution_lease_seconds must be greater than one second")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.execution_lease_seconds = execution_lease_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._closed = False
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS temporal_activity_results (
                    operation_key TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('RUNNING','FAILED','COMPLETED')),
                    owner_token TEXT,
                    lease_expires_at REAL,
                    result_json TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(temporal_activity_results)"
                ).fetchall()
            }
            if "request_fingerprint" not in columns:
                db.execute(
                    "ALTER TABLE temporal_activity_results "
                    "ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS temporal_resource_leases (
                    resource_key TEXT PRIMARY KEY,
                    owner_token TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise JournalError("activity journal is closed")
        db = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
        db.execute("PRAGMA busy_timeout = 15000")
        db.execute("PRAGMA synchronous = FULL")
        return db

    def close(self) -> None:
        """Prevent future journal operations (connections are short lived)."""
        self._closed = True

    @staticmethod
    def _serialize_result(value: Any) -> str:
        if is_dataclass(value) and not isinstance(value, type):
            payload = {"kind": "dataclass", "value": asdict(value)}
        elif value is None or isinstance(value, (str, int, float, bool)):
            payload = {"kind": "scalar", "value": value}
        else:
            raise TypeError(f"unsupported activity result type: {type(value).__name__}")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _deserialize_result(payload: str, result_type: type[T]) -> T:
        raw = json.loads(payload)
        value = raw["value"]
        if raw["kind"] == "scalar":
            if value is not None and result_type is not Any and not isinstance(value, result_type):
                raise JournalError(
                    f"journal result is {type(value).__name__}, expected {result_type.__name__}"
                )
            return value
        if raw["kind"] != "dataclass" or not isinstance(value, dict):
            raise JournalError("journal result payload has an invalid shape")
        if not isinstance(result_type, type) or not is_dataclass(result_type):
            raise JournalError("a dataclass result requires its dataclass result_type")
        hints = get_type_hints(result_type)
        for field in fields(result_type):
            if field.name in value and get_origin(hints.get(field.name)) is tuple:
                value[field.name] = tuple(value[field.name])
        return result_type(**value)

    def _claim(
        self, key: str, owner_token: str, request_fingerprint: str,
    ) -> tuple[str, str | None]:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state, owner_token, lease_expires_at, result_json, request_fingerprint "
                "FROM temporal_activity_results WHERE operation_key=?",
                (key,),
            ).fetchone()
            if row and row[4] != request_fingerprint:
                raise JournalError(
                    f"operation key {key!r} was reused for a different request"
                )
            if row and row[0] == "COMPLETED":
                db.execute("COMMIT")
                return "COMPLETED", row[3]
            if row and row[0] == "RUNNING" and row[2] is not None and row[2] > now:
                db.execute("COMMIT")
                return "WAIT", None
            recovering = bool(row)
            expiry = now + self.execution_lease_seconds
            db.execute(
                "INSERT INTO temporal_activity_results "
                "(operation_key,request_fingerprint,state,owner_token,lease_expires_at,result_json,updated_at) "
                "VALUES (?, ?, 'RUNNING', ?, ?, NULL, ?) "
                "ON CONFLICT(operation_key) DO UPDATE SET state='RUNNING',owner_token=excluded.owner_token, "
                "lease_expires_at=excluded.lease_expires_at,result_json=NULL,updated_at=excluded.updated_at",
                (key, request_fingerprint, owner_token, expiry, now),
            )
            db.execute("COMMIT")
        return ("RECOVER" if recovering else "CLAIMED"), None

    def _renew(self, key: str, owner_token: str) -> bool:
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE temporal_activity_results SET lease_expires_at=?,updated_at=? "
                "WHERE operation_key=? AND state='RUNNING' AND owner_token=?",
                (now + self.execution_lease_seconds, now, key, owner_token),
            )
            return cursor.rowcount == 1

    def _finish(self, key: str, owner_token: str, result_json: str) -> None:
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE temporal_activity_results SET state='COMPLETED',result_json=?, "
                "owner_token=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE operation_key=? AND state='RUNNING' AND owner_token=?",
                (result_json, now, key, owner_token),
            )
            if cursor.rowcount != 1:
                raise JournalError(f"lost execution lease for activity operation {key!r}")

    def _fail(self, key: str, owner_token: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE temporal_activity_results SET state='FAILED',owner_token=NULL, "
                "lease_expires_at=NULL,updated_at=? "
                "WHERE operation_key=? AND state='RUNNING' AND owner_token=?",
                (time.time(), key, owner_token),
            )

    def _claim_resource(self, key: str, owner: str) -> bool:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner_token,lease_expires_at FROM temporal_resource_leases "
                "WHERE resource_key=?", (key,),
            ).fetchone()
            if row and row[1] > now:
                db.execute("COMMIT")
                return False
            db.execute(
                "INSERT INTO temporal_resource_leases "
                "(resource_key,owner_token,lease_expires_at,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(resource_key) DO UPDATE SET owner_token=excluded.owner_token, "
                "lease_expires_at=excluded.lease_expires_at,updated_at=excluded.updated_at",
                (key, owner, now + self.execution_lease_seconds, now),
            )
            db.execute("COMMIT")
            return True

    def _renew_resource(self, key: str, owner: str) -> bool:
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE temporal_resource_leases SET lease_expires_at=?,updated_at=? "
                "WHERE resource_key=? AND owner_token=?",
                (now + self.execution_lease_seconds, now, key, owner),
            )
            return cursor.rowcount == 1

    def _release_resource(self, key: str, owner: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM temporal_resource_leases WHERE resource_key=? AND owner_token=?",
                (key, owner),
            )

    async def run_single_writer(
        self,
        resource_key: str,
        operation: Callable[[], Any],
        *,
        on_wait: Callable[[], None] | None = None,
    ) -> Any:
        """Serialize side effects for a resource across activities and processes.

        The lease is renewed while the operation runs and released on all normal
        exits. If renewal fails, the operation is cancelled and the result is
        rejected so a worker that lost ownership cannot publish a success.
        """
        if not isinstance(resource_key, str) or not resource_key.strip():
            raise ValueError("resource_key must be a non-empty string")
        if not callable(operation):
            raise TypeError("operation must be callable")
        key = resource_key.strip()
        owner = uuid.uuid4().hex
        while not self._claim_resource(key, owner):
            if on_wait is not None:
                on_wait()
            await asyncio.sleep(self.poll_interval_seconds)

        stop_renewal = asyncio.Event()
        lease_lost = asyncio.Event()

        async def renew_resource() -> None:
            interval = max(0.25, self.execution_lease_seconds / 3)
            while True:
                try:
                    await asyncio.wait_for(stop_renewal.wait(), timeout=interval)
                    return
                except TimeoutError:
                    if not self._renew_resource(key, owner):
                        lease_lost.set()
                        return

        try:
            operation_task = asyncio.create_task(operation())
            renewal_task = asyncio.create_task(renew_resource())
            done, _ = await asyncio.wait(
                (operation_task, renewal_task), return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done and not stop_renewal.is_set():
                renewal_error = renewal_task.exception()
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                raise JournalError(f"lost exclusive writer lease for {key!r}") from renewal_error
            result = await operation_task
            if lease_lost.is_set():
                raise JournalError(f"lost exclusive writer lease for {key!r}")
            return result
        finally:
            stop_renewal.set()
            if "operation_task" in locals() and not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
            if "renewal_task" in locals():
                if not renewal_task.done():
                    renewal_task.cancel()
                await asyncio.gather(renewal_task, return_exceptions=True)
            self._release_resource(key, owner)

    async def run(
        self,
        operation_key: str,
        operation: Any,
        *,
        result_type: type[T],
        request_fingerprint: str | None = None,
        recover: Any = None,
        on_wait: Callable[[], None] | None = None,
    ) -> T:
        """Run or retrieve an operation result under a persistent per-key lease."""
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise ValueError("operation_key must be a non-empty string")
        if not callable(operation):
            raise TypeError("operation must be callable")
        key = operation_key.strip()
        fingerprint = request_fingerprint or key
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("request_fingerprint must be a non-empty string")
        owner = uuid.uuid4().hex
        while True:
            state, result_json = self._claim(key, owner, fingerprint.strip())
            if state == "COMPLETED":
                if result_json is None:
                    raise JournalError("completed operation is missing its result")
                return self._deserialize_result(result_json, result_type)
            if state in ("CLAIMED", "RECOVER"):
                break
            if on_wait is not None:
                on_wait()
            await asyncio.sleep(self.poll_interval_seconds)

        stop_renewal = asyncio.Event()

        async def renew_lease() -> None:
            interval = max(0.25, self.execution_lease_seconds / 3)
            while True:
                try:
                    await asyncio.wait_for(stop_renewal.wait(), timeout=interval)
                    return
                except TimeoutError:
                    if not self._renew(key, owner):
                        return

        renewal_task = asyncio.create_task(renew_lease())
        try:
            if state == "RECOVER":
                if recover is None:
                    raise JournalError(
                        f"interrupted operation {key!r} requires a reconciliation handler"
                    )
                decision = await recover(key)
                if not isinstance(decision, RecoveryDecision):
                    raise TypeError("recovery handler must return RecoveryDecision")
                result = decision.result if decision.recovered else await operation(key)
            else:
                result = await operation(key)
            serialized = self._serialize_result(result)
            self._finish(key, owner, serialized)
            return self._deserialize_result(serialized, result_type)
        except BaseException:
            self._fail(key, owner)
            raise
        finally:
            stop_renewal.set()
            await renewal_task
