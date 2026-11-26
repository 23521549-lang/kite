"""Secretary API (Gói 4, worker (b)): the Workspace bubble's back end.

``create_router(ctx, auth, audit)`` returns an APIRouter; the agent trưởng mounts it in
``create_app`` (this module never touches ``app.py`` itself). All routes need the web
session (``auth``) like the other routes.

- ``GET /api/secretary/reports`` -> ``[{date, title}]`` (newest first).
- ``GET /api/secretary/reports/{date}`` -> ``{date, markdown}``.
- ``POST /api/secretary/chat`` — create a session in the secretary folder (the bubble's
  ``newSession``; the response is OpenCode's ``{data: {id, ...}}`` verbatim).
- ``/api/secretary/chat/{path}`` — a NARROW REST proxy to the OpenCode server for the
  secretary folder only: list the folder's sessions, read a session's message history,
  prompt a session. Every (method, path) is checked against ``app.py``'s ``OPENCODE_ROUTES``
  allowlist first (lazy import, so this module never creates an import cycle with
  ``app.py``), then against the secretary subset below; anything else is 403. Message
  history and prompts only go to a session that the list call shows inside the secretary
  folder, so a session id from the main chat can never be read or driven through this
  proxy. Basic ``opencode:<password>`` auth mirrors ``app.py``'s ``opencode_headers``.
  The prompt waits at most 5 seconds for the reply stream (see ``_post_prompt``): the
  bubble polls history afterwards, so a long agent turn never blocks the send button.
- ``POST /api/secretary/report/run`` — build the report for a day right now (manual
  check): save the file, post it into the secretary session, send the one ntfy summary.
  Body ``{"date": "YYYY-MM-DD"}`` is optional (default: the Bangkok day that just ended).
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import secretary as sec

_SID = r"[A-Za-z0-9_-]{1,80}"
# (method, path regex, allowed body keys) — the secretary subset of OPENCODE_ROUTES.
# Four operations only: create a session, list the folder's sessions, prompt, read message
# history. A strict subset of OPENCODE_ROUTES — no permission/form/reply, no PATCH/DELETE/
# interrupt/agent. The REST shape matches the review proxy (workspace_api.py) so the same
# ProxyChat component serves both bubbles with only a different base path.
SECRETARY_CHAT_ROUTES: tuple[tuple[str, re.Pattern, frozenset | None], ...] = (
    ("POST", re.compile(r"api/session"), frozenset({"agent"})),
    ("GET", re.compile(r"api/session"), None),
    ("GET", re.compile(rf"api/session/{_SID}/message"), None),
    ("POST", re.compile(rf"api/session/{_SID}/prompt"), frozenset({"text"})),
)
PROMPT_REPLY_TIMEOUT = 5.0  # seconds to wait for the prompt's reply stream; see _post_prompt


def _opencode_headers(password: str | None) -> dict[str, str]:
    """Basic ``opencode:<password>`` auth, mirroring ``app.py``'s ``opencode_headers``."""
    if not password:
        return {}
    return {"Authorization": "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()}


def secretary_chat_allowed(method: str, path: str, body: dict | None) -> frozenset:
    """Body keys the secretary proxy may forward for (method, path), or raise 403/400.

    Checks ``app.py``'s ``OPENCODE_ROUTES`` first (the web's allowlist for OpenCode),
    then the secretary subset: only create / list / prompt / read history inside the
    secretary folder.
    """
    from .app import OPENCODE_ROUTES

    keys: frozenset | None = None
    for methods, pattern, allowed in OPENCODE_ROUTES:
        if method in methods and pattern.fullmatch(path):
            keys = frozenset() if allowed is None else allowed
            break
    if keys is None:
        raise HTTPException(403, "web không mở đường này của OpenCode")
    for route_method, pattern, route_keys in SECRETARY_CHAT_ROUTES:
        if method == route_method and pattern.fullmatch(path):
            allowed = frozenset() if route_keys is None else route_keys
            extra = set(body or {}) - set(allowed)
            if extra:
                raise HTTPException(400, f"thư ký: không gửi được {', '.join(sorted(extra))} từ web")
            return allowed
    raise HTTPException(403, "thư ký: chỉ tạo session, liệt kê, đọc lịch sử và gửi prompt")


def _parse_session_id(data: Any) -> str | None:
    """An OpenCode session id from a create/list payload, in any shape the API returns."""
    if isinstance(data, dict):
        if isinstance(data.get("id"), str) and data["id"]:
            return data["id"]
        if isinstance(data.get("sessionID"), str) and data["sessionID"]:
            return data["sessionID"]
        inner = data.get("data")
        if isinstance(inner, dict):
            return _parse_session_id(inner)
    return None


def _session_ids_in_list(data: Any) -> set[str]:
    """Session ids from a ``GET api/session`` payload (``{"data": [...]}`` or a bare list)."""
    items = data.get("data") if isinstance(data, dict) else data
    ids = set()
    if isinstance(items, list):
        for item in items:
            session_id = _parse_session_id(item)
            if session_id:
                ids.add(session_id)
    return ids


async def _call_opencode(ctx: Any, method: str, path: str, *, json_body: dict | None = None,
                         params: dict | None = None):
    """One OpenCode call through ``ctx``'s http factory. Returns (status, parsed body).

    The parsed body is JSON when the response parses as JSON, else raw text.
    """
    import aiohttp

    url, password = await asyncio.to_thread(ctx.opencode)
    headers = _opencode_headers(password)
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    factory = getattr(ctx, "http_factory", None) or aiohttp.ClientSession
    client = factory()
    try:
        async with client.request(method, f"{url}/{path}", params=params or {},
                                  data=json.dumps(json_body, ensure_ascii=False).encode()
                                  if json_body is not None else None,
                                  headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)) as response:
            raw = await response.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                parsed = raw.decode("utf-8", "replace") if raw else None
            return response.status, parsed
    except Exception as exc:
        raise HTTPException(502, f"không gọi được OpenCode ({type(exc).__name__}); "
                                 "kiểm tra dịch vụ kite-opencode") from exc
    finally:
        await client.close()


