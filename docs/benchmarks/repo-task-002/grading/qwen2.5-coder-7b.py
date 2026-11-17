import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

SCHEMA_VERSION = 1


class CheckpointError(Exception):
    """Raised when a checkpoint cannot be safely saved or resumed."""


class CheckpointStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, state: Mapping[str, Any]) -> None:
        # Validate state
        if not isinstance(state, Mapping):
            raise ValueError("State must be a mapping")
        required_keys = {'task_id', 'repo_head', 'goal'}
        if not all(key in state and isinstance(state[key], str) and state[key] for key in required_keys):
            raise ValueError("Invalid state: missing or invalid keys")
        if not isinstance(state.get('summary'), str):
            raise ValueError("Invalid state: summary must be a string")
        if not isinstance(state.get('plan'), list) or not all(isinstance(item, str) for item in state['plan']):
            raise ValueError("Invalid state: plan must be a list of strings")
        if not isinstance(state.get('completed_steps'), list) or not all(isinstance(item, str) for item in state['completed_steps']):
            raise ValueError("Invalid state: completed_steps must be a list of strings")
        if not isinstance(state.get('changed_files'), list) or not all(isinstance(item, str) for item in state['changed_files']):
            raise ValueError("Invalid state: changed_files must be a list of strings")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode='w+', delete=False, dir=self.path.parent) as temp_file:
            temp_path = Path(temp_file.name)
            try:
                envelope = {'schema_version': SCHEMA_VERSION, 'state': state}
                json.dump(envelope, temp_file)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path.rename(self.path)
            except Exception as e:
                temp_path.unlink(missing_ok=True)
                raise CheckpointError(f"Failed to save checkpoint: {e}") from e

    def load(self, *, expected_repo_head: str | None = None) -> dict[str, Any]:
        if not self.path.exists():
            raise CheckpointError(f"Checkpoint file not found: {self.path}")
        try:
            with open(self.path, 'r') as file:
                envelope = json.load(file)
        except json.JSONDecodeError as e:
            raise CheckpointError(f"Malformed JSON in checkpoint file: {self.path}") from e
        if envelope.get('schema_version') != SCHEMA_VERSION:
            raise CheckpointError(f"Unsupported schema version: {envelope.get('schema_version')} (expected {SCHEMA_VERSION})")
        state = envelope.get('state')
        if not isinstance(state, Mapping):
            raise CheckpointError("Invalid state in checkpoint file")
        required_keys = {'task_id', 'repo_head', 'goal'}
        if not all(key in state and isinstance(state[key], str) and state[key] for key in required_keys):
            raise CheckpointError("Invalid state in checkpoint file: missing or invalid keys")
        if not isinstance(state.get('summary'), str):
            raise CheckpointError("Invalid state in checkpoint file: summary must be a string")
        if not isinstance(state.get('plan'), list) or not all(isinstance(item, str) for item in state['plan']):
            raise CheckpointError("Invalid state in checkpoint file: plan must be a list of strings")
        if not isinstance(state.get('completed_steps'), list) or not all(isinstance(item, str) for item in state['completed_steps']):
            raise CheckpointError("Invalid state in checkpoint file: completed_steps must be a list of strings")
        if not isinstance(state.get('changed_files'), list) or not all(isinstance(item, str) for item in state['changed_files']):
            raise CheckpointError("Invalid state in checkpoint file: changed_files must be a list of strings")
        if expected_repo_head and state['repo_head'] != expected_repo_head:
            raise CheckpointError(f"Repo head mismatch: expected {expected_repo_head}, got {state['repo_head']}")
        return state
