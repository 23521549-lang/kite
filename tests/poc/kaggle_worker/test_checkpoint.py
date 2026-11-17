from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from poc.kaggle_worker.checkpoint import CheckpointError, TaskCheckpoint, load_checkpoint, save_checkpoint


def sample():
    return TaskCheckpoint(
        task_id="task-17", task_contract="Add retry handling",
        plan=["inspect", "implement", "verify"], completed_steps=["inspect"],
        pending_steps=["implement", "verify"], repo_path="/workspace/app",
        branch="feature/retry", repo_head="abc123", changed_files=["src/client.py"],
        last_tool_result="Found request path", model_id="Qwen3.5-9B",
        provider_id="kaggle-worker-a", updated_at="2026-11-16T12:00:00Z",
    )


def test_round_trip_and_schema_version(tmp_path):
    path = tmp_path / "task.json"
    save_checkpoint(sample(), path)
    assert json.loads(path.read_text())["schema_version"] == 1
    assert load_checkpoint(path) == sample()


def test_rejects_malformed_schema_and_boolean_version(tmp_path):
    path = tmp_path / "task.json"
    path.write_text(json.dumps({"schema_version": True, **sample().__dict__}))
    with pytest.raises(CheckpointError):
        load_checkpoint(path)


def test_rejects_non_string_list_items_on_save(tmp_path):
    with pytest.raises(CheckpointError):
        save_checkpoint(replace(sample(), plan=["ok", 3]), tmp_path / "task.json")


def test_rejects_stale_repo_head_without_changing_file(tmp_path):
    path = tmp_path / "task.json"
    save_checkpoint(sample(), path)
    before = path.read_bytes()
    with pytest.raises(CheckpointError, match="HEAD"):
        load_checkpoint(path, expected_repo_head="def456")
    assert path.read_bytes() == before


def test_atomic_replace_failure_preserves_existing_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "task.json"
    save_checkpoint(sample(), path)
    before = path.read_bytes()
    monkeypatch.setattr("poc.kaggle_worker.checkpoint.os.replace", lambda *_: (_ for _ in ()).throw(OSError("simulated")))
    with pytest.raises(CheckpointError):
        save_checkpoint(replace(sample(), task_contract="new"), path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]

def test_checkpoint_can_seed_replacement_worker_without_old_session_state(tmp_path):
    old_worker_session = "session-task-17"
    checkpoint_path = tmp_path / "task.json"
    save_checkpoint(sample(), checkpoint_path)
    # A fresh process/provider has no reference to the old worker session.
    resumed = load_checkpoint(checkpoint_path, expected_repo_head="abc123")
    serialized = json.loads(checkpoint_path.read_text())
    assert "worker_session_id" not in serialized
    prompt = (
        f"Task: {resumed.task_contract}\n"
        f"Completed: {resumed.completed_steps}\n"
        f"Pending: {resumed.pending_steps}\n"
        f"Repo: {resumed.repo_path} at {resumed.repo_head}\n"
        f"Last tool result: {resumed.last_tool_result}"
    )
    assert "Add retry handling" in prompt
    assert "implement" in prompt
    assert old_worker_session not in prompt

def test_fresh_controller_resumes_task_with_replacement_provider(tmp_path):
    from poc.kaggle_worker.controller import WorkerController
    from poc.kaggle_worker.protocol import ModelRequest, ModelResponse, WorkerStatus

    class Provider:
        def __init__(self, name):
            self.name = name
            self.started, self.stopped, self.requests = [], [], []
        def start(self, task_id):
            self.started.append(task_id)
            return f"{self.name}-session"
        def status(self, session_id):
            return WorkerStatus.READY
        def invoke(self, session_id, request):
            self.requests.append((session_id, request.prompt))
            return ModelResponse(text="continue")
        def stop(self, session_id):
            self.stopped.append(session_id)

    path = tmp_path / "task.json"
    first = Provider("worker-a")
    controller_a = WorkerController(first)
    controller_a.run_request("task-17", ModelRequest("start task"))
    save_checkpoint(sample(), path)
    controller_a.complete("task-17")

    resumed = load_checkpoint(path, expected_repo_head="abc123")
    prompt = f"Task: {resumed.task_contract}; completed: {resumed.completed_steps}; pending: {resumed.pending_steps}"
    second = Provider("worker-b")
    controller_b = WorkerController(second)
    controller_b.run_request(resumed.task_id, ModelRequest(prompt))
    assert first.stopped == ["worker-a-session"]
    assert second.started == ["task-17"]
    assert second.requests[0][0] == "worker-b-session"
    assert "implement" in second.requests[0][1]
    assert "worker-a-session" not in second.requests[0][1]