async def _secretary_session_ids(ctx: Any, folder: Path) -> set[str]:
    """Session ids OpenCode reports inside the secretary folder (502 when unreachable)."""
    status, data = await _call_opencode(ctx, "GET", "api/session",
                                        params={"directory": str(folder), "limit": "100"})
    if status != 200:
        raise HTTPException(502, f"không liệt kê được session thư ký (HTTP {status})")
    return _session_ids_in_list(data)


async def _ensure_secretary_session(ctx: Any, folder: Path) -> str:
    """The standing secretary session id: persisted, else created in the secretary folder."""
    session_id = sec.load_secretary_session_id(ctx.state_dir)
    if session_id:
        return session_id
    status, data = await _call_opencode(ctx, "POST", "api/session",
                                        json_body={"location": {"directory": str(folder)}})
    if status not in (200, 201):
        raise HTTPException(502, f"không tạo được session thư ký (HTTP {status})")
    session_id = _parse_session_id(data)
    if not session_id:
        raise HTTPException(502, "OpenCode không trả id session thư ký")
    sec.save_secretary_session_id(ctx.state_dir, session_id)
    return session_id


async def _post_prompt(ctx: Any, session_id: str, text: str) -> dict:
    """POST a prompt; returns ``{"accepted": bool, "note": str}``.

    The prompt endpoint streams the agent's reply (SSE) and the stream can stay open
    while the agent works. This waits only ``PROMPT_REPLY_TIMEOUT`` seconds for the
    reply body: a timeout still means the prompt was accepted (HTTP 200 came back),
    and the reply lands in the session history — it is NOT awaited here, so posting a
    report never blocks the caller for a whole agent turn. (Hypothesis to verify by
    hand: the report appears in the bubble and the secretary answers there.)
    """
    import aiohttp

    url, password = await asyncio.to_thread(ctx.opencode)
    headers = _opencode_headers(password)
    headers["Content-Type"] = "application/json"
    factory = getattr(ctx, "http_factory", None) or aiohttp.ClientSession
    client = factory()
    try:
        async with client.request(
                "POST", f"{url}/api/session/{session_id}/prompt",
                data=json.dumps({"text": text}, ensure_ascii=False).encode(), headers=headers,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)) as response:
            if response.status not in (200, 201):
                return {"accepted": False, "note": f"OpenCode trả HTTP {response.status}"}
            try:
                await asyncio.wait_for(response.read(), timeout=PROMPT_REPLY_TIMEOUT)
                return {"accepted": True, "note": "đã gửi prompt, agent đã trả lời"}
            except asyncio.TimeoutError:
                return {"accepted": True, "note": "đã gửi prompt, không chờ agent trả lời"}
    except Exception as exc:
        return {"accepted": False, "note": f"{type(exc).__name__}: {exc}"}
    finally:
        await client.close()


