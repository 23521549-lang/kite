"""Docling fallback for the Knowledge store (Gói 6, worker (b)).

A document whose MarkItDown text is (near-)empty — a scanned PDF — is converted
again with Docling, in a detached background job, never inline in the upload
request (Docling is ~23x slower than MarkItDown and far heavier on RAM).

Two entry points:

* ``request_reconvert(state_dir, doc_id)`` — called by the web endpoint: refuses
  with ``ValueError`` when a reconvert is already running (the endpoint maps it
  to HTTP 409), otherwise sets the meta to ``status="converting"`` first (so
  the frontend sees the conversion and disables the button), spawns
  ``kite knowledge reconvert <doc_id>`` detached and returns
  ``{"status": "converting"}``. A spawn failure restores the previous status.
  Duplicate runs are refused by the lock only, never by the meta status, so
  the auto-trigger (called right after ingest, meta already ``"converting"``,
  no lock yet) is not refused.
* ``run_reconvert(state_dir, doc_id, reason=...)`` — called by that CLI command:
  does the conversion, archives the old ``converted.md`` under ``versions/``,
  writes the new Markdown and updates ``meta.json``. Every failure is recorded
  in the meta (``status="failed"`` + ``error``) and returned, never raised, so
  the CLI is the only place that prints the error and picks the exit code.

The lock (``reconvert.lock`` in the document folder) follows the bench lock in
``bench.py``: JSON ``{"pid", "started_at", "token"}``, created atomically with
``O_EXCL``; a lock whose pid is dead is stale and removed. ``request_reconvert``
owns the lock while it spawns the child and hands the child a one-time token,
so the child releases exactly the lock its requester created.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

# Meta helpers live in the sibling module (knowledge.py): atomic .tmp +
# os.replace, mode 0600. Imported directly — the parallel-work fallback was
# removed at integration (knowledge.py exists; dead code is not kept).
from .knowledge import load_meta as _load_meta
from .knowledge import write_meta as _write_meta


LOCK_NAME = "reconvert.lock"
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REASONS = ("auto-scan", "manual")


def _doc_dir(state_dir: Path, doc_id: str) -> Path:
    if not _DOC_ID_RE.match(doc_id or ""):
        raise ValueError(f"doc_id không hợp lệ: {doc_id!r}")
    return Path(state_dir) / "knowledge" / doc_id


def _pid_alive(pid) -> bool:
    """True when ``pid`` is a live process. A zombie counts as dead: a detached
    child the spawner never reaps (``start_new_session=True`` does not reap)
    would otherwise keep its lock "live" forever after a crash — the exact
    case a stale lock must clean up. Falls back to ``os.kill(pid, 0)`` when
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


def _read_lock(doc_dir: Path) -> dict | None:
    """The lock record, or None when there is no (live) lock. A stale lock — pid
    dead, or an unreadable/corrupt record — is removed."""
    path = doc_dir / LOCK_NAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        path.unlink(missing_ok=True)  # unreadable/corrupt: not a live lock
        return None
    if not isinstance(record, dict) or not _pid_alive(record.get("pid")):
        path.unlink(missing_ok=True)
        return None
    return record


def is_converting(state_dir: str | Path, doc_id: str) -> bool:
    """True when a reconvert job is currently running for this doc: a live pid
    holds ``reconvert.lock``. A meta status of ``"converting"`` alone does NOT
    count — it is also the state right after ingest, before the auto-triggered
    job has spawned (no lock yet). A stale lock is removed by ``_read_lock``."""
    try:
        return _read_lock(_doc_dir(Path(state_dir), doc_id)) is not None
    except ValueError:
        return False


