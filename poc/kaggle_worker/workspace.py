"""Prepare isolated Git worktrees and durable initial task checkpoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from .checkpoint import CheckpointError, TaskCheckpoint, load_checkpoint, save_checkpoint
from .protocol import ScheduledTask


class WorkspaceError(RuntimeError):
    """The requested task workspace cannot be prepared safely."""


@dataclass(frozen=True)
class PreparedWorkspace:
    task_id: str
    repo_path: Path
    workspace_path: Path
    checkpoint_path: Path
    branch: str
    repo_head: str


@dataclass(frozen=True)
class RepoInspection:
    task_id: str
    repo_path: str
    branch: str
    repo_head: str
    current_status: tuple[str, ...]
    top_level_entries: tuple[str, ...]
    manifest_files: tuple[str, ...]
    instruction_files: dict[str, str]


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def checkpoint_path_for_task(state_dir: Path, task_id: str) -> Path:
    if not _SAFE_TASK_ID.fullmatch(task_id):
        raise WorkspaceError("task_id must contain only letters, numbers, dot, underscore, or dash")
    return Path(state_dir).expanduser().resolve() / "checkpoints" / f"{task_id}.json"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise WorkspaceError(f"could not run Git: {error}") from error
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorkspaceError(f"Git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _contract(task: ScheduledTask) -> str:
    payload = json.loads(task.payload_json)
    goal = payload.get("goal")
    return goal if isinstance(goal, str) and goal.strip() else task.payload_json


def _existing_workspace(
    task: ScheduledTask, repo_root: Path, workspace_path: Path, checkpoint_path: Path
) -> PreparedWorkspace | None:
    if not checkpoint_path.exists():
        if workspace_path.exists():
            raise WorkspaceError(
                f"workspace exists without a checkpoint: {workspace_path}; inspect it before recovery"
            )
        return None
    try:
        checkpoint = load_checkpoint(checkpoint_path, expected_repo_head=task.repo_head)
    except CheckpointError as error:
        raise WorkspaceError(f"existing checkpoint is invalid: {error}") from error
    if (checkpoint.task_id != task.task_id or checkpoint.branch != task.branch
            or Path(checkpoint.repo_path).resolve() != workspace_path):
        raise WorkspaceError("existing checkpoint does not match this task/worktree")
    if not workspace_path.is_dir():
        raise WorkspaceError(f"checkpoint workspace is missing: {workspace_path}")
    actual_root = Path(_git(workspace_path, "rev-parse", "--show-toplevel")).resolve()
    actual_branch = _git(workspace_path, "branch", "--show-current")
    if actual_root != workspace_path or actual_branch != task.branch:
        raise WorkspaceError("existing Git worktree root or branch does not match checkpoint")
    return PreparedWorkspace(
        task.task_id, repo_root, workspace_path, checkpoint_path, task.branch, task.repo_head
    )


def prepare_task_workspace(
    task: ScheduledTask, repo_path: Path, *, state_dir: Path
) -> PreparedWorkspace:
    """Create a sibling worktree from the task's pinned HEAD and save its checkpoint.

    The source checkout must be clean and currently resolve to the pinned HEAD.
    Preparation is idempotent only when an existing checkpoint and worktree match
    the task exactly. This function never modifies source files or contacts a model.
    """
    if not _SAFE_TASK_ID.fullmatch(task.task_id):
        raise WorkspaceError("task_id must contain only letters, numbers, dot, underscore, or dash")
    repo_input = Path(repo_path).expanduser().resolve()
    if not repo_input.exists() or not repo_input.is_dir():
        raise WorkspaceError(f"repository path is not a directory: {repo_input}")
    try:
        repo_root = Path(_git(repo_input, "rev-parse", "--show-toplevel")).resolve()
    except WorkspaceError as error:
        raise WorkspaceError(f"not a Git repository: {repo_input}") from error

    workspace_parent = repo_root.parent / f".{repo_root.name}-kite-worktrees"
    workspace_path = (workspace_parent / task.task_id).resolve()
    checkpoint_path = checkpoint_path_for_task(state_dir, task.task_id)
    existing = _existing_workspace(task, repo_root, workspace_path, checkpoint_path)
    if existing is not None:
        return existing

    if _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise WorkspaceError("source repository must be clean before workspace preparation")
    resolved_head = _git(repo_root, "rev-parse", "--verify", f"{task.repo_head}^{{commit}}", check=False)
    if not resolved_head or resolved_head != _git(repo_root, "rev-parse", "HEAD"):
        raise WorkspaceError(
            f"task HEAD {task.repo_head!r} does not match current repository HEAD"
        )
    try:
        branch_check = subprocess.run(
            ["git", "-C", str(repo_root), "check-ref-format", "--branch", task.branch],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError as error:
        raise WorkspaceError(f"could not validate task branch: {error}") from error
    if branch_check.returncode != 0:
        raise WorkspaceError(f"invalid task branch name: {task.branch}")

    branch_exists = subprocess.run(
        ["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", f"refs/heads/{task.branch}"],
        check=False,
    ).returncode == 0
    if branch_exists:
        raise WorkspaceError(f"task branch already exists: {task.branch}")
    if workspace_path.exists():
        raise WorkspaceError(f"task workspace path already exists: {workspace_path}")

    workspace_parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "worktree", "add", "-b", task.branch, str(workspace_path), resolved_head)
    checkpoint = TaskCheckpoint(
        task_id=task.task_id,
        task_contract=_contract(task),
        plan=["Inspect repository instructions and relevant files", "Implement the requested change",
              "Run checks and prepare a patch for review"],
        completed_steps=[],
        pending_steps=["Inspect repository instructions and relevant files", "Implement the requested change",
                       "Run checks and prepare a patch for review"],
        repo_path=str(workspace_path),
        branch=task.branch,
        repo_head=task.repo_head,
        changed_files=[],
        last_tool_result="Isolated task workspace prepared at the pinned repository HEAD.",
        model_id=task.model_id or "unassigned",
        provider_id="not-configured",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        save_checkpoint(checkpoint, checkpoint_path)
    except CheckpointError:
        _git(repo_root, "worktree", "remove", "--force", str(workspace_path), check=False)
        raise
    return PreparedWorkspace(
        task.task_id, repo_root, workspace_path, checkpoint_path, task.branch, task.repo_head
    )


def inspect_task_workspace(checkpoint_path: Path) -> RepoInspection:
    """Read repository orientation files and save a compact resume context."""
    try:
        checkpoint = load_checkpoint(Path(checkpoint_path))
    except CheckpointError as error:
        raise WorkspaceError(f"checkpoint cannot be loaded: {error}") from error
    workspace_path = Path(checkpoint.repo_path).resolve()
    if not workspace_path.is_dir():
        raise WorkspaceError(f"checkpoint workspace is missing: {workspace_path}")
    actual_root = Path(_git(workspace_path, "rev-parse", "--show-toplevel")).resolve()
    if actual_root != workspace_path:
        raise WorkspaceError("checkpoint path is not the root of its Git worktree")
    if _git(workspace_path, "branch", "--show-current") != checkpoint.branch:
        raise WorkspaceError("worktree branch does not match checkpoint")

    status_text = _git(workspace_path, "status", "--porcelain=v1", "--untracked-files=all")
    status = tuple(status_text.splitlines()) if status_text else ()
    entries = sorted(entry.name for entry in workspace_path.iterdir() if entry.name != ".git")
    manifests = (
        "pyproject.toml", "requirements.txt", "setup.cfg", "package.json", "Cargo.toml",
        "go.mod", "pom.xml", "build.gradle",
    )
    manifest_files = tuple(name for name in manifests if (workspace_path / name).is_file())
    instruction_files: dict[str, str] = {}
    for name in ("AGENTS.md", "README.md"):
        source = workspace_path / name
        if source.is_file():
            content = source.read_text(encoding="utf-8", errors="replace")
            if len(content) > 12000:
                content = content[:12000] + "\n[truncated at 12000 characters]"
            instruction_files[name] = content

    inspection = RepoInspection(
        task_id=checkpoint.task_id,
        repo_path=str(workspace_path),
        branch=checkpoint.branch,
        repo_head=checkpoint.repo_head,
        current_status=status,
        top_level_entries=tuple(entries[:100]),
        manifest_files=manifest_files,
        instruction_files=instruction_files,
    )
    completed = list(checkpoint.completed_steps)
    step = "Inspect repository instructions and relevant files"
    if step not in completed:
        completed.append(step)
    pending = [item for item in checkpoint.pending_steps if item != step]
    updated = replace(
        checkpoint,
        completed_steps=completed,
        pending_steps=pending,
        last_tool_result=json.dumps(asdict(inspection), ensure_ascii=False, sort_keys=True),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    save_checkpoint(updated, Path(checkpoint_path))
    return inspection
