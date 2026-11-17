"""SQLite persistence for scheduler owners, workers, queued tasks, and leases."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Iterator, Protocol

from .protocol import OwnerPolicy, ScheduledTask, TaskLease, TaskState, WorkerProfile, WorkerResult
from .routing import RoutingRequest, WorkerLoad, select_worker


class StoreError(RuntimeError):
    """Invalid or conflicting scheduler state."""


class LeaseError(StoreError):
    """A lease is missing, expired, or does not match the operation."""


@dataclass(frozen=True)
class RouteReservation:
    lease: TaskLease
    provider_id: str
    model_id: str
    reserved_gpu_seconds: int
    reason: str


class SchedulerStore(Protocol):
    """Structural interface for scheduler persistence."""

    def register_owner_policy(self, policy: OwnerPolicy) -> None: ...
    def register_worker(self, profile: WorkerProfile) -> None: ...
    def enqueue(self, task: ScheduledTask) -> None: ...
    def get_worker(self, worker_id: str) -> WorkerProfile | None: ...
    def get_owner_policy(self, owner_id: str) -> OwnerPolicy | None: ...
    def claim_next(self, worker_id: str, now: float, lease_seconds: int,
                   max_observation_age_seconds: int) -> TaskLease | None: ...
    def reserve_for_task(self, task_id: str, idempotency_key: str, now: float,
                         lease_seconds: int, max_observation_age_seconds: int) -> RouteReservation | None: ...
    def heartbeat(self, lease_id: str, now: float, lease_seconds: int) -> TaskLease: ...
    def complete(self, lease_id: str, result: WorkerResult, now: float) -> None: ...
    def fail(self, lease_id: str, error: str, retryable: bool, now: float) -> None: ...
    def cancel(self, task_id: str, now: float) -> None: ...
    def get_task_state(self, task_id: str) -> str: ...
    def get_task(self, task_id: str) -> dict[str, object]: ...
    def list_tasks(self) -> list[dict[str, object]]: ...


class SQLiteSchedulerStore:
    """Thread-safe SQLite store with one transaction per state transition."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, timeout=10.0, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 10000")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owners (
                    owner_id TEXT PRIMARY KEY,
                    owner_authorized INTEGER NOT NULL CHECK (owner_authorized IN (0, 1)),
                    policy_approved INTEGER NOT NULL CHECK (policy_approved IN (0, 1)),
                    max_concurrent_sessions INTEGER NOT NULL CHECK (max_concurrent_sessions > 0),
                    quota_remaining_seconds INTEGER CHECK (quota_remaining_seconds IS NULL OR quota_remaining_seconds >= 0),
                    quota_observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
                    provider_id TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    model_ids_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    provider_ready INTEGER NOT NULL CHECK (provider_ready IN (0, 1)),
                    status_observed_at REAL NOT NULL,
                    max_concurrency INTEGER NOT NULL CHECK (max_concurrency > 0)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    model_id TEXT,
                    repo_head TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    estimated_gpu_seconds INTEGER NOT NULL CHECK (estimated_gpu_seconds > 0),
                    priority INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'QUEUED',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
                    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    reserved_gpu_seconds INTEGER NOT NULL CHECK (reserved_gpu_seconds >= 0),
                    actual_gpu_seconds INTEGER,
                    created_at REAL NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    retryable INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease_per_task
                    ON leases(task_id) WHERE status = 'ACTIVE';
                CREATE INDEX IF NOT EXISTS leases_by_owner_status ON leases(owner_id, status);
                CREATE INDEX IF NOT EXISTS tasks_by_state_priority ON tasks(state, priority DESC, created_at, task_id);
                """
            )
            # Prototype databases may predate result/error/retry metadata.
            # Keep EBS-backed restarts compatible without rebuilding lease rows.
            lease_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(leases)").fetchall()
            }
            migrations = {
                "result_json": "TEXT",
                "error": "TEXT",
                "retryable": "INTEGER",
                "idempotency_key": "TEXT",
                "model_id": "TEXT",
                "route_reason": "TEXT",
            }
            for name, column_type in migrations.items():
                if name not in lease_columns:
                    self._connection.execute(
                        f"ALTER TABLE leases ADD COLUMN {name} {column_type}"
                    )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS leases_by_idempotency_key "
                "ON leases(idempotency_key) WHERE idempotency_key IS NOT NULL"
            )

    def __enter__(self) -> SQLiteSchedulerStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @staticmethod
    def _worker_from_row(row: sqlite3.Row | None) -> WorkerProfile | None:
        if row is None:
            return None
        return WorkerProfile(
            worker_id=row["worker_id"], owner_id=row["owner_id"], provider_id=row["provider_id"],
            roles=tuple(json.loads(row["roles_json"])), model_ids=tuple(json.loads(row["model_ids_json"])),
            enabled=bool(row["enabled"]), provider_ready=bool(row["provider_ready"]),
            status_observed_at=row["status_observed_at"], max_concurrency=row["max_concurrency"],
        )

    @staticmethod
    def _owner_from_row(row: sqlite3.Row | None) -> OwnerPolicy | None:
        if row is None:
            return None
        return OwnerPolicy(
            owner_id=row["owner_id"], owner_authorized=bool(row["owner_authorized"]),
            policy_approved=bool(row["policy_approved"]),
            max_concurrent_sessions=row["max_concurrent_sessions"],
            quota_remaining_seconds=row["quota_remaining_seconds"],
            quota_observed_at=row["quota_observed_at"],
        )

    def register_owner_policy(self, policy: OwnerPolicy) -> None:
        try:
            with self._transaction() as db:
                db.execute(
                    """INSERT INTO owners VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id) DO UPDATE SET
                    owner_authorized=excluded.owner_authorized,
                    policy_approved=excluded.policy_approved,
                    max_concurrent_sessions=excluded.max_concurrent_sessions,
                    quota_remaining_seconds=excluded.quota_remaining_seconds,
                    quota_observed_at=excluded.quota_observed_at""",
                    (policy.owner_id, int(policy.owner_authorized), int(policy.policy_approved),
                     policy.max_concurrent_sessions, policy.quota_remaining_seconds, policy.quota_observed_at),
                )
        except sqlite3.Error as error:
            raise StoreError(f"could not register owner {policy.owner_id}: {error}") from error

    def register_worker(self, profile: WorkerProfile) -> None:
        try:
            with self._transaction() as db:
                previous = db.execute(
                    "SELECT owner_id FROM workers WHERE worker_id = ?", (profile.worker_id,)
                ).fetchone()
                if previous is not None and previous["owner_id"] != profile.owner_id:
                    raise StoreError(f"worker {profile.worker_id} cannot change owner")
                db.execute(
                    """INSERT INTO workers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                    provider_id=excluded.provider_id, roles_json=excluded.roles_json,
                    model_ids_json=excluded.model_ids_json, enabled=excluded.enabled,
                    provider_ready=excluded.provider_ready, status_observed_at=excluded.status_observed_at,
                    max_concurrency=excluded.max_concurrency""",
                    (profile.worker_id, profile.owner_id, profile.provider_id,
                     json.dumps(profile.roles), json.dumps(profile.model_ids), int(profile.enabled),
                     int(profile.provider_ready), profile.status_observed_at, profile.max_concurrency),
                )
        except sqlite3.IntegrityError as error:
            raise StoreError(f"could not register worker {profile.worker_id}: owner must be registered first") from error
        except sqlite3.Error as error:
            raise StoreError(f"could not register worker {profile.worker_id}: {error}") from error

    def enqueue(self, task: ScheduledTask) -> None:
        values = (
            task.task_id, task.role, task.model_id, task.repo_head, task.branch,
            task.payload_json, task.estimated_gpu_seconds, task.priority, task.created_at,
        )
        try:
            with self._transaction() as db:
                previous = db.execute(
                    "SELECT task_id, role, model_id, repo_head, branch, payload_json, estimated_gpu_seconds, priority, created_at FROM tasks WHERE task_id = ?",
                    (task.task_id,),
                ).fetchone()
                if previous is not None:
                    if tuple(previous) != values:
                        raise StoreError(f"task {task.task_id} already exists with different content")
                    return
                db.execute(
                    """INSERT INTO tasks (
                    task_id, role, model_id, repo_head, branch, payload_json,
                    estimated_gpu_seconds, priority, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", values,
                )
        except sqlite3.Error as error:
            raise StoreError(f"could not enqueue task {task.task_id}: {error}") from error

    def get_worker(self, worker_id: str) -> WorkerProfile | None:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            row = self._connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            return self._worker_from_row(row)

    def get_owner_policy(self, owner_id: str) -> OwnerPolicy | None:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            row = self._connection.execute("SELECT * FROM owners WHERE owner_id = ?", (owner_id,)).fetchone()
            return self._owner_from_row(row)

    def get_task_state(self, task_id: str) -> str:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            row = self._connection.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise StoreError(f"task {task_id} does not exist")
            return row["state"]

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "task_id": row["task_id"], "role": row["role"], "model_id": row["model_id"],
            "repo_head": row["repo_head"], "branch": row["branch"],
            "payload_json": row["payload_json"], "estimated_gpu_seconds": row["estimated_gpu_seconds"],
            "priority": row["priority"], "created_at": row["created_at"],
            "state": row["state"], "attempts": row["attempts"], "last_error": row["last_error"],
        }

    def get_task(self, task_id: str) -> dict[str, object]:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise StoreError(f"task {task_id} does not exist")
        return self._task_from_row(row)

    def list_tasks(self) -> list[dict[str, object]]:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            rows = self._connection.execute(
                "SELECT * FROM tasks ORDER BY created_at, task_id"
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    @staticmethod
    def _validate_time(value: float, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite timestamp")

    @staticmethod
    def _validate_positive_seconds(value: int, name: str) -> None:
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _charge_quota(db: sqlite3.Connection, owner_id: str, seconds: int) -> None:
        db.execute(
            """UPDATE owners SET quota_remaining_seconds =
            CASE WHEN quota_remaining_seconds IS NULL THEN NULL
                 WHEN quota_remaining_seconds <= ? THEN 0
                 ELSE quota_remaining_seconds - ? END
            WHERE owner_id = ?""",
            (seconds, seconds, owner_id),
        )

    def _expire_due_leases(self, db: sqlite3.Connection, now: float) -> None:
        expired = db.execute(
            "SELECT * FROM leases WHERE status='ACTIVE' AND expires_at<=? ORDER BY lease_id", (now,)
        ).fetchall()
        for row in expired:
            self._charge_quota(db, row["owner_id"], row["reserved_gpu_seconds"])
            db.execute(
                "UPDATE leases SET status='EXPIRED', actual_gpu_seconds=reserved_gpu_seconds, error='lease expired' WHERE lease_id=?",
                (row["lease_id"],),
            )
            db.execute(
                "UPDATE tasks SET state='QUEUED', last_error='lease expired' WHERE task_id=? AND state='LEASED'",
                (row["task_id"],),
            )

    def claim_next(self, worker_id: str, now: float, lease_seconds: int,
                   max_observation_age_seconds: int) -> TaskLease | None:
        self._validate_time(now, "now")
        self._validate_positive_seconds(lease_seconds, "lease_seconds")
        self._validate_positive_seconds(max_observation_age_seconds, "max_observation_age_seconds")
        with self._transaction() as db:
            self._expire_due_leases(db, now)
            worker_row = db.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
            if worker_row is None:
                return None
            worker = self._worker_from_row(worker_row)
            owner_row = db.execute("SELECT * FROM owners WHERE owner_id=?", (worker.owner_id,)).fetchone()
            owner = self._owner_from_row(owner_row)
            if owner is None:
                return None
            active_worker = db.execute(
                "SELECT COUNT(*) FROM leases WHERE worker_id=? AND status='ACTIVE' AND expires_at>?",
                (worker_id, now),
            ).fetchone()[0]
            if active_worker >= worker.max_concurrency:
                return None
            active_owner = db.execute(
                "SELECT COUNT(*) FROM leases WHERE owner_id=? AND status='ACTIVE' AND expires_at>?",
                (worker.owner_id, now),
            ).fetchone()[0]
            if active_owner >= owner.max_concurrent_sessions:
                return None
            reserved = db.execute(
                "SELECT COALESCE(SUM(reserved_gpu_seconds),0) FROM leases WHERE owner_id=? AND status='ACTIVE'",
                (worker.owner_id,),
            ).fetchone()[0]
            tasks = db.execute(
                "SELECT * FROM tasks WHERE state='QUEUED' ORDER BY priority DESC, created_at ASC, task_id ASC"
            ).fetchall()
            load = WorkerLoad(
                active_sessions=active_worker,
                owner_active_sessions=active_owner,
                owner_reserved_gpu_seconds=reserved,
            )
            task = None
            for item in tasks:
                decision = select_worker(
                    RoutingRequest(
                        role=item["role"], model_id=item["model_id"],
                        estimated_gpu_seconds=item["estimated_gpu_seconds"],
                    ),
                    (worker,),
                    {owner.owner_id: owner},
                    {worker.worker_id: load},
                    now=now,
                    max_observation_age_seconds=max_observation_age_seconds,
                )
                if decision is not None:
                    task = item
                    break
            if task is None:
                return None
            lease = TaskLease(
                lease_id=uuid.uuid4().hex, task_id=task["task_id"], worker_id=worker.worker_id,
                owner_id=worker.owner_id, expires_at=float(now + lease_seconds),
            )
            db.execute(
                "INSERT INTO leases (lease_id,task_id,worker_id,owner_id,expires_at,status,reserved_gpu_seconds,created_at) VALUES (?,?,?,?,?,'ACTIVE',?,?)",
                (lease.lease_id, lease.task_id, lease.worker_id, lease.owner_id, lease.expires_at,
                 task["estimated_gpu_seconds"], now),
            )
            db.execute("UPDATE tasks SET state='LEASED', attempts=attempts+1, last_error=NULL WHERE task_id=?", (task["task_id"],))
            return lease

    @staticmethod
    def _route_reservation_from_row(
        db: sqlite3.Connection, row: sqlite3.Row,
    ) -> RouteReservation:
        if not row["model_id"]:
            raise LeaseError("existing lease has no persisted model selection")
        worker_row = db.execute(
            "SELECT provider_id FROM workers WHERE worker_id=?", (row["worker_id"],)
        ).fetchone()
        if worker_row is None:
            raise LeaseError("existing lease references a missing worker")
        return RouteReservation(
            lease=TaskLease(
                lease_id=row["lease_id"], task_id=row["task_id"],
                worker_id=row["worker_id"], owner_id=row["owner_id"],
                expires_at=float(row["expires_at"]),
            ),
            provider_id=worker_row["provider_id"],
            model_id=row["model_id"],
            reserved_gpu_seconds=int(row["reserved_gpu_seconds"]),
            reason=row["route_reason"] or "recovered existing route reservation",
        )

    def get_route_reservation(self, idempotency_key: str, now: float) -> RouteReservation | None:
        """Read a live reservation by its durable operation key without side effects."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        self._validate_time(now, "now")
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            row = self._connection.execute(
                "SELECT * FROM leases WHERE idempotency_key=?", (idempotency_key.strip(),)
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "ACTIVE" or row["expires_at"] <= now:
                raise LeaseError("idempotent route reservation is no longer active")
            return self._route_reservation_from_row(self._connection, row)

    def reserve_for_task(
        self,
        task_id: str,
        idempotency_key: str,
        now: float,
        lease_seconds: int,
        max_observation_age_seconds: int,
    ) -> RouteReservation | None:
        """Select and reserve one worker/model atomically for a Temporal task.

        Repeated calls with the same key recover the original persisted route.
        A task already leased by another workflow is recovered rather than
        assigned a second concurrent writer.
        """
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        self._validate_time(now, "now")
        self._validate_positive_seconds(lease_seconds, "lease_seconds")
        self._validate_positive_seconds(max_observation_age_seconds, "max_observation_age_seconds")
        task_id, idempotency_key = task_id.strip(), idempotency_key.strip()

        with self._transaction() as db:
            self._expire_due_leases(db, now)
            task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                return None

            by_key = db.execute(
                "SELECT * FROM leases WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if by_key is not None:
                if by_key["task_id"] != task_id:
                    raise LeaseError("idempotency key is already assigned to a different task")
                if by_key["status"] != "ACTIVE" or by_key["expires_at"] <= now:
                    raise LeaseError("idempotent route reservation is no longer active")
                return self._route_reservation_from_row(db, by_key)

            existing = db.execute(
                "SELECT * FROM leases WHERE task_id=? AND status='ACTIVE' AND expires_at>?",
                (task_id, now),
            ).fetchone()
            if existing is not None:
                return self._route_reservation_from_row(db, existing)
            if task["state"] != TaskState.QUEUED.value:
                return None

            worker_rows = db.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
            workers: list[WorkerProfile] = []
            owners: dict[str, OwnerPolicy] = {}
            loads: dict[str, WorkerLoad] = {}
            owner_loads: dict[str, tuple[int, int]] = {}
            for worker_row in worker_rows:
                worker = self._worker_from_row(worker_row)
                workers.append(worker)
                owner_row = db.execute(
                    "SELECT * FROM owners WHERE owner_id=?", (worker.owner_id,)
                ).fetchone()
                owner = self._owner_from_row(owner_row)
                if owner is None:
                    continue
                owners[owner.owner_id] = owner
                if owner.owner_id not in owner_loads:
                    active_owner = db.execute(
                        "SELECT COUNT(*) FROM leases WHERE owner_id=? AND status='ACTIVE' AND expires_at>?",
                        (owner.owner_id, now),
                    ).fetchone()[0]
                    reserved = db.execute(
                        "SELECT COALESCE(SUM(reserved_gpu_seconds),0) FROM leases "
                        "WHERE owner_id=? AND status='ACTIVE' AND expires_at>?",
                        (owner.owner_id, now),
                    ).fetchone()[0]
                    owner_loads[owner.owner_id] = (active_owner, reserved)
                owner_active, owner_reserved = owner_loads[owner.owner_id]
                active_worker = db.execute(
                    "SELECT COUNT(*) FROM leases WHERE worker_id=? AND status='ACTIVE' AND expires_at>?",
                    (worker.worker_id, now),
                ).fetchone()[0]
                loads[worker.worker_id] = WorkerLoad(
                    active_sessions=active_worker,
                    owner_active_sessions=owner_active,
                    owner_reserved_gpu_seconds=owner_reserved,
                )

            decision = select_worker(
                RoutingRequest(
                    role=task["role"], model_id=task["model_id"],
                    estimated_gpu_seconds=task["estimated_gpu_seconds"],
                ),
                workers, owners, loads, now=now,
                max_observation_age_seconds=max_observation_age_seconds,
            )
            if decision is None:
                return None

            lease = TaskLease(
                lease_id=uuid.uuid4().hex,
                task_id=task_id,
                worker_id=decision.worker_id,
                owner_id=decision.owner_id,
                expires_at=float(now + lease_seconds),
            )
            db.execute(
                "INSERT INTO leases "
                "(lease_id,task_id,worker_id,owner_id,expires_at,status,reserved_gpu_seconds,created_at,"
                "idempotency_key,model_id,route_reason) "
                "VALUES (?,?,?,?,?,'ACTIVE',?,?,?,?,?)",
                (lease.lease_id, task_id, lease.worker_id, lease.owner_id, lease.expires_at,
                 task["estimated_gpu_seconds"], now, idempotency_key, decision.model_id,
                 decision.reason),
            )
            db.execute(
                "UPDATE tasks SET state='LEASED',attempts=attempts+1,last_error=NULL WHERE task_id=?",
                (task_id,),
            )
            return RouteReservation(
                lease=lease, provider_id=decision.provider_id,
                model_id=decision.model_id,
                reserved_gpu_seconds=task["estimated_gpu_seconds"],
                reason=decision.reason,
            )

    def heartbeat(self, lease_id: str, now: float, lease_seconds: int) -> TaskLease:
        self._validate_time(now, "now")
        self._validate_positive_seconds(lease_seconds, "lease_seconds")
        expired = False
        result = None
        with self._transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None or row["status"] != "ACTIVE":
                raise LeaseError(f"lease {lease_id} does not exist or is not active")
            if row["expires_at"] <= now:
                self._expire_due_leases(db, now)
                expired = True
            else:
                expiry = float(now + lease_seconds)
                db.execute("UPDATE leases SET expires_at=? WHERE lease_id=?", (expiry, lease_id))
                result = TaskLease(lease_id, row["task_id"], row["worker_id"], row["owner_id"], expiry)
        if expired:
            raise LeaseError(f"lease {lease_id} expired")
        return result

    def cancel(self, task_id: str, now: float) -> None:
        self._validate_time(now, "now")
        with self._transaction() as db:
            task = db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise StoreError(f"task {task_id} does not exist")
            if task["state"] == TaskState.CANCELLED.value:
                return
            if task["state"] in (TaskState.COMPLETED.value, TaskState.FAILED.value):
                raise StoreError(f"task {task_id} is already terminal")
            lease = db.execute("SELECT * FROM leases WHERE task_id=? AND status='ACTIVE'", (task_id,)).fetchone()
            if lease is not None:
                self._charge_quota(db, lease["owner_id"], lease["reserved_gpu_seconds"])
                db.execute(
                    "UPDATE leases SET status='CANCELLED', actual_gpu_seconds=reserved_gpu_seconds, error='task cancelled' WHERE lease_id=?",
                    (lease["lease_id"],),
                )
            db.execute("UPDATE tasks SET state='CANCELLED', last_error='task cancelled' WHERE task_id=?", (task_id,))

    def complete(self, lease_id: str, result: WorkerResult, now: float) -> None:
        self._validate_time(now, "now")
        serialized = json.dumps(asdict(result), sort_keys=True, separators=(",", ":"))
        expired = False
        with self._transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                raise LeaseError(f"lease {lease_id} does not exist")
            if row["status"] == "COMPLETED":
                if row["result_json"] == serialized:
                    return
                raise LeaseError(f"lease {lease_id} already completed with a different result")
            if row["status"] != "ACTIVE":
                raise LeaseError(f"lease {lease_id} is not active")
            if row["expires_at"] <= now:
                self._expire_due_leases(db, now)
                expired = True
            else:
                task = db.execute("SELECT repo_head FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
                if result.lease_id != lease_id or result.task_id != row["task_id"] or result.worker_id != row["worker_id"]:
                    raise LeaseError("worker result identity does not match the active lease")
                if result.base_repo_head != task["repo_head"]:
                    raise LeaseError("worker result repository HEAD does not match the task")
                db.execute(
                    "UPDATE leases SET status='COMPLETED', actual_gpu_seconds=?, result_json=? WHERE lease_id=?",
                    (result.usage_gpu_seconds, serialized, lease_id),
                )
                db.execute("UPDATE tasks SET state='COMPLETED' WHERE task_id=?", (row["task_id"],))
                self._charge_quota(db, row["owner_id"], result.usage_gpu_seconds)
        if expired:
            raise LeaseError(f"lease {lease_id} expired")

    def fail(self, lease_id: str, error: str, retryable: bool, now: float) -> None:
        self._validate_time(now, "now")
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error must be a non-empty string")
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        expired = False
        with self._transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                raise LeaseError(f"lease {lease_id} does not exist")
            if row["status"] == "FAILED":
                return
            if row["status"] != "ACTIVE":
                raise LeaseError(f"lease {lease_id} is not active")
            if row["expires_at"] <= now:
                self._expire_due_leases(db, now)
                expired = True
            else:
                # Without measured usage, charge the full reservation to avoid undercounting.
                self._charge_quota(db, row["owner_id"], row["reserved_gpu_seconds"])
                db.execute(
                    "UPDATE leases SET status='FAILED', actual_gpu_seconds=reserved_gpu_seconds, error=?, retryable=? WHERE lease_id=?",
                    (error, int(retryable), lease_id),
                )
                state = TaskState.QUEUED.value if retryable else TaskState.FAILED.value
                db.execute("UPDATE tasks SET state=?, last_error=? WHERE task_id=?", (state, error, row["task_id"]))
        if expired:
            raise LeaseError(f"lease {lease_id} expired")

