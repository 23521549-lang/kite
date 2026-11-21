"""Per-repository memory: what the person said was wrong in earlier tasks on the same repository.

Like a project's ``CLAUDE.md``, but written by Kite: each ``bad`` note on a task (``note_task`` in the
chat, ``kite journal note TASK --bad "why"``) becomes one line in ``<state>/memory/<repo>.md``, and
every later task on that repository gets the most recent lines in its goal. The file stays on the
Kite host (never in the repository) and is plain Markdown: read it, edit it or delete lines by hand.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import time

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
