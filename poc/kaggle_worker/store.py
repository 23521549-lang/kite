"""SQLite persistence for scheduler owners, workers, queued tasks, and leases."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import uuid
from typing import Iterator, Protocol

from .protocol import OwnerPolicy, ScheduledTask, TaskLease, TaskState, WorkerProfile, WorkerResult
from .routing import ModelPolicy, RoutingRequest, WorkerLoad, select_worker


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
    def set_last_apply_error(self, task_id: str, error: dict[str, object] | None) -> None: ...
    def list_tasks(self) -> list[dict[str, object]]: ...


class SQLiteSchedulerStore:
    """Thread-safe SQLite store with one transaction per state transition."""

    def __init__(self, path: Path, *, model_policy: ModelPolicy | None = None) -> None:
        self.path = Path(path)
        self.model_policy = model_policy
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
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    kite_session_id TEXT PRIMARY KEY,
                    opencode_session_id TEXT,
                    title TEXT,
                    environment_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'review', 'secretary')),
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER,
                    updated_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS chat_session_moves (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    from_env TEXT NOT NULL,
                    to_env TEXT NOT NULL,
                    moved_at INTEGER NOT NULL,
                    trigger TEXT NOT NULL CHECK (trigger IN ('picker', 'task')),
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS session_copies (
                    session_id TEXT PRIMARY KEY,
                    env_id TEXT NOT NULL,
                    copy_path TEXT NOT NULL,
                    base_head TEXT,
                    created_at INTEGER,
                    state TEXT NOT NULL DEFAULT 'active'
                );
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
            # Dispatcher columns (task type, capabilities, loaded/cached models).
            for table, columns in {
                "workers": {
                    "capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
                    "loaded_model": "TEXT",
                    "cached_models_json": "TEXT NOT NULL DEFAULT '[]'",
                },
                "tasks": {
                    "task_type": "TEXT NOT NULL DEFAULT 'coding'",
                    "required_capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
                    # Gói 2: trace nguồn gốc của task (NULL = "unknown origin", không đoán ngược).
                    "chat_session_id": "TEXT",
                    "environment_id": "TEXT",
                    "main_repo": "TEXT",
                    # Gói 2: lỗi áp ghi bền vững vào bản ghi task (nút chạy lại đọc từ đây).
                    "last_apply_error": "TEXT",
                },
            }.items():
                existing = {
                    row["name"]
                    for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for name, column_type in columns.items():
                    if name not in existing:
                        self._connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"
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
            capabilities=tuple(json.loads(row["capabilities_json"] or "[]")),
            loaded_model=row["loaded_model"],
            cached_models=tuple(json.loads(row["cached_models_json"] or "[]")),
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
                    """INSERT INTO workers (
                    worker_id, owner_id, provider_id, roles_json, model_ids_json, enabled,
                    provider_ready, status_observed_at, max_concurrency,
                    capabilities_json, loaded_model, cached_models_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                    provider_id=excluded.provider_id, roles_json=excluded.roles_json,
                    model_ids_json=excluded.model_ids_json, enabled=excluded.enabled,
                    provider_ready=excluded.provider_ready, status_observed_at=excluded.status_observed_at,
                    max_concurrency=excluded.max_concurrency,
                    capabilities_json=excluded.capabilities_json, loaded_model=excluded.loaded_model,
                    cached_models_json=excluded.cached_models_json""",
                    (profile.worker_id, profile.owner_id, profile.provider_id,
                     json.dumps(profile.roles), json.dumps(profile.model_ids), int(profile.enabled),
                     int(profile.provider_ready), profile.status_observed_at, profile.max_concurrency,
                     json.dumps(profile.capabilities), profile.loaded_model,
                     json.dumps(profile.cached_models)),
                )
        except sqlite3.IntegrityError as error:
            raise StoreError(f"could not register worker {profile.worker_id}: owner must be registered first") from error
        except sqlite3.Error as error:
            raise StoreError(f"could not register worker {profile.worker_id}: {error}") from error

    def enqueue(self, task: ScheduledTask) -> None:
        values = (
            task.task_id, task.role, task.model_id, task.repo_head, task.branch,
            task.payload_json, task.estimated_gpu_seconds, task.priority, task.created_at,
            task.task_type, json.dumps(task.required_capabilities),
            task.chat_session_id, task.environment_id, task.main_repo,
        )
        try:
            with self._transaction() as db:
                previous = db.execute(
                    "SELECT task_id, role, model_id, repo_head, branch, payload_json, estimated_gpu_seconds, "
                    "priority, created_at, task_type, required_capabilities_json, "
                    "chat_session_id, environment_id, main_repo FROM tasks WHERE task_id = ?",
                    (task.task_id,),
                ).fetchone()
                if previous is not None:
                    if tuple(previous) != values:
                        raise StoreError(f"task {task.task_id} already exists with different content")
                    return
                db.execute(
                    """INSERT INTO tasks (
                    task_id, role, model_id, repo_head, branch, payload_json,
                    estimated_gpu_seconds, priority, created_at, task_type, required_capabilities_json,
                    chat_session_id, environment_id, main_repo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values,
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
        # "main_repo" belongs to Gói 2 (not created here): read it only when the column exists,
        # so rerun_task can use it once that migration lands without breaking older databases.
        keys = row.keys()
        return {
            "task_id": row["task_id"], "role": row["role"], "model_id": row["model_id"],
            "repo_head": row["repo_head"], "branch": row["branch"],
            "payload_json": row["payload_json"], "estimated_gpu_seconds": row["estimated_gpu_seconds"],
            "priority": row["priority"], "created_at": row["created_at"],
            "state": row["state"], "attempts": row["attempts"], "last_error": row["last_error"],
            "task_type": row["task_type"],
            "required_capabilities": json.loads(row["required_capabilities_json"] or "[]"),
            "chat_session_id": row["chat_session_id"],
            "environment_id": row["environment_id"],
            # Raw JSON string (or None); callers that need the object parse it themselves.
            "last_apply_error": row["last_apply_error"],
            "main_repo": row["main_repo"] if "main_repo" in keys else None,
        }

    @staticmethod
    def _norm_repo_url(value: str) -> str:
        """One spelling for a repo URL: ``git@host:owner/repo.git`` and
        ``https://host/owner/repo`` compare equal; a trailing slash or ``.git`` is noise."""
        value = value.strip().rstrip("/")
        if value.endswith(".git"):
            value = value[:-4]
        if value.startswith("git@"):
            host, _, path = value[4:].partition(":")
            if host and path:
                return f"https://{host}/{path.strip('/')}"
        return value

    def environment_for_repo(self, repo_path_or_url: str) -> str | None:
        """The environment id a repository belongs to, or None when unknown.

        An environment IS a row of the web store's ``repos`` table
        (``<state_dir>/web.sqlite3`` — the scheduler database's sibling), matched by
        resolved local path, or by remote origin URL (a local path is also checked
        against its ``remote.origin.url``). Never raises: no web database (e.g. a
        Postgres web store), a missing table, or no match all mean None, and the
        caller falls back to its own lookup."""
        if not isinstance(repo_path_or_url, str) or not repo_path_or_url.strip():
            return None
        value = repo_path_or_url.strip()
        web_db = self.path.parent / "web.sqlite3"
        if not web_db.is_file():
            return None
        is_url = "://" in value or value.startswith("git@")
        repo_path: Path | None = None
        origin_url = ""
        if not is_url:
            try:
                repo_path = Path(value).expanduser().resolve()
            except (OSError, ValueError):
                return None
            try:
                origin = subprocess.run(
                    ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
                    capture_output=True, text=True, timeout=10)
                if origin.returncode == 0:
                    origin_url = origin.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                origin_url = ""
        wanted_url = self._norm_repo_url(value) if is_url else ""
        if origin_url:
            wanted_url = self._norm_repo_url(origin_url)
        try:
            db = sqlite3.connect(f"file:{web_db}?mode=ro", uri=True, timeout=5.0)
            try:
                db.row_factory = sqlite3.Row
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "repos" not in tables:
                    return None
                for row in db.execute("SELECT id, path, url FROM repos").fetchall():
                    if repo_path is not None and row["path"]:
                        try:
                            if Path(row["path"]).expanduser().resolve() == repo_path:
                                return str(row["id"])
                        except (OSError, ValueError):
                            pass
                    if wanted_url and row["url"] and self._norm_repo_url(row["url"]) == wanted_url:
                        return str(row["id"])
            finally:
                db.close()
        except sqlite3.Error:
            return None
        return None

    def get_task(self, task_id: str) -> dict[str, object]:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise StoreError(f"task {task_id} does not exist")
        return self._task_from_row(row)

    def set_last_apply_error(self, task_id: str, error: dict[str, object] | None) -> None:
        """Persist a structured apply failure (JSON) on a task row, or clear it (None) on success."""
        try:
            with self._transaction() as db:
                cursor = db.execute(
                    "UPDATE tasks SET last_apply_error=? WHERE task_id=?",
                    (json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else None,
                     task_id),
                )
                if cursor.rowcount == 0:
                    raise StoreError(f"task {task_id} does not exist")
        except sqlite3.Error as exc:
            raise StoreError(f"could not record apply error for task {task_id}: {exc}") from exc

    def list_tasks(self) -> list[dict[str, object]]:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            rows = self._connection.execute(
                "SELECT * FROM tasks ORDER BY created_at, task_id"
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    # ------------------------------------------------------------------ chat sessions (Gói 2)
    # A registry row is never deleted: archive/delete only change `status`, so the
    # Environment -> Session -> Task trace stays readable after a session is gone.
    SESSION_KINDS = ("chat", "review", "secretary")
    SESSION_STATUSES = ("active", "archived", "deleted")
    MOVE_TRIGGERS = ("picker", "task")

    @staticmethod
    def _chat_session_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "kite_session_id": row["kite_session_id"],
            "opencode_session_id": row["opencode_session_id"],
            "title": row["title"],
            "environment_id": row["environment_id"],
            "kind": row["kind"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _require_session_id(value: str, name: str = "kite_session_id") -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip()

    def create_chat_session(self, kite_session_id: str, opencode_session_id: str | None,
                            environment_id: str, title: str | None = None,
                            kind: str = "chat") -> dict[str, object]:
        kite_session_id = self._require_session_id(kite_session_id)
        environment_id = self._require_session_id(environment_id, "environment_id")
        if kind not in self.SESSION_KINDS:
            raise ValueError(f"kind must be one of {', '.join(self.SESSION_KINDS)}")
        if title is not None and not isinstance(title, str):
            raise ValueError("title must be a string or None")
        now = int(time.time())
        try:
            with self._transaction() as db:
                db.execute(
                    """INSERT INTO chat_sessions (
                    kite_session_id, opencode_session_id, title, environment_id, kind,
                    status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                    (kite_session_id, opencode_session_id, title, environment_id, kind, now, now),
                )
        except sqlite3.IntegrityError as error:
            raise StoreError(f"chat session {kite_session_id} already exists") from error
        except sqlite3.Error as error:
            raise StoreError(f"could not create chat session {kite_session_id}: {error}") from error
        session = self.get_chat_session(kite_session_id)
        assert session is not None
        return session

    def get_chat_session(self, kite_session_id: str) -> dict[str, object] | None:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            row = self._connection.execute(
                "SELECT * FROM chat_sessions WHERE kite_session_id = ?",
                (self._require_session_id(kite_session_id),),
            ).fetchone()
        return self._chat_session_from_row(row) if row is not None else None

    def list_chat_sessions(self, *, status: str | None = None,
                           environment_id: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            query = "SELECT * FROM chat_sessions"
            clauses, params = [], []
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            if environment_id is not None:
                clauses.append("environment_id = ?")
                params.append(environment_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            rows = self._connection.execute(query + " ORDER BY created_at, kite_session_id",
                                            params).fetchall()
        return [self._chat_session_from_row(row) for row in rows]

    def set_chat_session_status(self, kite_session_id: str, status: str) -> dict[str, object]:
        if status not in self.SESSION_STATUSES:
            raise ValueError(f"status must be one of {', '.join(self.SESSION_STATUSES)}")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM chat_sessions WHERE kite_session_id = ?",
                             (self._require_session_id(kite_session_id),)).fetchone()
            if row is None:
                raise StoreError(f"chat session {kite_session_id} does not exist")
            db.execute("UPDATE chat_sessions SET status = ?, updated_at = ? WHERE kite_session_id = ?",
                       (status, int(time.time()), kite_session_id))
        session = self.get_chat_session(kite_session_id)
        assert session is not None
        return session

    def set_session_environment(self, kite_session_id: str, environment_id: str) -> dict[str, object]:
        environment_id = self._require_session_id(environment_id, "environment_id")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM chat_sessions WHERE kite_session_id = ?",
                             (self._require_session_id(kite_session_id),)).fetchone()
            if row is None:
                raise StoreError(f"chat session {kite_session_id} does not exist")
            db.execute("UPDATE chat_sessions SET environment_id = ?, updated_at = ? WHERE kite_session_id = ?",
                       (environment_id, int(time.time()), kite_session_id))
        session = self.get_chat_session(kite_session_id)
        assert session is not None
        return session

    def record_session_move(self, kite_session_id: str, from_env: str, to_env: str,
                            trigger: str, note: str | None = None) -> dict[str, object]:
        if trigger not in self.MOVE_TRIGGERS:
            raise ValueError(f"trigger must be one of {', '.join(self.MOVE_TRIGGERS)}")
        from_env = self._require_session_id(from_env, "from_env")
        to_env = self._require_session_id(to_env, "to_env")
        now = int(time.time())
        with self._transaction() as db:
            row = db.execute("SELECT * FROM chat_sessions WHERE kite_session_id = ?",
                             (self._require_session_id(kite_session_id),)).fetchone()
            if row is None:
                raise StoreError(f"chat session {kite_session_id} does not exist")
            cursor = db.execute(
                "INSERT INTO chat_session_moves (session_id, from_env, to_env, moved_at, trigger, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kite_session_id, from_env, to_env, now, trigger, note),
            )
            db.execute("UPDATE chat_sessions SET environment_id = ?, updated_at = ? WHERE kite_session_id = ?",
                       (to_env, now, kite_session_id))
            move_id = cursor.lastrowid
        return {"id": move_id, "session_id": kite_session_id, "from_env": from_env,
                "to_env": to_env, "moved_at": now, "trigger": trigger, "note": note}

    def list_session_moves(self, kite_session_id: str) -> list[dict[str, object]]:
        with self._lock:
            if self._connection is None:
                raise StoreError("scheduler store is closed")
            rows = self._connection.execute(
                "SELECT * FROM chat_session_moves WHERE session_id = ? ORDER BY moved_at, id",
                (self._require_session_id(kite_session_id),),
            ).fetchall()
        return [{"id": row["id"], "session_id": row["session_id"], "from_env": row["from_env"],
                 "to_env": row["to_env"], "moved_at": row["moved_at"],
                 "trigger": row["trigger"], "note": row["note"]} for row in rows]

    @staticmethod
    def _routing_request(task: sqlite3.Row) -> RoutingRequest:
        return RoutingRequest(
            role=task["role"], model_id=task["model_id"],
            estimated_gpu_seconds=task["estimated_gpu_seconds"],
            task_type=task["task_type"] or "coding",
            required_capabilities=tuple(json.loads(task["required_capabilities_json"] or "[]")),
        )

    def update_worker_status(
        self,
        worker_id: str,
        *,
        provider_ready: bool,
        observed_at: float,
        loaded_model: str | None = None,
        cached_models: tuple[str, ...] = (),
    ) -> WorkerProfile:
        """Record a worker heartbeat without touching owner, roles, models, or enablement."""
        self._validate_time(observed_at, "observed_at")
        if type(provider_ready) is not bool:
            raise ValueError("provider_ready must be a boolean")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
            if row is None:
                raise StoreError(f"worker {worker_id} is not registered")
            current = self._worker_from_row(row)
            updated = WorkerProfile(
                worker_id=current.worker_id, owner_id=current.owner_id,
                provider_id=current.provider_id, roles=current.roles, model_ids=current.model_ids,
                enabled=current.enabled, provider_ready=provider_ready,
                status_observed_at=float(observed_at), max_concurrency=current.max_concurrency,
                capabilities=current.capabilities, loaded_model=loaded_model,
                cached_models=tuple(cached_models),
            )
            db.execute(
                "UPDATE workers SET provider_ready=?, status_observed_at=?, loaded_model=?, "
                "cached_models_json=? WHERE worker_id=?",
                (int(provider_ready), float(observed_at), loaded_model,
                 json.dumps(list(cached_models)), worker_id),
            )
            # The owner's quota is Kite's own ledger (charged on complete/fail/expire/cancel), so a
            # live worker heartbeat also confirms it is current. Without this, routing treats the
            # quota as stale five minutes after `provider sync` and never routes a task again.
            db.execute("UPDATE owners SET quota_observed_at=? WHERE owner_id=?",
                       (float(observed_at), current.owner_id))
        return updated

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
                    self._routing_request(item),
                    (worker,),
                    {owner.owner_id: owner},
                    {worker.worker_id: load},
                    now=now,
                    max_observation_age_seconds=max_observation_age_seconds,
                    model_policy=self.model_policy,
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
                self._routing_request(task),
                workers, owners, loads, now=now,
                max_observation_age_seconds=max_observation_age_seconds,
                model_policy=self.model_policy,
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
                # Kaggle bills session wall time, not reported tokens. A worker that reports
                # no usage (OpenCode reports 0) is charged at least the lease's elapsed time.
                elapsed = max(0, math.ceil(now - float(row["created_at"])))
                charged = max(result.usage_gpu_seconds, elapsed)
                db.execute(
                    "UPDATE leases SET status='COMPLETED', actual_gpu_seconds=?, result_json=? WHERE lease_id=?",
                    (charged, serialized, lease_id),
                )
                db.execute("UPDATE tasks SET state='COMPLETED' WHERE task_id=?", (row["task_id"],))
                self._charge_quota(db, row["owner_id"], charged)
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