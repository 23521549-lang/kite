from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kite_agent.checkpoint import CheckpointError, CheckpointStore


def valid_state():
    return {
        "task_id": "task-17",
        "repo_head": "abc123",
        "goal": "Fix worker resume",
        "plan": ["inspect", "implement", "verify"],
        "completed_steps": ["inspect"],
        "changed_files": ["src/a.py"],
        "summary": "Tests remain.",
    }


def test_round_trip_and_schema_envelope(tmp_path):
    store = CheckpointStore(tmp_path / "nested" / "state.json")
    state = valid_state()
    store.save(state)
    assert store.load(expected_repo_head="abc123") == state
    import json
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    assert envelope["schema_version"] == 1
    assert envelope["state"] == state


def test_save_does_not_mutate_input(tmp_path):
    state = valid_state()
    before = {key: value[:] if isinstance(value, list) else value for key, value in state.items()}
    CheckpointStore(tmp_path / "state.json").save(state)
    assert state == before


def test_repo_head_mismatch_is_rejected_without_changing_file(tmp_path):
    store = CheckpointStore(tmp_path / "state.json")
    store.save(valid_state())
    before = store.path.read_bytes()
    try:
        store.load(expected_repo_head="new-head")
    except CheckpointError:
        pass
    else:
        raise AssertionError("stale checkpoint was accepted")
    assert store.path.read_bytes() == before
