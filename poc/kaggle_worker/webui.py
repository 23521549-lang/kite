"""Build the web front end (web/, React + Vite) into web/dist, which ``kite web serve`` serves.

Node.js comes from the ``ui`` extra (the nodejs-wheel package puts ``node`` and ``npm`` in Kite's own
venv), so no system package is needed; a ``npm`` on PATH works too. ``kite update`` calls
``build_if_needed`` after the tests pass: it rebuilds only when the front end's sources changed (their
hash is kept in dist/.kite-source), installs packages only when package-lock.json changed, and swaps the
new build in at the end so the page never disappears half-built. A failed build leaves the last good one.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
SOURCES = ("package.json", "package-lock.json", "index.html", "vite.config.ts", "tsconfig.json", "src", "public")
STAMP = ".kite-source"
Runner = Callable[..., subprocess.CompletedProcess]


def source_hash(web: Path) -> str:
    """One hash over the front end's sources (not node_modules, not dist)."""
    digest = hashlib.sha256()
    for name in SOURCES:
        root = web / name
        files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else [root] if root.is_file() else []
        for path in files:
            digest.update(path.relative_to(web).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def built_hash(web: Path) -> str | None:
    try:
        return (web / "dist" / STAMP).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def find_npm(python: str = sys.executable) -> str | None:
    """npm from Kite's venv (the ``ui`` extra), else from PATH."""
    venv_npm = Path(python).parent / "npm"
    if venv_npm.is_file() and os.access(venv_npm, os.X_OK):
        return str(venv_npm)
    return shutil.which("npm")


def status(web: Path | None = None) -> dict:
    web = web or REPO / "web"
    if not (web / "package.json").is_file():
        return {"present": False}
    current, built = source_hash(web), built_hash(web)
    return {"present": True, "built": built is not None, "current": built == current, "source": current,
            "dist": str(web / "dist")}


def build(web: Path | None = None, *, python: str = sys.executable, runner: Runner = subprocess.run,
          force: bool = False) -> dict:
    """Build web/ into web/dist when its sources changed (or ``force``)."""
    web = web or REPO / "web"
    if not (web / "package.json").is_file():
        return {"ui": "không có thư mục web/"}
    current = source_hash(web)
    if not force and built_hash(web) == current:
        return {"ui": "giao diện đã là bản mới nhất", "source": current}
    npm = find_npm(python)
    if npm is None:
        return {"ui_error": "chưa có Node.js để build giao diện: cài bằng "
                            "uv pip install --python .venv/bin/python -e '.[ui]' (hoặc cài nodejs >= 20.19)"}
    env = dict(os.environ)
    # The ui extra's node sits next to npm: Vite's and tsc's `#!/usr/bin/env node` must find it.
    env["PATH"] = os.pathsep.join([str(Path(npm).parent), env.get("PATH", "")])
    env.pop("NODE_OPTIONS", None)
    env["npm_config_update_notifier"] = "false"
    lock = web / "package-lock.json"
    installed = web / "node_modules" / ".kite-lock"
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()[:16] if lock.is_file() else ""
    report: dict = {"source": current}
    try:
        stale = not installed.is_file() or installed.read_text(encoding="utf-8").strip() != lock_hash
    except OSError:
        stale = True
    if stale:
        done = runner([npm, "ci", "--no-audit", "--no-fund", "--loglevel=error"], cwd=str(web), env=env,
                      capture_output=True, text=True, timeout=1200)
        if done.returncode != 0:
            return {**report, "ui_error": "npm ci lỗi: " + (done.stderr or done.stdout).strip()[-600:]}
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text(lock_hash + "\n", encoding="utf-8")
        report["installed"] = True
    out = web / ".dist-new"
    shutil.rmtree(out, ignore_errors=True)
    env["KITE_WEB_OUT"] = str(out)
    done = runner([npm, "run", "build", "--silent"], cwd=str(web), env=env, capture_output=True, text=True,
                  timeout=1200)
    if done.returncode != 0 or not (out / "index.html").is_file():
        shutil.rmtree(out, ignore_errors=True)
        return {**report, "ui_error": "build giao diện lỗi (giữ bản cũ): " + (done.stdout + done.stderr).strip()[-800:]}
    (out / STAMP).write_text(current + "\n", encoding="utf-8")
    dist, old = web / "dist", web / ".dist-old"
    shutil.rmtree(old, ignore_errors=True)
    if dist.exists():
        dist.rename(old)
    out.rename(dist)
    shutil.rmtree(old, ignore_errors=True)
    return {**report, "ui": "đã build giao diện mới", "dist": str(dist)}
