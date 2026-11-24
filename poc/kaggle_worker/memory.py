"""Per-repository memory: what the person said was wrong in earlier tasks on the same repository.

Like a project's ``CLAUDE.md``, but written by Kite: each ``bad`` note on a task (``note_task`` in the
chat, ``kite journal note TASK --bad "why"``) becomes one line in ``<state>/memory/<repo>.md``, and
every later task on that repository gets the most recent lines in its goal. The file stays on the
Kite host (never in the repository) and is plain Markdown: read it, edit it or delete lines by hand.

Anything the chat *model* proposes (the ``remember`` / ``note_task`` MCP tools) does NOT land here
directly: it waits in a pending queue (``pending.json`` below) until the owner approves it with
``kite memory approve <id>`` — a poisoned task result must not be able to plant standing
instructions. Notes the owner types in their own terminal (``kite journal note``) skip the queue.
Entries approved from the queue carry a provenance label with the approval date.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .journal import read_jsonl

MEMORY_LINES = 10            # the newest lines that go into a task
MEMORY_CHARS = 1500
LINE_CHARS = 400


def repo_key(repo: str | Path) -> str:
    path = str(Path(repo).expanduser().resolve())
    name = re.sub(r"[^a-z0-9-]+", "-", Path(path).name.lower()).strip("-")[:40] or "repo"
    return f"{name}-{hashlib.sha1(path.encode('utf-8')).hexdigest()[:8]}"


def memory_path(state_dir: Path, repo: str | Path) -> Path:
    return Path(state_dir) / "memory" / f"{repo_key(repo)}.md"


def remember(state_dir: Path, repo: str | Path, task_id: str, text: str, *, clock=time.time) -> Path:
    """Add one lesson for ``repo`` (one line: newlines folded, long text cut)."""
    path = memory_path(state_dir, repo)
    lesson = " ".join(text.split())[:LINE_CHARS]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# Kite: lưu ý cho repo {Path(repo).expanduser().resolve()}\n\n"
                        "Mỗi dòng là một lần người dùng chê kết quả một task. Sửa hoặc xoá dòng tuỳ ý.\n\n",
                        encoding="utf-8")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"- {time.strftime('%Y-%m-%d', time.localtime(clock()))} ({task_id}): {lesson}\n")
    return path


def lessons(state_dir: Path, repo: str | Path, *, limit: int = MEMORY_LINES, chars: int = MEMORY_CHARS) -> list[str]:
    """The newest lessons for ``repo``, oldest first, within ``chars``."""
    path = memory_path(state_dir, repo)
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
    except OSError:
        return []
    kept: list[str] = []
    for line in reversed(lines[-limit:]):
        if sum(len(x) for x in kept) + len(line) > chars:
            break
        kept.insert(0, line)
    return kept


def lessons_note(state_dir: Path, repo: str | Path) -> str:
    """The block a task's goal gets, or "" when there is nothing to remember."""
    found = lessons(state_dir, repo)
    if not found:
        return ""
    return ("\n\nLưu ý từ người dùng ở các task trước trên repo này (nhận xét khi kết quả chưa đạt; làm theo, "
            "trừ khi yêu cầu ở trên nói khác):\n" + "\n".join(found))


def repo_of_task(state_dir: Path, task_id: str) -> str | None:
    """The repository a finished task ran on (from the journal of task runs)."""
    for run in reversed(read_jsonl(Path(state_dir) / "journal" / "tasks.jsonl")):
        if run.get("task_id") == task_id and run.get("repo"):
            return str(run["repo"])
    return None


def remember_note(state_dir: Path, task_id: str, verdict: str, text: str) -> str | None:
    """A user's note on a task: a ``bad`` one with a reason becomes a lesson for the task's repository.
    Returns the memory file, or None when nothing was remembered."""
    if verdict != "bad" or not text.strip():
        return None
    repo = repo_of_task(state_dir, task_id)
    if repo is None:
        return None
    return str(remember(state_dir, repo, task_id, text))


def lesson_lines(state_dir: Path, repo: str | Path) -> list[str]:
    """Every lesson line kept for ``repo`` (``- date (task): text``), oldest first."""
    try:
        text = memory_path(state_dir, repo).read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.startswith("- ")]


