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

A block whose SEARCH is not in the file it names (or names a file that does not exist) goes to the one
other file that has that SEARCH text, when the caller says where to look (``search_files``), as Aider
does with the other files in the chat. Bench v5: qwen copied the prompt's example path
``đường/dẫn/tới/file.py`` in 3 of 4 tasks, with SEARCH text that was right, and every fix was lost.

A long SEARCH may also differ from the file on a line the block does not change (a slip while copying:
bench v5, R3, ``f"{self.MAX_BLOB_BYTES} — reduce"`` copied as ``f"{self.MAX_BLOB_BYTES — reduce"`` in
both SEARCH and REPLACE). The block still applies, with the file's own text on that line, when that
is the only place in the file that comes so close (see ``_near``); a slip on a line the block changes
never applies.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Sequence

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


def _shift(new: list[str], have: str, had: str) -> list[str]:
    """Replacement lines written at indentation ``had``, moved to the file's ``have``."""
    return [(have + l[len(had):] if l.startswith(had) else have + l.lstrip()) if l.strip() else l for l in new]


SLIP_EVERY = 8           # at most one line copied wrong per this many SEARCH lines
SLIP_SIMILAR = 0.8       # and that line still this close to the file's


def _near(file_lines: list[str], wanted: list[str], new: list[str], notes: list[str] | None) -> str | None:
    """A SEARCH that differs from the file only on lines the block keeps (same text in SEARCH and
    REPLACE): those lines keep the file's text. First and last lines must match, at most one
    different line per ``SLIP_EVERY``, each still close, and only one place in the file that close."""
    n = len(wanted)
    allowed = n // SLIP_EVERY
    if not allowed:
        return None
    target = [line.strip() for line in wanted]
    places = []
    for start in range(len(file_lines) - n + 1):
        if file_lines[start].strip() != target[0] or file_lines[start + n - 1].strip() != target[-1]:
            continue
        slips = [j for j in range(n) if file_lines[start + j].strip() != target[j]]
        if len(slips) <= allowed and all(difflib.SequenceMatcher(
                None, file_lines[start + j].strip(), target[j], autojunk=False).ratio() >= SLIP_SIMILAR for j in slips):
            places.append((start, slips))
    if len(places) != 1:
        return None
    start, slips = places[0]
    have = next((_indent(l) for l in file_lines[start:start + n] if l.strip()), "")
    had = next((_indent(l) for l in wanted if l.strip()), "")
    out: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wanted, new, autojunk=False).get_opcodes():
        if tag == "equal":
            out += file_lines[start + i1:start + i2]           # the file's own lines, slips included
        elif any(i1 <= j < i2 for j in slips):
            return None                                        # the block changes a line it copied wrong
        else:
            out += _shift(new[j1:j2], have, had)
    if notes is not None:
        notes.append("line " + ", ".join(str(start + j + 1) for j in slips) + " kept as in the file "
                     "(SEARCH had it slightly different)")
    return "\n".join(file_lines[:start] + out + file_lines[start + n:])


def _replace_lines(content: str, search: str, replace: str, notes: list[str] | None = None) -> str | None:
    """Whole lines only (a SEARCH never matches half a line): exactly, then ignoring trailing
    spaces, then ignoring indentation (the replacement is shifted to the file's indentation), then
    with a slip on a line the block keeps (``_near``)."""
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
            new = _shift(new, have, had)
        return "\n".join(file_lines[:start] + new + file_lines[start + len(target):])
    return _near(file_lines, wanted, new, notes)


MAX_SEARCH_BYTES = 2_000_000     # a bigger file is not source code worth searching for a lost block


def _first_line(search: str) -> str:
    return next((line.strip() for line in search.split("\n") if line.strip()), "")


def _elsewhere(workspace: Path, number: int, block: EditBlock, tiers: Sequence[Sequence[str]],
               load: Callable[[Path], str | None], named: Path | None,
               notes: list[str] | None) -> tuple[Path, str] | None:
    """The one file, in the first tier that has any, whose text takes this block's SEARCH."""
    seen = {named} if named is not None else set()
    probe = _first_line(block.search)
    for tier in tiers:
        hits: list[tuple[Path, str | None]] = []
        for name in tier:
            try:
                target = _safe_path(workspace, name)
            except EditError:
                continue
            if target in seen:
                continue
            seen.add(target)
            content = load(target)
            if content is None or probe not in content:
                continue
            slipped: list[str] = []
            try:
                updated = _replace_lines(content, block.search, block.replace, slipped)
            except EditError:                      # in this file, but more than once
                hits.append((target, None))
                continue
            if updated is not None:
                hits.append((target, updated))
                if notes is not None and len(hits) == 1:
                    notes += [f"{target.relative_to(workspace).as_posix()}: {note}" for note in slipped]
        if len(hits) > 1:
            names = ", ".join(t.relative_to(workspace).as_posix() for t, _ in hits[:4])
            raise EditError(f"block {number} ({block.path}): no such SEARCH there, and it is in several files "
                            f"({names}); name the file")
        if hits:
            target, updated = hits[0]
            if updated is None:
                raise EditError(f"block {number} ({target.relative_to(workspace).as_posix()}): SEARCH matches "
                                "more than one place; add surrounding lines to make it unique")
            return target, updated
    return None


def apply(workspace: Path, blocks: list[EditBlock], search_files: Sequence[Sequence[str]] = (),
          notes: list[str] | None = None) -> list[str]:
    """Apply all blocks or none; return the changed files. EditError says which block failed.

    ``search_files``: tiers of repository paths (say, the files shown to the model, then every tracked
    file) where a block that does not apply to the file it names is looked for. ``notes`` gets one
    line per block Kite had to set right: ``"named → used"`` for one that went to another file, and
    which lines kept the file's text for a slip (``_near``)."""
    if not blocks:
        raise EditError("the answer has no edit blocks")
    workspace = Path(workspace)
    pending: dict[Path, str | None] = {}

    def load(target: Path, any_size: bool = False) -> str | None:
        if target in pending:
            return pending[target]
        try:
            if not target.is_file() or (not any_size and target.stat().st_size > MAX_SEARCH_BYTES):
                return None
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    for number, block in enumerate(blocks, 1):
        try:
            target: Path | None = _safe_path(workspace, block.path)
        except EditError:
            if not block.search.strip() or not search_files:
                raise
            target = None
        if not block.search.strip():
            if target not in pending:
                pending[target] = target.read_text(encoding="utf-8") if target.is_file() else None
            if pending[target] is not None:
                raise EditError(f"block {number} ({block.path}): empty SEARCH but the file exists")
            pending[target] = block.replace.rstrip("\n") + "\n"
            continue
        current = load(target, any_size=True) if target is not None else None
        slipped: list[str] = []
        updated = _replace_lines(current, block.search, block.replace, slipped) if current is not None else None
        if updated is not None and notes is not None:
            notes += [f"{block.path}: {note}" for note in slipped]
        if updated is None and search_files:
            found_notes: list[str] = []
            found = _elsewhere(workspace, number, block, search_files, load, target, found_notes)
            if found is not None:
                target, updated = found
                if notes is not None:
                    notes += [f"{block.path} → {target.relative_to(workspace).as_posix()}", *found_notes]
        if updated is None:
            if current is None:
                raise EditError(f"block {number} ({block.path}): the file does not exist")
            raise EditError(f"block {number} ({block.path}): SEARCH text is not in the file")
        pending[target] = updated
    changed = []
    for target, content in pending.items():
        if content is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(target.relative_to(workspace).as_posix())
    return list(dict.fromkeys(changed))
