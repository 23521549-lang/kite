from pathlib import Path
import subprocess
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from poc.kaggle_worker.workspace import RepoInspection, WorkspaceError, inspect_task_workspace
except (ModuleNotFoundError, ImportError):
    class WorkspaceError(RuntimeError):
        pass
    RepoInspection = inspect_task_workspace = None


def make_repo(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True)
    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(path), *args], check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.strip()
    git("init", "--quiet")
    git("config", "user.email", "kite-test@example.invalid")
    git("config", "user.name", "Kite Test")
    (path / "AGENTS.md").write_text("Use small changes.\n", encoding="utf-8")
    (path / "README.md").write_text("Demo repository\n", encoding="utf-8")
    (path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    git("add", ".")
    git("commit", "--quiet", "-m", "initial")
    return path, git("rev-parse", "HEAD")


def test_inspection_reads_only_orientation_files_and_saves_resume_context(tmp_path):
    from poc.kaggle_worker.checkpoint import load_checkpoint
    from poc.kaggle_worker.cli import main
    import json

    repo, head = make_repo(tmp_path / "repo")
    state = tmp_path / "state"
    task_db = state / "tasks.sqlite3"
    assert main(["--db", str(task_db), "task", "add", "--task-id", "inspect-me",
                 "--goal", "Study repo", "--repo-head", head]) == 0
    # The command writes only to a sibling worktree, not to the original checkout.
    from poc.kaggle_worker.store import SQLiteSchedulerStore
    from poc.kaggle_worker.protocol import ScheduledTask
    with SQLiteSchedulerStore(task_db) as store:
        row = store.get_task("inspect-me")
        task = ScheduledTask(
            task_id=row["task_id"], role=row["role"], model_id=row["model_id"],
            repo_head=row["repo_head"], branch=row["branch"], payload_json=row["payload_json"],
            estimated_gpu_seconds=row["estimated_gpu_seconds"], priority=row["priority"],
            created_at=row["created_at"],
        )
    from poc.kaggle_worker.workspace import prepare_task_workspace
    prepared = prepare_task_workspace(task, repo, state_dir=state)

    inspection = inspect_task_workspace(prepared.checkpoint_path)
    assert isinstance(inspection, RepoInspection)
    assert inspection.repo_head == head
    assert "AGENTS.md" in inspection.instruction_files
    assert "Use small changes." in inspection.instruction_files["AGENTS.md"]
    assert "Demo repository" in inspection.instruction_files["README.md"]
    assert "pyproject.toml" in inspection.manifest_files
    checkpoint = load_checkpoint(prepared.checkpoint_path, expected_repo_head=head)
    assert "Inspect repository instructions and relevant files" in checkpoint.completed_steps
    assert "Implement the requested change" in checkpoint.pending_steps
    assert "Use small changes." in checkpoint.last_tool_result
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "Use small changes.\n"


def test_inspection_rejects_missing_or_tampered_checkpoint(tmp_path):
    assert inspect_task_workspace is not None
    with pytest.raises(WorkspaceError, match="checkpoint"):
        inspect_task_workspace(tmp_path / "missing.json")


def test_cli_inspect_updates_checkpoint_and_returns_repo_context(tmp_path, capsys):
    import json
    from poc.kaggle_worker.cli import main

    repo, head = make_repo(tmp_path / "cli-repo")
    db = tmp_path / "state" / "tasks.sqlite3"
    assert main(["--db", str(db), "task", "add", "--task-id", "inspect-cli",
                 "--goal", "Review repository", "--repo-head", head]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "task", "prepare", "inspect-cli", "--repo", str(repo)]) == 0
    capsys.readouterr()

    result = main(["--db", str(db), "task", "inspect", "inspect-cli"] )
    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["instruction_files"]["AGENTS.md"] == "Use small changes.\n"
    assert "README.md" in output["top_level_entries"]
    assert "pyproject.toml" in output["manifest_files"]
