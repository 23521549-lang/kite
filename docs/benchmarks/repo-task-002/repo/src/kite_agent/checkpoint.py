from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1


class CheckpointError(Exception):
    """Raised when a checkpoint cannot be safely saved or resumed."""


class CheckpointStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, state: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def load(self, *, expected_repo_head: str | None = None) -> dict[str, Any]:
        raise NotImplementedError
