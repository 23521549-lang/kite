"""One repository copy per chat session (Gói 2, Q3 — cơ chế lai kiểu GitHub).

Every chat session that runs a task on a repository gets its own lazy clone:

``<state_dir>/envs/<env_id>/sessions/<session_id>/work/`` on branch
``kite-session/<session_id>``. The copy is created lazily, when the session
hands over its first task on that repository (a session that only chats costs
zero disk); it is fast-forwarded to the main repository's newest HEAD every
time it is reused; after a patch is applied to the main repository the copy is
deleted (best-effort) and re-created from the newest HEAD on the next task.

The copy keeps its ``origin`` remote pointing at the main repository: that is
what the fast-forward fetches from, and what lets ``_finished_patch`` resolve
the main repository back from a task that ran on the copy. The copy lives in
Kite's own state directory (same trust domain as the main repository), never
inside a sandbox, so keeping the remote is safe.

The ``session_copies`` table (owned by worker (a) in ``store.py``) is only a
registry mirror: every function here works when the table is missing (fresh
database, or a store created before the Gói 2 migration) and records the row
when the table exists. Nothing here creates the table — that is the store's
job — so a missing table is never an error.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time


class SessionCopyError(RuntimeError):
    """A session copy cannot be created, fast-forwarded, or read."""


def copy_branch(session_id: str) -> str:
    """The branch a session's copy lives on."""
    return f"kite-session/{session_id}"


def _validate_id(name: str, value: str) -> str:
    """A path component: never ``..``, never a slash, never empty."""
    if not isinstance(value, str) or not value.strip():
        raise SessionCopyError(f"{name} đang trống")
    if value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise SessionCopyError(f"{name} không hợp lệ: {value!r}")
    return value


def copy_path(state_dir: Path, env_id: str, session_id: str) -> Path:
    """Where a session's copy of a repository lives (created lazily)."""
    return (Path(state_dir) / "envs" / _validate_id("env_id", env_id)
            / "sessions" / _validate_id("session_id", session_id) / "work")


def copy_identity_from_path(state_dir: Path, path: Path) -> tuple[str, str] | None:
    """``(env_id, session_id)`` for a path inside a session copy, else None."""
    try:
        rel = Path(path).resolve().relative_to(Path(state_dir).resolve() / "envs")
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 4 and parts[1] == "sessions" and parts[3] == "work":
        return parts[0], parts[2]
    return None


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    # core.fsmonitor could name a program from the repository's config: never run it.
    return subprocess.run(["git", "-c", "core.fsmonitor=false", *args],
                          cwd=str(cwd) if cwd else None, capture_output=True, text=True)


def _git_head(repo: Path) -> str:
    result = _git("-C", str(repo), "rev-parse", "HEAD")
    if result.returncode != 0:
        raise SessionCopyError(f"{repo} không phải repo Git có ít nhất một commit")
    return result.stdout.strip()


def _is_dirty(repo: Path) -> bool:
    return bool(_git("-C", str(repo), "status", "--porcelain",
                     "--untracked-files=no").stdout.strip())


def _record(db_path: Path | None, *, env_id: str, session_id: str, path: Path,
            base_head: str, state: str, clock: float | None = None) -> None:
    """Mirror the copy in the ``session_copies`` table when it exists (best-effort)."""
    if db_path is None:
        return
    try:
        db = sqlite3.connect(db_path, timeout=10.0)
        try:
            db.execute(
                """INSERT INTO session_copies
                   (session_id, env_id, copy_path, base_head, created_at, state)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                   env_id=excluded.env_id, copy_path=excluded.copy_path,
                   base_head=excluded.base_head, state=excluded.state""",
                (session_id, env_id, str(path), base_head,
                 int(clock if clock is not None else time.time()), state))
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        # No table yet (pre-migration store) or a locked database: the copy on
        # disk is the truth; the registry row is advisory only.
        pass


