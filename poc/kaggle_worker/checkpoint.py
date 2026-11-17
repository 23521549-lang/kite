"""Versioned, durable task checkpoints for worker replacement."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any


SCHEMA_VERSION = 1
_FIELDS = (
    "task_id", "task_contract", "plan", "completed_steps", "pending_steps",
    "repo_path", "branch", "repo_head", "changed_files", "last_tool_result",
    "model_id", "provider_id", "updated_at",
)
_LIST_FIELDS = ("plan", "completed_steps", "pending_steps", "changed_files")


class CheckpointError(RuntimeError):
    """Checkpoint is invalid, unreadable, or could not be durably written."""


@dataclass(frozen=True)
class TaskCheckpoint:
    task_id: str
    task_contract: str
    plan: list[str]
    completed_steps: list[str]
    pending_steps: list[str]
    repo_path: str
    branch: str
    repo_head: str
    changed_files: list[str]
    last_tool_result: str
    model_id: str
    provider_id: str
    updated_at: str


def _validate(value: Any) -> TaskCheckpoint:
    if not isinstance(value, TaskCheckpoint):
        raise CheckpointError("checkpoint must be a TaskCheckpoint")
    data = asdict(value)
    for key in _FIELDS:
        if key not in data:
            raise CheckpointError(f"missing checkpoint field: {key}")
    for key in _FIELDS:
        if key in _LIST_FIELDS:
            items = data[key]
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise CheckpointError(f"{key} must be a list of strings")
        elif not isinstance(data[key], str):
            raise CheckpointError(f"{key} must be a string")
    return value


def save_checkpoint(checkpoint: TaskCheckpoint, path: Path) -> None:
    """Atomically replace a versioned JSON checkpoint in its destination directory."""
    checkpoint = _validate(checkpoint)
    destination = Path(path)
    temporary: str | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, **asdict(checkpoint)}
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except (OSError, TypeError, ValueError) as error:
        raise CheckpointError(f"could not save checkpoint at {destination}: {error}") from error
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def load_checkpoint(path: Path, *, expected_repo_head: str | None = None) -> TaskCheckpoint:
    """Load and validate checkpoint; optionally reject a stale repository HEAD."""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CheckpointError(f"could not read checkpoint at {source}: {error}") from error
    if not isinstance(raw, dict):
        raise CheckpointError("checkpoint document must be an object")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != SCHEMA_VERSION:
        raise CheckpointError(f"unsupported or invalid schema_version: {raw.get('schema_version')!r}")
    expected_keys = {"schema_version", *_FIELDS}
    if set(raw) != expected_keys:
        missing, extra = expected_keys - set(raw), set(raw) - expected_keys
        raise CheckpointError(f"checkpoint fields mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    data = {key: raw[key] for key in _FIELDS}
    checkpoint = _validate(TaskCheckpoint(**data))
    if expected_repo_head is not None and checkpoint.repo_head != expected_repo_head:
        raise CheckpointError(
            f"checkpoint repo HEAD {checkpoint.repo_head!r} does not match current HEAD {expected_repo_head!r}"
        )
    return checkpoint
