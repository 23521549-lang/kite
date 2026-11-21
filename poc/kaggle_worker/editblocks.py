"""SEARCH/REPLACE edit blocks: how the fast path gets a whole fix from one model answer.

The format is Aider's (github.com/Aider-AI/aider, Apache-2.0), which code models know well::

    services/backend/main.py
    <<<<<<< SEARCH
    exact lines that are in the file now
    =======
    the lines that replace them
    >>>>>>> REPLACE

An empty SEARCH creates a new file (only a file that does not exist yet). A block applies only if
its SEARCH lines are in the file exactly once, as whole lines; if not, the same lines compared
without trailing spaces, then without indentation (the replacement is re-indented to match). Nothing is
written unless every block applies, so a half-applied answer never reaches the tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re

HEAD = re.compile(r"^\s*<{5,9} ?SEARCH\s*$")
DIVIDER = re.compile(r"^\s*={5,9}\s*$")
TAIL = re.compile(r"^\s*>{5,9} ?REPLACE\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


class EditError(ValueError):
    pass


@dataclass(frozen=True)
class EditBlock:
    path: str
    search: str
    replace: str


def _file_name(line: str) -> str:
    """``services/x.py`` out of lines like ``services/x.py``, ```` ```python services/x.py ````,
    ``**services/x.py**``, ``File: services/x.py:``."""
    text = line.strip()
    text = re.sub(r"^(```|~~~)\w*\s*", "", text)
    text = re.sub(r"^(#+|\*+|-|File:|Tệp:|File)\s*", "", text, flags=re.I)
    return text.strip().strip("`*'\":").strip()


def parse(text: str, default_path: str = "") -> list[EditBlock]:
    """Every SEARCH/REPLACE block in a model answer (thinking text and prose around them ignored).

    ``default_path``: the file a block with no file name line before it belongs to, when the
    caller asked for exactly one file (the reproduction test)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    lines = text.splitlines()
    blocks, i, last_path = [], 0, default_path
    while i < len(lines):
        if not HEAD.match(lines[i]):
            i += 1
            continue
        path = ""
        for back in range(i - 1, max(-1, i - 4), -1):
            candidate = lines[back]
            if not candidate.strip() or (FENCE.match(candidate) and not _file_name(candidate)):
                continue
            path = _file_name(candidate)
            break
        if not path or " " in path or path.endswith(("SEARCH", "REPLACE")) or "=" * 5 in path:
            path = last_path                       # a second block for the same file
        search, replace, j = [], [], i + 1
        while j < len(lines) and not DIVIDER.match(lines[j]):
            search.append(lines[j])
            j += 1
        j += 1
        while j < len(lines) and not TAIL.match(lines[j]):
            replace.append(lines[j])
            j += 1
        if j >= len(lines) or not path:
            raise EditError("an edit block is not closed or has no file name")
        blocks.append(EditBlock(path, "\n".join(search), "\n".join(replace)))
        last_path, i = path, j + 1
    return blocks


def _safe_path(workspace: Path, path: str) -> Path:
    clean = PurePosixPath(path.replace("\\", "/"))
    if clean.is_absolute() or ".." in clean.parts or not clean.parts or clean.parts[0] == ".git":
        raise EditError(f"{path}: not a path inside the repository")
    return Path(workspace) / Path(*clean.parts)


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _replace_lines(content: str, search: str, replace: str) -> str | None:
    """Whole lines only (a SEARCH never matches half a line): exactly, then ignoring trailing
    spaces, then ignoring indentation (the replacement is shifted to the file's indentation)."""
    file_lines, wanted = content.split("\n"), search.strip("\n").split("\n")
    new = replace.strip("\n").split("\n") if replace.strip("\n") else []
    for key in (lambda line: line, str.rstrip, str.strip):
        target = [key(line) for line in wanted]
        hits = [i for i in range(len(file_lines) - len(target) + 1)
                if [key(line) for line in file_lines[i:i + len(target)]] == target]
        if len(hits) > 1:
            raise EditError("SEARCH matches more than one place; add surrounding lines to make it unique")
        if not hits:
            continue
        start = hits[0]
        if key is str.strip:                       # same lines, other indentation: shift the replacement
            have = next((_indent(l) for l in file_lines[start:start + len(target)] if l.strip()), "")
            had = next((_indent(l) for l in wanted if l.strip()), "")
            new = [(have + l[len(had):] if l.startswith(had) else have + l.lstrip()) if l.strip() else l
                   for l in new]
        return "\n".join(file_lines[:start] + new + file_lines[start + len(target):])
    return None


def apply(workspace: Path, blocks: list[EditBlock]) -> list[str]:
    """Apply all blocks or none; return the changed files. EditError says which block failed."""
    if not blocks:
        raise EditError("the answer has no edit blocks")
    pending: dict[Path, str | None] = {}
    for number, block in enumerate(blocks, 1):
        target = _safe_path(workspace, block.path)
        if target not in pending:
            pending[target] = target.read_text(encoding="utf-8") if target.is_file() else None
        current = pending[target]
        if not block.search.strip():
            if current is not None:
                raise EditError(f"block {number} ({block.path}): empty SEARCH but the file exists")
            pending[target] = block.replace.rstrip("\n") + "\n"
            continue
        if current is None:
            raise EditError(f"block {number} ({block.path}): the file does not exist")
        updated = _replace_lines(current, block.search, block.replace)
        if updated is None:
            raise EditError(f"block {number} ({block.path}): SEARCH text is not in the file")
        pending[target] = updated
    changed = []
    for target, content in pending.items():
        if content is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(target.relative_to(workspace).as_posix())
    return changed