def ensure_copy(env_id: str, session_id: str, main_repo: str | Path, *,
                state_dir: Path, db_path: Path | None = None,
                clock: float | None = None) -> Path:
    """Create a session's copy lazily, or fast-forward an existing one to the
    main repository's newest HEAD. Returns the copy path.

    A dirty copy is never fast-forwarded over: its uncommitted changes are
    Kite-internal state that must not be silently discarded, so this raises
    instead and the caller reports it.
    """
    env_id = _validate_id("env_id", env_id)
    session_id = _validate_id("session_id", session_id)
    main = Path(main_repo).expanduser().resolve()
    head = _git_head(main)
    dest = copy_path(state_dir, env_id, session_id)
    branch = copy_branch(session_id)
    if dest.exists():
        _git_head(dest)  # raises unless the existing copy is a Git repo with a commit
        if _is_dirty(dest):
            raise SessionCopyError(
                f"bản sao của session {session_id} có thay đổi chưa commit; "
                "dọn tay rồi giao lại task")
        _git("-C", str(dest), "fetch", "--quiet", "origin")
        if _git("-C", str(dest), "rev-parse", "--verify", "--quiet",
                f"refs/heads/{branch}").returncode != 0:
            checked = _git("-C", str(dest), "checkout", "-q", "-b", branch, head)
            if checked.returncode != 0:
                raise SessionCopyError(f"không tạo được nhánh {branch} trên bản sao: "
                                       + checked.stderr.strip()[-300:])
        else:
            checked = _git("-C", str(dest), "checkout", "-q", branch)
            if checked.returncode != 0:
                raise SessionCopyError(f"không chuyển được bản sao sang nhánh {branch}: "
                                       + checked.stderr.strip()[-300:])
            merged = _git("-C", str(dest), "merge", "--ff-only", "--quiet", head)
            if merged.returncode != 0:
                raise SessionCopyError(
                    f"bản sao của session {session_id} không fast-forward được lên HEAD mới nhất "
                    f"của bản chính ({head[:8]}): lịch sử bản chính đã bị viết lại; xóa bản sao rồi "
                    "giao lại task")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cloned = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(main), str(dest)],
                                capture_output=True, text=True)
        if cloned.returncode != 0:
            raise SessionCopyError(f"không clone được bản sao từ {main}: "
                                   + cloned.stderr.strip()[-300:])
        branched = _git("-C", str(dest), "checkout", "-q", "-b", branch)
        if branched.returncode != 0:
            raise SessionCopyError(f"không tạo được nhánh {branch} trên bản sao: "
                                   + branched.stderr.strip()[-300:])
    _record(db_path, env_id=env_id, session_id=session_id, path=dest,
            base_head=_git_head(dest), state="active", clock=clock)
    return dest


def sync_copy(env_id: str, session_id: str, main_repo: str | Path, *,
              state_dir: Path, db_path: Path | None = None,
              clock: float | None = None) -> Path:
    """Fast-forward an existing copy to the main repository's newest HEAD.

    Unlike :func:`ensure_copy` this never creates a copy: it raises
    :class:`SessionCopyError` when the session has no copy yet, so callers
    that only want to refresh can tell "no copy" apart from "copy created".
    """
    dest = copy_path(state_dir, env_id, session_id)
    if not dest.exists():
        raise SessionCopyError(f"session {session_id} chưa có bản sao (giao task trước đã)")
    return ensure_copy(env_id, session_id, main_repo, state_dir=state_dir,
                       db_path=db_path, clock=clock)


def delete_copy(env_id: str, session_id: str, *, state_dir: Path,
                db_path: Path | None = None, clock: float | None = None) -> bool:
    """Delete a session's copy. Best-effort: never raises — returns False when
    the copy could not be removed (the caller logs it; a sweeper retries).
    A copy that is already gone counts as deleted.
    """
    try:
        dest = copy_path(state_dir, env_id, session_id)
    except SessionCopyError:
        return False
    try:
        if dest.exists() or dest.is_symlink():
            shutil.rmtree(dest)
        gone = not dest.exists()
    except OSError:
        gone = False
    if gone and db_path is not None:
        try:
            db = sqlite3.connect(db_path, timeout=10.0)
            try:
                db.execute("UPDATE session_copies SET state='deleted' WHERE session_id=?",
                           (session_id,))
                db.commit()
            finally:
                db.close()
        except sqlite3.Error:
            pass
    return gone


def copy_size(path: str | Path) -> int:
    """Disk used by a copy in bytes (files under it, ``.git`` included)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total
