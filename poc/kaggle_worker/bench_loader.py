"""Load validated v1 bench tasks from disk (worker (b) — Gói 7).

This module is the thin loading layer over the frozen validator
(``bench_standard``): it never re-implements a validation rule. A task
folder is accepted exactly when ``bench_standard.load_task_file`` says so;
every failure comes back as a structured ``ValidationError`` naming the
file/field, never as a bare exception (except ``load_task_or_raise``,
which raises ``BenchTaskInvalid`` carrying the same ``.errors`` list for
callers that load exactly one task).

Layout of one task folder (see ``docs/bench-standard-v1.md`` §1)::

    <bench-root>/<task-id>/
      task.yaml          # required
      files/             # starting point for kind=code|qa|terminal
      tests/             # hidden tests (required for code|repo|terminal)
      solution.*         # optional reference fix (never shown to the agent)

``discover_tasks`` lists every task under one root and additionally
reports a duplicated ``id`` (the spec requires ids to be unique inside
one task set); the validator checks one file at a time, so uniqueness
across the set is the loader's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .bench_standard import BenchTask, BenchTaskInvalid, ValidationError, load_task_file

# A task folder is a directory whose name is the task id; only immediate
# subdirectories of the root are tasks (no recursive search: a nested
# directory is part of a task, e.g. ``files/sub/...``).


@dataclass(frozen=True)
class TaskLoadResult:
    """One task folder after loading: either a validated ``task`` or the
    structured ``errors`` that explain why it did not load."""

    task_dir: Path
    task: BenchTask | None
    errors: tuple[ValidationError, ...] = ()

    @property
    def ok(self) -> bool:
        return self.task is not None and not self.errors


def load_task_dir(task_dir: Path | str) -> TaskLoadResult:
    """Load one task folder (``task.yaml`` next to ``files/``/``tests/``).

    A missing or unreadable ``task.yaml`` is one ``missing_file`` error,
    not an exception — the caller listing many tasks needs the error, not
    a crash halfway through the list.
    """
    task_dir = Path(task_dir)
    task, errors = load_task_file(task_dir / "task.yaml")
    return TaskLoadResult(task_dir=task_dir, task=task, errors=tuple(errors))


def load_task_or_raise(task_dir: Path | str) -> BenchTask:
    """Load one task folder, raising ``BenchTaskInvalid`` (with ``.errors``)
    when it does not validate."""
    result = load_task_dir(task_dir)
    if result.task is None:
        raise BenchTaskInvalid(list(result.errors))
    return result.task


def task_dir_of(task: BenchTask) -> Path | None:
    """The folder a task was loaded from (``task.yaml``'s parent), or
    ``None`` when the task was built in memory and never loaded from disk
    — the dispatcher then needs an explicit ``task_dir``."""
    return task.source_path.parent if task.source_path is not None else None


def discover_tasks(root: Path | str) -> list[TaskLoadResult]:
    """Load every task folder directly under ``root`` (name order).

    Only subdirectories that contain a ``task.yaml`` are tasks; anything
    else (a README, a stray file) is ignored, not reported. A task ``id``
    seen twice is reported on the second occurrence with code
    ``duplicate_id`` — the spec requires ids to be unique in one set.
    """
    root = Path(root)
    results: list[TaskLoadResult] = []
    seen_ids: dict[str, Path] = {}
    if not root.is_dir():
        return results
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir() and (p / "task.yaml").is_file()):
        result = load_task_dir(task_dir)
        if result.task is not None:
            first = seen_ids.setdefault(result.task.id, task_dir)
            if first != task_dir:
                result = TaskLoadResult(
                    task_dir=task_dir, task=None,
                    errors=(ValidationError(
                        "id", "duplicate_id",
                        f"id {result.task.id!r} đã có ở {first} (id phải duy nhất trong một tập bài)"),))
        results.append(result)
    return results


def read_dir_files(dir_path: Path | str) -> dict[str, str]:
    """Every file under ``dir_path`` as ``{posix relative path: text}``.

    Used for ``files/`` (the starting point) and ``tests/`` (the hidden
    tests). A file that is not UTF-8 text raises ``ValueError`` naming
    the file — silently skipping it would change what the agent starts
    from or what the hidden tests check.
    """
    dir_path = Path(dir_path)
    out: dict[str, str] = {}
    if not dir_path.is_dir():
        return out
    for path in sorted(p for p in dir_path.rglob("*") if p.is_file()):
        try:
            out[path.relative_to(dir_path).as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"file {path} không phải văn bản UTF-8: {exc}") from exc
    return out


def read_solution_files(task_dir: Path | str) -> dict[str, str]:
    """The reference fix of a task folder, as ``{repo path: content}``.

    Convention: a file ``solution.<relpath>`` at the task root is the
    solved version of repo file ``<relpath>`` (e.g. ``solution.stats.py``
    solves ``stats.py``). Only files directly at the task root whose name
    starts with ``solution.`` are read; anything else is not a solution
    file. The solution is only ever used by Kite's own fixture checks
    (``make_fixture(..., solved=True)``) — it is never shown to the agent.
    """
    task_dir = Path(task_dir)
    out: dict[str, str] = {}
    if not task_dir.is_dir():
        return out
    for path in sorted(task_dir.iterdir()):
        if path.is_file() and path.name.startswith("solution.") and len(path.name) > len("solution."):
            try:
                out[path.name[len("solution."):]] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"file {path} không phải văn bản UTF-8: {exc}") from exc
    return out
