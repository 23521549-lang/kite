"""Free disk space taken by old task folders, keeping what each task left as evidence.

Every task, ``kite sandbox run`` and bench run keeps a full clone of the repository (``work``), its
installed libraries (``deps``) and the code map (``graph``) next to the small files that say what
happened (``result.json``, ``result.patch``, ``agent.log``, ``test.log``, ``trace.jsonl``,
``results.json``, ``README.md``). The big folders are only needed while the task runs or while it
can still be continued (``send_instruction`` works on the same clone), so after ``days`` without any
change they can go; the small files stay. ``deps`` linked to the shared library cache is only a
link: removing it never touches the cache. The library cache itself (``deps-cache``) is removed only
when asked, entry by entry, when no task has used it for ``days``.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import time

HEAVY = ("work", "deps", "graph")


@dataclass(frozen=True)
class Item:
    path: Path
    kind: str            # task | sandbox | bench | bench-repos | deps-cache
    size: int            # bytes freed by removing it (0 for a link)
    age_days: float


def tree_size(path: Path) -> int:
    """Bytes under ``path`` without following links (a link itself counts as 0)."""
    path = Path(path)
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.lstat().st_size
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def last_change(folder: Path) -> float:
    """Newest modification time of ``folder`` and the files and folders directly in it (cheap: the
    logs and results a running task keeps writing are there)."""
    times = [folder.lstat().st_mtime]
    for entry in folder.iterdir():
        try:
            times.append(entry.lstat().st_mtime)
        except OSError:
            pass
    return max(times)


def plan(state_dir: Path, *, days: float = 7, now: float | None = None, deps_cache: bool = False) -> list[Item]:
    """What ``cleanup`` would remove under the Kite state folder, biggest first."""
    state_dir, now = Path(state_dir), time.time() if now is None else now
    limit = days * 86400
    items: list[Item] = []

    def heavy_of(run: Path, kind: str) -> None:
        if not run.is_dir() or run.is_symlink():
            return
        age = now - last_change(run)
        if age < limit:
            return
        for name in HEAVY:
            target = run / name
            if target.is_symlink() or target.exists():
                items.append(Item(target, kind, tree_size(target), age / 86400))

    for kind, parent in (("task", state_dir / "tasks"), ("sandbox", state_dir / "sandbox")):
        if parent.is_dir():
            for run in sorted(parent.iterdir()):
                heavy_of(run, kind)
    bench = state_dir / "bench"
    if bench.is_dir():
        for stamp in sorted(p for p in bench.iterdir() if p.is_dir()):
            if (stamp / "runs").is_dir():
                for run in sorted((stamp / "runs").iterdir()):
                    heavy_of(run, "bench")
            repos = stamp / "repos"
            if repos.is_dir() and now - last_change(stamp) >= limit:
                items.append(Item(repos, "bench-repos", tree_size(repos), (now - last_change(stamp)) / 86400))
    cache = state_dir / "deps-cache"
    if deps_cache and cache.is_dir():
        # An entry stays while a task that is kept still links to it.
        going = {item.path for item in items}
        links = [*state_dir.glob("tasks/*/deps"), *state_dir.glob("sandbox/*/deps"),
                 *state_dir.glob("bench/*/runs/*/deps")]
        kept = {Path(os.path.realpath(link)) for link in links if link.is_symlink() and link not in going}
        for entry in sorted(p for p in cache.iterdir() if p.is_dir() and not p.name.startswith(".")):
            age = now - entry.lstat().st_mtime
            if age >= limit and entry.resolve() not in kept:
                items.append(Item(entry, "deps-cache", tree_size(entry), age / 86400))
    return sorted(items, key=lambda item: item.size, reverse=True)


def _writable_then_retry(function, path, _info) -> None:
    # Packages and Git objects are often read-only: make the entry (and its folder) writable, retry.
    for target in (os.path.dirname(path), path):
        try:
            os.chmod(target, os.lstat(target).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
    function(path)


def remove(items: list[Item]) -> tuple[int, list[str]]:
    """Remove the planned items; returns (bytes freed, errors)."""
    freed, errors = 0, []
    for item in items:
        try:
            if item.path.is_symlink() or item.path.is_file():
                item.path.unlink()
            elif item.path.exists():
                shutil.rmtree(item.path, onerror=_writable_then_retry)
            freed += item.size
        except OSError as exc:
            errors.append(f"{item.path}: {exc}")
    return freed, errors


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"
