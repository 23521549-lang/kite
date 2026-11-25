"""Approved experience documents: distilled lessons for one repository, versioned.

Like ``memory.py``'s per-repo lessons, but curated: worker (a)'s ``experience.distill``
turns raw lessons/notes/runs into rules, this module stores the *proposed* document in a
pending queue until the owner approves it, then promotes it to the official document and
archives the previous version — one file per approved version, never overwritten.

Layout (all under ``<state>/memory/``, same convention as ``memory.py``)::

    <repo_key>.experience.md            the approved document (vN)
    <repo_key>.experience.proposed.md   the pending proposal (vN+1)
    <repo_key>.experience.v<N>.md       one file per approved version, never overwritten
    experience_pending.json             the queue (atomic write, mode 0600, like pending.json)

The approved document is what later tasks on the repository get in their goal
(:func:`experience_note`); the raw lesson lines stay untouched as the fallback.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .memory import repo_key

_MARKER = re.compile(r"<!-- kite:experience version=(\d+) updated=([0-9-]+) -->")
_TITLE_VERSION = re.compile(r"\(v\d+")
_SOURCE = re.compile(r"\[nguồn: ([^\]]+)\]")
_REFUTED_SUFFIX = re.compile(r"\s*\(cần xem lại\)\s*$")


def experience_path(state_dir: Path, repo: str | Path) -> Path:
    return Path(state_dir) / "memory" / f"{repo_key(repo)}.experience.md"


def proposed_path(state_dir: Path, repo: str | Path) -> Path:
    return Path(state_dir) / "memory" / f"{repo_key(repo)}.experience.proposed.md"


def archive_path(state_dir: Path, repo: str | Path, version: int) -> Path:
    return Path(state_dir) / "memory" / f"{repo_key(repo)}.experience.v{version}.md"


def pending_path(state_dir: Path) -> Path:
    return Path(state_dir) / "memory" / "experience_pending.json"


def _date(clock) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(clock()))


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


def _parse_version(text: str) -> tuple[int, str | None]:
    """(version, updated_at) from a document's marker; (0, None) when there is none."""
    match = _MARKER.search(text)
    if match is None:
        return 0, None
    return int(match.group(1)), match.group(2)


def current_version(state_dir: Path, repo: str | Path) -> int:
    """The approved document's version, 0 when there is no approved document yet."""
    try:
        return _parse_version(experience_path(state_dir, repo).read_text(encoding="utf-8"))[0]
    except OSError:
        return 0


def _rule_lines(text: str) -> list[dict]:
    """The ``- <rule> [nguồn: ...]`` lines of a document, as {text, source}.

    The citation is parsed wherever it sits on the line: the real renderer
    appends `` (cần xem lại)`` after it for refuted rules, so anchoring the
    citation to end-of-line loses those rules' sources (and their counts).
    That suffix is a rendering flag, not part of the rule, so it is stripped
    for a stable round-trip (parsed text == distilled text).
    """
    rules = []
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        source = _SOURCE.search(body)
        # A rendered line can list several sources ("task t-1 / bài học <date>, task t-2");
        # the first fragment is the rule's primary source and the key it is matched by.
        first = source.group(1).split(", ")[0].strip() if source else ""
        text = _REFUTED_SUFFIX.sub("", _SOURCE.sub("", body)).strip()
        rules.append({"text": " ".join(text.split()), "source": first})
    return rules


def _source_key(rule: dict) -> str:
    """The key a rule is matched by: its source, or its text when it has none.

    Sources come in two shapes: the ``source`` string parsed back from a rendered
    document (``task <id>`` / ``bài học <date>``) and the ``sources`` list of
    ``{task_id, date, verdict}`` dicts that :func:`experience.distill` returns.
    """
    source = rule.get("source")
    if isinstance(source, str) and source:
        return source
    sources = rule.get("sources") or []
    first = sources[0] if sources else None
    if isinstance(first, dict):
        parts = []
        if first.get("task_id"):
            parts.append(f"task {first['task_id']}")
        if first.get("date"):
            parts.append(f"bài học {first['date']}")
        if parts:
            return " / ".join(parts)
    elif isinstance(first, str) and first:
        return first
    return rule.get("text", "")


