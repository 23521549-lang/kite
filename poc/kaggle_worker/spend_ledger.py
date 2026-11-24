"""Durable spend storage: a small SQLite database in the state dir.

The spend ledger (relay.py) used to be rebuilt from the journal's ``spend`` records at every
gateway start, so deleting or damaging the journal silently reset every worker's daily and
monthly caps to zero. This store is the ledger's memory instead: each settled USD amount is
appended here (one row per charge) and the day/month totals are rebuilt from it. The journal
keeps its human-readable ``spend`` lines; they are imported here exactly once (see
``import_journal_once``) so money spent before this store existed is not forgotten.

It also keeps one cursor per pod worker: the timestamp its hourly billing has been counted up
to. A gateway that restarts while a rented pod keeps running reads the cursor back and counts
the gap, instead of billing only from the restart (pods.py).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


class SpendStore:
    """Append-only spend rows + pod billing cursors, in one SQLite file (WAL mode)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS spend (
                worker TEXT NOT NULL, usd REAL NOT NULL, ts REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS spend_worker_ts ON spend (worker, ts);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pod_cursor (worker TEXT PRIMARY KEY, accrued_until REAL NOT NULL);
            """)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ spend rows
    def add(self, worker: str, usd: float, ts: float) -> None:
        with self._lock:
            self._db.execute("INSERT INTO spend (worker, usd, ts) VALUES (?, ?, ?)", (worker, usd, ts))
            self._db.commit()

    def records(self) -> list[dict[str, Any]]:
        """Every charge as a journal-shaped record (``worker``/``usd``/``ts``), oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT worker, usd, ts FROM spend ORDER BY ts, rowid").fetchall()
        return [{"worker": worker, "usd": usd, "ts": ts} for worker, usd, ts in rows]

    def import_journal_once(self, records: list[dict]) -> int:
        """Copy journal ``spend`` records into the store the first time only; return how many.

        The flag lives in the store itself, so a second gateway start (or a CLI run) never
        counts the same journal lines twice. Records after the import are the store's own.
        """
        with self._lock:
            # BEGIN IMMEDIATE takes the database write lock up front: the gateway and a
            # short-lived CLI run must not both pass the flag check and import twice.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                done = self._db.execute(
                    "SELECT value FROM meta WHERE key = 'journal_imported'").fetchone()
                if done:
                    self._db.execute("COMMIT")
                    return 0
                count = 0
                for record in records:
                    worker, usd, ts = record.get("worker"), record.get("usd"), record.get("ts")
                    if isinstance(worker, str) and isinstance(usd, (int, float)) and isinstance(ts, (int, float)):
                        self._db.execute("INSERT INTO spend (worker, usd, ts) VALUES (?, ?, ?)",
                                         (worker, float(usd), float(ts)))
                        count += 1
                self._db.execute("INSERT INTO meta (key, value) VALUES ('journal_imported', '1')")
                self._db.execute("COMMIT")
                return count
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ pod billing cursors
    def pod_cursor(self, worker: str) -> float | None:
        """The timestamp this pod's hourly billing has been counted up to (None: not billing)."""
        with self._lock:
            row = self._db.execute("SELECT accrued_until FROM pod_cursor WHERE worker = ?",
                                   (worker,)).fetchone()
        return float(row[0]) if row else None

    def set_pod_cursor(self, worker: str, ts: float | None) -> None:
        with self._lock:
            if ts is None:
                self._db.execute("DELETE FROM pod_cursor WHERE worker = ?", (worker,))
            else:
                self._db.execute(
                    "INSERT INTO pod_cursor (worker, accrued_until) VALUES (?, ?) "
                    "ON CONFLICT(worker) DO UPDATE SET accrued_until = excluded.accrued_until",
                    (worker, float(ts)))
            self._db.commit()