async def _post_report_to_secretary(ctx: Any, markdown: str) -> dict:
    """Ensure the folder + session, then post the report markdown into it."""
    folder = sec.secretary_dir(ctx.state_dir)
    try:
        sec.ensure_secretary_dir(config=ctx.config(), state_dir=ctx.state_dir,
                                 config_path=ctx.config_path)
    except Exception as exc:
        return {"posted": False, "error": f"không tạo được thư mục thư ký: {exc}"}
    try:
        session_id = await _ensure_secretary_session(ctx, folder)
    except HTTPException as exc:
        return {"posted": False, "error": str(exc.detail)}
    result = await _post_prompt(ctx, session_id, markdown)
    return {"posted": result["accepted"], "session_id": session_id,
            "error": None if result["accepted"] else result["note"],
            "note": result["note"] if result["accepted"] else None}


def _make_notifier(ctx: Any):
    """A ``Notifier`` from the config's ntfy reference, or None (off / unreadable)."""
    try:
        config = ctx.config()
        reference = config.notify.ntfy
        if not reference:
            return None
        from ..config import resolve_secret
        from ..notify import Notifier

        return Notifier(resolve_secret(reference))
    except Exception:
        return None


def _scan_fn_for(ctx: Any):
    """A zero-arg closure around the contract ``scan_needs_decision``, or None.

    Only when worker (c)'s module has landed AND the scheduler database already exists:
    this endpoint never creates ``scheduler.sqlite3`` itself. The store/ledger are built
    exactly like ``cli.py`` builds the gateway ledger.
    """
    try:
        from ..needs_decision import scan_needs_decision
    except ImportError:
        return None
    db_path = Path(ctx.state_dir) / "scheduler.sqlite3"
    if not db_path.is_file():
        return None
    try:
        from ..journal import Journal
        from ..relay import SpendLedger
        from ..spend_ledger import SpendStore
        from ..store import SQLiteSchedulerStore

        journal = Journal(ctx.state_dir / "journal")
        store = SQLiteSchedulerStore(db_path)
        ledger = SpendLedger(journal.read("spend"), store=SpendStore(ctx.state_dir / "spend.sqlite3"))

        def scan() -> list[dict]:
            return scan_needs_decision(store, journal, ctx.state_dir, ledger)

        return scan
    except Exception:
        return None


