"""Locks for background jobs that must run one at a time.

Two kinds, by who has to see the lock:

* ``hold`` / ``holder``: an ``fcntl.flock`` on a shared file, held for the whole run by the process
  doing the work. The kernel drops it when that process exits, so a crashed job leaves no stale
  lock, and the check needs no PID. Use it when the job and the checks can run in different
  containers: each container numbers its processes on its own, so a PID written in one means
  nothing in another (the bench lock: a run started from the chat, checked by ``kite busy`` in the
  gateway container).
* ``acquire`` / ``read`` / ``release``: a record with the pid and a one-time token, for a job one
  process starts and its detached child finishes in the same container (Docling reconvert, code map
  rebuild): the token lets the child release exactly the lock its parent made.

And for the small JSON state files several processes update (the chat sessions' MCP servers, the
web, the worker, the CLI): ``updating`` around each read-modify-write, ``write_json`` to replace the
file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from typing import IO, Any
import uuid


# ------------------------------------------------------------------ JSON state files
@contextmanager
def updating(path: Path) -> Iterator[None]:
    """Hold an exclusive lock for one read-modify-write of ``path``, so two writers cannot both read
    the old content and one of them lose the other's change. The lock is a ``.lock`` file next to it:
    every write replaces ``path`` itself, which would drop a lock taken on it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write_json(path: Path, data: Any, **dump: Any) -> None:
    """Replace ``path`` with ``data`` in one step, readable by the owner only (mkstemp: 0600). Each
    write has its own temporary file, so two writers never write into the same one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, **dump)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------ flock: across containers
def hold(path: Path, record: dict) -> IO[str] | None:
    """Take the lock and write ``record`` into the file; returns the open handle (keep it open for
    the whole run, close it to release) or None when another process holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")        # never truncate before the lock is ours
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(record, ensure_ascii=False))
    handle.flush()
    return handle


def holder(path: Path) -> dict | None:
    """The record of the process holding the lock, or None when nobody holds it."""
    try:
        handle = open(path, encoding="utf-8")
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                record = json.loads(handle.read() or "{}")
            except ValueError:
                record = {}
            return record if isinstance(record, dict) else {}
        fcntl.flock(handle, fcntl.LOCK_UN)
        return None


# ------------------------------------------------------------------ pid + token: parent and child
def pid_alive(pid) -> bool:
    """True when ``pid`` is a live process in this PID namespace. A zombie counts as dead: a
    detached child the spawner never reaps (``start_new_session=True`` does not reap) would
    otherwise keep its lock "live" forever after a crash. Falls back to ``os.kill(pid, 0)`` when
    ``/proc`` is unavailable."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
            state = stream.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError, ValueError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read(path: Path) -> dict | None:
    """The lock record, or None when there is no live lock. A stale lock (dead pid, unreadable or
    corrupt record) is removed."""
    path = Path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    if not isinstance(record, dict) or not pid_alive(record.get("pid")):
        path.unlink(missing_ok=True)
        return None
    return record


def acquire(path: Path, busy_message, *, pid: int | None = None) -> tuple[Path, str]:
    """Create the lock atomically; returns (path, token). Raises ValueError(busy_message(record))
    when another live job holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    record = json.dumps({"pid": pid if pid is not None else os.getpid(),
                         "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "token": token})
    for _ in range(3):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            held = read(path)              # removes a stale lock, if that is what it is
            if held is not None:
                raise ValueError(busy_message(held)) from None
    else:
        raise ValueError(f"không tạo được lock {path}")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(record)
    return path, token


def release(path: Path, *, pid: int | None = None, token: str | None = None) -> None:
    """Remove the lock, but only when it is still ours (same pid, or the one-time token handed to
    the spawned child)."""
    path = Path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(record, dict):
        return
    if (token is not None and record.get("token") == token) or (pid is not None and record.get("pid") == pid):
        path.unlink(missing_ok=True)
