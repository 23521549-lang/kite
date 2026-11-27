"""Codemap rebuild for the Environment Context tab (F4).

A repo's code map is graphify's tree-sitter graph (``codemap.py``), built
once and reused from ``<state_dir>/codemap/<env_id>/graphify-out/graph.json``.
Building can take minutes on a big repo, so — like the Docling reconvert in
``docling_job.py`` — the rebuild never runs inline in a web request:

* ``request_rebuild(state_dir, env_id, repo_path)`` — called by the web
  endpoint: refuses with ``ValueError`` when graphify is not installed or a
  rebuild is already running (the endpoint maps those to 503 / 409),
  otherwise sets the meta to ``status="building"`` first (so the frontend
  sees the build and disables the button), spawns ``kite codemap build``
  detached and returns ``{"env_id", "status": "building", "log"}``. A spawn
  failure restores the previous status.
* ``run_rebuild(state_dir, env_id, ...)`` — called by that CLI command:
  resolves the repo from the web store, deletes any old ``graph.json`` (a
  rebuild must be fresh, not a reuse), builds, writes the meta. Every
  failure is recorded in the meta (``status="failed"`` + ``error``) and
  returned, never raised, so the CLI is the only place that prints the
  error and picks the exit code.

The lock (``rebuild.lock`` in the codemap folder) follows the bench lock in
``bench.py`` via ``docling_job.py``: JSON ``{"pid", "started_at", "token"}``,
created atomically with ``O_EXCL``; a lock whose pid is dead is stale and
removed. ``request_rebuild`` owns the lock while it spawns the child and
hands the child a one-time token, so the child releases exactly the lock its
requester created.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .. import codemap as codemap_mod, joblock
from .env_context import codemap_dir, read_codemap_meta, write_codemap_meta

LOCK_NAME = "rebuild.lock"


def _read_lock(codemap_path: Path) -> dict | None:
    """The lock record, or None when there is no live lock (a stale one is removed)."""
    return joblock.read(codemap_path / LOCK_NAME)


def is_building(state_dir: str | Path, env_id: str) -> bool:
    """True when a codemap rebuild is currently running for this environment:
    a live pid holds ``rebuild.lock``. A meta status of ``"building"`` alone
    does NOT count — it is also the state right after the request, before the
    spawned job has started (no lock yet). A stale lock is removed by
    ``_read_lock``."""
    try:
        return _read_lock(codemap_dir(state_dir, env_id)) is not None
    except ValueError:
        return False


def _acquire_lock(codemap_path: Path) -> tuple[Path, str]:
    """Create the lock atomically; returns (lock path, token). Raises ValueError when another live
    job holds it."""
    return joblock.acquire(codemap_path / LOCK_NAME, lambda held: (
        "bản đồ code đang được tạo lại "
        f"(pid {held.get('pid')}, từ {held.get('started_at')}); chờ nó xong rồi thử lại"))


def _release_lock(codemap_path: Path, *, pid: int | None = None, token: str | None = None) -> None:
    """Remove the lock, but only when it is still ours (same pid, or the child's token)."""
    joblock.release(codemap_path / LOCK_NAME, pid=pid, token=token)


def _now_epoch() -> float:
    return time.time()


def _log_path(state_dir: Path, env_id: str) -> Path:
    return (Path(state_dir) / "journal"
            / f"codemap-rebuild-{env_id}-{time.strftime('%Y%m%d-%H%M%S')}.log")


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


def request_rebuild(state_dir: str | Path, env_id: str, repo_path: str | None, *,
                    config_path: str | Path | None = None,
                    kite_command: list[str] | None = None) -> dict:
    """Start a codemap rebuild in the background.

    Raises ValueError when there is no repo (the Default environment), when
    graphify is not installed, or when a rebuild is already running (the web
    endpoint maps those to 409 / 503 / 409). Otherwise spawns
    ``kite codemap build`` detached (``start_new_session=True``, like
    ``KiteOps._spawn_detached``) and returns
    ``{"env_id", "status": "building", "log"}``.

    The meta is set to ``status="building"`` BEFORE spawning, so the frontend
    (which polls on that status and disables the rebuild button while it is
    set) sees the build in progress. If spawning fails, the previous status
    is restored in the meta before the error is raised.
    """
    state_dir = Path(state_dir)
    if not repo_path:
        raise ValueError("môi trường Default không gắn repo nên không có bản đồ code")
    if codemap_mod.graphify_cli() is None:
        raise ValueError("chưa cài graphify — cài thêm bằng `pip install -e \".[codemap]\"` "
                         "rồi tạo lại bản đồ code")
    out_dir = codemap_dir(state_dir, env_id)
    lock_path, token = _acquire_lock(out_dir)
    old_meta = read_codemap_meta(state_dir, env_id)
    try:
        write_codemap_meta(state_dir, env_id, {**old_meta, "status": "building", "error": None})
    except Exception:
        _release_lock(out_dir, token=token)
        raise
    log = _log_path(state_dir, env_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    config = (Path(config_path) if config_path is not None else _find_config(state_dir))
    if config is None and kite_command is None:
        # No config, no state for the child to find: fail now, loudly, instead
        # of spawning a job that can only report "no such environment". (When the
        # caller supplies the full kite_command, it owns the invocation.)
        try:
            write_codemap_meta(state_dir, env_id, old_meta)
        except Exception:
            pass
        _release_lock(out_dir, token=token)
        raise ValueError(
            f"không tìm thấy config.yaml/config.json trong {state_dir}: "
            "job tạo bản đồ không biết state nào để dùng")
    command = list(kite_command) if kite_command is not None else _kite_command(config)
    args = [*command, "codemap", "build", env_id, "--lock-token", token]
    try:
        with log.open("w", encoding="utf-8") as stream:
            proc = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    cwd=str(Path.home()))
    except OSError as exc:
        try:  # best effort: put the previous status back before reporting
            write_codemap_meta(state_dir, env_id, old_meta)
        except Exception:
            pass
        _release_lock(out_dir, token=token)
        raise ValueError(f"không khởi động được job tạo bản đồ: {exc}") from exc
    # The lock now belongs to the child: re-record it under the child's pid so
    # a stale check sees the real worker, while the token stays the same.
    try:
        record = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("token") == token:
            record["pid"] = proc.pid
            lock_path.write_text(json.dumps(record), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass
    return {"env_id": env_id, "status": "building", "log": str(log)}


def _repo_path(state_dir: Path, env_id: str) -> str | None:
    """The repo folder of one environment, from the web store (the CLI runs
    outside the web process, so it reads the same store)."""
    from .db import Store, connect, sqlite_url

    store = Store(connect(sqlite_url(state_dir / "web.sqlite3")))
    store.migrate()
    for row in store.repo_rows():
        if str(row["id"]) == env_id:
            return row["path"]
    return None


def run_rebuild(state_dir: str | Path, env_id: str, *, lock_token: str | None = None,
                cli: list[str] | None = None) -> dict:
    """Build one environment's codemap in this process (the CLI calls this).

    Never raises for a build failure: the failure is written into
    ``meta.json`` (``status="failed"`` + ``error``) and returned. The only
    exceptions that escape are a bad ``env_id`` (caller error) and a failure
    to write the meta itself. A rebuild already running is reported as failed
    *without* touching the meta, so it cannot clobber the running job's
    ``"building"`` status.
    """
    state_dir = Path(state_dir)
    out_dir = codemap_dir(state_dir, env_id)  # raises ValueError on a bad env_id
    if is_building(state_dir, env_id) and lock_token is None:
        return {"env_id": env_id, "status": "failed",
                "error": "bản đồ code đang được tạo lại; chờ nó xong rồi thử lại"}
    repo_path = _repo_path(state_dir, env_id)
    if not repo_path:
        return {"env_id": env_id, "status": "failed",
                "error": f"không có môi trường {env_id!r} trong kho web"}
    # Build into a staging dir first: a failed rebuild keeps the old map
    # instead of leaving the environment with no map at all. ``build()``
    # reuses an existing graph.json in its out dir, so staging is also what
    # makes a rebuild fresh rather than a no-op.
    staging = out_dir / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    built = codemap_mod.build(Path(repo_path), staging, cli=cli)
    if built is None:
        shutil.rmtree(staging, ignore_errors=True)
        error = ("không tạo được bản đồ code (chưa cài graphify, hoặc graphify lỗi — "
                 "xem log job để biết chi tiết)")
        try:
            write_codemap_meta(state_dir, env_id, {"status": "failed", "built_at": None, "error": error})
        finally:
            _release_lock(out_dir, token=lock_token)
        return {"env_id": env_id, "status": "failed", "error": error}
    target = out_dir / "graphify-out"
    target.mkdir(parents=True, exist_ok=True)
    os.replace(staging / "graphify-out" / "graph.json", target / "graph.json")
    shutil.rmtree(staging, ignore_errors=True)
    result = {"env_id": env_id, "status": "ready", "built_at": _now_epoch(), "error": None,
              "graph": str(target / "graph.json")}
    write_codemap_meta(state_dir, env_id, result)
    _release_lock(out_dir, token=lock_token)
    return result
