from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo" / "src"))

from kite_agent.checkpoint import CheckpointError, CheckpointStore


def state():
    return {"task_id": "t1", "repo_head": "h1", "goal": "resume", "plan": [], "completed_steps": [], "changed_files": [], "summary": ""}


def test_missing_file_raises_checkpoint_error(tmp_path):
    try:
        CheckpointStore(tmp_path / "missing.json").load()
    except CheckpointError:
        return
    raise AssertionError("missing checkpoint was accepted")


def test_malformed_json_raises_checkpoint_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{broken", encoding="utf-8")
    try:
        CheckpointStore(path).load()
    except CheckpointError:
        return
    raise AssertionError("malformed JSON was accepted")


def test_unknown_schema_version_is_rejected(tmp_path):
    import json
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"schema_version": 2, "state": state()}), encoding="utf-8")
    try:
        CheckpointStore(path).load()
    except CheckpointError:
        return
    raise AssertionError("unsupported schema was accepted")


def test_wrong_field_types_are_rejected(tmp_path):
    bad = state()
    bad["completed_steps"] = "inspect"
    try:
        CheckpointStore(tmp_path / "bad.json").save(bad)
    except CheckpointError:
        return
    raise AssertionError("wrong field type was accepted")


def test_unknown_fields_are_rejected_to_avoid_persisting_ephemeral_worker_data(tmp_path):
    bad = state()
    bad["worker_session_id"] = "ephemeral"
    try:
        CheckpointStore(tmp_path / "bad.json").save(bad)
    except CheckpointError:
        return
    raise AssertionError("unknown/ephemeral field was persisted")
import json
from pathlib import Path
import pytest
from kite_agent.checkpoint import CheckpointError, CheckpointStore


def test_list_members_must_be_strings(tmp_path):
    state = {"task_id": "t1", "repo_head": "h1", "goal": "resume", "plan": ["ok", {"worker_session_id": "x"}], "completed_steps": [], "changed_files": [], "summary": ""}
    with pytest.raises(CheckpointError):
        CheckpointStore(tmp_path / "state.json").save(state)


def test_list_members_are_validated_during_load(tmp_path):
    path = tmp_path / "state.json"
    state = {"task_id": "t1", "repo_head": "h1", "goal": "resume", "plan": [3], "completed_steps": [], "changed_files": [], "summary": ""}
    path.write_text(json.dumps({"schema_version": 1, "state": state}), encoding="utf-8")
    with pytest.raises(CheckpointError):
        CheckpointStore(path).load()


def test_boolean_is_not_a_schema_version(tmp_path):
    path = tmp_path / "state.json"
    state = {"task_id": "t1", "repo_head": "h1", "goal": "resume", "plan": [], "completed_steps": [], "changed_files": [], "summary": ""}
    path.write_text(json.dumps({"schema_version": True, "state": state}), encoding="utf-8")
    with pytest.raises(CheckpointError):
        CheckpointStore(path).load()


def test_replace_failure_preserves_old_checkpoint_and_cleans_temp(tmp_path, monkeypatch):
    import kite_agent.checkpoint as checkpoint_module
    store = CheckpointStore(tmp_path / "state.json")
    state = {"task_id": "t1", "repo_head": "h1", "goal": "resume", "plan": [], "completed_steps": [], "changed_files": [], "summary": "old"}
    store.save(state)
    before = store.path.read_bytes()
    fail = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected replace error"))
    monkeypatch.setattr(checkpoint_module.os, "replace", fail)
    monkeypatch.setattr(Path, "rename", fail)
    state["summary"] = "new"
    with pytest.raises(OSError):
        store.save(state)
    assert store.path.read_bytes() == before
    assert list(tmp_path.glob(".checkpoint_tmp_*")) == []

import json
import pytest
from pathlib import Path
from kite_agent.checkpoint import CheckpointError, CheckpointStore


def test_saving_twice_atomically_replaces_existing_file(tmp_path):
    store = CheckpointStore(tmp_path / "state.json")
    first = {"task_id": "t1", "repo_head": "h1", "goal": "resume", "plan": [], "completed_steps": [], "changed_files": [], "summary": "old"}
    second = {**first, "summary": "new"}
    store.save(first)
    store.save(second)
    assert store.load() == second


def test_non_mapping_envelope_raises_checkpoint_error(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(CheckpointError):
        CheckpointStore(path).load()