def _rule_counts(old: list[dict], new: list[dict]) -> tuple[int, int, int]:
    """(added, removed, changed) between old and new rules, matched by source.

    A rule is keyed by its source (``task <id>`` / ``bài học <date>``) when it has one,
    else by its text: same key with different text counts as changed, a key only on one
    side counts as added/removed.
    """
    old_by_key = {_source_key(r): r["text"] for r in old}
    new_by_key = {_source_key(r): r["text"] for r in new}
    added = sum(1 for k in new_by_key if k not in old_by_key)
    removed = sum(1 for k in old_by_key if k not in new_by_key)
    changed = sum(1 for k in new_by_key if k in old_by_key and new_by_key[k] != old_by_key[k])
    return added, removed, changed


_LESSON_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) \(([^)]+)\): (.*)$")


def lesson_dicts(state_dir: Path, repo: str | Path) -> list[dict]:
    """The repo's lesson lines as ``{date, task_id, text}`` — the shape
    :func:`experience.distill` takes (``memory.lesson_lines`` returns raw lines)."""
    from .memory import lesson_lines

    dicts = []
    for line in lesson_lines(state_dir, repo):
        match = _LESSON_LINE.match(line)
        if match:
            dicts.append({"date": match.group(1), "task_id": match.group(2), "text": match.group(3)})
        else:
            dicts.append({"date": None, "task_id": None,
                          "text": line[2:] if line.startswith("- ") else line})
    return dicts


def _read_doc(state_dir: Path, repo: str | Path) -> str:
    try:
        return experience_path(state_dir, repo).read_text(encoding="utf-8")
    except OSError:
        return ""


def _restamp(text: str, version: int, updated: str) -> str:
    """Rewrite a rendered document's version (title + marker) and updated date."""
    text = _TITLE_VERSION.sub(f"(v{version}", text, count=1)
    text = _MARKER.sub(f"<!-- kite:experience version={version} updated={updated} -->", text, count=1)
    return text


