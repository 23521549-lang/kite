"""Scheduled plans and the polling scheduler engine (Gói 4).

Kite has no timer before this module: Temporal only runs tasks, it has no
Schedule (``create_schedule``/``ScheduleSpec`` do not appear anywhere in the
codebase). The package lead decided the engine is a periodic scan loop inside
the web process, NOT the Temporal Schedule API, so plans (including the
secretary's 00:00 report trigger) still fire when Temporal is down; a trigger
only needs the SQLite store plus the OpenCode HTTP API.

Time rules (contract): schedule times and display use Asia/Bangkok; the store
keeps epoch UTC. Tick 30 seconds, schedules are minute-accurate.

A ``once`` plan means "today at HH:MM Bangkok". There is no separate date
column, so a one-time plan created after today's HH:MM has already missed its
only run: it is created ``expired`` with no next run instead of silently
slipping to tomorrow.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import inspect
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
import urllib.error
import urllib.request
from typing import Callable, Iterator, Protocol
from zoneinfo import ZoneInfo
import uuid

from .journal import Journal
from .store import StoreError

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

#: Scan interval of the engine loop (contract: 30 s tick).
TICK_SECONDS = 30.0
#: A schedule counts as still running when its previous run started less
#: than this long ago (contract: 15 minutes — the lead agent has not yet had
#: time to split the plan into tasks).
OVERLAP_GRACE_SECONDS = 15 * 60

SCHEDULE_KINDS = ("once", "daily", "weekly")
SCHEDULE_STATUSES = ("enabled", "disabled", "expired")
#: Weekday numbers for ``weekly`` schedules: 1 = Monday .. 7 = Sunday (ISO).
WEEKDAYS = frozenset(range(1, 8))

logger = logging.getLogger("kite.schedules")


# ------------------------------------------------------------------ time math

def parse_run_at(run_at: str) -> tuple[int, int]:
    """Validate ``HH:MM`` (24-hour clock) and return ``(hour, minute)``."""
    if not isinstance(run_at, str):
        raise ValueError("run_at must be an HH:MM string")
    parts = run_at.split(":")
    if (len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2
            or not all(part.isdigit() for part in parts)):
        raise ValueError(f"run_at must be HH:MM, got {run_at!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"run_at must be HH:MM, got {run_at!r}")
    return hour, minute


def compute_next_run(
    kind: str,
    run_at: str,
    weekdays: tuple[int, ...],
    now: float,
    *,
    tz: ZoneInfo = BANGKOK_TZ,
) -> float | None:
    """Next run instant (epoch UTC) strictly after ``now``, or None.

    ``once`` is today at HH:MM Bangkok; when that instant has passed there is
    no next run (the caller marks the schedule ``expired``). ``daily`` is the
    next occurrence of HH:MM; ``weekly`` is the next occurrence on one of
    ``weekdays``. All arithmetic is done in Bangkok time, so a daily run
    crosses midnight Bangkok correctly.
    """
    if kind not in SCHEDULE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(SCHEDULE_KINDS)}, got {kind!r}")
    hour, minute = parse_run_at(run_at)
    if kind == "weekly":
        if not weekdays:
            raise ValueError("weekly schedules need at least one weekday")
        if any(day not in WEEKDAYS for day in weekdays):
            raise ValueError("weekdays must be ISO numbers 1 (Monday) .. 7 (Sunday)")
    now_dt = datetime.fromtimestamp(now, tz)

    def candidate_on(day: datetime) -> datetime:
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if kind == "once":
        candidate = candidate_on(now_dt)
        return candidate.timestamp() if candidate > now_dt else None
    if kind == "daily":
        candidate = candidate_on(now_dt)
        if candidate <= now_dt:
            candidate += timedelta(days=1)
        return candidate.timestamp()
    wanted = set(weekdays)
    for offset in range(8):
        day = now_dt + timedelta(days=offset)
        if day.isoweekday() not in wanted:
            continue
        candidate = candidate_on(day)
        if candidate > now_dt:
            return candidate.timestamp()
    return None  # Unreachable: a weekday always recurs within 7 days.


def daily_at_bangkok(run_at: str) -> Callable[[datetime], datetime]:
    """Due function for a builtin job that runs once a day at ``run_at``.

    Given a Bangkok-aware ``now``, returns the next occurrence of ``run_at``
    strictly after ``now``. Used for the secretary's 00:00 daily report:
    ``register_builtin("secretary_daily_report", daily_at_bangkok("00:00"), func)``.
    """
    hour, minute = parse_run_at(run_at)

    def next_due(now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("daily_at_bangkok needs a timezone-aware datetime")
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    return next_due


# ------------------------------------------------------------------ store

@dataclass(frozen=True)
class Schedule:
    id: str
    plan_path: str
    kind: str
    run_at: str
    weekdays: tuple[int, ...] = ()
    next_run: float | None = None
    last_run: float | None = None
    last_run_task_ids: tuple[str, ...] = ()
    status: str = "enabled"
    created_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "plan_path": self.plan_path, "kind": self.kind,
            "run_at": self.run_at, "weekdays": list(self.weekdays),
            "next_run": self.next_run, "last_run": self.last_run,
            "last_run_task_ids": list(self.last_run_task_ids),
            "status": self.status, "created_at": self.created_at,
        }


_SCHEDULE_COLUMNS = {
    "id": "TEXT PRIMARY KEY",
    "plan_path": "TEXT NOT NULL",
    "kind": "TEXT NOT NULL",
    "run_at": "TEXT NOT NULL",
    "weekdays": "TEXT NOT NULL DEFAULT '[]'",
    "next_run": "REAL",
    "last_run": "REAL",
    "last_run_task_ids": "TEXT NOT NULL DEFAULT '[]'",
    "status": "TEXT NOT NULL DEFAULT 'enabled'",
    "created_at": "REAL NOT NULL",
}


class ScheduleStore:
    """The ``schedules`` table in ``scheduler.sqlite3`` (same file as tasks).

    This class owns only the ``schedules`` table; it never touches the task,
    worker or lease tables except for one read-only overlap check
    (``active_tasks_since``). It opens its own connection to the same file
    following the ``SQLiteSchedulerStore`` conventions (``BEGIN IMMEDIATE``,
    busy timeout, ``CREATE TABLE IF NOT EXISTS`` plus a PRAGMA/ALTER TABLE
    migration for pre-existing tables).
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, timeout=10.0, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 10000")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock:
            columns = ", ".join(f"{name} {decl}" for name, decl in _SCHEDULE_COLUMNS.items())
            self._connection.execute(f"CREATE TABLE IF NOT EXISTS schedules ({columns})")
            existing = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(schedules)").fetchall()
            }
            for name, decl in _SCHEDULE_COLUMNS.items():
                if name not in existing:
                    self._connection.execute(f"ALTER TABLE schedules ADD COLUMN {name} {decl}")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS schedules_by_next_run ON schedules(status, next_run)"
            )

    def __enter__(self) -> ScheduleStore:
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
                raise StoreError("schedule store is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @staticmethod
    def _schedule_from_row(row: sqlite3.Row) -> Schedule:
        return Schedule(
            id=row["id"], plan_path=row["plan_path"], kind=row["kind"],
            run_at=row["run_at"], weekdays=tuple(json.loads(row["weekdays"] or "[]")),
            next_run=row["next_run"], last_run=row["last_run"],
            last_run_task_ids=tuple(json.loads(row["last_run_task_ids"] or "[]")),
            status=row["status"], created_at=row["created_at"],
        )

    @staticmethod
    def _validate(
        *, plan_path: str, kind: str, run_at: str, weekdays: tuple[int, ...],
    ) -> None:
        if not isinstance(plan_path, str) or not plan_path.strip():
            raise ValueError("plan_path must be a non-empty string")
        if kind not in SCHEDULE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(SCHEDULE_KINDS)}, got {kind!r}")
        parse_run_at(run_at)
        if kind == "weekly":
            if not weekdays:
                raise ValueError("weekly schedules need at least one weekday")
            if any(day not in WEEKDAYS for day in weekdays):
                raise ValueError("weekdays must be ISO numbers 1 (Monday) .. 7 (Sunday)")
        elif weekdays:
            raise ValueError(f"weekdays only apply to weekly schedules, not {kind}")

    def create(
        self, *, plan_path: str, kind: str, run_at: str,
        weekdays: tuple[int, ...] = (), now: float | None = None,
    ) -> Schedule:
        """Create a schedule and compute its first run.

        A ``once`` plan whose time today already passed is created
        ``expired`` with ``next_run`` None — it missed its only run.
        """
        now = self.clock() if now is None else now
        weekdays = tuple(weekdays)
        self._validate(plan_path=plan_path, kind=kind, run_at=run_at, weekdays=weekdays)
        next_run = compute_next_run(kind, run_at, weekdays, now)
        status = "expired" if next_run is None else "enabled"
        schedule = Schedule(
            id=uuid.uuid4().hex, plan_path=plan_path.strip(), kind=kind, run_at=run_at,
            weekdays=weekdays, next_run=next_run, last_run=None,
            last_run_task_ids=(), status=status, created_at=now,
        )
        try:
            with self._transaction() as db:
                db.execute(
                    "INSERT INTO schedules (id, plan_path, kind, run_at, weekdays, next_run,"
                    " last_run, last_run_task_ids, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (schedule.id, schedule.plan_path, schedule.kind, schedule.run_at,
                     json.dumps(list(schedule.weekdays)), schedule.next_run, schedule.last_run,
                     json.dumps(list(schedule.last_run_task_ids)), schedule.status,
                     schedule.created_at),
                )
        except sqlite3.Error as error:
            raise StoreError(f"could not create schedule: {error}") from error
        return schedule

    def get(self, schedule_id: str) -> Schedule | None:
        with self._lock:
            if self._connection is None:
                raise StoreError("schedule store is closed")
            row = self._connection.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._schedule_from_row(row) if row is not None else None

    def list(self) -> list[Schedule]:
        with self._lock:
            if self._connection is None:
                raise StoreError("schedule store is closed")
            rows = self._connection.execute(
                "SELECT * FROM schedules ORDER BY created_at, id"
            ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def due(self, now: float | None = None) -> list[Schedule]:
        """Enabled schedules whose next run has arrived (minute-accurate)."""
        now = self.clock() if now is None else now
        with self._lock:
            if self._connection is None:
                raise StoreError("schedule store is closed")
            rows = self._connection.execute(
                "SELECT * FROM schedules WHERE status = 'enabled'"
                " AND next_run IS NOT NULL AND next_run <= ? ORDER BY next_run, id",
                (now,),
            ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def update(
        self, schedule_id: str, *, now: float | None = None, **changes: object,
    ) -> Schedule:
        """Change a schedule; timing changes recompute the next run.

        Allowed keys: ``plan_path``, ``kind``, ``run_at``, ``weekdays``,
        ``status`` (``enabled``/``disabled``; ``expired`` is set by the
        engine, not by hand). Re-enabling a schedule recomputes its next run;
        a one-time plan whose time already passed stays ``expired`` and the
        update is rejected — make a new plan instead.
        """
        now = self.clock() if now is None else now
        allowed = {"plan_path", "kind", "run_at", "weekdays", "status"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"cannot update schedule fields: {', '.join(sorted(unknown))}")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            if row is None:
                raise StoreError(f"schedule {schedule_id} does not exist")
            current = self._schedule_from_row(row)
            plan_path = str(changes["plan_path"]) if "plan_path" in changes else current.plan_path
            kind = str(changes["kind"]) if "kind" in changes else current.kind
            run_at = str(changes["run_at"]) if "run_at" in changes else current.run_at
            weekdays = (
                tuple(int(day) for day in changes["weekdays"])  # type: ignore[arg-type]
                if "weekdays" in changes else current.weekdays
            )
            status = str(changes["status"]) if "status" in changes else current.status
            if "status" in changes and status not in ("enabled", "disabled"):
                raise ValueError("status can only be set to 'enabled' or 'disabled'")
            self._validate(plan_path=plan_path, kind=kind, run_at=run_at, weekdays=weekdays)
            timing_changed = (
                kind != current.kind or run_at != current.run_at or weekdays != current.weekdays
            )
            re_enabling = status == "enabled" and current.status != "enabled"
            next_run = current.next_run
            if timing_changed or re_enabling:
                next_run = compute_next_run(kind, run_at, weekdays, now)
                if next_run is None:
                    raise ValueError(
                        "lịch một lần đã hết giờ chạy, tạo lịch mới thay vì bật lại lịch này"
                    )
                # A disabled schedule keeps its status when only its timing
                # changes; an expired one-time plan revives when its time is
                # moved to a future slot.
                if re_enabling or current.status == "expired":
                    status = "enabled"
            updated = Schedule(
                id=current.id, plan_path=plan_path.strip(), kind=kind, run_at=run_at,
                weekdays=weekdays, next_run=next_run, last_run=current.last_run,
                last_run_task_ids=current.last_run_task_ids, status=status,
                created_at=current.created_at,
            )
            db.execute(
                "UPDATE schedules SET plan_path=?, kind=?, run_at=?, weekdays=?,"
                " next_run=?, status=? WHERE id=?",
                (updated.plan_path, updated.kind, updated.run_at,
                 json.dumps(list(updated.weekdays)), updated.next_run, updated.status,
                 updated.id),
            )
        return updated

    def delete(self, schedule_id: str) -> bool:
        with self._transaction() as db:
            cursor = db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            return cursor.rowcount > 0

    def mark_run(
        self, schedule_id: str, *, now: float | None = None,
        task_ids: tuple[str, ...] = (),
    ) -> Schedule:
        """Record a completed trigger: advance the next run, or expire a once plan."""
        now = self.clock() if now is None else now
        with self._transaction() as db:
            row = db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            if row is None:
                raise StoreError(f"schedule {schedule_id} does not exist")
            current = self._schedule_from_row(row)
            if current.kind == "once":
                next_run, status = None, "expired"
            else:
                next_run = compute_next_run(current.kind, current.run_at, current.weekdays, now)
                status = current.status
            db.execute(
                "UPDATE schedules SET next_run=?, last_run=?, last_run_task_ids=?,"
                " status=? WHERE id=?",
                (next_run, now, json.dumps(list(task_ids)), status, schedule_id),
            )
        updated = self.get(schedule_id)
        assert updated is not None
        return updated

    def record_blocked_tasks(self, schedule_id: str, task_ids: tuple[str, ...]) -> None:
        """Remember which tasks blocked a run (overlap skip), for the detail view."""
        with self._transaction() as db:
            db.execute(
                "UPDATE schedules SET last_run_task_ids=? WHERE id=?",
                (json.dumps(list(task_ids)), schedule_id),
            )

    def active_tasks_since(self, since: float) -> list[dict[str, object]]:
        """Tasks created at/after ``since`` that are still QUEUED or LEASED.

        Read-only overlap input: a previous plan run is still running while
        the lead agent's tasks from it have not finished. An empty list when
        the tasks table does not exist yet (a fresh store used only for
        schedules).
        """
        with self._lock:
            if self._connection is None:
                raise StoreError("schedule store is closed")
            exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
            if exists is None:
                return []
            rows = self._connection.execute(
                "SELECT task_id, state, created_at FROM tasks"
                " WHERE created_at >= ? AND state IN ('QUEUED', 'LEASED')"
                " ORDER BY created_at, task_id",
                (since,),
            ).fetchall()
        return [
            {"task_id": row["task_id"], "state": row["state"], "created_at": row["created_at"]}
            for row in rows
        ]


# ------------------------------------------------------------------ OpenCode trigger

class OpenCodeStarterError(RuntimeError):
    """The OpenCode server could not create a session or take a prompt."""


class OpenCodeSessionStarter(Protocol):
    """Minimal OpenCode surface the engine needs (injectable for tests).

    ``create_session`` opens a new session in ``directory``; ``send_prompt``
    posts the first prompt into it. Both are synchronous; the engine runs
    them in a worker thread.
    """

    def create_session(self, directory: str, title: str) -> str: ...
    def send_prompt(self, session_id: str, text: str) -> None: ...


class UrllibOpenCodeStarter:
    """The real starter: the same two calls the web chat proxy forwards.

    ``POST {url}/api/session`` with ``{"title", "location": {"directory"}}``
    — the proxy (``web/app.py``) forces ``location.directory`` to the main
    chat directory and only allows the ``agent`` body field, so a lead
    session takes its model from that directory's ``opencode.json`` — then
    ``POST /api/session/{id}/prompt`` with ``{"text"}``. Auth is Basic
    ``opencode:<password>``, exactly like ``opencode_headers`` in the web
    app. Only loopback servers are accepted, mirroring ``OpenCodeClient``.
    """

    def __init__(self, url: str, password: str | None, *, timeout_seconds: float = 30.0) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in (
            "127.0.0.1", "localhost", "::1",
        ):
            raise ValueError("OpenCode starter only talks to a loopback server")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.url = url.rstrip("/")
        self.password = password
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, body: dict) -> dict:
        import base64

        payload = json.dumps(body, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.password:
            encoded = base64.b64encode(f"opencode:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        request = urllib.request.Request(
            self.url + path, data=payload, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise OpenCodeStarterError(
                f"OpenCode HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenCodeStarterError(f"OpenCode request failed: {exc}") from exc
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as exc:
            raise OpenCodeStarterError("OpenCode returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise OpenCodeStarterError("OpenCode response must be an object")
        return value

    def create_session(self, directory: str, title: str) -> str:
        created = self._request(
            "POST", "/api/session",
            {"title": title, "location": {"directory": directory}},
        )
        session_id = created.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise OpenCodeStarterError("OpenCode created a session without an id")
        return session_id

    def send_prompt(self, session_id: str, text: str) -> None:
        from urllib.parse import quote

        self._request(
            "POST", f"/api/session/{quote(session_id, safe='')}/prompt", {"text": text},
        )


# ------------------------------------------------------------------ engine

@dataclass
class BuiltinJob:
    """A job the engine runs on its own clock (registered, not scheduled)."""

    name: str
    func: Callable[[], object]
    interval_seconds: float | None = None
    due_fn: Callable[[datetime], datetime] | None = None
    next_due: datetime | None = None  # Bangkok-aware (due_fn jobs)
    next_due_epoch: float | None = None  # interval jobs

    def arm(self, now: float) -> None:
        """Set the first due time from ``now`` (called lazily on first tick)."""
        if self.due_fn is not None:
            self.next_due = self.due_fn(datetime.fromtimestamp(now, BANGKOK_TZ))
        elif self.interval_seconds is not None:
            self.next_due_epoch = now + self.interval_seconds


class SchedulerEngine:
    """Polling loop: due plan schedules + registered builtin jobs.

    One cycle never kills the loop: a failing schedule trigger or builtin job
    is logged and written to the journal, and the next tick still runs. A
    failing trigger does NOT advance the schedule, so the next tick retries
    it instead of silently dropping a plan.
    """

    def __init__(
        self,
        store: ScheduleStore,
        *,
        chat_dir: Path,
        starter: OpenCodeSessionStarter | None = None,
        note: Callable[[dict], None] | None = None,
        clock: Callable[[], float] = time.time,
        tick_seconds: float = TICK_SECONDS,
        overlap_grace_seconds: float = OVERLAP_GRACE_SECONDS,
    ) -> None:
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if overlap_grace_seconds < 0:
            raise ValueError("overlap_grace_seconds must not be negative")
        self.store = store
        self.chat_dir = Path(chat_dir)
        self.starter = starter
        self._note = note
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.overlap_grace_seconds = overlap_grace_seconds
        self._jobs: dict[str, BuiltinJob] = {}
        self._stop = asyncio.Event()
        self._journal: Journal | None = None

    # -- journal notes ("báo thư ký": a note the secretary can read) --

    def _journal_note(self, record: dict) -> None:
        if self._note is not None:
            self._note(record)
            return
        if self._journal is None:
            self._journal = Journal(self.store.path.parent / "journal")
        self._journal.write("notes", record)

    def _note_error(self, kind: str, detail: dict) -> None:
        record = {"kind": kind, **detail}
        try:
            self._journal_note(record)
        except Exception:  # A journal failure must never kill the loop.
            logger.exception("kite.schedules: could not write journal note %s", kind)
        logger.warning("kite.schedules: %s %s", kind, detail)

    # -- builtin registry --

    def register_builtin(
        self,
        name: str,
        schedule: float | Callable[[datetime], datetime],
        func: Callable[[], object],
    ) -> BuiltinJob:
        """Register a builtin job: ``schedule`` is either an interval in
        seconds or a due function ``now (Bangkok-aware) -> next due time``.

        The contract builtins are registered by the wiring (agent
        trưởng, in ``cli.py``/``app.py``), not here: ``secretary_daily_report``
        with ``daily_at_bangkok("00:00")`` and ``needs_decision_scan`` /
        ``mcp_sweep`` with interval 300 — their functions live in the sibling modules of the
        workers that own them.
        """
        if not name or not isinstance(name, str):
            raise ValueError("builtin job name must be a non-empty string")
        if name in self._jobs:
            raise ValueError(f"builtin job {name!r} is already registered")
        if not callable(func):
            raise ValueError("builtin job func must be callable")
        if isinstance(schedule, (int, float)):
            if schedule <= 0:
                raise ValueError("builtin interval must be positive")
            job = BuiltinJob(name=name, func=func, interval_seconds=float(schedule))
        elif callable(schedule):
            job = BuiltinJob(name=name, func=func, due_fn=schedule)
        else:
            raise ValueError("builtin schedule must be seconds or a due function")
        self._jobs[name] = job
        return job

    # -- one cycle --

    def _overlap(self, schedule: Schedule, now: float) -> list[str]:
        """Task ids proving a previous run is still going, else []."""
        if schedule.last_run is None:
            return []
        if now - schedule.last_run < self.overlap_grace_seconds:
            # The lead agent has not yet had time to split the plan into
            # tasks; treat the run as still in progress.
            return ["<grace>"]
        active = self.store.active_tasks_since(schedule.last_run)
        return [str(task["task_id"]) for task in active]

    async def _trigger_plan(self, schedule: Schedule, now: float) -> str:
        """Wake the lead agent in a fresh OpenCode session for this plan.

        Returns the new OpenCode session id. Raises on any failure; the
        caller records the failure and leaves the schedule due for retry.
        """
        if self.starter is None:
            raise OpenCodeStarterError("no OpenCode starter configured for plan triggers")
        if not self.chat_dir.is_dir() or not (self.chat_dir / "opencode.json").is_file():
            raise OpenCodeStarterError(
                f"chưa có thư mục chat của Kite ở {self.chat_dir}: chạy kite chat setup"
            )
        session_id = await asyncio.to_thread(
            self.starter.create_session, str(self.chat_dir),
            f"Kế hoạch: {schedule.plan_path}",
        )
        prompt = (
            f"Đọc file plan `{schedule.plan_path}` và tách thành task qua `start_task`. "
            "Mỗi task chạy qua sandbox như task thường; kết quả chờ duyệt."
        )
        await asyncio.to_thread(self.starter.send_prompt, session_id, prompt)
        return session_id

    async def _run_schedule(self, schedule: Schedule, now: float) -> dict[str, object]:
        blocking = self._overlap(schedule, now)
        if blocking:
            real_ids = [task_id for task_id in blocking if task_id != "<grace>"]
            self.store.record_blocked_tasks(schedule.id, tuple(real_ids))
            self._note_error("schedule_overlap_skipped", {
                "schedule_id": schedule.id, "plan_path": schedule.plan_path,
                "task_ids": real_ids,
                "reason": ("lần chạy trước mới bắt đầu chưa tới 15 phút"
                           if blocking == ["<grace>"]
                           else "task của lần chạy trước chưa xong"),
            })
            return {"schedule_id": schedule.id, "skipped": True, "task_ids": real_ids}
        try:
            session_id = await self._trigger_plan(schedule, now)
        except Exception as exc:  # One failed trigger must not kill the cycle.
            self._note_error("schedule_trigger_failed", {
                "schedule_id": schedule.id, "plan_path": schedule.plan_path,
                "error": str(exc)[:500],
            })
            return {"schedule_id": schedule.id, "error": str(exc)[:500]}
        self.store.mark_run(schedule.id, now=now, task_ids=())
        self._journal_note({
            "kind": "schedule_triggered", "schedule_id": schedule.id,
            "plan_path": schedule.plan_path, "opencode_session_id": session_id,
        })
        logger.info("kite.schedules: triggered plan %s in session %s",
                    schedule.plan_path, session_id)
        return {"schedule_id": schedule.id, "session_id": session_id}

    async def _run_builtin(self, job: BuiltinJob, now: float) -> dict[str, object]:
        try:
            result = job.func()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # A failing job never kills the engine.
            self._note_error("builtin_job_failed", {
                "job": job.name, "error": str(exc)[:500],
            })
            return {"job": job.name, "error": str(exc)[:500]}
        return {"job": job.name, "ok": True}

    async def run_once(self, *, now: float | None = None) -> dict[str, list[dict[str, object]]]:
        """Run one scan cycle: due schedules, then due builtin jobs."""
        now = self.clock() if now is None else now
        summary: dict[str, list[dict[str, object]]] = {"ran": [], "skipped": [], "errors": []}
        try:
            due = self.store.due(now)
        except Exception as exc:
            self._note_error("schedule_scan_failed", {"error": str(exc)[:500]})
            summary["errors"].append({"scan": str(exc)[:500]})
            due = []
        for schedule in due:
            try:
                outcome = await self._run_schedule(schedule, now)
            except Exception as exc:  # Last-resort guard: the cycle goes on.
                self._note_error("schedule_cycle_failed", {
                    "schedule_id": schedule.id, "error": str(exc)[:500],
                })
                summary["errors"].append({"schedule_id": schedule.id, "error": str(exc)[:500]})
                continue
            if outcome.get("skipped"):
                summary["skipped"].append(outcome)
            elif "error" in outcome:
                summary["errors"].append(outcome)
            else:
                summary["ran"].append(outcome)
        now_bangkok = datetime.fromtimestamp(now, BANGKOK_TZ)
        for job in self._jobs.values():
            if job.next_due is None and job.next_due_epoch is None:
                job.arm(now)
            is_due = (
                (job.next_due is not None and now_bangkok >= job.next_due)
                or (job.next_due_epoch is not None and now >= job.next_due_epoch)
            )
            if not is_due:
                continue
            outcome = await self._run_builtin(job, now)
            if "error" in outcome:
                summary["errors"].append(outcome)
            else:
                summary["ran"].append(outcome)
            if job.due_fn is not None:
                try:
                    rearmed = job.due_fn(now_bangkok)
                except Exception as exc:
                    self._note_error("builtin_job_failed", {
                        "job": job.name, "error": f"due_fn failed: {exc}"[:500],
                    })
                    job.next_due = now_bangkok + timedelta(hours=1)
                    continue
                if rearmed <= now_bangkok:  # A due_fn must always move forward.
                    self._note_error("builtin_job_failed", {
                        "job": job.name,
                        "error": "due_fn returned a time that is not in the future",
                    })
                    job.next_due = now_bangkok + timedelta(hours=1)
                else:
                    job.next_due = rearmed
            elif job.interval_seconds is not None:
                job.next_due_epoch = now + job.interval_seconds
        return summary

    # -- loop --

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        """Scan forever until :meth:`stop`; a bad cycle only logs + retries."""
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # Nothing escapes the loop.
                logger.exception("kite.schedules: cycle failed: %s", exc)
                try:
                    self._journal_note({"kind": "scheduler_cycle_failed",
                                        "error": str(exc)[:500]})
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass


__all__ = [
    "BANGKOK_TZ", "TICK_SECONDS", "OVERLAP_GRACE_SECONDS",
    "Schedule", "ScheduleStore", "compute_next_run", "daily_at_bangkok",
    "parse_run_at", "OpenCodeStarterError", "OpenCodeSessionStarter",
    "UrllibOpenCodeStarter", "BuiltinJob", "SchedulerEngine",
]
