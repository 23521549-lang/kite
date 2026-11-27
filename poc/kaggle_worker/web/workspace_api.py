"""The Workspace page's back end: the review queue, "Cần bạn quyết", and the review assistant.

The review assistant is an advisory OpenCode session per task, in
``<state_dir>/review/<task-slug>/``. It only advises: there is deliberately no apply/commit
endpoint here — applying and committing stay on the Task page, behind the owner's one-time
confirmation code. Its chat is a narrow proxy to the OpenCode server: only four operations —
create a session, list the folder's sessions, prompt, and read message history — and the last
two only for a session OpenCode itself lists under that task's review folder. No
permission/form/reply, no PATCH/DELETE/interrupt/agent: the review panel never answers a
permission request or drives a session outside its folder. The four operations are a strict
subset of the Chat tab's ``OPENCODE_ROUTES`` allowlist (``web/app.py``), checked by test.

This module owns no route registration: the Gói 4 lead mounts the router in ``create_app``
(``app.py``) at merge time. ``auth``/``audit`` are the closures from ``create_app``, so session,
CSRF, Origin and re-authentication checks are exactly the Chat tab's.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..backend import task_slug
from ..journal import Journal
from ..needs_decision import PATCH_EXCERPT_CHARS, scan_needs_decision

_SID = r"[A-Za-z0-9_-]{1,80}"          # same session-id shape as app.py
# The review proxy's allowlist, in app.py's (methods, pattern, keys) shape: create a session,
# list the folder's sessions, prompt, read message history. A strict subset of OPENCODE_ROUTES —
# no permission/form/reply, no PATCH/DELETE/interrupt/agent (see test_goi4_workspace.py).
REVIEW_ROUTES_SPEC: tuple[tuple[str, str, frozenset | None], ...] = (
    ("POST", r"api/session", frozenset({"agent"})),
    ("GET", r"api/session", None),
    ("GET", r"api/session/{S}/message", None),
    ("POST", r"api/session/{S}/prompt", frozenset({"text"})),
)
REVIEW_ROUTES: tuple[tuple[frozenset, re.Pattern, frozenset | None], ...] = tuple(
    (frozenset(methods.split()), re.compile(pattern.replace("{S}", _SID)),
     None if keys is None else frozenset(keys))
    for methods, pattern, keys in REVIEW_ROUTES_SPEC)
_SESSION_MESSAGE_RE = re.compile(r"api/session/(" + _SID + r")/(message|prompt)")

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _opencode_headers(password: str | None) -> dict[str, str]:
    """The same Basic auth the Chat tab uses (``app.py`` keeps its copy inside ``create_app``)."""
    if not password:
        return {}
    return {"Authorization": "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()}

REVIEW_INSTRUCTIONS = """# Trợ thủ review — task {task_id}

Bạn là trợ thủ review của Kite: đọc patch của task này và tư vấn cho chủ máy bằng tiếng Việt.
CHỈ TƯ VẤN — bạn không áp patch, không commit, không giao task mới, không sửa repo thật.
Nếu chủ hỏi có nên áp patch không, trả lời bằng nhận xét (rủi ro, chỗ cần xem lại), rồi để chủ
tự bấm Áp/Commit ở trang Task. Không tự tạo mã xác nhận, không đoán mã xác nhận.

## Task cần review

{goal}

## Patch (tóm tắt)

