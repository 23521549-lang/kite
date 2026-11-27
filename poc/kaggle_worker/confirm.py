"""One-time confirmation codes for the two MCP tools that change a real repository.

``apply_result`` and ``commit_result`` act on the owner's working tree, so a chat model's say-so is
not enough: the owner runs ``kite tasks confirm <task-id>`` in a local terminal, sees the patch's
SHA-256 and stats there, and gets a short-lived code per action. The code is bound to
(action, task_id, patch_sha256), expires after 15 minutes and is consumed by the first successful
use. Only the code's SHA-256 digest is kept on disk (``<state>/confirmations.json``, mode 0600),
so reading the state directory does not hand out usable codes. The lead model cannot mint codes:
issuing one is a CLI action, not an MCP tool.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import time

from . import joblock

CODE_TTL_SECONDS = 900          # 15 minutes
ACTIONS = ("apply", "commit")


class ConfirmError(Exception):
    """A confirmation code was missing, unknown, bound to something else, expired or reused."""


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / "confirmations.json"


def _digest(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def patch_sha256(patch: str | bytes) -> str:
    data = patch.encode("utf-8") if isinstance(patch, str) else patch
    return hashlib.sha256(data).hexdigest()


def _load(state_dir: Path) -> dict:
    try:
        data = json.loads(_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(state_dir: Path, records: dict) -> None:
    joblock.write_json(_path(state_dir), records, indent=1, sort_keys=True)


def issue_code(state_dir: Path, action: str, task_id: str, patch_hash: str, *,
               clock=time.time) -> str:
    """Mint a code for one (action, task, patch) triple; returns the plaintext code (shown once,
    to the person running the CLI). Expired and used records are pruned on every issue."""
    if action not in ACTIONS:
        raise ConfirmError(f"action phải là một trong: {', '.join(ACTIONS)}")
    now = clock()
    code = secrets.token_urlsafe(18)
    with joblock.updating(_path(state_dir)):
        records = {d: r for d, r in _load(state_dir).items()
                   if isinstance(r, dict) and not r.get("used") and r.get("expires_at", 0) > now}
        records[_digest(code)] = {"action": action, "task_id": task_id, "patch_sha256": patch_hash,
                                  "created_at": now, "expires_at": now + CODE_TTL_SECONDS, "used": False}
        _save(state_dir, records)
    return code


def _record(state_dir: Path, code: str) -> tuple[dict, dict]:
    if not (code or "").strip():
        raise ConfirmError("thiếu mã xác nhận: người dùng chạy `kite tasks confirm <task-id>` trong "
                           "terminal của máy này và đưa mã cho bạn; mã chỉ dùng được 1 lần trong 15 phút")
    records = _load(state_dir)
    record = records.get(_digest(code))
    if record is None:
        raise ConfirmError("mã xác nhận không đúng (chưa từng được cấp, hoặc đã bị thay bằng mã mới)")
    return records, record


def check_code(state_dir: Path, action: str, task_id: str, patch_hash: str, code: str, *,
               clock=time.time) -> None:
    """Validate a code without consuming it; raises ConfirmError saying exactly what is wrong."""
    _records, record = _record(state_dir, code)
    if record.get("used"):
        raise ConfirmError("mã xác nhận đã được dùng rồi; mỗi mã chỉ dùng 1 lần — người dùng chạy lại "
                           "`kite tasks confirm` để lấy mã mới")
    if clock() > record.get("expires_at", 0):
        raise ConfirmError("mã xác nhận đã hết hạn (15 phút); người dùng chạy lại `kite tasks confirm`")
    if (record.get("action"), record.get("task_id"), record.get("patch_sha256")) != (action, task_id, patch_hash):
        raise ConfirmError("mã xác nhận không khớp với việc này (khác thao tác, khác task, hoặc patch "
                           "đã thay đổi từ lúc cấp mã); người dùng kiểm tra lại bằng `kite tasks confirm`")


def consume_code(state_dir: Path, action: str, task_id: str, patch_hash: str, code: str, *,
                 clock=time.time) -> None:
    """Validate and burn a code after the action it guards has succeeded. Under the lock, so two
    callers with the same code cannot both pass the check before either burns it."""
    with joblock.updating(_path(state_dir)):
        check_code(state_dir, action, task_id, patch_hash, code, clock=clock)
        records = _load(state_dir)
        records[_digest(code)]["used"] = True
        _save(state_dir, records)
