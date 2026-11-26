""""Cần bạn quyết": the durable record of everything that needs the owner's decision.

ntfy is best-effort (a message can be lost), so this module is the record that survives: it scans
the state Kite already keeps — finished tasks with a patch still waiting, lessons waiting for
approval, spend near a cap, tasks that failed — and turns each into one item with a stable ``key``.
The scan only reads; sending ntfy is a separate step (``notify_new_items``), one message per new
key, so a lost message never loses the decision itself.

The secretary (Gói 4, worker (b)) owns the scan schedule; ``GET /api/workspace/needs-decision``
only reads, it never sends.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from . import memory
from .backend import task_slug
from .journal import Journal

FAILED_WINDOW_SECONDS = 24 * 3600          # a FAILED task stays "needs a decision" for a day
PATCH_EXCERPT_CHARS = 3000                 # how much of a patch goes into a review AGENTS.md
NOTIFY_LABELS = {
    "patch_awaiting": "patch chờ duyệt",
    "lesson_pending": "bài học chờ duyệt",
    "quota_near": "sắp chạm trần chi phí",
    "task_failed": "task lỗi",
}


def _goal(task: dict[str, Any]) -> str:
    """The goal text of a scheduler task row ("" when the payload is not JSON)."""
    try:
        return str(json.loads(task.get("payload_json") or "{}").get("goal") or "")
    except (ValueError, AttributeError):
        return ""


def _applied_and_committed(journal: Journal) -> tuple[set[str], set[str]]:
    """Task ids with a successful apply/commit, from journal/actions.jsonl.

    Every MCP tool call is recorded there by ``KiteOps.logged`` (``mcp_server.py:142-162``), so a
    successful ``apply_result``/``commit_result`` is the trustworthy signal that the owner already
    acted on a patch. The same rule is used by ``journal.summarize`` (``journal.py:205-206``).
    The web audit log (``web/db.py``) records the same actions, but the scan does not receive the
    web store, and the journal is the copy the scheduler side already owns.
    """
    actions = journal.read("actions") if journal is not None else []
    applied = {a.get("task_id") for a in actions if a.get("tool") == "apply_result" and a.get("ok")}
    committed = {a.get("task_id") for a in actions if a.get("tool") == "commit_result" and a.get("ok")}
    return applied - {None}, committed - {None}


def _cap_pair(spec: Any) -> tuple[float | None, float | None]:
    """(daily_usd, monthly_usd) caps of one worker, from a dict or a RelaySpec-like object."""
    if isinstance(spec, dict):
        return spec.get("daily_usd"), spec.get("monthly_usd")
    return getattr(spec, "daily_cap", None), getattr(spec, "monthly_cap", None)


def _pct(spent: float, cap: float | None) -> float | None:
    if cap is None or cap <= 0:
        return None
    return spent / cap * 100


def scan_needs_decision(store: Any, journal: Journal, state_dir: str | Path, ledger: Any, *,
                        near_cap_pct: float = 80, caps: dict[str, Any] | None = None,
                        clock: Any = time.time) -> list[dict]:
    """Everything that needs the owner's decision right now, newest first.

    ``store`` is the scheduler store (only ``list_tasks()`` is used); ``journal`` the state dir's
    journal; ``ledger`` a ``SpendLedger`` (only ``spent(worker, now)`` is used, or None to skip the
    quota kind); ``caps`` maps worker id -> caps (``{"daily_usd": .., "monthly_usd": ..}`` or a
    RelaySpec-like object). Without ``caps`` the ``quota_near`` kind is skipped: the scan never
    invents a cap. Kaggle/RunPod workers have no USD cap in the config (their quota is GPU seconds
    or an hourly pod price), so they are skipped the same way and never reported here.

    ``near_cap_pct`` is a threshold, not a policy number baked into the scan: it comes from
    ``notify.near_cap_pct`` in the config (default 80) and must be in (0, 100].
    """
    if isinstance(near_cap_pct, bool) or not isinstance(near_cap_pct, (int, float)) \
            or not 0 < near_cap_pct <= 100:
        raise ValueError("near_cap_pct must be between 0 and 100")
    now = clock()
    state_dir = Path(state_dir)
    items: list[dict] = []

    try:
        tasks = store.list_tasks() if store is not None else []
    except Exception:
        tasks = []
    applied, committed = _applied_and_committed(journal)
    acted = applied | committed
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        state = task.get("state")
        created_at = task.get("created_at")
        created = float(created_at) if isinstance(created_at, (int, float)) else now
        if state == "COMPLETED":
            patch_path = state_dir / "tasks" / task_slug(task_id) / "result.patch"
            if task_id not in acted and patch_path.is_file():
                goal = _goal(task)
                items.append({
                    "kind": "patch_awaiting",
                    "title": (goal[:200] or task_id),
                    "detail": "Patch đã xong, chờ chủ duyệt để áp/commit (áp/commit vẫn ở trang Task).",
                    "task_id": task_id, "link": f"/tasks/{task_id}",
                    "created_at": created, "key": f"patch:{task_id}",
                })
        elif state == "FAILED" and now - created <= FAILED_WINDOW_SECONDS:
            goal = _goal(task)
            items.append({
                "kind": "task_failed",
                "title": (goal[:200] or task_id),
                "detail": f"Task lỗi ({str(task.get('last_error') or '')[:200] or 'không rõ lý do'}).",
                "task_id": task_id, "link": f"/tasks/{task_id}",
                "created_at": created, "key": f"failed:{task_id}",
            })

    for entry in memory.list_pending(state_dir):
        task_id = entry.get("task_id") or None
        items.append({
            "kind": "lesson_pending",
            "title": str(entry.get("text") or "")[:200],
            "detail": f"Bài học cho {entry.get('repo') or 'repo'} chờ chủ duyệt mới có hiệu lực.",
            "task_id": task_id,
            "link": f"/tasks/{task_id}" if task_id and task_id != "chat" else "/workspace",
            "created_at": float(entry.get("created_at") or now),
            "key": f"lesson:{entry.get('id')}",
        })

    if ledger is not None and caps:
        spent = getattr(ledger, "spent", None)
        if callable(spent):
            for worker_id in sorted(caps):
                daily_cap, monthly_cap = _cap_pair(caps[worker_id])
                try:
                    day, month = spent(worker_id, now)
                except Exception:
                    continue
                for period, amount, cap in (("daily", day, daily_cap), ("monthly", month, monthly_cap)):
                    pct = _pct(float(amount or 0), cap)
                    if pct is not None and pct >= near_cap_pct:
                        when = "hôm nay" if period == "daily" else "tháng này"
                        items.append({
                            "kind": "quota_near",
                            "title": f"Worker {worker_id} sắp chạm trần chi phí",
                            "detail": f"Đã chi {float(amount or 0):.2f}/{cap:.2f} USD {when} "
                                      f"({pct:.0f}%, ngưỡng {near_cap_pct:g}%).",
                            "task_id": None, "link": "/workers",
                            "created_at": now, "key": f"quota:{worker_id}:{period}",
                        })

    items.sort(key=lambda item: (-float(item["created_at"]), item["key"]))
    return items


# ---------------------------------------------------------------------- ntfy (best-effort)

def _notified_path(state_path: str | Path) -> Path:
    """``state_path`` is the notified.json file itself (``state_dir/needs_decision/notified.json``)."""
    return Path(state_path)


def _load_notified(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items()
            if isinstance(v, (int, float))} if isinstance(data, dict) else {}


def _save_notified(path: Path, data: dict[str, float]) -> None:
    """Atomic write, like ``memory._save_pending`` (``memory.py:185-205``): temp file + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=1, ensure_ascii=False)
    os.replace(temp, path)


def notify_new_items(notifier: Any, items: list[dict], state_path: str | Path, *,
                     clock: Any = time.time) -> list[dict]:
    """Send ntfy once per new item key; returns the items that were sent.

    The message carries only the task id and a short label — never code or goal text
    (``notify.py``: the topic name is the only key, so messages stay short). A key already in
    ``notified.json`` is skipped, so a repeated scan never re-sends. ``notifier`` is a
    ``Notifier`` (or None: notifications off, nothing is sent and nothing is recorded). A failed
    send is not recorded, so the next scan retries it.
    """
    if notifier is None:
        return []
    path = _notified_path(state_path)
    notified = _load_notified(path)
    now = clock()
    sent: list[dict] = []
    for item in items:
        key = item["key"]
        if key in notified:
            continue
        label = NOTIFY_LABELS.get(item["kind"], "cần bạn quyết")
        task_id = item.get("task_id")
        message = (f"{task_id}\n{label}" if task_id else label)
        try:
            notifier("Kite: cần bạn quyết", message, tags=("warning",), priority=4)
        except Exception:
            continue
        notified[key] = now
        sent.append(item)
    if sent:
        _save_notified(path, notified)
    return sent