{summary}
"""


def _patch_files(patch_text: str) -> list[str]:
    """Changed files, from the ``diff --git`` headers of a patch."""
    files = []
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) >= 4 and parts[3].startswith("b/"):
                files.append(parts[3][2:])
    return files


def _review_summary(patch_text: str) -> str:
    files = _patch_files(patch_text)
    head = "File đổi:\n" + "\n".join(f"- {name}" for name in files) if files else "Không đọc được danh sách file đổi."
    excerpt = patch_text[:PATCH_EXCERPT_CHARS]
    if len(patch_text) > PATCH_EXCERPT_CHARS:
        excerpt += f"\n... (còn {len(patch_text) - PATCH_EXCERPT_CHARS} ký tự, xem đầy đủ ở trang Task)"
    return head + "\n\n```diff\n" + excerpt + "\n```"


class _StoreAdapter:
    """The scheduler store as ``scan_needs_decision`` uses it: only ``list_tasks()``."""

    def __init__(self, tasks: list[dict]):
        self._tasks = tasks

    def list_tasks(self) -> list[dict]:
        return self._tasks


def _scheduler_tasks(ctx: Any) -> list[dict]:
    """All scheduler tasks, or [] when the scheduler database is not there yet.

    The store is opened read-only in effect (only ``list_tasks()`` is called) and closed again;
    when the file does not exist yet nothing is created — an empty queue is the right answer.
    """
    ops = ctx.ops()
    db_path = getattr(ops, "db_path", None)
    if db_path is None or not Path(db_path).is_file():
        return []
    from ..store import SQLiteSchedulerStore

    store = SQLiteSchedulerStore(db_path)
    try:
        return store.list_tasks()
    finally:
        store.close()


def _caps(ctx: Any) -> dict[str, Any]:
    """Worker id -> relay caps, from the config (relay workers only).

    Kaggle/RunPod workers have no USD cap in the config (their quota is GPU seconds or an hourly
    pod price), so they are absent here and the scan skips them.
    """
    caps: dict[str, Any] = {}
    try:
        providers = ctx.config().providers
    except Exception:
        return caps
    for provider in providers or []:
        relay = getattr(provider, "relay", None)
        if relay is None:
            continue
        daily, monthly = getattr(relay, "daily_cap", None), getattr(relay, "monthly_cap", None)
        if daily is not None or monthly is not None:
            caps[provider.effective_worker_id] = {"daily_usd": daily, "monthly_usd": monthly}
    return caps


def _near_cap_pct(ctx: Any) -> float:
    try:
        return float(ctx.config().notify.near_cap_pct)
    except Exception:
        return 80


def _scan(ctx: Any) -> list[dict]:
    journal = ctx.journal() if hasattr(ctx, "journal") else Journal(ctx.state_dir / "journal")
    ledger = None
    try:
        ledger = ctx.ledger()
    except Exception:
        ledger = None
    return scan_needs_decision(_StoreAdapter(_scheduler_tasks(ctx)), journal, ctx.state_dir, ledger,
                               near_cap_pct=_near_cap_pct(ctx), caps=_caps(ctx))


def _review_dir(ctx: Any, task_id: str) -> Path:
    return Path(ctx.state_dir) / "review" / task_slug(task_id)


def _task_patch(ctx: Any, task_id: str) -> tuple[str, str]:
    """(goal, patch text) of a task, or 404 when the task has no patch to review."""
    tasks = {t.get("task_id"): t for t in _scheduler_tasks(ctx)}
    if task_id not in tasks:
        raise HTTPException(404, f"không có task {task_id}")
    patch_path = Path(ctx.state_dir) / "tasks" / task_slug(task_id) / "result.patch"
    if not patch_path.is_file():
        raise HTTPException(404, "task chưa có patch để review")
    goal = ""
    try:
        goal = str(json.loads(tasks[task_id].get("payload_json") or "{}").get("goal") or "")
    except (ValueError, AttributeError):
        goal = ""
    try:
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(404, "không đọc được patch của task") from exc
    return goal, patch_text


def _ensure_review_dir(ctx: Any, task_id: str) -> Path:
    """Create the review folder (opencode.json + AGENTS.md with the task's context)."""
    review_dir = _review_dir(ctx, task_id)
    goal, patch_text = _task_patch(ctx, task_id)
    review_dir.mkdir(parents=True, exist_ok=True)
    opencode_path = review_dir / "opencode.json"
    if not opencode_path.is_file():
        chat_config = Path(ctx.chat_dir) / "opencode.json" if getattr(ctx, "chat_dir", None) else None
        if chat_config is not None and chat_config.is_file():
            # Same model/provider/MCP setup as the main chat; only the instructions differ.
            opencode_path.write_bytes(chat_config.read_bytes())
        else:
            # No main chat configured yet: OpenCode runs with its own defaults.
            opencode_path.write_text("{}\n", encoding="utf-8")
    (review_dir / "AGENTS.md").write_text(
        REVIEW_INSTRUCTIONS.format(task_id=task_id, goal=goal or "(không có goal)",
                                   summary=_review_summary(patch_text)),
        encoding="utf-8")
    return review_dir


def create_router(ctx: Any, auth: Any, audit: Any) -> APIRouter:
    """The Workspace router; mounted into ``create_app`` by the Gói 4 lead at merge time."""
    router = APIRouter()

    @router.get("/api/workspace/queue")
    async def queue(request: Request):
        auth(request)
        return {"items": [item for item in _scan(ctx) if item["kind"] == "patch_awaiting"]}

    @router.get("/api/workspace/needs-decision")
    async def needs_decision(request: Request):
        # Read-only: this never sends ntfy. Sending is the secretary's scan job.
        auth(request)
        return {"items": _scan(ctx)}

    # ---------------------------------------------------------- the review proxy
    def review_route(method: str, path: str):
        for methods, pattern, keys in REVIEW_ROUTES:
            if method in methods and pattern.fullmatch(path):
                return keys if keys is not None else frozenset()
        raise HTTPException(403, "web không mở đường này của OpenCode")

    async def session_in_review_dir(client, url: str, headers: dict, review_dir: Path,
                                    session_id: str) -> bool:
        """True when OpenCode lists ``session_id`` under ``review_dir``.

        The list endpoint is the authority: a session id from another folder (e.g. the main
        chat) is neither readable nor promptable through the review proxy. An OpenCode that
        does not answer is a 502, not a silent "no".
        """
        import aiohttp

        try:
            async with client.get(f"{url}/api/session",
                                  params={"directory": str(review_dir), "limit": "100"},
                                  headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise HTTPException(502, "không liệt kê được phiên review của OpenCode")
                data = await response.json(content_type=None)
        except HTTPException:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise HTTPException(502, f"không gọi được OpenCode ({type(exc).__name__})") from exc
        items = data.get("data") if isinstance(data, dict) else None
        return isinstance(items, list) and any(
            isinstance(item, dict) and item.get("id") == session_id for item in items)

    async def review_proxy(request: Request, task_id: str, path: str):
        session = auth(request)
        review_dir = _review_dir(ctx, task_id)
        if not review_dir.is_dir():
            raise HTTPException(404, "chưa có phiên review cho task này: tạo phiên mới trước")
        keys = review_route(request.method, path)
        import aiohttp

        url, password = await asyncio.to_thread(ctx.opencode)
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("accept", "last-event-id")}
        headers.update(_opencode_headers(password))
        factory = ctx.http_factory or aiohttp.ClientSession
        client = factory()
        try:
            match = _SESSION_MESSAGE_RE.fullmatch(path)
            if match and not await session_in_review_dir(client, url, headers, review_dir,
                                                         match.group(1)):
                raise HTTPException(404, "phiên không thuộc thư mục review của task này")

            body: bytes | None = None
            params = list(request.query_params.multi_items())
            if request.method not in _SAFE_METHODS and request.method != "DELETE":
                data = await request.json() if await request.body() else {}
                if not isinstance(data, dict):
                    raise HTTPException(400, "body phải là JSON object")
                extra = set(data) - set(keys)
                if extra:
                    raise HTTPException(400, f"OpenCode: không gửi được {', '.join(sorted(extra))} từ web")
                if path == "api/session":
                    data["location"] = {"directory": str(review_dir)}
                body = json.dumps(data, ensure_ascii=False).encode()
            if path == "api/session" and request.method == "GET":
                # Listing is always scoped to this task's review folder, whatever the browser asks.
                params = [(k, v) for k, v in params if k != "directory"]
                params.append(("directory", str(review_dir)))
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                upstream = await client.request(request.method, f"{url}/{path}", params=params,
                                                data=body, headers=headers,
                                                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
            except aiohttp.ClientError as exc:
                await client.close()
                raise HTTPException(502, f"không gọi được OpenCode ({type(exc).__name__}); "
                                         "kiểm tra dịch vụ kite-opencode") from exc
            if path.endswith("/prompt"):
                audit(session, "review_chat", task_id)
        except HTTPException:
            await client.close()
            raise

        async def stream():
            try:
                async for chunk in upstream.content.iter_any():
                    yield chunk
            finally:
                upstream.release()
                await client.close()

        kept = {k: v for k, v in upstream.headers.items()
                if k.lower() in ("content-type", "cache-control")}
        return StreamingResponse(stream(), status_code=upstream.status, headers=kept)

    @router.post("/api/workspace/review/{task_id}/chat")
    async def review_create(request: Request, task_id: str):
        """Create a review session for a task (its folder + AGENTS.md are written first)."""
        session = auth(request)
        _ensure_review_dir(ctx, task_id)
        audit(session, "review_chat_create", task_id)
        return await review_proxy(request, task_id, "api/session")

    @router.api_route("/api/workspace/review/{task_id}/chat/{path:path}",
                      methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def review_chat(request: Request, task_id: str, path: str):
        return await review_proxy(request, task_id, path)

    return router
