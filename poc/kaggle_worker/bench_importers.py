"""Import external bench tasks into Kite v1 task folders (worker (b) — Gói 7).

Two importers, one per source. Both write a task folder that the frozen
validator (``bench_standard``) accepts, and both **skip with an explicit
reason** when the source cannot be expressed in v1 — nothing is silently
dropped, nothing is silently invented. Every skip is an ``ImportResult``
with ``skipped=True`` and a ``reason`` naming the missing requirement.

Field maps (the contract — tests assert each line of these tables):

Terminal-Bench (``import_terminal_bench``; source layout verified against
the Terminal-Bench task docs, 2026-11-25: ``task.yaml`` + ``Dockerfile`` +
``run-tests.sh`` + ``tests/test_outputs.py`` + ``solution.sh``)::

    TB directory name            -> Kite ``id`` (the TB id IS the dir name)
    TB ``instruction``           -> Kite ``goal``
    TB ``difficulty``            -> Kite ``difficulty`` (verbatim; missing -> "unknown")
    TB ``max_agent_timeout_sec`` -> Kite ``timeouts.agent``
    TB ``max_test_timeout_sec``  -> Kite ``timeouts.test``
    TB ``Dockerfile``            -> copied verbatim; Kite ``environment.dockerfile``
    TB ``tests/``                -> copied VERBATIM to Kite ``tests/`` (hidden tests)
    TB ``run-tests.sh``          -> copied verbatim (reference; how TB ran the tests)
    TB ``docker-compose.yaml``   -> copied verbatim when present (reference)
    TB ``solution.sh``/``.yaml`` -> copied to ``tb-solution/`` (reference ONLY)
    TB ``category``/``tags``/``author_*``/``parser_name``/estimates
                                 -> Kite ``terminal_bench:`` provenance block
                                 (extra top-level key; the validator ignores it)

    NOT mapped:
    - ``solution.sh`` is a *script* that produces the fix, not a file patch,
      so it does NOT become Kite ``solution.*`` (which holds repo file
      contents for ``make_fixture(..., solved=True)``).
    - No TB field maps to Kite ``expected_files`` (the TB convention "a file
      named in both the tests and the solution is an expected output" is a
      heuristic, not a field), so imported TB tasks score only the
      ``Coding`` axis. Adding ``Đúng việc`` needs the task author to add
      ``expected_files`` by hand.
    - The TB starting state lives in the Docker image, not in files: the
      imported task gets an EMPTY ``files/`` directory (the validator only
      needs the directory to exist) and the run driver must build the
      starting state from the copied ``Dockerfile`` (worker (c) territory).

    Skipped (with reason) when: no ``task.yaml`` / not a mapping / no
    ``instruction`` / ``parser_name`` is not ``pytest`` / no ``tests/`` /
    no ``Dockerfile`` / a timeout is present but not a positive number /
    the produced task fails v1 validation / the destination already exists.

SWE-bench (``import_swebench``; one instance dict from the dataset)::

    instance ``instance_id``     -> Kite ``id``
    instance ``problem_statement``-> Kite ``goal``
    instance ``repo``            -> Kite ``repo``
    instance ``base_commit``     -> Kite ``base_commit``
    gold ``patch``: files it touches that are NOT tests
                                 -> Kite ``expected_files`` (+ axis ``Đúng việc``)
    gold ``patch``: touches no non-test file
                                 -> axes are ``["Coding"]`` only
    ``test_patch`` applied to a checkout of the repo at ``base_commit``
                                 -> Kite ``tests/`` (hidden tests)
    instance ``FAIL_TO_PASS`` / ``PASS_TO_PASS``
                                 -> Kite ``swe_bench:`` provenance block
    gold ``patch`` / ``test_patch`` -> kept as ``swe_bench_patch.diff`` /
                                 ``swe_bench_test_patch.diff`` (provenance)
    caller-supplied ``timeouts`` -> Kite ``timeouts`` (SWE-bench carries
                                 no timeouts; the caller decides — there is
                                 NO hidden default)
    SWE-bench carries no difficulty -> Kite ``difficulty`` is the explicit
                                 marker ``SWE_DIFFICULTY_UNRATED``
                                 ("chưa xếp hạng"), never a fake level

    NOT mapped / limits (documented, not silent):
    - Python-centric: SWE-bench is a Python benchmark; the test-file
      heuristic (``_is_test_path``) only knows Python test layouts.
    - Only the ``Coding`` / ``Đúng việc`` axes: a SWE-bench instance says
      nothing about speed, memory, cost or stability.
    - Per-repo environment setup is NOT automated: SWE-bench keeps install
      commands per repository, not per instance, so the imported task has
      an empty ``environment:`` and the task author must add the repo's
      setup. (This is why SWE-bench tasks are heavy to run.)
    - ``RepoTask.fix`` has no source in SWE-bench (the dataset has no fix
      commit SHA); the dispatcher (``bench.py``) therefore treats the
      folder's ``tests/`` as the hidden tests and documents the placeholder
      it uses for ``fix`` — see ``dispatch_v1_task``.
    - ``test_patch`` is a diff, not file contents: materialising ``tests/``
      needs a checkout of the repo at ``base_commit`` (``repo_checkout``).
      Without one the import is skipped with a reason, not guessed.

    Skipped (with reason) when: a required instance field is missing /
    empty / ``test_patch`` touches no file / ``repo_checkout`` is missing /
    the checkout is not a git repo at ``base_commit`` / ``git apply`` of
    the test patch fails / the produced task fails v1 validation / the
    destination already exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shutil
import subprocess
from pathlib import Path
import tempfile

import yaml

from .bench_standard import load_task_file, validate_task_data

# --- shared -----------------------------------------------------------------

TB_SOURCE = "terminal-bench"
SWE_SOURCE = "swe-bench"

# Terminal-Bench's own documented timeout defaults (task docs: 360.0 /
# 60.0). These are the *source's* defaults, used only when the TB task
# omits the field, and the import records a warning when it uses one.
TB_DEFAULT_AGENT_TIMEOUT_SEC = 360.0
TB_DEFAULT_TEST_TIMEOUT_SEC = 60.0
# The only TB result parser Kite's hidden-test runner (pytest) can run.
TB_SUPPORTED_PARSERS: tuple[str, ...] = ("pytest",)
# Imported TB tasks score this axis; see the module docstring for why no
# other axis is claimed.
TB_AXES: tuple[str, ...] = ("Coding",)
# SWE-bench carries no difficulty; this value says so explicitly instead
# of inventing a level.
SWE_DIFFICULTY_UNRATED = "chưa xếp hạng"


@dataclass(frozen=True)
class ImportResult:
    """The outcome of one import: either an imported task folder
    (``ok``) or a skip with an explicit ``reason``."""

    task_id: str | None
    dest: Path | None
    source: str
    skipped: bool = False
    reason: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.skipped and self.task_id is not None and self.dest is not None


class _SkipImport(Exception):
    """Internal: the source cannot be expressed in v1 — carry the reason out."""


def _write_task_yaml(dest: Path, data: dict) -> None:
    (dest / "task.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _validate_or_skip(data: dict, *, task_id: str) -> None:
    """The produced task must pass v1 validation, or the import is a skip
    (the source cannot be expressed in v1) — never a half-written task."""
    errors = validate_task_data(data)
    if errors:
        raise _SkipImport(
            f"bài {task_id!r} sau khi nhập không đạt chuẩn v1: "
            + "; ".join(f"{e.field}: {e.message}" for e in errors))


def _check_dest(dest_root: Path, task_id: str) -> Path:
    dest = Path(dest_root) / task_id
    if dest.exists():
        raise _SkipImport(f"thư mục đích {dest} đã tồn tại (không ghi đè)")
    return dest


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def _diff_files(patch_text: str) -> list[str]:
    """Files a unified diff touches, from its ``+++ b/<path>`` lines.

    ``/dev/null`` targets (deletions) carry no new content and are left
    out — there is nothing to copy for them.
    """
    files: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[len("+++ b/"):].split("\t")[0].strip().strip('"')
        if path and path != "/dev/null" and path not in files:
            files.append(path)
    return files


def _is_test_path(path: str) -> bool:
    """Heuristic for "this diff file is a test" (Python-centric, see the
    module docstring): ``test_*.py`` / ``*_test.py`` / anything under a
    ``tests/`` or ``test/`` directory."""
    name = path.rsplit("/", 1)[-1]
    return (name.startswith("test_") or name.endswith("_test.py")
            or "/tests/" in f"/{path}" or "/test/" in f"/{path}")


# --- Terminal-Bench ----------------------------------------------------------


def _tb_positive_number(value: object, default: float, *, field_name: str,
                        warnings: list[str]) -> float:
    if value is None:
        warnings.append(f"TB thiếu {field_name}, dùng mặc định của Terminal-Bench ({default}s)")
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _SkipImport(f"TB {field_name} phải là số dương (giây), đang là {value!r}")
    return value


def import_terminal_bench(src_dir: Path | str, dest_root: Path | str, *,
                          task_id: str | None = None) -> ImportResult:
    """Import one Terminal-Bench task folder into a Kite v1 task folder.

    The TB task id is the source directory's name (Terminal-Bench has no
    id field in ``task.yaml``); pass ``task_id`` to override it.
    """
    src = Path(src_dir)
    task_id = task_id or src.name
    warnings: list[str] = []
    try:
        task_path = src / "task.yaml"
        if not task_path.is_file():
            raise _SkipImport(f"thiếu task.yaml trong {src} (không phải thư mục bài Terminal-Bench)")
        try:
            data = yaml.safe_load(task_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise _SkipImport(f"task.yaml lỗi cú pháp YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise _SkipImport("task.yaml của Terminal-Bench phải là một mapping YAML")
        instruction = data.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise _SkipImport("thiếu instruction (mô tả bài) trong task.yaml")
        parser = data.get("parser_name", "pytest")
        if parser not in TB_SUPPORTED_PARSERS:
            raise _SkipImport(
                f"parser_name {parser!r} không nhập được: Kite chỉ chạy test ẩn bằng pytest, "
                f"hỗ trợ {list(TB_SUPPORTED_PARSERS)}")
        tests_dir = src / "tests"
        if not tests_dir.is_dir() or not any(p.is_file() for p in tests_dir.rglob("*")):
            raise _SkipImport("thiếu tests/ (test ẩn) trong bài Terminal-Bench")
        if not (src / "Dockerfile").is_file():
            raise _SkipImport(
                "thiếu Dockerfile: trạng thái bắt đầu của bài Terminal-Bench nằm trong image, "
                "không có Dockerfile thì không tái tạo được điểm bắt đầu")
        difficulty = data.get("difficulty")
        if not isinstance(difficulty, str) or not difficulty.strip():
            difficulty = "unknown"
            warnings.append("TB thiếu difficulty, dùng 'unknown'")
        agent_timeout = _tb_positive_number(
            data.get("max_agent_timeout_sec"), TB_DEFAULT_AGENT_TIMEOUT_SEC,
            field_name="max_agent_timeout_sec", warnings=warnings)
        test_timeout = _tb_positive_number(
            data.get("max_test_timeout_sec"), TB_DEFAULT_TEST_TIMEOUT_SEC,
            field_name="max_test_timeout_sec", warnings=warnings)

        provenance: dict = {"task": src.name, "parser_name": parser}
        for key in ("category", "tags", "author_name", "author_email",
                    "run_tests_in_same_shell", "expert_time_estimate_min",
                    "junior_time_estimate_min"):
            if data.get(key) is not None:
                provenance[key] = data[key]
        kite_data: dict = {
            "id": task_id,
            "goal": instruction.strip(),
            "kind": "terminal",
            "language": "en",
            "axes": list(TB_AXES),
            "difficulty": difficulty,
            "timeouts": {"agent": agent_timeout, "test": test_timeout},
            "environment": {"dockerfile": "Dockerfile"},
            "terminal_bench": provenance,
        }
        _validate_or_skip(kite_data, task_id=task_id)
        dest = _check_dest(Path(dest_root), task_id)
        dest.mkdir(parents=True)
        _write_task_yaml(dest, kite_data)
        (dest / "files").mkdir()  # empty: the TB starting state is the Docker image
        for name in ("Dockerfile", "run-tests.sh", "docker-compose.yaml"):
            if (src / name).is_file():
                _copy_file(src / name, dest / name)
        for path in sorted(p for p in tests_dir.rglob("*") if p.is_file()):
            _copy_file(path, dest / "tests" / path.relative_to(tests_dir))
        for name in ("solution.sh", "solution.yaml"):
            if (src / name).is_file():
                _copy_file(src / name, dest / "tb-solution" / name)
        task, errors = load_task_file(dest / "task.yaml")
        if task is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise _SkipImport(
                "thư mục bài sau khi nhập không nạp được: "
                + "; ".join(f"{e.field}: {e.message}" for e in errors))
        return ImportResult(task_id=task_id, dest=dest, source=TB_SOURCE,
                            warnings=tuple(warnings))
    except _SkipImport as exc:
        return ImportResult(task_id=task_id, dest=None, source=TB_SOURCE,
                            skipped=True, reason=str(exc), warnings=tuple(warnings))


# --- SWE-bench ---------------------------------------------------------------


def _materialize_test_patch(*, repo_checkout: Path, base_commit: str,
                            test_patch: str, dest_tests_dir: Path) -> list[str]:
    """Apply ``test_patch`` in a throwaway worktree of ``repo_checkout`` at
    ``base_commit`` and copy the touched files into ``dest_tests_dir``.

    A worktree (not a copy) is used so a big repository costs no extra
    disk beyond the patch itself; the worktree is removed afterwards.
    Returns the touched file paths (relative). Raises ``_SkipImport``
    when the checkout is not a usable git repo or the patch does not apply.
    """
    files = _diff_files(test_patch)
    if not files:
        raise _SkipImport("test_patch không chạm file nào (không có test ẩn để nhập)")
    tmp = Path(tempfile.mkdtemp(prefix="kite-swe-"))
    worktree = tmp / "wt"
    patch_file = tmp / "test_patch.diff"
    patch_file.write_text(test_patch, encoding="utf-8")
    try:
        add = subprocess.run(["git", "-C", str(repo_checkout), "worktree", "add", "--detach",
                              str(worktree), base_commit],
                             capture_output=True, text=True)
        if add.returncode != 0:
            raise _SkipImport(
                f"không tạo được worktree của {repo_checkout} ở {base_commit}: "
                f"{(add.stderr or add.stdout).strip()[-500:]}")
        applied = subprocess.run(["git", "-C", str(worktree), "apply", str(patch_file)],
                                 capture_output=True, text=True)
        if applied.returncode != 0:
            raise _SkipImport(
                "không áp được test_patch lên checkout ở base_commit: "
                f"{(applied.stderr or applied.stdout).strip()[-500:]}")
        for rel in files:
            source = worktree / rel
            if source.is_file():
                _copy_file(source, dest_tests_dir / rel)
        return files
    finally:
        subprocess.run(["git", "-C", str(repo_checkout), "worktree", "remove", "--force",
                        str(worktree)], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def swebench_to_task_data(instance: dict, *, timeouts: dict,
                          expected_files: list[str]) -> dict:
    """The pure instance -> ``task.yaml`` mapping (no filesystem access).

    ``expected_files`` comes from the gold patch (non-test files it
    touches); the caller computes it with ``_diff_files`` + ``_is_test_path``.
    """
    axes = ["Coding", "Đúng việc"] if expected_files else ["Coding"]
    provenance: dict = {"instance_id": instance["instance_id"], "repo": instance["repo"]}
    for key in ("version", "FAIL_TO_PASS", "PASS_TO_PASS"):
        if instance.get(key) is not None:
            provenance[key] = instance[key]
    data: dict = {
        "id": instance["instance_id"],
        "goal": instance["problem_statement"].strip(),
        "kind": "repo",
        "language": "en",
        "axes": axes,
        "difficulty": SWE_DIFFICULTY_UNRATED,
        "timeouts": dict(timeouts),
        "environment": {},
        "repo": instance["repo"],
        "base_commit": instance["base_commit"],
        "swe_bench": provenance,
    }
    if expected_files:
        data["expected_files"] = list(expected_files)
    return data


def import_swebench(instance: dict, dest_root: Path | str, *,
                    timeouts: dict, repo_checkout: Path | str | None = None) -> ImportResult:
    """Import one SWE-bench instance dict into a Kite v1 task folder.

    ``timeouts`` is required (SWE-bench carries no timeouts — the caller
    decides; there is no hidden default). ``repo_checkout`` is a local
    checkout of the instance's repo: without it the test patch cannot be
    materialised into ``tests/`` and the import is skipped with a reason.
    """
    if not isinstance(instance, dict):
        raise ValueError(f"instance SWE-bench phải là mapping, đang là {type(instance).__name__}")
    if not isinstance(timeouts, dict) or not timeouts or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0
            for v in timeouts.values()):
        raise ValueError("timeouts phải là mapping không rỗng, mọi giá trị là số giây dương")
    task_id = instance.get("instance_id") if isinstance(instance.get("instance_id"), str) else None
    warnings: list[str] = []
    try:
        for name in ("repo", "instance_id", "base_commit", "problem_statement", "patch", "test_patch"):
            value = instance.get(name)
            if not isinstance(value, str) or not value.strip():
                raise _SkipImport(f"instance SWE-bench thiếu trường {name!r} (rỗng hoặc không phải chuỗi)")
        if repo_checkout is None:
            raise _SkipImport(
                "cần checkout của repo ở base_commit để giải test_patch thành file test "
                "(test_patch chỉ là diff, không chứa đủ nội dung file); chưa có checkout nên chưa nhập được")
        gold_files = _diff_files(instance["patch"])
        if not gold_files:
            raise _SkipImport("patch (gold) rỗng: không biết bản sửa chạm file nào")
        expected_files = [p for p in gold_files if not _is_test_path(p)]
        if not expected_files:
            warnings.append("patch chỉ chạm file test nên bài chỉ chấm trục Coding")
        warnings.append("environment trống: setup theo repo chưa tự động, tác giả bài phải thêm setup của repo")
        kite_data = swebench_to_task_data(instance, timeouts=timeouts,
                                          expected_files=expected_files)
        _validate_or_skip(kite_data, task_id=task_id or "?")
        dest = _check_dest(Path(dest_root), task_id)
        dest.mkdir(parents=True)
        (dest / "swe_bench_patch.diff").write_text(instance["patch"], encoding="utf-8")
        (dest / "swe_bench_test_patch.diff").write_text(instance["test_patch"], encoding="utf-8")
        hidden = _materialize_test_patch(repo_checkout=Path(repo_checkout),
                                         base_commit=instance["base_commit"],
                                         test_patch=instance["test_patch"],
                                         dest_tests_dir=dest / "tests")
        if not hidden:
            shutil.rmtree(dest, ignore_errors=True)
            raise _SkipImport("test_patch không cho ra file test nào trong tests/")
        _write_task_yaml(dest, kite_data)
        task, errors = load_task_file(dest / "task.yaml")
        if task is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise _SkipImport(
                "thư mục bài sau khi nhập không nạp được: "
                + "; ".join(f"{e.field}: {e.message}" for e in errors))
        return ImportResult(task_id=task_id, dest=dest, source=SWE_SOURCE,
                            warnings=tuple(warnings))
    except _SkipImport as exc:
        return ImportResult(task_id=task_id, dest=None, source=SWE_SOURCE,
                            skipped=True, reason=str(exc), warnings=tuple(warnings))