def propose(state_dir: Path, repo: str | Path, rules: list[dict], *, clock=time.time) -> dict:
    """Render a proposed document from ``rules`` and queue it for the owner's approval.

    ``rules`` are worker (a)'s distilled rules: ``{text, kind, sources}``. Returns
    ``{"entry": <queue entry>, "diff": <unified diff vs the approved document>}``.
    Raises ValueError when a proposal for this repo is already waiting.
    """
    from .experience import render_document  # worker (a); imported here so this module loads without it

    state_dir = Path(state_dir)
    if any(e.get("repo") == str(Path(repo).expanduser().resolve()) and e.get("status") == "pending"
           for e in _load_pending(state_dir)):
        raise ValueError(f"đã có bản đề xuất tài liệu kinh nghiệm đang chờ duyệt cho {repo}; "
                         "duyệt hoặc từ chối bản đó trước rồi mới chưng cất bản mới")
    version = current_version(state_dir, repo) + 1
    updated = _date(clock)
    content = render_document(repo, rules, version=version, updated=updated, clock=clock)
    old_rules = _rule_lines(_read_doc(state_dir, repo))
    added, removed, changed = _rule_counts(old_rules, rules)
    entry = {"id": uuid.uuid4().hex[:8], "repo": str(Path(repo).expanduser().resolve()),
             "version": version, "created_at": clock(), "rules_added": added,
             "rules_removed": removed, "rules_changed": changed, "status": "pending"}
    path = proposed_path(state_dir, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    entries = _load_pending(state_dir)
    entries.append(entry)
    _save_pending(state_dir, entries)
    return {"entry": entry, "diff": diff_text(state_dir, entry["id"])}


def _find_entry(state_dir: Path, entry_id: str) -> tuple[list[dict], dict]:
    entries = _load_pending(state_dir)
    for entry in entries:
        if entry.get("id") == entry_id:
            return entries, entry
    raise ValueError(f"không có đề xuất tài liệu kinh nghiệm '{entry_id}'; "
                     "xem danh sách đang chờ bằng `kite experience`")


def approve(state_dir: Path, entry_id: str, *, clock=time.time) -> dict:
    """Promote a pending proposal to the official document, archiving the old one.

    The old approved document (when there is one) is kept as ``.v<N>.md``; the new
    document is re-stamped to ``current version + 1`` so a stale proposal can never
    bump the version twice. Approving an already decided entry raises ValueError.
    """
    state_dir = Path(state_dir)
    entries, entry = _find_entry(state_dir, entry_id)
    if entry.get("status") != "pending":
        raise ValueError(f"đề xuất '{entry_id}' đã {entry.get('status')}; không duyệt lại được")
    proposed = proposed_path(state_dir, entry["repo"])
    try:
        content = proposed.read_text(encoding="utf-8")
    except OSError:
        raise ValueError(f"không còn file đề xuất cho '{entry_id}'") from None
    old_version = current_version(state_dir, entry["repo"])
    official = experience_path(state_dir, entry["repo"])
    if old_version:
        archive = archive_path(state_dir, entry["repo"], old_version)
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(official.read_text(encoding="utf-8"), encoding="utf-8")
    new_version = old_version + 1
    official.parent.mkdir(parents=True, exist_ok=True)
    official.write_text(_restamp(content, new_version, _date(clock)), encoding="utf-8")
    proposed.unlink(missing_ok=True)
    entry["status"] = "approved"
    entry["version"] = new_version
    _save_pending(state_dir, entries)
    return {**entry, "file": str(official)}


def reject(state_dir: Path, entry_id: str) -> dict:
    """Drop a pending proposal: its file and its queue entry are removed."""
    state_dir = Path(state_dir)
    entries, entry = _find_entry(state_dir, entry_id)
    if entry.get("status") != "pending":
        raise ValueError(f"đề xuất '{entry_id}' đã {entry.get('status')}; không từ chối lại được")
    proposed_path(state_dir, entry["repo"]).unlink(missing_ok=True)
    entries.remove(entry)
    _save_pending(state_dir, entries)
    return {**entry, "status": "rejected"}


def list_pending(state_dir: Path) -> list[dict]:
    """Proposals still waiting for the owner, oldest first."""
    return [e for e in _load_pending(state_dir) if e.get("status") == "pending"]


def show(state_dir: Path, repo: str | Path) -> dict:
    """The approved document for ``repo`` plus its pending proposal, if any."""
    state_dir = Path(state_dir)
    content = _read_doc(state_dir, repo)
    version, updated = _parse_version(content) if content else (0, None)
    pending = next((e for e in list_pending(state_dir)
                    if e.get("repo") == str(Path(repo).expanduser().resolve())), None)
    return {"repo": str(Path(repo).expanduser().resolve()), "version": version,
            "updated_at": updated, "content": content, "has_doc": bool(content), "pending": pending}


def diff_text(state_dir: Path, entry_id: str) -> str:
    """Unified diff between a proposal and the approved document (against empty for v1,
    so the whole new document shows as added lines for the owner to review)."""
    state_dir = Path(state_dir)
    _entries, entry = _find_entry(state_dir, entry_id)
    proposed = proposed_path(state_dir, entry["repo"])
    try:
        new = proposed.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        raise ValueError(f"không còn file đề xuất cho '{entry_id}'") from None
    old = _read_doc(state_dir, entry["repo"]).splitlines(keepends=True)
    return "".join(difflib.unified_diff(old, new, fromfile="tài liệu đã duyệt",
                                        tofile=f"đề xuất v{entry.get('version')}"))


def experience_note(state_dir: Path, repo: str | Path) -> str:
    """The approved document as a block for a task's goal, or "" when there is none."""
    content = _read_doc(state_dir, repo)
    if not content.strip():
        return ""
    return ("\n\nTài liệu kinh nghiệm đã duyệt cho repo này (làm theo, trừ khi yêu cầu ở trên nói khác):\n"
            + content.rstrip("\n"))