def forget(path: Path, number: int) -> str | None:
    """Remove the ``number``-th (from 1) ``- `` line of a memory file; returns it, or None."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return None
    items = [i for i, line in enumerate(lines) if line.startswith("- ")]
    if not 1 <= number <= len(items):
        return None
    removed = lines.pop(items[number - 1])
    Path(path).write_text("".join(lines), encoding="utf-8")
    return removed.strip()


# ---------------------------------------------------------------------- what the person prefers
# Like a person's memory in Claude: things the person asked the chat to keep in mind for every
# conversation ("hỏi trước khi tạo task", "trả lời ngắn"). The lead agent saves them with the
# ``remember`` tool; they go into the chat's AGENTS.md, so every new chat session starts with them.

PREFERENCE_LINES = 30


def preferences_path(state_dir: Path) -> Path:
    return Path(state_dir) / "memory" / "_nguoi-dung.md"


def preferences(state_dir: Path) -> list[str]:
    try:
        text = preferences_path(state_dir).read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.startswith("- ")][-PREFERENCE_LINES:]


def add_preference(state_dir: Path, text: str, *, clock=time.time) -> Path:
    path = preferences_path(state_dir)
    line = " ".join(text.split())[:LINE_CHARS]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Kite: điều người dùng dặn chat nhớ\n\nMỗi dòng một điều. Sửa hoặc xoá tuỳ ý.\n\n",
                        encoding="utf-8")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"- {time.strftime('%Y-%m-%d', time.localtime(clock()))}: {line}\n")
    return path


PREFERENCES_START = "<!-- kite:nguoi-dung -->"
PREFERENCES_END = "<!-- /kite:nguoi-dung -->"


def preferences_block(state_dir: Path) -> str:
    """The AGENTS.md section with what the person asked to keep in mind ("" when nothing)."""
    found = preferences(state_dir)
    if not found:
        return ""
    return (f"\n{PREFERENCES_START}\n## Người dùng đã dặn (luôn làm theo)\n\n" + "\n".join(found)
            + f"\n{PREFERENCES_END}\n")


def refresh_agents_file(agents: Path, state_dir: Path) -> bool:
    """Put the current preferences into an existing chat AGENTS.md (replacing the old section)."""
    try:
        text = Path(agents).read_text(encoding="utf-8")
    except OSError:
        return False
    text = re.sub(re.escape("\n" + PREFERENCES_START) + r".*?" + re.escape(PREFERENCES_END) + r"\n?", "", text,
                  flags=re.S)
    Path(agents).write_text(text.rstrip("\n") + "\n" + preferences_block(state_dir), encoding="utf-8")
    return True


# ---------------------------------------------------------------------- the pending queue
# Model-proposed lessons and preferences wait here (JSON, newest last) until the owner approves or
# rejects them from their own terminal. Approving writes the entry into the effective store above
# with a provenance label; rejecting drops it. Nothing in this section is read by task goals or by
# the chat's AGENTS.md — only the effective stores are.

def pending_path(state_dir: Path) -> Path:
    return Path(state_dir) / "memory" / "pending.json"


def _load_pending(state_dir: Path) -> list[dict]:
    try:
        data = json.loads(pending_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_pending(state_dir: Path, entries: list[dict]) -> None:
    path = pending_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(entries, stream, indent=1, ensure_ascii=False)
    os.replace(temp, path)


def _propose(state_dir: Path, entry: dict, *, clock=time.time) -> dict:
    entries = _load_pending(state_dir)
    entry = {**entry, "id": uuid.uuid4().hex[:8], "created_at": clock()}
    entries.append(entry)
    _save_pending(state_dir, entries)
    return entry


def propose_lesson(state_dir: Path, repo: str | Path, task_id: str, text: str, *, clock=time.time) -> dict:
    """Queue a model-proposed lesson for ``repo``; effective only after the owner approves it."""
    return _propose(state_dir, {"kind": "lesson", "repo": str(Path(repo).expanduser().resolve()),
                                "task_id": task_id, "text": " ".join(text.split())[:LINE_CHARS]}, clock=clock)


def propose_preference(state_dir: Path, text: str, *, clock=time.time) -> dict:
    """Queue a model-proposed standing preference; effective only after the owner approves it."""
    return _propose(state_dir, {"kind": "preference", "repo": "", "task_id": "chat",
                                "text": " ".join(text.split())[:LINE_CHARS]}, clock=clock)


def propose_note(state_dir: Path, task_id: str, verdict: str, text: str) -> dict | None:
    """The pending-queue twin of :func:`remember_note` for the chat path: a ``bad`` verdict with a
    reason becomes a *proposed* lesson for the task's repository, or None when nothing is queued."""
    if verdict != "bad" or not text.strip():
        return None
    repo = repo_of_task(state_dir, task_id)
    if repo is None:
        return None
    return propose_lesson(state_dir, repo, task_id, text)


def list_pending(state_dir: Path) -> list[dict]:
    """Every queued entry, oldest first: {id, kind, repo, task_id, text, created_at}."""
    return _load_pending(state_dir)


def _take_pending(state_dir: Path, entry_id: str) -> dict:
    entries = _load_pending(state_dir)
    for index, entry in enumerate(entries):
        if entry.get("id") == entry_id:
            entries.pop(index)
            _save_pending(state_dir, entries)
            return entry
    raise ValueError(f"không có mục chờ duyệt '{entry_id}'; xem danh sách bằng `kite memory list-pending`")


def approve_pending(state_dir: Path, entry_id: str, *, clock=time.time) -> dict:
    """Move one queued entry into the effective store, labelled with its approval date."""
    entry = _take_pending(state_dir, entry_id)
    label = f" (chủ máy đã duyệt {time.strftime('%Y-%m-%d', time.localtime(clock()))})"
    if entry["kind"] == "lesson":
        path = remember(state_dir, entry["repo"], entry.get("task_id") or "chat",
                        entry["text"] + label, clock=clock)
    else:
        path = add_preference(state_dir, entry["text"] + label, clock=clock)
    return {**entry, "approved": True, "file": str(path)}


def reject_pending(state_dir: Path, entry_id: str) -> dict:
    """Drop one queued entry without making it effective."""
    return {**_take_pending(state_dir, entry_id), "approved": False}


# ---------------------------------------------------------------------- how a repository is tested
# A repository without a committed .kite.yml gets one kept by Kite instead (same keys), so nobody
# writes and commits the file by hand: start_task saves Kite's proposal the first time, the chat
# shows and changes it (repo_config tool), and a committed .kite.yml always wins.

def repo_config_path(state_dir: Path, repo: str | Path) -> Path:
    return Path(state_dir) / "memory" / f"{repo_key(repo)}.kite.yml"


def save_repo_config(state_dir: Path, repo: str | Path, text: str) -> Path:
    path = repo_config_path(state_dir, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"# Kite: cách cài và chạy test của {Path(repo).expanduser().resolve()} (repo chưa có .kite.yml)\n"
    body = "\n".join(line for line in text.strip().splitlines() if not line.startswith("# Kite: cách cài"))
    path.write_text(header + body + "\n", encoding="utf-8")
    return path