def create_router(ctx: Any, auth: Any, audit: Any) -> APIRouter:
    """The secretary router; mounted by the agent trưởng in ``create_app``."""
    router = APIRouter()

    @router.get("/api/secretary/reports")
    async def list_secretary_reports(request: Request):
        auth(request)
        return {"reports": sec.list_reports(ctx.state_dir)}

    @router.get("/api/secretary/reports/{date}")
    async def get_secretary_report(request: Request, date: str):
        auth(request)
        if sec.parse_report_date(date) is None:
            raise HTTPException(400, "ngày phải dạng YYYY-MM-DD")
        markdown = sec.load_report(ctx.state_dir, date)
        if markdown is None:
            raise HTTPException(404, f"không có báo cáo ngày {date}")
        return {"date": date, "markdown": markdown}

    @router.post("/api/secretary/chat")
    async def secretary_chat_create(request: Request):
        """Create a secretary session (the bubble's ``newSession``).

        The body may only carry ``agent``; the session is always created in the secretary
        folder. The response is OpenCode's payload verbatim (``{data: {id, ...}}``).
        """
        session = auth(request)
        folder = sec.secretary_dir(ctx.state_dir)
        if not (folder / "opencode.json").is_file():
            raise HTTPException(409, "chưa có thư mục thư ký: chạy kite chat setup cho thư ký")
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise HTTPException(400, "body phải là một object JSON")
        secretary_chat_allowed("POST", "api/session", body)
        create_body: dict[str, Any] = {"location": {"directory": str(folder)}}
        if body.get("agent"):
            create_body["agent"] = body["agent"]
        audit(session, "secretary_chat", "api/session")
        status, data = await _call_opencode(ctx, "POST", "api/session", json_body=create_body)
        return JSONResponse(status_code=status, content=data if data is not None else {})

    @router.api_route("/api/secretary/chat/{path:path}", methods=["GET", "POST"])
    async def secretary_chat_proxy(request: Request, path: str):
        """The bubble's narrow proxy: list sessions, read history, prompt — secretary folder only.

        Message history and prompts only reach a session OpenCode lists inside the secretary
        folder (a session id from the main chat is a 404); an OpenCode that does not answer
        the list is a 502, not a silent "no". Listing is always scoped to the secretary
        folder, whatever the browser asks.
        """
        session = auth(request)
        folder = sec.secretary_dir(ctx.state_dir)
        if not (folder / "opencode.json").is_file():
            raise HTTPException(409, "chưa có thư mục thư ký: chạy kite chat setup cho thư ký")
        body: dict = {}
        if request.method == "POST":
            try:
                body = await request.json() if await request.body() else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HTTPException(400, "body phải là JSON") from None
            if not isinstance(body, dict):
                raise HTTPException(400, "body phải là một object JSON")
        secretary_chat_allowed(request.method, path, body)
        audit(session, "secretary_chat", path)
        if request.method == "GET" and path == "api/session":
            status, data = await _call_opencode(
                ctx, "GET", "api/session", params={"directory": str(folder), "limit": "100"})
            return JSONResponse(status_code=status, content=data if data is not None else {})
        match = re.fullmatch(rf"api/session/({_SID})/(message|prompt)", path)
        if match is None:
            raise HTTPException(403, "thư ký: chỉ liệt kê, đọc lịch sử và gửi prompt")
        session_id, operation = match.group(1), match.group(2)
        known = await _secretary_session_ids(ctx, folder)
        if session_id not in known:
            raise HTTPException(404, "session không thuộc thư mục thư ký")
        if operation == "message":
            status, data = await _call_opencode(ctx, "GET", path)
            return JSONResponse(status_code=status, content=data if data is not None else {})
        result = await _post_prompt(ctx, session_id, body.get("text") or "")
        if not result["accepted"]:
            raise HTTPException(502, f"không gửi được prompt: {result['note']}")
        return {"accepted": True, "note": result["note"]}

    @router.post("/api/secretary/report/run")
    async def run_secretary_report(request: Request):
        session = auth(request)
        try:
            payload = await request.json() if await request.body() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "body phải là JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "body phải là một object JSON")
        day = payload.get("date")
        if day is not None and sec.parse_report_date(day) is None:
            raise HTTPException(400, "ngày phải dạng YYYY-MM-DD")
        from ..journal import Journal

        journal = Journal(ctx.state_dir / "journal")
        notifier = _make_notifier(ctx)
        scan_fn = _scan_fn_for(ctx)
        result = await asyncio.to_thread(
            sec.run_daily_report, ctx.state_dir, day=day, journal=journal,
            scan_fn=scan_fn, notifier=notifier)
        posted = await _post_report_to_secretary(ctx, Path(result["path"]).read_text(encoding="utf-8"))
        audit(session, "secretary_report_run", result["date"])
        return {"date": result["date"], "path": result["path"],
                "done": len(result["data"]["done"]), "failed": len(result["data"]["failed"]),
                "spend_total_usd": result["data"]["spend_total_usd"],
                "needs_decision": len(result["data"]["needs_decision"]),
                "needs_decision_available": result["needs_decision_available"],
                "notified": result["notified"], "posted": posted}

    return router