def _acquire_lock(doc_dir: Path, *, pid: int | None = None) -> tuple[Path, str]:
    """Create the lock atomically. Returns (lock path, token). Raises ValueError
    when another live reconvert holds it."""
    path = doc_dir / LOCK_NAME
    token = uuid.uuid4().hex
    record = json.dumps({"pid": pid if pid is not None else os.getpid(),
                         "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "token": token})
    for _ in range(3):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            held = _read_lock(doc_dir)  # removes a stale lock, if that is what it is
            if held is not None:
                raise ValueError(
                    f"tài liệu {doc_dir.name} đang được chuyển lại bằng Docling "
                    f"(pid {held.get('pid')}, từ {held.get('started_at')}); chờ nó xong rồi thử lại"
                ) from None
            continue
    else:
        raise ValueError(f"không tạo được lock {path}")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(record)
    return path, token


def _release_lock(doc_dir: Path, *, pid: int | None = None, token: str | None = None) -> None:
    """Remove the lock, but only when it is still ours (same pid, or same
    one-time token handed to the spawned child)."""
    path = doc_dir / LOCK_NAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(record, dict):
        return
    if token is not None and record.get("token") == token:
        path.unlink(missing_ok=True)
    elif pid is not None and record.get("pid") == pid:
        path.unlink(missing_ok=True)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _log_path(state_dir: Path, doc_id: str) -> Path:
    return (Path(state_dir) / "journal"
            / f"knowledge-reconvert-{doc_id}-{time.strftime('%Y%m%d-%H%M%S')}.log")


def _sanitize_no_proxy_for_httpx() -> None:
    """Drop bracketed-IPv6 entries (``[fd8b:...::1]``) from ``NO_PROXY``/``no_proxy``.

    httpx (used by the HuggingFace model download) raises ``InvalidURL:
    Invalid port: ':1]'`` on such entries while parsing the no-proxy list,
    which breaks every first Docling run on a host behind an IPv6 egress
    proxy — this VM is one. The entries are only removed from this process's
    own environment, and only the bracketed ones httpx cannot parse; the proxy
    variables themselves are untouched.
    """
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if not value:
            continue
        kept = [entry for entry in value.split(",") if "[" not in entry]
        if len(kept) != len(value.split(",")):
            os.environ[key] = ",".join(kept)


def convert_with_docling(path: str | Path) -> tuple[str, str]:
    """Convert one file with Docling. Returns (markdown, docling_version).

    Raises RuntimeError with a clear message when Docling is not installed or
    the conversion fails (missing model included) — never a bare ImportError
    leaking out of the job.
    """
    path = Path(path)
    _sanitize_no_proxy_for_httpx()
    try:
        import docling
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise RuntimeError(
            "Docling chưa được cài đặt (cài bằng `pip install -e .[docling]`); "
            "không chuyển lại được tài liệu bằng Docling"
        ) from exc
    try:
        converter = DocumentConverter()
        result = converter.convert(str(path))
        markdown = result.document.export_to_markdown()
    except Exception as exc:
        raise RuntimeError(f"Docling không chuyển được {path}: {exc}") from exc
    version = getattr(docling, "__version__", "unknown")
    return markdown, str(version)


def _kite_command(config_path: Path | None) -> list[str]:
    """How to run this Kite again for the detached job (same shape as
    ``KiteOps._spawn_detached``: the installed ``kite`` command when present)."""
    kite = shutil.which("kite")
    if kite:
        command = [kite]
    else:  # pragma: no cover - fallback when no console script is installed
        command = [sys.executable, "-m", "poc.kaggle_worker.cli"]
    if config_path is not None:
        command += ["--config", str(config_path)]
    return command


def _find_config(state_dir: Path) -> Path | None:
    for name in ("config.yaml", "config.json"):
        candidate = Path(state_dir) / name
        if candidate.is_file():
            return candidate
    return None


def request_reconvert(state_dir: str | Path, doc_id: str, *, reason: str = "manual",
                      config_path: str | Path | None = None,
                      kite_command: list[str] | None = None) -> dict:
    """Start a Docling reconvert in the background.

    Raises ValueError when the document does not exist, when ``reason`` is
    unknown, or when a reconvert is already running (the web endpoint maps
    that case to HTTP 409). Otherwise spawns ``kite knowledge reconvert`` detached
    (``start_new_session=True``, like ``KiteOps._spawn_detached``) and returns
    ``{"doc_id", "status": "converting", "log"}``.

    The meta is set to ``status="converting"`` BEFORE spawning, so the frontend
    (which polls on that status and disables the reconvert button while it is
    set) sees the conversion in progress. If spawning fails, the previous
    status is restored in the meta before the error is raised.

    Duplicate runs are refused by the LOCK only (a live pid holding
    ``reconvert.lock``), never by the meta status: the auto-trigger calls this
    right after ingest, when the meta is already ``"converting"`` but no lock
    exists yet — refusing on the status would kill the auto-trigger.
    """
    state_dir = Path(state_dir)
    if reason not in _REASONS:
        raise ValueError(f"reason không hợp lệ: {reason!r} (chỉ 'auto-scan' hoặc 'manual')")
    doc_dir = _doc_dir(state_dir, doc_id)
    if not doc_dir.is_dir():
        raise ValueError(f"không có tài liệu {doc_id!r}")
    try:
        meta = _load_meta(doc_dir)
    except (ValueError, FileNotFoundError, OSError) as exc:
        # load_meta raises FileNotFoundError when meta.json is missing.
        raise ValueError(f"không có tài liệu {doc_id!r}: {exc}") from exc
    lock_path, token = _acquire_lock(doc_dir)
    old_status = meta.get("status")
    try:
        meta["status"] = "converting"
        _write_meta(doc_dir, meta)
    except Exception:
        _release_lock(doc_dir, token=token)
        raise
    log = _log_path(state_dir, doc_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    config = (Path(config_path) if config_path is not None else _find_config(state_dir))
    if config is None and kite_command is None:
        # No config, no state for the child to find: fail now, loudly, instead
        # of spawning a job that can only report "no such document". (When the
        # caller supplies the full kite_command, it owns the invocation.)
        try:
            meta["status"] = old_status
            _write_meta(doc_dir, meta)
        except Exception:
            pass
        _release_lock(doc_dir, token=token)
        raise ValueError(
            f"không tìm thấy config.yaml/config.json trong {state_dir}: "
            "job chuyển lại không biết state nào để dùng")
    command = list(kite_command) if kite_command is not None else _kite_command(config)
    args = [*command, "knowledge", "reconvert", doc_id, "--reason", reason,
            "--lock-token", token]
    try:
        with log.open("w", encoding="utf-8") as stream:
            proc = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    cwd=str(Path.home()))
    except OSError as exc:
        try:  # best effort: put the previous status back before reporting
            meta["status"] = old_status
            _write_meta(doc_dir, meta)
        except Exception:
            pass
        _release_lock(doc_dir, token=token)
        raise ValueError(f"không khởi động được job chuyển lại: {exc}") from exc
    # The lock now belongs to the child: re-record it under the child's pid so
    # a stale check sees the real worker, while the token stays the same.
    try:
        record = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("token") == token:
            record["pid"] = proc.pid
            lock_path.write_text(json.dumps(record), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass
    return {"doc_id": doc_id, "status": "converting", "log": str(log)}


def run_reconvert(state_dir: str | Path, doc_id: str, reason: str = "manual",
                  _lock_token: str | None = None) -> dict:
    """Run one Docling reconvert in this process (the CLI calls this).

    Never raises for a conversion failure: the failure is written into
    ``meta.json`` (``status="failed"`` + ``error``) and returned. The only
    exceptions that escape are a bad ``doc_id``/``reason`` (caller error) and a
    failure to write the meta itself. A reconvert already running is reported
    as failed *without* touching the meta, so it cannot clobber the running
    job's ``"converting"`` status.
    """
    state_dir = Path(state_dir)
    if reason not in _REASONS:
        raise ValueError(f"reason không hợp lệ: {reason!r} (chỉ 'auto-scan' hoặc 'manual')")
    doc_dir = _doc_dir(state_dir, doc_id)
    if not doc_dir.is_dir():
        return {"doc_id": doc_id, "status": "failed",
                "error": f"không có tài liệu {doc_id!r}"}
    if _lock_token is None:
        try:
            _acquire_lock(doc_dir)
        except ValueError as exc:
            return {"doc_id": doc_id, "status": "failed", "error": str(exc)}
        release = lambda: _release_lock(doc_dir, pid=os.getpid())  # noqa: E731
    else:
        release = lambda: _release_lock(doc_dir, token=_lock_token)  # noqa: E731
    try:
        try:
            meta = _load_meta(doc_dir)
        except (ValueError, FileNotFoundError, OSError) as exc:
            # load_meta raises FileNotFoundError when meta.json is missing.
            return {"doc_id": doc_id, "status": "failed", "error": str(exc)}
        filename = str(meta.get("filename") or "")
        suffix = Path(filename).suffix
        original = doc_dir / f"original{suffix}" if suffix else None
        if original is None or not original.is_file():
            return _fail(meta, doc_dir, doc_id, reason,
                         "không còn file gốc của tài liệu nên không chuyển lại được")
        try:
            markdown, version = convert_with_docling(original)
        except Exception as exc:
            return _fail(meta, doc_dir, doc_id, reason, str(exc))
        if not markdown.strip():
            return _fail(meta, doc_dir, doc_id, reason,
                         "Docling chuyển xong nhưng không đọc được nội dung nào "
                         "(tài liệu có thể là ảnh scan mờ hoặc trống)")
        # Archive the previous Markdown before replacing it.
        previous_markdown = None
        converted = doc_dir / "converted.md"
        if converted.is_file():
            versions = doc_dir / "versions"
            versions.mkdir(parents=True, exist_ok=True)
            old_converter = str(meta.get("converter") or "unknown")
            archived = versions / f"{time.strftime('%Y%m%d-%H%M%S')}-{old_converter}.md"
            shutil.copy2(converted, archived)
            previous_markdown = f"versions/{archived.name}"
        converted.write_text(markdown, encoding="utf-8")
        at = _now_iso()
        history = list(meta.get("history") or [])
        history.append({"converter": "docling", "converter_version": version, "at": at,
                        "reason": reason, "previous_markdown": previous_markdown})
        meta.update({"converter": "docling", "converter_version": version,
                     "converted_at": at, "status": "ready", "error": None,
                     "history": history})
        _write_meta(doc_dir, meta)
        return {"doc_id": doc_id, "status": "ready", "converter": "docling",
                "converter_version": version, "converted_at": at,
                "chars": len(markdown)}
    finally:
        release()


def _fail(meta: dict, doc_dir: Path, doc_id: str, reason: str, error: str) -> dict:
    """Record a failed reconvert in the meta (status + error) and return it."""
    at = _now_iso()
    history = list(meta.get("history") or [])
    # History entries keep exactly the contracted keys; the failure itself is
    # recorded in the meta's top-level "error" (see below).
    history.append({"converter": "docling", "converter_version": None,
                    "at": at, "reason": reason, "previous_markdown": None})
    meta.update({"status": "failed", "error": error, "history": history})
    _write_meta(doc_dir, meta)
    return {"doc_id": doc_id, "status": "failed", "error": error}
