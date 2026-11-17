from pathlib import Path
import subprocess
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from poc.kaggle_worker.protocol import ScheduledTask
try:
    from poc.kaggle_worker.workspace import WorkspaceError, prepare_task_workspace
except ModuleNotFoundError:
    class WorkspaceError(RuntimeError):
        pass
    prepare_task_workspace = None


def make_repo(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True)
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *args], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout.strip()
    git("init", "--quiet")
    git("config", "user.email", "kite-test@example.invalid")
    git("config", "user.name", "Kite Test")
    (path / "README.md").write_text("starter\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "--quiet", "-m", "initial")
    return path, git("rev-parse", "HEAD")


def task(head: str, **changes) -> ScheduledTask:
    data = dict(
        task_id="task-workspace", role="coder", model_id=None, repo_head=head,
        branch="agent/task-workspace", payload_json='{"goal":"Add a feature"}',
        estimated_gpu_seconds=120, priority=0, created_at=1.0,
    )
    data.update(changes)
    return ScheduledTask(**data)


def prepare(*args, **kwargs):
    assert prepare_task_workspace is not None, "workspace preparation has not been implemented"
    return prepare_task_workspace(*args, **kwargs)


def test_prepare_creates_isolated_branch_and_initial_checkpoint(tmp_path):
    repo, head = make_repo(tmp_path / "source")
    prepared = prepare(task(head), repo, state_dir=tmp_path / "state")

    assert prepared.repo_head == head
    assert prepared.branch == "agent/task-workspace"
    assert prepared.workspace_path != repo
    assert (prepared.workspace_path / "README.md").read_text(encoding="utf-8") == "starter\n"
    assert prepared.checkpoint_path.is_file()
    from poc.kaggle_worker.checkpoint import load_checkpoint
    checkpoint = load_checkpoint(prepared.checkpoint_path, expected_repo_head=head)
    assert checkpoint.task_id == "task-workspace"
    assert checkpoint.task_contract == "Add a feature"
    assert checkpoint.repo_path == str(prepared.workspace_path)
    assert checkpoint.branch == prepared.branch
    assert checkpoint.pending_steps
    assert checkpoint.provider_id == "not-configured"


def test_prepare_is_idempotent_and_resumes_existing_checkpoint(tmp_path):
    repo, head = make_repo(tmp_path / "source")
    item = task(head)
    first = prepare(item, repo, state_dir=tmp_path / "state")
    resumed = prepare(item, repo, state_dir=tmp_path / "state")
    assert resumed == first

    (repo / "README.md").write_text("source advanced\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "advance source"], check=True)
    assert prepare(item, repo, state_dir=tmp_path / "state") == first


def test_prepare_rejects_stale_head_before_creating_workspace(tmp_path):
    repo, _ = make_repo(tmp_path / "source")
    with pytest.raises(WorkspaceError, match="HEAD"):
        prepare(task("deadbeef"), repo, state_dir=tmp_path / "state")
    assert list(tmp_path.glob(".*-kite-worktrees")) == []


def test_prepare_rejects_dirty_source_repo_without_copying_changes(tmp_path):
    repo, head = make_repo(tmp_path / "source")
    (repo / "local.txt").write_text("uncommitted", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="clean"):
        prepare(task(head), repo, state_dir=tmp_path / "state")
    assert not list(tmp_path.glob(".*-kite-worktrees"))


@pytest.mark.parametrize("task_id", ["../escape", "", "two/parts"])
def test_prepare_rejects_unsafe_task_id(tmp_path, task_id):
    repo, head = make_repo(tmp_path / "source")
    with pytest.raises((WorkspaceError, ValueError)):
        prepare(task(head, task_id=task_id), repo, state_dir=tmp_path / "state")
    assert not list(tmp_path.glob(".*-kite-worktrees"))


def test_prepare_rejects_existing_branch_or_workspace_conflict(tmp_path):
    repo, head = make_repo(tmp_path / "source")
    task_value = task(head)
    subprocess.run(["git", "-C", str(repo), "branch", task_value.branch], check=True)
    with pytest.raises(WorkspaceError):
        prepare(task_value, repo, state_dir=tmp_path / "state")
    assert not list(tmp_path.glob(".*-kite-worktrees"))



def test_cli_prepare_uses_queued_task_and_reports_no_provider_start(tmp_path, capsys):
    from poc.kaggle_worker.cli import main
    import json

    repo, head = make_repo(tmp_path / "source")
    db = tmp_path / "state.sqlite3"
    created = main([
        "--db", str(db), "task", "add", "--task-id", "cli-task", "--goal", "Add a feature",
        "--repo-head", head, "--branch", "agent/cli-task",
    ])
    assert created == 0
    capsys.readouterr()

    result = main(["--db", str(db), "task", "prepare", "cli-task", "--repo", str(repo)])
    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["task_id"] == "cli-task"
    assert output["repo_head"] == head
    assert output["provider_started"] is False
    assert Path(output["checkpoint_path"]).is_file()
    assert Path(output["workspace_path"]).is_dir()
