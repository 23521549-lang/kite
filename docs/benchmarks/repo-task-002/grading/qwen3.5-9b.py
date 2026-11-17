from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1


class CheckpointError(Exception):
    """Raised when a checkpoint cannot be safely saved or resumed."""


class CheckpointStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()

    def _validate_state(self, state: Mapping[str, Any]) -> None:
        """Validate that the state dictionary meets all structural and content requirements."""
        if not isinstance(state, dict):
            raise CheckpointError("State must be a mapping (dict).")

        required_keys = {"task_id", "repo_head", "goal", "summary", "plan", "completed_steps", "changed_files"}
        if set(state.keys()) != required_keys:
            raise CheckpointError(f"State must contain exactly keys: {required_keys}")

        for key in required_keys:
            if key not in state:
                raise CheckpointError(f"Missing required key: {key}")
            if not isinstance(state[key], list) and key in ("plan", "completed_steps", "changed_files"):
                raise CheckpointError(f"Key '{key}' must be a list.")
            if not isinstance(state[key], str) and key in ("task_id", "repo_head", "goal", "summary"):
                raise CheckpointError(f"Key '{key}' must be a string.")

        if not state["task_id"].strip():
            raise CheckpointError("task_id must be non-empty.")
        if not state["repo_head"].strip():
            raise CheckpointError("repo_head must be non-empty.")
        if not state["goal"].strip():
            raise CheckpointError("goal must be non-empty.")

    def _validate_envelope(self, envelope: dict[str, Any]) -> None:
        """Validate the JSON envelope structure."""
        if not isinstance(envelope, dict):
            raise CheckpointError("Envelope must be a mapping (dict).")
        if "schema_version" not in envelope:
            raise CheckpointError("Envelope missing 'schema_version'.")
        if "state" not in envelope:
            raise CheckpointError("Envelope missing 'state'.")

        version = envelope["schema_version"]
        if not isinstance(version, int):
            raise CheckpointError("schema_version must be an integer.")
        if version != SCHEMA_VERSION:
            raise CheckpointError(f"Unsupported schema version: {version} (expected {SCHEMA_VERSION})")

        self._validate_state(envelope["state"])

    def save(self, state: Mapping[str, Any]) -> None:
        """Save the checkpoint state atomically without mutating input."""
        self._validate_state(state)

        parent = self.path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

        envelope = {"schema_version": SCHEMA_VERSION, "state": state}
        fd, temp_path = tempfile.mkstemp(dir=self.path.parent, prefix=".checkpoint_tmp_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(envelope, f, ensure_ascii=False)
            os.replace(temp_path, self.path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def load(self, *, expected_repo_head: str | None = None) -> dict[str, Any]:
        """Load and validate a checkpoint; never alter it while loading."""
        if not self.path.exists():
            raise CheckpointError(f"Checkpoint file not found: {self.path}")

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                content = f.read()
                envelope = json.loads(content)
        except json.JSONDecodeError as e:
            raise CheckpointError(f"Malformed JSON in checkpoint: {e}")

        self._validate_envelope(envelope)
        state = envelope["state"]
        if expected_repo_head is not None and state["repo_head"] != expected_repo_head:
            raise CheckpointError(f"Expected repo_head '{expected_repo_head}' does not match checkpoint '{state['repo_head']}'")
        return state
