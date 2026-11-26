"""The web's own tables (SQLAlchemy Core): the account, sessions, login attempts, audit log, repositories,
settings. SQLite under the state folder by default; Postgres when ``kite web serve --db`` names one
(the compose deployment). Workers, tasks, the journal, memory and bench stay where Kite keeps them
(config.yaml with 0600 secret files, the task store, the journal files): CLI and web never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Callable

from sqlalchemy import (Boolean, Column, Float, Integer, MetaData, String, Table, Text, create_engine, delete,
                        event, func, insert, select, text, update)

SCHEMA_VERSION = 2
metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(64), unique=True, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("totp_secret", Text, nullable=False),          # sealed (security.Sealer)
    Column("totp_pending", Text),                         # sealed; a new secret until its first code is confirmed
    Column("totp_last", Integer, nullable=False, default=0),
    Column("failed", Integer, nullable=False, default=0),
    Column("locked_until", Float, nullable=False, default=0.0),
    Column("created_at", Float, nullable=False),
    Column("password_changed_at", Float, nullable=False),
)
sessions = Table(
    "sessions", metadata,
    Column("id", String(64), primary_key=True),           # SHA-256 of the cookie's token
    Column("user_id", Integer, nullable=False),
    Column("csrf", String(64), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("last_seen", Float, nullable=False),
    Column("ip", String(64), nullable=False, default=""),
    Column("agent", String(200), nullable=False, default=""),
)
recovery_codes = Table(
    "recovery_codes", metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, nullable=False, index=True),
    Column("code_hash", String(64), nullable=False),     # security.recovery_digest of the code
    Column("created_at", Float, nullable=False),
    Column("used_at", Float),                            # set once, when the code logs its owner in
)
login_attempts = Table(
    "login_attempts", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", Float, nullable=False, index=True),
    Column("ip", String(64), nullable=False),
    Column("username", String(64), nullable=False),
    Column("ok", Boolean, nullable=False),
)
audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", Float, nullable=False, index=True),
    Column("username", String(64), nullable=False),
    Column("ip", String(64), nullable=False),
    Column("action", String(64), nullable=False),
    Column("target", String(200), nullable=False, default=""),
    Column("ok", Boolean, nullable=False),
    Column("detail", Text, nullable=False, default="{}"),
)
repos = Table(
    "repos", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(100), unique=True, nullable=False),
    Column("path", Text, nullable=False),                 # folder on this machine (a clone, or the repo itself)
    Column("url", Text, nullable=False, default=""),      # where it comes from (GitHub...), for the owner
    Column("description", Text, nullable=False, default=""),  # F4: what the repo is for, written by the owner
    Column("created_at", Float, nullable=False),
)
settings = Table(
    "settings", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
)
schema_version = Table("schema_version", metadata, Column("version", Integer, nullable=False))

# Each step takes the schema from version N-1 to N; the first is create_all.
def _migrate_v2_add_repo_description(conn) -> None:
    """v1 -> v2 (F4): the environment description the owner writes for a repo."""
    conn.execute(text("ALTER TABLE repos ADD COLUMN description TEXT NOT NULL DEFAULT ''"))


MIGRATIONS: list[Callable[[Any], None]] = [_migrate_v2_add_repo_description]


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str
    totp_secret: str
    totp_pending: str | None
    totp_last: int
    failed: int
    locked_until: float


def connect(url: str):
    engine = create_engine(url, future=True, pool_pre_ping=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(connection, _record):       # one writer at a time, readers do not wait
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{Path(path).resolve()}"


class Store:
    """Every query the web makes, in one place."""

    def __init__(self, engine, *, clock: Callable[[], float] = time.time) -> None:
        self.engine, self.clock = engine, clock

    def migrate(self) -> int:
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            row = conn.execute(select(schema_version.c.version)).first()
            version = row[0] if row else 0
            if row is None:
                conn.execute(insert(schema_version).values(version=SCHEMA_VERSION))
                return SCHEMA_VERSION
            for step in MIGRATIONS[version - 1:]:     # future schema changes
                step(conn)
            if version != SCHEMA_VERSION:
                conn.execute(update(schema_version).values(version=SCHEMA_VERSION))
        return SCHEMA_VERSION

    # ------------------------------------------------------------------ users
    @staticmethod
    def _user(row) -> User | None:
        return None if row is None else User(row.id, row.username, row.password_hash, row.totp_secret,
                                             row.totp_pending, row.totp_last, row.failed, row.locked_until)

    def user_by_name(self, username: str) -> User | None:
        with self.engine.connect() as conn:
            return self._user(conn.execute(select(users).where(users.c.username == username)).first())

    def user(self, user_id: int) -> User | None:
        with self.engine.connect() as conn:
            return self._user(conn.execute(select(users).where(users.c.id == user_id)).first())

    def user_count(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(users)).scalar_one()

    def create_user(self, username: str, password_hash: str, totp_sealed: str) -> int:
        now = self.clock()
        with self.engine.begin() as conn:
            return conn.execute(insert(users).values(
                username=username, password_hash=password_hash, totp_secret=totp_sealed, totp_last=0, failed=0,
                locked_until=0.0, created_at=now, password_changed_at=now)).inserted_primary_key[0]

    def update_user(self, user_id: int, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(users).where(users.c.id == user_id).values(**values))

    # ------------------------------------------------------------------ sessions
    def add_session(self, sid: str, user_id: int, csrf: str, ip: str, agent: str) -> None:
        now = self.clock()
        with self.engine.begin() as conn:
            conn.execute(insert(sessions).values(id=sid, user_id=user_id, csrf=csrf, created_at=now, last_seen=now,
                                                 ip=ip[:64], agent=agent[:200]))

    def session(self, sid: str):
        with self.engine.connect() as conn:
            return conn.execute(select(sessions).where(sessions.c.id == sid)).first()

    def touch_session(self, sid: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(sessions).where(sessions.c.id == sid).values(last_seen=self.clock()))

    def drop_session(self, sid: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(sessions).where(sessions.c.id == sid))

    def update_session_fingerprint(self, sid: str, ip: str, agent: str) -> None:
        """Adopt the address/browser a session is now seen from (after a successful re-authentication):
        the drift check in the web compares every request against these stored values."""
        with self.engine.begin() as conn:
            conn.execute(update(sessions).where(sessions.c.id == sid).values(ip=ip[:64], agent=agent[:200]))

    # ------------------------------------------------------------------ recovery codes
    def replace_recovery_codes(self, user_id: int, code_hashes: list[str]) -> None:
        """A fresh set replaces the old one whole (a new 2FA key retires the previous codes)."""
        now = self.clock()
        with self.engine.begin() as conn:
            conn.execute(delete(recovery_codes).where(recovery_codes.c.user_id == user_id))
            conn.execute(insert(recovery_codes),
                         [{"user_id": user_id, "code_hash": h, "created_at": now, "used_at": None}
                          for h in code_hashes])

    def use_recovery_code(self, user_id: int, code_hash: str) -> bool:
        """Spend one code: True only the first time its digest is presented (the update is guarded on
        ``used_at IS NULL``, so two simultaneous tries cannot both get in)."""
        with self.engine.begin() as conn:
            row = conn.execute(select(recovery_codes.c.id).where(
                (recovery_codes.c.user_id == user_id) & (recovery_codes.c.code_hash == code_hash)
                & recovery_codes.c.used_at.is_(None))).first()
            if row is None:
                return False
            spent = conn.execute(update(recovery_codes).where(
                (recovery_codes.c.id == row[0]) & recovery_codes.c.used_at.is_(None)
            ).values(used_at=self.clock()))
            return spent.rowcount > 0

    def recovery_codes_left(self, user_id: int) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(recovery_codes).where(
                (recovery_codes.c.user_id == user_id) & recovery_codes.c.used_at.is_(None))).scalar_one()

    def drop_sessions(self, user_id: int, *, keep: str | None = None) -> int:
        with self.engine.begin() as conn:
            query = delete(sessions).where(sessions.c.user_id == user_id)
            if keep:
                query = query.where(sessions.c.id != keep)
            return conn.execute(query).rowcount

    def list_sessions(self, user_id: int) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(sessions).where(sessions.c.user_id == user_id)
                                .order_by(sessions.c.last_seen.desc())).all()
        return [{"id": r.id[:12], "created_at": r.created_at, "last_seen": r.last_seen, "ip": r.ip, "agent": r.agent}
                for r in rows]

    def prune_sessions(self, *, idle: float, absolute: float) -> None:
        now = self.clock()
        with self.engine.begin() as conn:
            conn.execute(delete(sessions).where((sessions.c.last_seen < now - idle)
                                                | (sessions.c.created_at < now - absolute)))
            conn.execute(delete(login_attempts).where(login_attempts.c.ts < now - 30 * 86400))

    # ------------------------------------------------------------------ login attempts
    def attempt(self, ip: str, username: str, ok: bool) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(login_attempts).values(ts=self.clock(), ip=ip[:64], username=username[:64], ok=ok))

    def failures_from(self, ip: str, seconds: float) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(login_attempts).where(
                (login_attempts.c.ip == ip) & (login_attempts.c.ok.is_(False))
                & (login_attempts.c.ts >= self.clock() - seconds))).scalar_one()

    # ------------------------------------------------------------------ audit
    def audit(self, username: str, ip: str, action: str, target: str = "", ok: bool = True,
              detail: dict | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(audit_log).values(
                ts=self.clock(), username=username[:64], ip=ip[:64], action=action[:64], target=str(target)[:200],
                ok=ok, detail=json.dumps(detail or {}, ensure_ascii=False, default=str)[:4000]))

    def audit_rows(self, limit: int = 100, before: float | None = None) -> list[dict]:
        query = select(audit_log).order_by(audit_log.c.ts.desc()).limit(max(1, min(limit, 500)))
        if before is not None:
            query = query.where(audit_log.c.ts < before)
        with self.engine.connect() as conn:
            rows = conn.execute(query).all()
        return [{"ts": r.ts, "username": r.username, "ip": r.ip, "action": r.action, "target": r.target,
                 "ok": r.ok, "detail": json.loads(r.detail or "{}")} for r in rows]

    # ------------------------------------------------------------------ repos and settings
    def repo_rows(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(repos).order_by(repos.c.name)).all()
        return [{"id": r.id, "name": r.name, "path": r.path, "url": r.url,
                 "description": getattr(r, "description", "") or "", "created_at": r.created_at} for r in rows]

    def set_repo_description(self, repo_id: int, description: str) -> bool:
        """Save the owner's description for one environment; False when the id is unknown."""
        with self.engine.begin() as conn:
            return conn.execute(update(repos).where(repos.c.id == repo_id)
                                .values(description=description)).rowcount > 0

    def add_repo(self, name: str, path: str, url: str = "") -> int:
        with self.engine.begin() as conn:
            return conn.execute(insert(repos).values(name=name, path=path, url=url,
                                                     created_at=self.clock())).inserted_primary_key[0]

    def drop_repo(self, repo_id: int) -> bool:
        with self.engine.begin() as conn:
            return conn.execute(delete(repos).where(repos.c.id == repo_id)).rowcount > 0

    def setting(self, key: str, default: Any = None) -> Any:
        with self.engine.connect() as conn:
            row = conn.execute(select(settings.c.value).where(settings.c.key == key)).first()
        return default if row is None else json.loads(row[0])

    def set_setting(self, key: str, value: Any) -> None:
        text = json.dumps(value, ensure_ascii=False)
        with self.engine.begin() as conn:
            if conn.execute(update(settings).where(settings.c.key == key).values(value=text)).rowcount == 0:
                conn.execute(insert(settings).values(key=key, value=text))
