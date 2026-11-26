"""The web back end: FastAPI routes for every tab, the events WebSocket, and two proxies (the OpenCode
server for Chat, Kite's browser for the Worker tab), and the built front end (web/dist).

Security (spec 5.4): one account; argon2 password and TOTP, both needed; 5 wrong tries lock the account
for 15 minutes and 20 from one address in 15 minutes block that address — the same counting guards
re-authentication for sensitive actions, an expired lock restarts the count, and a lock is audited and
notified; 8 one-time recovery codes stand in for a lost authenticator; a session seen from a new
address or browser must re-authenticate before it can write again; the session cookie is
``__Host-`` prefixed, HttpOnly, Secure, SameSite=Strict, 12 hours idle and 7 days at most; every write
needs the session's CSRF token in ``X-CSRF-Token`` and a same-site Origin; WebSockets check Origin;
answers carry a strict Content-Security-Policy; there is no /docs. Sensitive actions go to the audit log.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hmac
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import ConfigError, load_config
from ..journal import FILES, Journal, read_jsonl, summarize
from ..mcp_server import KiteToolError
from . import knowledge as know
from . import security as sec
from . import workers as wk
from .monitor import register_monitor_routes
from .monitor_alerts import register_monitor_alert_routes
from .presets import PROVIDER_PRESETS
from .db import Store, User

IDLE_SECONDS = 12 * 3600
ABSOLUTE_SECONDS = 7 * 86400
LOCK_AFTER, LOCK_SECONDS = 5, 900
IP_LIMIT, IP_WINDOW = 20, 900
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
       "object-src 'none'")
GatewayCall = Callable[[str, str, dict | None], Awaitable[dict]]
OPENCODE_TESTED = "2.4.0"            # the OpenCode version the Chat tab was built and tested against
# Gói 2: soft warning when this many chat sessions are open at once (measured RSS of one
# `kite mcp serve` is ~62 MB; the limit is a warning, never a block).
MAX_OPEN_SESSIONS_SOFT = 10
_SID = r"[A-Za-z0-9_-]{1,80}"
# The OpenCode routes the Chat tab needs, and the body fields it may send: nothing else of the OpenCode
# server (its shell, terminal, file and credential routes) is reachable from the web. A new session is
# always made in the chat folder of its Kite session (Kite sets its location), and a prompt carries
# text only. Without a kite_session_id the new session falls back to the legacy single chat folder.
OPENCODE_ROUTES: tuple[tuple[frozenset, re.Pattern, frozenset | None], ...] = tuple(
    (frozenset(methods.split()), re.compile(pattern.replace("{S}", _SID)), None if keys is None else frozenset(keys))
    for methods, pattern, keys in (
        ("GET", r"api/(info|agent|model|event|permission/request)", None),
        ("GET", r"api/session", None),
        ("POST", r"api/session", ("agent", "kite_session_id")),
        ("GET DELETE", r"api/session/{S}", None),
        ("PATCH", r"api/session/{S}", ("title", "archived")),
        ("GET", r"api/session/{S}/(message|permission|form)", None),
        ("POST", r"api/session/{S}/prompt", ("text",)),
        ("POST", r"api/session/{S}/interrupt", ()),
        ("POST", r"api/session/{S}/agent", ("agent",)),
        ("POST", r"api/session/{S}/permission/{S}/reply", ("decision", "message")),
        ("POST", r"api/session/{S}/form/{S}/reply", ("answer",)),
    ))


class GatewayDown(RuntimeError):
    pass


@dataclass
class Session:
    sid: str
    user: User
    csrf: str
    ip: str
    agent: str = ""
    # The request's address or browser differs from the ones this session was opened from: reads keep
    # working, but writes wait for a fresh password + 2FA (see auth() and check_second_factor).
    needs_reauth: bool = False


class WebContext:
    """What the routes need: the config file, the web's store, Kite's operations, the gateway."""

    def __init__(self, config_path: Path, store: Store, sealer: sec.Sealer, *,
                 ops_factory: Callable[[Any], Any], gateway: GatewayCall, clock: Callable[[], float] = time.time,
                 secure_cookies: bool = True, opencode_url: str = "http://127.0.0.1:4096",
                 opencode_password: str | None = None, gateway_ws: str = "ws://127.0.0.1:8787",
                 restart: Callable[[list[str]], Awaitable[dict]] | None = None, dist: Path | None = None,
                 http_factory: Callable | None = None, ledger_factory: Callable[[], Any] | None = None,
                 quota: Callable[[], Awaitable[dict]] | None = None, events_every: float = 2.0,
                 opencode_locator: Callable[[], tuple[str, str | None]] | None = None,
                 chat_dir: Path | None = None) -> None:
        self.config_path = Path(config_path)
        self.store, self.sealer, self.gateway = store, sealer, gateway
        self.clock, self.secure_cookies = clock, secure_cookies
        self.opencode_url, self.opencode_password = opencode_url.rstrip("/"), opencode_password
        self.opencode_locator, self.chat_dir = opencode_locator, chat_dir
        self.gateway_ws, self.restart, self.dist = gateway_ws.rstrip("/"), restart, dist
        self.http_factory, self.quota, self.events_every = http_factory, quota, events_every
        self._ops_factory, self._ledger_factory = ops_factory, ledger_factory
        self._config, self._config_mtime, self._ops = None, None, None

    @property
    def state_dir(self) -> Path:
        return self.config_path.parent

    @property
    def cookie(self) -> str:
        return "__Host-kite" if self.secure_cookies else "kite_session"

    def config(self):
        mtime = self.config_path.stat().st_mtime_ns
        if self._config is None or mtime != self._config_mtime:
            self._config, self._config_mtime, self._ops = load_config(self.config_path), mtime, None
        return self._config

    def ops(self):
        config = self.config()
        if self._ops is None:
            self._ops = self._ops_factory(config)
        return self._ops

    def ledger(self):
        if self._ledger_factory is not None:
            return self._ledger_factory()
        from ..relay import SpendLedger

        return SpendLedger(Journal(self.state_dir / "journal").read("spend"))

    def journal(self) -> Journal:
        return Journal(self.state_dir / "journal")

    def opencode(self) -> tuple[str, str | None]:
        """Where the OpenCode server listens and its password (found again on every call: the background
        service may have restarted)."""
        if self.opencode_locator is not None:
            url, password = self.opencode_locator()
            return url.rstrip("/"), password
        return self.opencode_url, self.opencode_password


def _session_busy(item: Any) -> bool:
    """An OpenCode 2 session is working when its last ``busy`` time is after its last ``idle`` time."""
    times = item.get("time") if isinstance(item, dict) else None
    if not isinstance(times, dict) or item.get("parentID") or times.get("archived"):
        return False
    busy, idle = times.get("busy"), times.get("idle")
    return isinstance(busy, (int, float)) and (not isinstance(idle, (int, float)) or busy > idle)


def env_cost_usd(journal: Any, tasks: list[dict[str, Any]], env_id: str, *,
                 month_only: bool = False, now: float | None = None) -> float | None:
    """USD spent on tasks of one environment (a lower bound, never fabricated).

    Journal spend records carry ``tag=<task_id>`` (relay.py records
    ``"tag": job.tag``); a task's environment comes from the scheduler store.
    Chat (``"chat"``), bench (``"bench-*"``), pod (``kind="pod"``, no tag) and
    pre-Gói-2 spend have no task link and are excluded. Branch retries tag
    ``"<task_id>-b<N>"``: the suffix is stripped. Unreadable journal: None
    (unknown, not zero).

    With ``month_only``, only records dated in the current UTC month count —
    the same UTC month the spend ledger uses (relay.py ``_month``: "làm mới
    ngày 1, UTC"), so the number matches the "Chi tháng này" label. A record
    without a numeric ``ts`` has no provable month and is skipped in that
    mode (every record the journal itself writes carries ``ts``).
    """
    task_env = {t.get("task_id"): t.get("environment_id") for t in tasks}
    try:
        records = journal.read("spend")
    except Exception:
        return None
    month: tuple[int, int] | None = None
    if month_only:
        current = time.gmtime(now if now is not None else time.time())
        month = (current.tm_year, current.tm_mon)
    total = 0.0
    for record in records:
        tag = record.get("tag")
        if not isinstance(tag, str) or not tag:
            continue
        base = re.sub(r"-b\d+$", "", tag)
        if task_env.get(base) != env_id:
            continue
        if month is not None:
            ts = record.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            stamp = time.gmtime(float(ts))
            if (stamp.tm_year, stamp.tm_mon) != month:
                continue
        usd = record.get("usd")
        if isinstance(usd, (int, float)):
            total += float(usd)
    return round(total, 6)


def find_opencode(home: Path, fallback_url: str, fallback_password: str | None) -> tuple[str, str | None]:
    """OpenCode 2 runs one background server per user (the TUI and the web chat share it): it writes its
    address and password to ``~/.local/state/opencode/service.json``. Only a loopback address is used.
    Without that file: the address in Kite's config and the password in ``secrets/opencode_server_password``."""
    url, password = fallback_url, fallback_password
    try:
        data = json.loads((home / ".local" / "state" / "opencode" / "service.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("url"), str):
        host = urlsplit(data["url"]).hostname
        if host in ("127.0.0.1", "localhost", "::1"):
            url = data["url"]
            password = data.get("password") if isinstance(data.get("password"), str) else None
            if password is None:
                try:
                    saved = json.loads((home / ".config" / "opencode" / "service.json").read_text(encoding="utf-8"))
                    password = saved.get("password") if isinstance(saved, dict) else None
                except (OSError, ValueError):
                    password = None
    return url.rstrip("/"), password


# ---------------------------------------------------------------- request bodies
class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=200)
    code: str = Field(max_length=20)        # a 6-digit TOTP code, or a one-time recovery code (XXXX-XXXX-XXXX)


class PasswordIn(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)
    code: str = Field(max_length=12)


class TotpBeginIn(BaseModel):
    password: str = Field(max_length=200)
    code: str = Field(max_length=12)


class TotpConfirmIn(BaseModel):
    code: str = Field(max_length=12)


class TaskIn(BaseModel):
    repo: str = Field(max_length=500)
    goal: str = Field(max_length=20000)
    task_type: str = "coding"
    model: str | None = None
    continue_from: str = ""
    ignore_uncommitted: bool = False


class TextIn(BaseModel):
    text: str = Field(default="", max_length=20000)


class NoteIn(BaseModel):
    verdict: str
    reason: str = Field(default="", max_length=2000)


class CommitIn(BaseModel):
    message: str = Field(max_length=2000)


class RepoIn(BaseModel):
    name: str = Field(max_length=100)
    path: str = Field(max_length=500)
    url: str = Field(default="", max_length=500)


class MemoryIn(BaseModel):
    text: str = Field(max_length=2000)
    repo: str = ""


class ExperienceIn(BaseModel):
    repo: str


class RepoConfigIn(BaseModel):
    repo: str
    config: str = Field(max_length=20000)


class StartIn(BaseModel):
    model: str | None = None
    force: bool = False


class SwitchIn(BaseModel):
    on: bool


class BenchIn(BaseModel):
    label: str = ""
    suites: str = "code"
    models: str = ""
    worker: str = ""
    # Network approval (Gói 7, bench-standard §5): the hosts the owner confirmed for this
    # run, after seeing them. The server re-validates them (format + declared in task.yaml)
    # in bench_start; a run without network_ack never gets the network.
    network_ack: bool = False
    network_hosts: list[str] = []


class RestartIn(BaseModel):
    services: list[str] = ["kite-gateway"]
    force: bool = False


class CleanupIn(BaseModel):
    days: float = 7


class ChatSessionIn(BaseModel):
    environment_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    kind: str = "chat"


class ChatMoveIn(BaseModel):
    to_env: str


class EnvIn(BaseModel):
    name: str = Field(max_length=100)
    path: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=500)
    branch: str | None = Field(default=None, max_length=200)
    pat: str | None = Field(default=None, max_length=500)


class PatIn(BaseModel):
    pat: str = Field(max_length=500)


# ---------------------------------------------------------------- the app
def client_ip(request) -> str:
    """The caller's address; behind Caddy on this machine, the one Caddy saw (last X-Forwarded-For entry)."""
    host = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and host in ("127.0.0.1", "::1"):
        return forwarded.split(",")[-1].strip()[:64]
    return host[:64]


def same_origin(request, origin: str | None) -> bool:
    """A browser's Origin must be this site: its host and port are the ones the request came to."""
    if not origin:
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return bool(parts.netloc) and parts.netloc.lower() == (request.headers.get("host") or "").lower()


def create_app(ctx: WebContext, *, start_scheduler: bool = False) -> FastAPI:
    """Build the web app.

    ``start_scheduler`` is True only for the real server (``cli.py`` ``_web_serve``): the
    Gói 4 scheduler engine (plan schedules + 00:00 secretary report + 5-minute
    needs-decision scan) runs as a background task of the web process. Tests and other
    callers get no engine.
    """

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        task = None
        if start_scheduler:
            try:
                from .scheduler_wiring import start_scheduler_engine

                task = start_scheduler_engine(ctx)
            except Exception as exc:
                print(f"kite web: không khởi động được động cơ hẹn giờ ({exc}); lịch sẽ không chạy",
                      file=sys.stderr, flush=True)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Kite", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(ConfigError)
    async def config_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(GatewayDown)
    async def gateway_down(_request, exc):
        return JSONResponse({"error": f"không gọi được gateway: {exc}"}, status_code=502)

    @app.exception_handler(KiteToolError)
    async def tool_error(_request, exc):
        # Gói 2/Gói 3: an apply error carries structured fields (ApplyConflictError, defined by
        # worker (b) in mcp_server.py); an ordinary tool error stays {"error": str} as before.
        body: dict[str, Any] = {"error": str(exc)}
        code = getattr(exc, "code", None)
        if code is not None:
            body["code"] = code
        task_id = getattr(exc, "task_id", None)
        if task_id is not None:
            body["task_id"] = task_id
        head = getattr(exc, "head", None)
        if head is not None:
            body["head"] = head
        failed_files = getattr(exc, "failed_files", None)
        if failed_files is not None:
            body["failed_files"] = list(failed_files)
        return JSONResponse(body, status_code=400)

    @app.exception_handler(ValueError)
    async def value_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    # ------------------------------------------------------------------ sessions
    def session_of(request) -> Session | None:
        token = request.cookies.get(ctx.cookie)
        if not token:
            return None
        sid = sec.digest(token)
        row = ctx.store.session(sid)
        now = ctx.clock()
        if row is None:
            return None
        if now - row.last_seen > IDLE_SECONDS or now - row.created_at > ABSOLUTE_SECONDS:
            ctx.store.drop_session(sid)
            return None
        user = ctx.store.user(row.user_id)
        if user is None:
            return None
        if now - row.last_seen > 60:
            ctx.store.touch_session(sid)
        ip, agent = client_ip(request), request.headers.get("user-agent", "")
        drift = bool((row.ip and row.ip != ip) or (row.agent and row.agent != agent))
        return Session(sid, user, row.csrf, ip, agent, drift)

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        # Every API route needs a session, before its body is even read: a new route is closed by default.
        if request.url.path.startswith("/api/") and request.url.path not in ("/api/auth/login", "/api/health"):
            if await asyncio.to_thread(session_of, request) is None:
                return JSONResponse({"error": "cần đăng nhập"}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
        return await call_next(request)

    # Writes stay open on a drifted session only where re-authentication itself happens (and signing
    # out, which must always work); a successful check_second_factor adopts the new address/browser.
    REAUTH_PATHS = ("/api/auth/password", "/api/auth/totp/begin", "/api/auth/totp/confirm",
                    "/api/auth/logout")

    def auth(request: Request) -> Session:
        session = session_of(request)
        if session is None:
            raise HTTPException(401, "cần đăng nhập")
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not same_origin(request, origin):
                raise HTTPException(403, "yêu cầu từ trang khác")
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
                raise HTTPException(403, "thiếu mã CSRF")
            if session.needs_reauth and request.url.path not in REAUTH_PATHS:
                raise HTTPException(403, "phiên đăng nhập đã đổi địa chỉ IP hoặc trình duyệt; "
                                         "nhập lại mật khẩu và mã 2FA ở trang tài khoản để tiếp tục")
        return session

    def audit(session: Session | None, action: str, target: str = "", ok: bool = True,
              detail: dict | None = None, *, ip: str = "", username: str = "") -> None:
        ctx.store.audit(session.user.username if session else username, session.ip if session else ip, action,
                        target, ok, detail)

    def note_lockout(username: str, ip: str) -> None:
        """Best effort: tell the owner the account just locked (ntfy, when configured — the same
        mechanism the settings page tests). A notification problem never breaks signing in."""
        try:
            config = ctx.config()
            if not config.notify.ntfy:
                return
            from .. import notify
            from ..config import resolve_secret

            notify.send(resolve_secret(config.notify.ntfy), "Kite: tài khoản web bị khóa",
                        f"Tài khoản {username} tạm khóa {LOCK_SECONDS // 60} phút vì nhập sai nhiều lần "
                        f"(lần gần nhất từ {ip}). Nếu không phải bạn, hãy kiểm tra máy.")
        except Exception:
            pass

    def register_failure(user: User, ip: str, now: float) -> bool:
        """One more wrong try against the account (sign-in or re-authentication — both count the same).
        True when this try locks the account; the lock is audited as its own event. A lock that has
        already expired starts the count from zero, so the owner is never one typo away from a fresh
        lock for the rest of time — an attacker must spend a full set of tries on every lock."""
        base = 0 if 0 < user.locked_until <= now else user.failed
        failed = base + 1
        locked = failed >= LOCK_AFTER
        ctx.store.update_user(user.id, failed=failed,
                              locked_until=now + LOCK_SECONDS if locked else (0.0 if base == 0
                                                                              else user.locked_until))
        if locked:
            ctx.store.audit(user.username, ip, "lockout", user.username, False,
                            {"minutes": LOCK_SECONDS // 60})
        return locked

    def set_cookie(response: Response, token: str) -> None:
        response.set_cookie(ctx.cookie, token, max_age=ABSOLUTE_SECONDS, httponly=True, secure=ctx.secure_cookies,
                            samesite="strict", path="/")

    # ------------------------------------------------------------------ auth
    @app.post("/api/auth/login")
    async def login(body: LoginIn, request: Request, response: Response):
        ip = client_ip(request)
        origin = request.headers.get("origin")
        if origin is not None and not same_origin(request, origin):
            raise HTTPException(403, "yêu cầu từ trang khác")
        if ctx.store.failures_from(ip, IP_WINDOW) >= IP_LIMIT:
            raise HTTPException(429, "quá nhiều lần đăng nhập sai từ địa chỉ này; thử lại sau 15 phút")
        if not ctx.store.user_count():
            raise HTTPException(409, "máy này chưa có tài khoản: chạy `sudo kite-stack admin` (hoặc `kite web admin`) "
                                     "trên máy")
        now = ctx.clock()
        user = ctx.store.user_by_name(body.username.strip())
        if user is not None and user.locked_until > now:
            ctx.store.attempt(ip, body.username, False)
            raise HTTPException(429, f"tài khoản tạm khóa {round((user.locked_until - now) / 60) or 1} phút "
                                     "vì nhập sai nhiều lần")
        password_ok = await asyncio.to_thread(sec.verify_password, user.password_hash if user else None, body.password)
        counter = None
        via_recovery = False
        if user is not None and password_ok:
            counter = sec.verify_totp(ctx.sealer.unseal(user.totp_secret), body.code, now=now,
                                      last_counter=user.totp_last)
            if counter is None:
                # A one-time recovery code stands in for the 2FA code exactly once (it is spent here,
                # and only ever behind a correct password).
                via_recovery = await asyncio.to_thread(ctx.store.use_recovery_code, user.id,
                                                       sec.recovery_digest(body.code))
        if user is None or not password_ok or (counter is None and not via_recovery):
            ctx.store.attempt(ip, body.username, False)
            if user is not None and register_failure(user, ip, now):
                await asyncio.to_thread(note_lockout, user.username, ip)
            audit(None, "login", body.username[:64], False, ip=ip, username=body.username[:64])
            raise HTTPException(401, "sai tên đăng nhập, mật khẩu hoặc mã 2FA")
        ctx.store.update_user(user.id, failed=0, locked_until=0.0,
                              **({"totp_last": counter} if counter is not None else {}))
        ctx.store.attempt(ip, user.username, True)
        ctx.store.prune_sessions(idle=IDLE_SECONDS, absolute=ABSOLUTE_SECONDS)
        token, csrf = sec.new_token(), sec.new_token()
        ctx.store.add_session(sec.digest(token), user.id, csrf, ip, request.headers.get("user-agent", ""))
        if await asyncio.to_thread(_needs_rehash, user.password_hash):
            ctx.store.update_user(user.id, password_hash=await asyncio.to_thread(sec.hash_password, body.password))
        set_cookie(response, token)
        ctx.store.audit(user.username, ip, "login_recovery" if via_recovery else "login", user.username, True)
        return {"username": user.username, "csrf": csrf}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        session = auth(request)
        ctx.store.drop_session(session.sid)
        response.delete_cookie(ctx.cookie, path="/", secure=ctx.secure_cookies, httponly=True, samesite="strict")
        audit(session, "logout")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request):
        session = auth(request)
        return {"username": session.user.username, "csrf": session.csrf,
                "sessions": ctx.store.list_sessions(session.user.id)}

    @app.post("/api/auth/logout-others")
    async def logout_others(request: Request):
        session = auth(request)
        dropped = ctx.store.drop_sessions(session.user.id, keep=session.sid)
        audit(session, "logout_others", detail={"sessions": dropped})
        return {"dropped": dropped}

    def check_second_factor(session: Session, password: str, code: str) -> int:
        """Re-proving both factors for a sensitive action, with the same protections as signing in:
        the per-address limit, the account's failure counter and its lock all apply here too — a
        session on its own is not a licence to guess at the password and the 2FA code forever.
        Success clears the counter (like a sign-in) and adopts the session's current address and
        browser, which is what releases a session that drifted (see session_of)."""
        user = ctx.store.user(session.user.id)
        now, ip = ctx.clock(), session.ip
        if ctx.store.failures_from(ip, IP_WINDOW) >= IP_LIMIT:
            audit(session, "reauth", ok=False, detail={"reason": "ip_limited"})
            raise HTTPException(429, "quá nhiều lần nhập sai từ địa chỉ này; thử lại sau 15 phút")
        if user.locked_until > now:
            raise HTTPException(429, f"tài khoản tạm khóa {round((user.locked_until - now) / 60) or 1} phút "
                                     "vì nhập sai nhiều lần")
        counter = sec.verify_totp(ctx.sealer.unseal(user.totp_secret), code, now=now,
                                  last_counter=user.totp_last)
        if not sec.verify_password(user.password_hash, password) or counter is None:
            ctx.store.attempt(ip, user.username, False)
            locked = register_failure(user, ip, now)
            audit(session, "reauth", ok=False)
            if locked:
                note_lockout(user.username, ip)
            raise HTTPException(403, "mật khẩu hoặc mã 2FA không đúng")
        ctx.store.attempt(ip, user.username, True)
        ctx.store.update_user(user.id, failed=0, locked_until=0.0)
        ctx.store.update_session_fingerprint(session.sid, ip, session.agent)
        return counter

    @app.post("/api/auth/password")
    async def change_password(body: PasswordIn, request: Request):
        session = auth(request)
        counter = await asyncio.to_thread(check_second_factor, session, body.current, body.code)
        problem = sec.password_problem(body.new)
        if problem:
            raise HTTPException(400, problem)
        hashed = await asyncio.to_thread(sec.hash_password, body.new)
        ctx.store.update_user(session.user.id, password_hash=hashed, password_changed_at=ctx.clock(),
                              totp_last=counter)
        dropped = ctx.store.drop_sessions(session.user.id, keep=session.sid)
        audit(session, "password_changed", detail={"other_sessions_closed": dropped})
        return {"ok": True, "other_sessions_closed": dropped}

    @app.post("/api/auth/totp/begin")
    async def totp_begin(body: TotpBeginIn, request: Request):
        session = auth(request)
        counter = await asyncio.to_thread(check_second_factor, session, body.password, body.code)
        secret = sec.new_totp_secret()
        ctx.store.update_user(session.user.id, totp_pending=ctx.sealer.seal(secret), totp_last=counter)
        audit(session, "totp_begin")
        return {"secret": secret, "uri": sec.otpauth_uri(secret, session.user.username),
                "next": "thêm mã này vào app xác thực rồi gửi mã 6 số đầu tiên tới /api/auth/totp/confirm"}

    @app.post("/api/auth/totp/confirm")
    async def totp_confirm(body: TotpConfirmIn, request: Request):
        session = auth(request)
        user = ctx.store.user(session.user.id)
        if not user.totp_pending:
            raise HTTPException(400, "chưa bắt đầu đổi 2FA")
        secret = ctx.sealer.unseal(user.totp_pending)
        counter = sec.verify_totp(secret, body.code, now=ctx.clock())
        if counter is None:
            raise HTTPException(400, "mã chưa đúng; kiểm tra giờ trên điện thoại")
        ctx.store.update_user(user.id, totp_secret=user.totp_pending, totp_pending=None, totp_last=counter)
        codes = sec.new_recovery_codes()
        ctx.store.replace_recovery_codes(user.id, [sec.recovery_digest(c) for c in codes])
        audit(session, "totp_changed")
        return {"ok": True, "recovery_codes": codes,
                "next": "lưu các mã khôi phục này ở nơi an toàn (chỉ hiện lần này): mỗi mã đăng nhập "
                        "thay mã 2FA được đúng một lần khi mất điện thoại"}

    # ------------------------------------------------------------------ gateway helpers
    async def gateway_status() -> dict | None:
        try:
            return await ctx.gateway("GET", "/status", None)
        except GatewayDown:
            return None

    # ------------------------------------------------------------------ overview
    # Gói 8 — Giám sát: GET /api/monitor (worker (a)) + route cảnh báo (worker (c)).
    register_monitor_routes(app, ctx, auth)
    register_monitor_alert_routes(app, ctx, auth)

    @app.get("/api/overview")
    async def overview(request: Request):
        auth(request)
        status = await gateway_status()
        ledger = ctx.ledger()
        config = ctx.config()
        rows = wk.worker_rows(config, status, ledger, _auto_click(ctx))
        tasks = await ctx.ops().list_tasks(limit=50)
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        quota = None
        if ctx.quota is not None:
            try:
                quota = await ctx.quota()
            except Exception:
                quota = None
        return {"gateway_up": status is not None, "workers": rows,
                "queued_jobs": (status or {}).get("queued_jobs", 0), "running_jobs": (status or {}).get("running_jobs", 0),
                "spend": {"today_usd": round(sum(r["spend"]["today_usd"] for r in rows), 4),
                          "month_usd": round(sum(r["spend"]["month_usd"] for r in rows), 4)},
                "tasks": {"counts": counts, "recent": tasks[:10]}, "gpu_quota": quota}

    # ------------------------------------------------------------------ providers and workers
    @app.get("/api/provider-types")
    async def provider_types(request: Request):
        auth(request)
        return {"types": wk.PROVIDER_TYPES}

    @app.get("/api/provider-presets")
    async def provider_presets(request: Request):
        auth(request)
        return {"presets": PROVIDER_PRESETS}

    @app.post("/api/providers/test")
    async def provider_test(request: Request):
        auth(request)
        body = await _json(request)
        return await wk.test_connection(body, session_factory=ctx.http_factory)

    @app.get("/api/workers")
    async def workers(request: Request):
        auth(request)
        status = await gateway_status()
        return {"gateway_up": status is not None,
                "workers": wk.worker_rows(ctx.config(), status, ctx.ledger(), _auto_click(ctx))}

    @app.post("/api/workers")
    async def add_worker(request: Request):
        session = auth(request)
        body = await _json(request)
        try:
            report = wk.add_worker(ctx.config_path, body, sealer=ctx.sealer)
        except ConfigError as exc:
            audit(session, "worker_add", str(body.get("id", "")), False, {"error": str(exc), "type": body.get("type")})
            raise
        audit(session, "worker_add", report["added"], True, {"type": report["type"]})
        return report

    @app.patch("/api/workers/{worker_id}")
    async def edit_worker(worker_id: str, request: Request):
        session = auth(request)
        body = await _json(request)
        report = wk.update_worker(ctx.config_path, worker_id, body, sealer=ctx.sealer)
        audit(session, "worker_update", worker_id, True,
              {"fields": [k for k in body if k not in wk.SECRET_FIELDS],
               "keys_changed": [k for k in body if k in wk.SECRET_FIELDS]})
        return report

    @app.delete("/api/workers/{worker_id}")
    async def delete_worker(worker_id: str, request: Request):
        session = auth(request)
        report = wk.remove_worker(ctx.config_path, worker_id)
        audit(session, "worker_remove", worker_id)
        return report

    def worker_kind(worker_id: str) -> str:
        provider = next((p for p in ctx.config().providers if p.effective_worker_id == worker_id), None)
        if provider is None or provider.adapter not in ("pull", "relay"):
            raise HTTPException(404, f"không có worker {worker_id}")
        return wk.kind_of(provider)

    @app.post("/api/workers/{worker_id}/start")
    async def start_worker(worker_id: str, request: Request, body: StartIn | None = None):
        session = auth(request)
        kind, body = worker_kind(worker_id), body or StartIn()
        if kind in ("openai", "anthropic"):
            report = await ctx.gateway("POST", "/kite/switch", {"worker": worker_id, "on": True})
        else:
            report = await ctx.gateway("POST", "/kite/wake", {"worker": worker_id, "model": body.model,
                                                              "reason": "web", "force": body.force})
        audit(session, "worker_start", worker_id, True, {"kind": kind, "paid": kind != "kaggle"})
        return report

    @app.post("/api/workers/{worker_id}/stop")
    async def stop_worker(worker_id: str, request: Request):
        session = auth(request)
        kind = worker_kind(worker_id)
        if kind in ("openai", "anthropic"):
            report = await ctx.gateway("POST", "/kite/switch", {"worker": worker_id, "on": False})
        else:
            report = await ctx.gateway("POST", "/kite/stop", {"worker": worker_id})
        audit(session, "worker_stop", worker_id, True, {"kind": kind})
        return report

    @app.post("/api/workers/{worker_id}/auto")
    async def auto_start(worker_id: str, body: SwitchIn, request: Request):
        session = auth(request)
        worker_kind(worker_id)
        report = await ctx.gateway("POST", "/kite/switch", {"worker": worker_id, "auto": body.on})
        audit(session, "worker_autostart", worker_id, True, {"on": body.on})
        return report

    @app.post("/api/workers/{worker_id}/auto-click")
    async def auto_click(worker_id: str, body: SwitchIn, request: Request):
        session = auth(request)
        if worker_kind(worker_id) != "kaggle":
            raise HTTPException(400, "chỉ worker Kaggle có công tắc Kite tự bấm")
        from ..browser import set_auto_click

        set_auto_click(ctx.state_dir, worker_id, body.on)
        audit(session, "kaggle_auto_click", worker_id, True, {"on": body.on})
        return {"worker": worker_id, "auto_click": body.on}

    @app.post("/api/workers/{worker_id}/browser")
    async def open_browser(worker_id: str, request: Request):
        session = auth(request)
        if worker_kind(worker_id) != "kaggle":
            raise HTTPException(400, "trình duyệt Kite chỉ dành cho worker Kaggle")
        report = await ctx.gateway("POST", "/kite/browser/open", {"worker": worker_id})
        audit(session, "kaggle_browser_open", worker_id)
        return report

    @app.get("/api/spend")
    async def spend(request: Request, days: int = 31):
        auth(request)
        since = ctx.clock() - max(1, min(days, 400)) * 86400
        by_day: dict[str, dict[str, float]] = {}
        for record in ctx.journal().read("spend", since):
            day = str(record.get("at", ""))[:10]
            worker = str(record.get("worker"))
            by_day.setdefault(day, {})[worker] = round(by_day.get(day, {}).get(worker, 0.0)
                                                       + float(record.get("usd") or 0), 6)
        return {"days": [{"day": d, "workers": w, "usd": round(sum(w.values()), 6)} for d, w in sorted(by_day.items())]}

    # ------------------------------------------------------------------ tasks
    @app.get("/api/tasks")
    async def tasks(request: Request, limit: int = 30):
        auth(request)
        return {"tasks": await ctx.ops().list_tasks(limit=max(1, min(limit, 50)))}

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str, request: Request):
        auth(request)
        ops = ctx.ops()
        report: dict[str, Any] = {"log": await ops.task_log(task_id, limit=80)}
        try:
            report["status"] = await ops.task_status(task_id)
        except Exception as exc:                  # Temporal down: the trace above still tells the story
            report["status"] = {"error": str(exc)[:300]}
        if report["status"].get("workflow") == "COMPLETED":
            report["result"] = await ops.task_result(task_id)
        # Trace + lỗi áp bền vững: worker (a) (Gói 3) đọc 3 cột này từ hàng task trong store
        # và trả trong task_status (nơi đã đọc hàng task cho queue_state); app.py chỉ chuyền qua,
        # null khi task cũ chưa đóng dấu / chưa từng áp lỗi. Không đoán ngược từ nguồn khác.
        report["environment_id"] = report["status"].get("environment_id")
        report["chat_session_id"] = report["status"].get("chat_session_id")
        report["last_apply_error"] = report["status"].get("last_apply_error")
        from ..journal import show_task
        from ..backend import task_slug

        resolved = report["log"].get("task_id", task_id)
        report["journal"] = show_task(ctx.journal(), resolved,
                                      ctx.state_dir / "tasks" / task_slug(resolved) / "trace.jsonl")
        # Gói 2/Gói 3: stamp the origin on the detail, the same fields the list carries.
        # Absent while the scheduler store has no such task: the trace above still stands.
        for key in ("chat_session_id", "environment_id", "main_repo", "last_apply_error"):
            report.setdefault(key, None)
        try:
            with scheduler_store() as store:
                row = store.get_task(resolved)
            for key in ("chat_session_id", "environment_id", "main_repo", "last_apply_error"):
                report[key] = row.get(key)
        except Exception:
            pass
        return report

    @app.get("/api/tasks/{task_id}/patch")
    async def task_patch(task_id: str, request: Request):
        auth(request)
        from ..backend import task_slug

        resolved = ctx.ops().resolve(task_id)
        path = ctx.state_dir / "tasks" / task_slug(resolved) / "result.patch"
        if not path.is_file():
            raise HTTPException(404, "task chưa có patch")
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))

    @app.post("/api/tasks")
    async def new_task(body: TaskIn, request: Request):
        session = auth(request)
        ops = ctx.ops()
        report = await ops.logged("start_task", ops.start_task, body.repo, body.goal, body.task_type, body.model,
                                  body.continue_from, body.ignore_uncommitted)
        audit(session, "task_start", report.get("task_id", ""), True, {"repo": body.repo})
        return report

    async def task_action(request: Request, task_id: str, name: str, *args, audited: bool = False):
        session = auth(request)
        ops = ctx.ops()
        try:
            report = await ops.logged(name, getattr(ops, name), task_id, *args)
        except Exception as exc:
            if audited:
                audit(session, name, task_id, False, {"error": str(exc)[:300]})
            raise
        if audited:
            audit(session, name, report.get("task_id", task_id) if isinstance(report, dict) else task_id)
        return report

    @app.post("/api/tasks/{task_id}/apply")
    async def task_apply(task_id: str, request: Request):
        return await task_action(request, task_id, "apply_result", audited=True)

    @app.post("/api/tasks/{task_id}/rerun")
    async def task_rerun(task_id: str, request: Request):
        # rerun_task (worker (a), mcp_server.py): tạo task mới với continue_from=task_id,
        # tự rơi về task mới từ đầu khi patch cũ không gộp được; trả về task_id mới.
        report = await task_action(request, task_id, "rerun_task", audited=True)
        return {"task_id": report["task_id"]}

    @app.post("/api/tasks/{task_id}/apply-error/dismiss")
    async def task_dismiss_apply_error(task_id: str, request: Request):
        # dismiss_apply_error (worker (a), mcp_server.py): xóa last_apply_error của task
        # trong store + ghi journal note "patch đã bỏ". Chỉ KiteOps chạm được store task.
        return await task_action(request, task_id, "dismiss_apply_error", audited=True)

    @app.post("/api/tasks/{task_id}/commit")
    async def task_commit(task_id: str, body: CommitIn, request: Request):
        return await task_action(request, task_id, "commit_result", body.message, audited=True)

    @app.post("/api/tasks/{task_id}/cancel")
    async def task_cancel(task_id: str, request: Request):
        return await task_action(request, task_id, "cancel_task", audited=True)

    @app.post("/api/tasks/{task_id}/pause")
    async def task_pause(task_id: str, request: Request):
        return await task_action(request, task_id, "pause_task")

    @app.post("/api/tasks/{task_id}/resume")
    async def task_resume(task_id: str, request: Request):
        return await task_action(request, task_id, "resume_task")

    @app.post("/api/tasks/{task_id}/instruct")
    async def task_instruct(task_id: str, body: TextIn, request: Request):
        return await task_action(request, task_id, "send_instruction", body.text)

    @app.post("/api/tasks/{task_id}/note")
    async def task_note(task_id: str, body: NoteIn, request: Request):
        return await task_action(request, task_id, "note_task", body.verdict, body.reason)

    # ------------------------------------------------------------------ repositories
    @app.get("/api/repos")
    async def repos(request: Request):
        auth(request)
        ops = ctx.ops()
        return {"repos": ctx.store.repo_rows(), "allowed_roots": [str(r) for r in ops.allowed_roots],
                "memory": await ops.memory_show("")}

    @app.post("/api/repos")
    async def add_repo(body: RepoIn, request: Request):
        session = auth(request)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", body.name):
            raise HTTPException(400, "tên repo chỉ gồm chữ, số, '.', '_', '-'")
        await ctx.ops().repo_status(body.path)          # inside the allowed folders, a Git repo with a commit
        repo_id = ctx.store.add_repo(body.name, str(Path(body.path).expanduser().resolve()), body.url)
        audit(session, "repo_add", body.name, True, {"path": body.path})
        return {"id": repo_id}

    @app.delete("/api/repos/{repo_id}")
    async def remove_repo(repo_id: int, request: Request):
        session = auth(request)
        if not ctx.store.drop_repo(repo_id):
            raise HTTPException(404, "không có repo này")
        audit(session, "repo_remove", str(repo_id))
        return {"ok": True}

    @app.get("/api/repos/status")
    async def repo_status(request: Request, repo: str):
        auth(request)
        return await ctx.ops().repo_status(repo)

    @app.get("/api/repos/memory")
    async def repo_memory(request: Request, repo: str = ""):
        auth(request)
        return await ctx.ops().memory_show(repo)

    @app.post("/api/repos/memory")
    async def repo_remember(body: MemoryIn, request: Request):
        session = auth(request)
        report = await ctx.ops().remember(body.text, body.repo)
        audit(session, "memory_add", body.repo or "chat")
        return report

    @app.delete("/api/repos/memory/{number}")
    async def repo_forget(number: int, request: Request, repo: str = ""):
        session = auth(request)
        report = await ctx.ops().memory_forget(number, repo)
        audit(session, "memory_forget", repo or "chat", True, {"number": number})
        return report

    # ------------------------------------------------------- experience documents (Gói 10)
    @app.get("/api/repos/experience")
    async def repo_experience(request: Request, repo: str):
        auth(request)
        return await ctx.ops().experience_show(repo)

    @app.post("/api/repos/experience/distill")
    async def repo_experience_distill(body: ExperienceIn, request: Request):
        session = auth(request)
        report = await ctx.ops().experience_distill(body.repo)
        audit(session, "experience_distill", body.repo, True, {"proposal_id": report["proposal_id"]})
        return report

    @app.post("/api/repos/experience/{proposal_id}/approve")
    async def repo_experience_approve(proposal_id: str, request: Request):
        session = auth(request)
        report = await ctx.ops().experience_approve(proposal_id)
        audit(session, "experience_approve", proposal_id, True, {"version": report["version"]})
        return report

    @app.post("/api/repos/experience/{proposal_id}/reject")
    async def repo_experience_reject(proposal_id: str, request: Request):
        session = auth(request)
        report = await ctx.ops().experience_reject(proposal_id)
        audit(session, "experience_reject", proposal_id)
        return report

    @app.get("/api/repos/experience/{proposal_id}/diff")
    async def repo_experience_diff(proposal_id: str, request: Request):
        auth(request)
        return await ctx.ops().experience_diff(proposal_id)

    @app.get("/api/repos/experience/improve")
    async def repo_experience_improve(request: Request):
        auth(request)
        return await ctx.ops().improve_list()

    @app.post("/api/repos/experience/improve/{proposal_id}/approve")
    async def repo_experience_improve_approve(proposal_id: str, request: Request):
        session = auth(request)
        report = await ctx.ops().improve_approve(proposal_id)
        audit(session, "improve_approve", proposal_id)
        return report

    @app.post("/api/repos/experience/improve/{proposal_id}/reject")
    async def repo_experience_improve_reject(proposal_id: str, request: Request):
        session = auth(request)
        report = await ctx.ops().improve_reject(proposal_id)
        audit(session, "improve_reject", proposal_id)
        return report

    @app.get("/api/repos/config")
    async def repo_config(request: Request, repo: str):
        auth(request)
        return await ctx.ops().repo_config(repo)

    @app.put("/api/repos/config")
    async def save_repo_config(body: RepoConfigIn, request: Request):
        session = auth(request)
        report = await ctx.ops().repo_config(body.repo, body.config)
        audit(session, "repo_config", body.repo)
        return report

    # ------------------------------------------------------------------ chat sessions + environments (Gói 2)
    def scheduler_store():
        """The scheduler SQLite store: the chat_sessions registry lives there. KiteOps owns the
        database path; a caller without _store cannot serve these routes."""
        ops = ctx.ops()
        factory = getattr(ops, "_store", None)
        if factory is None:
            raise HTTPException(501, "scheduler store chưa sẵn sàng cho session chat")
        return factory()

    def session_dir(kite_session_id: str) -> Path:
        return ctx.state_dir / "chat-sessions" / kite_session_id

    def check_session_id(kite_session_id: str) -> str:
        if not re.fullmatch(_SID, kite_session_id or ""):
            raise HTTPException(400, "id session không hợp lệ")
        return kite_session_id

    def session_out(row: dict[str, Any], *, warning: str | None = None) -> dict[str, Any]:
        out = {**row, "directory": str(session_dir(str(row["kite_session_id"])))}
        if warning is not None:
            out["warning"] = warning
        return out

    def env_rows() -> list[dict[str, Any]]:
        rows = [{"id": "default", "name": "Default", "path": "", "url": "", "kind": "default"}]
        for repo in ctx.store.repo_rows():
            rows.append({"id": str(repo["id"]), "name": repo["name"], "path": repo["path"],
                         "url": repo["url"], "kind": "github" if repo["url"] else "local"})
        return rows

    def require_env(env_id: str) -> dict[str, Any]:
        for row in env_rows():
            if row["id"] == env_id:
                return row
        raise HTTPException(404, "không có môi trường này")

    @app.get("/api/chat/sessions")
    async def chat_sessions(request: Request):
        auth(request)
        with scheduler_store() as store:
            return {"sessions": [session_out(row) for row in store.list_chat_sessions()]}

    @app.post("/api/chat/sessions")
    async def create_chat_session(body: ChatSessionIn, request: Request):
        session = auth(request)
        kind = (body.kind or "chat").strip()
        if kind not in ("chat", "review", "secretary"):
            raise HTTPException(400, "kind phải là chat, review hoặc secretary")
        environment_id = (body.environment_id or "default").strip() or "default"
        if environment_id != "default":
            require_env(environment_id)
        kite_session_id = uuid.uuid4().hex
        directory = session_dir(kite_session_id)
        try:
            _write_session_files(ctx, kite_session_id, directory)
            with scheduler_store() as store:
                open_count = len(store.list_chat_sessions(status="active"))
                row = store.create_chat_session(kite_session_id, None, environment_id,
                                                body.title, kind=kind)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        warning = "many_sessions" if open_count + 1 >= MAX_OPEN_SESSIONS_SOFT else None
        audit(session, "chat_session_create", kite_session_id, True,
              {"environment_id": environment_id, "kind": kind})
        return session_out(row, warning=warning)

    @app.post("/api/chat/sessions/{kite_session_id}/move")
    async def move_chat_session(kite_session_id: str, body: ChatMoveIn, request: Request):
        session = auth(request)
        check_session_id(kite_session_id)
        to_env = (body.to_env or "").strip()
        require_env(to_env)
        with scheduler_store() as store:
            row = store.get_chat_session(kite_session_id)
            if row is None:
                raise HTTPException(404, "không có session này")
            if row["status"] != "active":
                raise HTTPException(409, "session đã lưu trữ hoặc đã xóa")
            from_env = str(row["environment_id"])
            if from_env != "default":
                raise HTTPException(409, "session đã ở một repo thì không đổi môi trường nữa")
            if to_env == "default":
                raise HTTPException(409, "session đã ở Default")
            store.record_session_move(kite_session_id, from_env, to_env, "picker")
            updated = store.get_chat_session(kite_session_id)
        audit(session, "chat_session_move", kite_session_id, True, {"from": from_env, "to": to_env})
        assert updated is not None
        return session_out(updated)

    @app.post("/api/chat/sessions/{kite_session_id}/archive")
    async def archive_chat_session(kite_session_id: str, request: Request):
        session = auth(request)
        check_session_id(kite_session_id)
        with scheduler_store() as store:
            if store.get_chat_session(kite_session_id) is None:
                raise HTTPException(404, "không có session này")
            updated = store.set_chat_session_status(kite_session_id, "archived")
        audit(session, "chat_session_archive", kite_session_id)
        return session_out(updated)

    @app.delete("/api/chat/sessions/{kite_session_id}")
    async def delete_chat_session(kite_session_id: str, request: Request):
        session = auth(request)
        check_session_id(kite_session_id)
        with scheduler_store() as store:
            if store.get_chat_session(kite_session_id) is None:
                raise HTTPException(404, "không có session này")
            # Only the status changes: the registry row stays, so the trace survives.
            updated = store.set_chat_session_status(kite_session_id, "deleted")
        audit(session, "chat_session_delete", kite_session_id)
        return session_out(updated)

    @app.get("/api/chat/sessions/{kite_session_id}/moves")
    async def chat_session_moves(kite_session_id: str, request: Request):
        auth(request)
        check_session_id(kite_session_id)
        with scheduler_store() as store:
            if store.get_chat_session(kite_session_id) is None:
                raise HTTPException(404, "không có session này")
            return {"moves": store.list_session_moves(kite_session_id)}

    async def env_card(env: dict[str, Any]) -> dict[str, Any]:
        """One environment's card: identity + live repo state + counts. Every source is
        best-effort (a missing repo, a store without the registry, or a lesson lookup that
        fails leaves that field null instead of failing the whole list). Cost counts only
        task-attributed spend (see env_cost_usd); chat/pod/bench spend is excluded, so the
        number is a lower bound, never a fabrication."""
        card: dict[str, Any] = {**env, "branch": None, "head": None, "clean": None,
                                "last_sync": None, "session_count": None, "task_count": None,
                                "cost_usd": None, "spend_month_usd": None,
                                "lesson_count": None, "recent_commits": []}
        if env["path"]:
            try:
                status = await ctx.ops().repo_status(env["path"])
                card["branch"] = status.get("branch")
                card["head"] = status.get("head")
                changes = status.get("changes") or ""
                card["clean"] = changes == "(không có thay đổi)"
                card["recent_commits"] = [line for line in (status.get("recent_commits") or "").splitlines()
                                          if line.strip()]
                fetch_head = Path(env["path"]) / ".git" / "FETCH_HEAD"
                if fetch_head.is_file():
                    card["last_sync"] = fetch_head.stat().st_mtime
            except Exception:
                pass
            try:
                lessons = (await ctx.ops().memory_show(env["path"])).get("lessons")
                card["lesson_count"] = len(lessons) if isinstance(lessons, list) else None
            except Exception:
                pass
        try:
            with scheduler_store() as store:
                card["session_count"] = len(store.list_chat_sessions(status="active",
                                                                     environment_id=env["id"]))
                tasks = store.list_tasks()
                card["task_count"] = len([t for t in tasks
                                          if t.get("environment_id") == env["id"]])
                card["cost_usd"] = env_cost_usd(ctx.journal(), tasks, str(env["id"]))
                # The card's "Chi tháng này" reads spend_month_usd: the same
                # task-attributed spend, restricted to the current UTC month
                # (env_cost_usd month_only). cost_usd stays the all-time
                # lower bound for any other consumer.
                card["spend_month_usd"] = env_cost_usd(ctx.journal(), tasks, str(env["id"]),
                                                        month_only=True, now=ctx.clock())
        except Exception:
            pass
        return card

    @app.get("/api/environments")
    async def environments(request: Request):
        auth(request)
        return {"environments": [await env_card(env) for env in env_rows()]}

    @app.get("/api/environments/{env_id}")
    async def environment(env_id: str, request: Request):
        auth(request)
        return await env_card(require_env(env_id))

    @app.post("/api/environments")
    async def add_environment(body: EnvIn, request: Request):
        session = auth(request)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", body.name):
            raise HTTPException(400, "tên môi trường chỉ gồm chữ, số, '.', '_', '-'")
        name, url = body.name.strip(), (body.url or "").strip()
        path = (body.path or "").strip()
        branch = (body.branch or "").strip() or None
        pat = (body.pat or "").strip() or None
        if branch is not None and not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch):
            raise HTTPException(400, "tên branch không hợp lệ")
        if branch is not None and not url:
            raise HTTPException(400, "branch chỉ dùng khi clone từ URL")
        if pat is not None and not url:
            raise HTTPException(400, "PAT chỉ dùng khi clone từ URL")
        if url and not path:
            report = await clone_environment(name, url, branch, pat)
            audit(session, "env_add", name, True, {"path": report["path"], "cloned": True})
            return report
        if not path:
            raise HTTPException(400, "cần path của repo đã có hoặc URL để clone")
        await ctx.ops().repo_status(path)          # inside the allowed folders, a Git repo with a commit
        resolved = str(Path(path).expanduser().resolve())
        repo_id = ctx.store.add_repo(name, resolved, url)
        audit(session, "env_add", name, True, {"path": path})
        return {"id": str(repo_id), "path": resolved, "cloned": False}

    async def clone_environment(name: str, url: str, branch: str | None,
                                pat: str | None) -> dict[str, Any]:
        """Clone a repo URL into a Kite-managed folder and register it as an environment.

        With a PAT the credentialed helper clones (PAT sealed at rest, never in the URL);
        without one the repo must be public and plain ``git clone`` runs. Either way the
        row, the folder and the PAT file are all removed again when the clone fails, so a
        failed clone leaves no half-registered environment behind."""
        try:
            from ..github_env import GitHubEnvError, clone_with_pat
        except ImportError:
            raise HTTPException(501, "clone từ URL chưa sẵn sàng (module github_env của worker (b))") from None
        dest = ctx.state_dir / "envs" / f"clone-{uuid.uuid4().hex[:12]}" / "repo"
        if dest.exists():
            raise HTTPException(409, "thư mục đích đã tồn tại; thử lại")
        repo_id = ctx.store.add_repo(name, str(dest), url)
        pat_path = ctx.state_dir / "secrets" / f"github_pat_{repo_id}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if pat is not None:
                wk.write_secret(pat_path, pat, sealer=ctx.sealer)
                await asyncio.to_thread(clone_with_pat, url, str(dest), pat_path, sealer=ctx.sealer)
            else:
                _plain_clone(url, dest)
        except Exception as exc:
            ctx.store.drop_repo(repo_id)
            shutil.rmtree(dest.parent, ignore_errors=True)
            try:
                pat_path.unlink()
            except OSError:
                pass
            if isinstance(exc, GitHubEnvError):
                raise HTTPException(400, str(exc)) from exc
            raise
        if branch is not None:
            done = subprocess.run(["git", "-C", str(dest), "checkout", branch],
                                  capture_output=True, text=True)
            if done.returncode != 0:
                ctx.store.drop_repo(repo_id)
                shutil.rmtree(dest.parent, ignore_errors=True)
                try:
                    pat_path.unlink()
                except OSError:
                    pass
                raise HTTPException(400, f"không checkout được branch {branch}: "
                                         + (done.stderr or done.stdout).strip()[-200:])
        return {"id": str(repo_id), "path": str(dest), "cloned": True}

    @app.post("/api/environments/{env_id}/sync")
    async def sync_environment(env_id: str, request: Request):
        session = auth(request)
        env = require_env(env_id)
        if env_id == "default":
            raise HTTPException(409, "môi trường Default không có repo để đồng bộ")
        if not env["url"]:
            return await ctx.ops().repo_status(env["path"])
        try:
            from ..github_env import GitHubEnvError, clone_with_pat, pull_with_pat
        except ImportError:
            raise HTTPException(501, "đồng bộ GitHub chưa sẵn sàng (module github_env của worker (b))") from None
        pat_path = ctx.state_dir / "secrets" / f"github_pat_{env_id}"
        path = Path(env["path"])
        try:
            # The PAT file is sealed with the web key: the helper needs the sealer to read it.
            if path.is_dir():
                result = pull_with_pat(str(path), pat_path, sealer=ctx.sealer)
            else:
                result = clone_with_pat(env["url"], str(path), pat_path, sealer=ctx.sealer)
            if asyncio.iscoroutine(result):
                result = await result
        except GitHubEnvError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(session, "env_sync", env_id, True)
        return result if isinstance(result, dict) else {"ok": True, "result": str(result)}

    @app.delete("/api/environments/{env_id}")
    async def remove_environment(env_id: str, request: Request):
        session = auth(request)
        require_env(env_id)
        if env_id == "default":
            raise HTTPException(409, "không xóa được môi trường Default")
        with scheduler_store() as store:
            active = store.list_chat_sessions(status="active", environment_id=env_id)
            if active:
                raise HTTPException(409, f"còn {len(active)} session đang mở trong môi trường này")
            busy = [t for t in store.list_tasks()
                    if t.get("environment_id") == env_id and t.get("state") in ("QUEUED", "LEASED")]
            if busy:
                raise HTTPException(409, f"còn {len(busy)} task đang chạy trong môi trường này")
        if not ctx.store.drop_repo(int(env_id)):
            raise HTTPException(404, "không có môi trường này")
        pat_path = ctx.state_dir / "secrets" / f"github_pat_{env_id}"
        try:
            pat_path.unlink()
        except OSError:
            pass
        audit(session, "env_remove", env_id)
        return {"ok": True}

    @app.post("/api/environments/{env_id}/pat")
    async def set_env_pat(env_id: str, body: PatIn, request: Request):
        session = auth(request)
        env = require_env(env_id)
        if env_id == "default" or not env["url"]:
            raise HTTPException(409, "chỉ repo GitHub mới dùng PAT")
        pat = (body.pat or "").strip()
        if not pat:
            raise HTTPException(400, "PAT trống")
        # Sealed at rest with the web key; never returned to the browser.
        wk.write_secret(ctx.state_dir / "secrets" / f"github_pat_{env_id}", pat, sealer=ctx.sealer)
        audit(session, "env_pat_set", env_id)
        return {"ok": True}

    @app.delete("/api/environments/{env_id}/pat")
    async def delete_env_pat(env_id: str, request: Request):
        session = auth(request)
        require_env(env_id)
        pat_path = ctx.state_dir / "secrets" / f"github_pat_{env_id}"
        try:
            pat_path.unlink()
        except FileNotFoundError:
            raise HTTPException(404, "môi trường này chưa có PAT") from None
        audit(session, "env_pat_delete", env_id)
        return {"ok": True}

    # ------------------------------------------------------------------ journal
    @app.get("/api/journal/summary")
    async def journal_summary(request: Request, days: float = 7):
        auth(request)
        since = ctx.clock() - days * 86400 if days > 0 else None
        return await asyncio.to_thread(summarize, ctx.journal(), since=since)

    @app.get("/api/journal/events")
    async def journal_events(request: Request, file: str = "gateway", limit: int = 100, before: float | None = None,
                             tag: str | None = None):
        auth(request)
        if file not in FILES:
            raise HTTPException(400, f"file phải là một trong: {', '.join(FILES)}")
        records = ctx.journal().read(file)
        if before is not None:
            records = [r for r in records if (r.get("ts") or 0) < before]
        if tag:
            records = [r for r in records if tag in (r.get("tag"), r.get("task_id"), r.get("worker"))]
        return {"file": file, "records": records[-max(1, min(limit, 500)):][::-1]}

    @app.get("/api/journal/daily")
    async def journal_daily(request: Request, days: int = 30):
        auth(request)
        since = ctx.clock() - max(1, min(days, 400)) * 86400
        return {"days": await asyncio.to_thread(daily, ctx.journal(), since)}

    # ------------------------------------------------------------------ bench
    @app.get("/api/bench/history")
    async def bench_history(request: Request):
        auth(request)
        from .. import bench

        text, rows = await asyncio.to_thread(bench.history, [ctx.state_dir / "bench", Path.home()])
        extra, official, declared_hosts = await asyncio.to_thread(_bench_history_extra, ctx.state_dir)
        for row in rows:
            fields = extra.get((row.get("folder"), row.get("model")))
            if fields is not None:
                row.update(fields)
        return {"runs": rows, "table": text, "official": official,
                "declared_network_hosts": declared_hosts}

    @app.post("/api/bench/run")
    async def bench_run(body: BenchIn, request: Request):
        session = auth(request)
        ops = ctx.ops()
        report = await ops.logged("bench_start", ops.bench_start, True, body.label, body.suites, body.models,
                                  "", "", 0, body.worker, network_hosts=body.network_hosts,
                                  network_ack=body.network_ack)
        audit(session, "bench_start", body.label, True, {"suites": body.suites, "models": body.models,
                                                         "network_hosts": body.network_hosts,
                                                         "network_ack": body.network_ack})
        return report

    # ------------------------------------------------------------------ knowledge
    @app.post("/api/knowledge", status_code=201)
    async def knowledge_upload(request: Request, file: UploadFile = File(...), scope: str = Form("global"),
                               environment_id: str | None = Form(None), tags: str = Form("")):
        session = auth(request)
        if scope not in ("global", "environment"):
            raise HTTPException(400, "scope phải là 'global' hoặc 'environment'")
        if scope == "environment" and not (environment_id or "").strip():
            raise HTTPException(400, "scope 'environment' cần có environment_id")
        # Read at most one byte over the limit: a bigger file is refused without loading it all.
        data = await file.read(know.MAX_FILE_BYTES + 1)
        if len(data) > know.MAX_FILE_BYTES:
            raise HTTPException(413, "file quá lớn; tối đa 20 MB")
        tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
        root = ctx.state_dir / "knowledge"
        before = {path.name for path in root.iterdir()} if root.is_dir() else set()
        try:
            # Conversion runs off the event loop and must finish within 120 seconds; a
            # timeout leaves no half-saved document behind (see _drop_orphan_knowledge_dirs).
            meta = await asyncio.wait_for(
                asyncio.to_thread(know.ingest_document, ctx.state_dir, file.filename or "", data, scope,
                                  environment_id, tag_list),
                timeout=120)
        except asyncio.TimeoutError:
            _drop_orphan_knowledge_dirs(ctx.state_dir, before)
            audit(session, "knowledge_upload", "", False, {"error": "timeout 120s",
                                                           "filename": file.filename or ""})
            raise HTTPException(504, "chuyển đổi quá lâu (quá 120 giây); thử lại sau")
        except ValueError as exc:
            audit(session, "knowledge_upload", "", False, {"error": str(exc)[:300]})
            raise
        audit(session, "knowledge_upload", meta["doc_id"], True,
              {"filename": meta["filename"], "scope": scope, "status": meta["status"]})
        if meta["status"] == "converting":
            # MarkItDown found (near-)no text: a scan. Start the Docling job
            # in the background; a failure to start it only logs — the doc
            # stays "converting" and the owner can press reconvert by hand.
            try:
                from . import docling_job
            except ImportError:
                log.warning("knowledge upload: chưa có docling_job nên không chạy job Docling cho %s",
                            meta["doc_id"])
            else:
                try:
                    await asyncio.to_thread(docling_job.request_reconvert, ctx.state_dir,
                                            meta["doc_id"], reason="auto-scan")
                except ValueError as exc:
                    log.warning("knowledge upload: không khởi động được job Docling cho %s: %s",
                                meta["doc_id"], exc)
        return meta

    @app.get("/api/knowledge")
    async def knowledge_list(request: Request, scope: str | None = None, environment_id: str | None = None,
                             tag: str | None = None):
        auth(request)
        return {"documents": await asyncio.to_thread(know.list_documents, ctx.state_dir, scope,
                                                     environment_id, tag)}

    @app.get("/api/knowledge/{doc_id}")
    async def knowledge_get(doc_id: str, request: Request):
        auth(request)
        try:
            return await asyncio.to_thread(know.get_document, ctx.state_dir, doc_id)
        except FileNotFoundError:
            raise HTTPException(404, "không có tài liệu này")

    @app.get("/api/knowledge/{doc_id}/markdown")
    async def knowledge_markdown(doc_id: str, request: Request):
        auth(request)
        try:
            text = await asyncio.to_thread(know.read_markdown, ctx.state_dir, doc_id)
        except FileNotFoundError:
            raise HTTPException(404, "không có tài liệu này")
        return PlainTextResponse(text)

    @app.delete("/api/knowledge/{doc_id}")
    async def knowledge_delete(doc_id: str, request: Request):
        session = auth(request)
        try:
            await asyncio.to_thread(know.delete_document, ctx.state_dir, doc_id)
        except FileNotFoundError:
            raise HTTPException(404, "không có tài liệu này")
        audit(session, "knowledge_delete", doc_id)
        return {"ok": True, "doc_id": doc_id}

    @app.post("/api/knowledge/{doc_id}/reconvert")
    async def knowledge_reconvert(doc_id: str, request: Request):
        session = auth(request)
        try:
            meta = await asyncio.to_thread(know.get_document, ctx.state_dir, doc_id)
        except FileNotFoundError:
            raise HTTPException(404, "không có tài liệu này")
        try:
            # docling_job may not exist yet in a partial tree: the route must
            # still import and answer. Without it, no job is started and the stored
            # document is left exactly as it was.
            from . import docling_job
        except ImportError:
            log.warning("knowledge reconvert: chưa có docling_job nên không chạy job Docling cho %s", doc_id)
            return {"doc_id": doc_id, "status": "converting"}
        # A live job is the only thing that blocks a reconvert: the meta status
        # alone can be "converting" with no job running (ingest found a scan
        # and the auto-trigger has not spawned yet, or a spawn failed).
        if await asyncio.to_thread(docling_job.is_converting, ctx.state_dir, doc_id):
            raise HTTPException(409, "tài liệu đang chuyển đổi; thử lại sau")
        try:
            report = await asyncio.to_thread(docling_job.request_reconvert, ctx.state_dir, doc_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        audit(session, "knowledge_reconvert", doc_id, True, {"status": report.get("status")})
        return report

    # ------------------------------------------------------------------ settings and system
    @app.get("/api/settings")
    async def settings(request: Request):
        auth(request)
        config = ctx.config()
        from .. import update

        version = await asyncio.to_thread(_git_head, update.REPO)
        if not version.get("commit"):            # the Compose image has no .git: its revision is baked in
            from .. import stack

            version = {"commit": stack.version(), "subject": os.environ.get("KITE_VERSION_SUBJECT", ""),
                       "date": os.environ.get("KITE_VERSION_DATE", "")}
        return {"ntfy": bool(config.notify.ntfy), "gateway": {"host": config.gateway.host, "port": config.gateway.port,
                                                             "public_url": config.gateway.public_url},
                "version": version, "state_dir": str(ctx.state_dir),
                "chat_preferences": (await ctx.ops().memory_show(""))["chat"]}

    @app.post("/api/settings/ntfy/test")
    async def ntfy_test(request: Request):
        session = auth(request)
        config = ctx.config()
        if not config.notify.ntfy:
            raise HTTPException(400, "chưa cài ntfy: kite notify setup")
        from .. import notify
        from ..config import resolve_secret

        await asyncio.to_thread(notify.send, resolve_secret(config.notify.ntfy), "Kite: thử thông báo",
                                "Gửi từ trang web của Kite.")
        audit(session, "ntfy_test")
        return {"sent": True}

    @app.get("/api/health")
    async def health():
        # Unauthenticated on purpose: the container's health check and Caddy only learn that the web answers.
        return {"ok": True}

    @app.get("/api/system/doctor")
    async def doctor(request: Request):
        auth(request)
        return await ctx.ops().doctor()

    @app.get("/api/system/update")
    async def update_plan(request: Request):
        auth(request)
        return await ctx.ops().update_kite(False)

    @app.post("/api/system/update")
    async def update_run(request: Request):
        session = auth(request)
        ops = ctx.ops()
        try:
            report = await ops.logged("update_kite", ops.update_kite, True)
        except Exception as exc:
            audit(session, "update", ok=False, detail={"error": str(exc)[:300]})
            raise
        audit(session, "update", detail={"log": report.get("log")})
        return report

    @app.get("/api/system/cleanup")
    async def cleanup_plan(request: Request, days: float = 7):
        auth(request)
        return await ctx.ops().cleanup(False, days)

    @app.post("/api/system/cleanup")
    async def cleanup_run(body: CleanupIn, request: Request):
        session = auth(request)
        report = await ctx.ops().cleanup(True, body.days)
        audit(session, "cleanup", detail={"freed": report.get("freed"), "days": body.days})
        return report

    @app.post("/api/system/restart")
    async def restart(body: RestartIn, request: Request):
        session = auth(request)
        allowed = {"kite-gateway", "kite-workflow-worker"}
        services = [s for s in body.services if s in allowed]
        if not services or len(services) != len(body.services):
            raise HTTPException(400, f"chỉ khởi động lại được: {', '.join(sorted(allowed))}")
        running = [t["task_id"] for t in await ctx.ops().list_tasks(limit=50) if t["state"] == "LEASED"]
        if running and not body.force:
            raise HTTPException(409, f"đang có task chạy ({', '.join(running)}); khởi động lại sẽ cắt ngang nó")
        if ctx.restart is None:
            raise HTTPException(501, "máy này không cho web khởi động lại dịch vụ")
        report = await ctx.restart(services)
        audit(session, "restart", ",".join(services), bool(report.get("ok")), report)
        return report

    @app.get("/api/audit")
    async def audit_rows(request: Request, limit: int = 100, before: float | None = None):
        auth(request)
        return {"rows": ctx.store.audit_rows(limit, before)}

    # ------------------------------------------------------------------ Chat: the OpenCode server
    def opencode_headers(password: str | None) -> dict[str, str]:
        if not password:
            return {}
        import base64

        return {"Authorization": "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()}

    @app.get("/api/chat/info")
    async def chat_info(request: Request):
        auth(request)
        import aiohttp

        url, password = await asyncio.to_thread(ctx.opencode)
        report: dict[str, Any] = {"chat_dir": str(ctx.chat_dir) if ctx.chat_dir else None,
                                  "ready": bool(ctx.chat_dir and (ctx.chat_dir / "opencode.json").is_file()),
                                  "tested_version": OPENCODE_TESTED, "busy": 0,
                                  "opencode": {"reachable": False, "version": None, "error": None}}
        factory = ctx.http_factory or aiohttp.ClientSession
        headers = opencode_headers(password)
        data = sessions = None
        try:
            async with factory() as client:
                async with client.get(f"{url}/api/info", headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=5)) as response:
                    data = await response.json(content_type=None) if response.status == 200 else None
                    if response.status != 200:
                        report["opencode"]["error"] = f"HTTP {response.status}"
                if isinstance(data, dict) and ctx.chat_dir is not None:
                    # How many chat sessions are working now: the lead agent's desk in the 3D office.
                    async with client.get(f"{url}/api/session", headers=headers,
                                          params={"directory": str(ctx.chat_dir), "limit": "30"},
                                          timeout=aiohttp.ClientTimeout(total=5)) as response:
                        sessions = await response.json(content_type=None) if response.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            report["opencode"]["error"] = type(exc).__name__
        if isinstance(data, dict):
            report["opencode"].update(reachable=True, version=data.get("version"))
        if isinstance(sessions, dict) and isinstance(sessions.get("data"), list):
            report["busy"] = sum(1 for item in sessions["data"] if _session_busy(item))
        return report

    def opencode_route(method: str, path: str) -> frozenset | None:
        for methods, pattern, keys in OPENCODE_ROUTES:
            if method in methods and pattern.fullmatch(path):
                return keys if keys is not None else frozenset()
        raise HTTPException(403, "web không mở đường này của OpenCode")

    @app.api_route("/api/opencode/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def opencode(path: str, request: Request):
        session = auth(request)
        keys = opencode_route(request.method, path)
        import aiohttp

        body: bytes | None = None
        if request.method not in SAFE_METHODS and request.method != "DELETE":
            data = await _json(request) if await request.body() else {}
            extra = set(data) - set(keys)
            if extra:
                raise HTTPException(400, f"OpenCode: không gửi được {', '.join(sorted(extra))} từ web")
            if path == "api/session":
                # Gói 2: route the new OpenCode session into its Kite session's folder, not the
                # single legacy chat folder. kite_session_id is Kite's own field: it is consumed
                # here and never forwarded to OpenCode.
                kite_sid = data.pop("kite_session_id", None)
                if kite_sid is not None:
                    check_session_id(kite_sid)
                    with scheduler_store() as store:
                        row = store.get_chat_session(kite_sid)
                    if row is None:
                        raise HTTPException(404, "không có session chat này")
                    chat_dir = session_dir(kite_sid)
                else:
                    chat_dir = ctx.chat_dir
                if chat_dir is None or not (chat_dir / "opencode.json").is_file():
                    raise HTTPException(409, "chưa có thư mục chat của Kite: chạy kite chat setup")
                data["location"] = {"directory": str(chat_dir)}
            if path.endswith("/reply") and "decision" in data and data["decision"] not in ("once", "reject"):
                # "Always allow" outlives the chat folder's opencode.json (it is saved for every project).
                raise HTTPException(400, "từ web chỉ cho phép một lần hoặc từ chối")
            body = json.dumps(data, ensure_ascii=False).encode()
        url, password = await asyncio.to_thread(ctx.opencode)
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("accept", "last-event-id")}
        headers.update(opencode_headers(password))
        if body is not None:
            headers["Content-Type"] = "application/json"
        factory = ctx.http_factory or aiohttp.ClientSession
        client = factory()
        try:
            upstream = await client.request(request.method, f"{url}/{path}",
                                            params=list(request.query_params.multi_items()),
                                            data=body, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
        except aiohttp.ClientError as exc:
            await client.close()
            raise HTTPException(502, f"không gọi được OpenCode ({type(exc).__name__}); "
                                     "kiểm tra dịch vụ kite-opencode") from exc
        if path.endswith("/prompt"):
            audit(session, "chat_message", path[:200])

        async def stream():
            try:
                async for chunk in upstream.content.iter_any():
                    yield chunk
            finally:
                upstream.release()
                await client.close()

        kept = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "cache-control")}
        return StreamingResponse(stream(), status_code=upstream.status, headers=kept)

    # ------------------------------------------------------------------ WebSockets
    def ws_session(websocket: WebSocket) -> Session | None:
        if not same_origin(websocket, websocket.headers.get("origin")):
            return None
        return session_of(websocket)

    @app.websocket("/api/ws")
    async def events(websocket: WebSocket):
        session = ws_session(websocket)
        if session is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        tail = JournalTail(ctx.state_dir / "journal")
        last_status, last_tasks = None, None
        try:
            while True:
                status = await gateway_status()
                if status != last_status:
                    last_status = status
                    await websocket.send_json({"t": "status", "gateway_up": status is not None, "status": status})
                for name, record in tail.new_records():
                    await websocket.send_json({"t": "event", "file": name, "record": record})
                running = await asyncio.to_thread(task_activity, ctx, await ctx.ops().list_tasks(limit=20))
                if running != last_tasks:
                    last_tasks = running
                    await websocket.send_json({"t": "tasks", "tasks": running})
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=ctx.events_every)
                except asyncio.TimeoutError:
                    pass
        except WebSocketDisconnect:
            return

    @app.websocket("/api/browser/ws")
    async def browser_screen(websocket: WebSocket):
        session = ws_session(websocket)
        if session is None:
            await websocket.close(code=4401)
            return
        import aiohttp

        try:
            token = (await ctx.gateway("GET", "/kite/browser/token", None))["token"]
        except (GatewayDown, KeyError):
            await websocket.close(code=4502)
            return
        await websocket.accept()
        ctx.store.audit(session.user.username, session.ip, "kaggle_browser_view", "", True)
        port = urlsplit(ctx.gateway_ws).port
        factory = ctx.http_factory or aiohttp.ClientSession
        async with factory() as client:
            async with client.ws_connect(f"{ctx.gateway_ws}/kite/browser/ws?k={token}",
                                         headers={"Origin": f"http://127.0.0.1:{port}"}, max_msg_size=8 << 20) as upstream:
                async def down():
                    async for message in upstream:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await websocket.send_text(message.data)
                        else:
                            break

                async def up():
                    while True:
                        text = await websocket.receive_text()
                        if len(text) <= 1 << 16:
                            await upstream.send_str(text)

                tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(up())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
        try:
            await websocket.close()
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ Gói 4: Workspace
    # Mounted before the catch-all page route below, like every /api/* route: the page
    # handler 404s anything under api/ it does not know, so these must come first.
    from .schedules_api import create_router as create_schedules_router
    from .secretary_api import create_router as create_secretary_router
    from .workspace_api import create_router as create_workspace_router

    app.include_router(create_schedules_router(ctx, auth, audit))
    app.include_router(create_secretary_router(ctx, auth, audit))
    app.include_router(create_workspace_router(ctx, auth, audit))

    # Gói 5 — Connect: the owner's MCP/skill/platform store (routes live in
    # web/connect.py). Mounted BEFORE the /{path:path} catch-all below, or the
    # catch-all would 404 every /api/connect/... route.
    from . import connect as connect_api

    app.include_router(connect_api.build_router(ctx, auth))

    # ------------------------------------------------------------------ the page
    @app.get("/kite-mini.js")
    async def mini_js():
        return Response(MINI_JS, media_type="text/javascript")

    @app.get("/{path:path}", include_in_schema=False)
    async def page(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "không có API này")
        if ctx.dist is not None and (ctx.dist / "index.html").is_file():
            target = (ctx.dist / path).resolve()
            if path and target.is_file() and ctx.dist.resolve() in target.parents and not target.name.startswith("."):
                # Vite puts a content hash in every asset name: a new build never reuses one.
                cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
                return FileResponse(target, headers={"Cache-Control": cache})
            if path.startswith("assets/"):
                raise HTTPException(404, "không có file này")
            return FileResponse(ctx.dist / "index.html", headers={"Cache-Control": "no-cache"})
        return HTMLResponse(MINI_PAGE)

    return app


# ---------------------------------------------------------------- helpers
def _plain_clone(url: str, dest: Path) -> None:
    """``git clone`` a public repository: https, or a local file:// URL (a sample repo on this
    machine). No credentials of any kind: a URL carrying userinfo is refused, and the PAT path
    is for private repos only (see ``clone_with_pat``)."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("https", "file"):
        raise HTTPException(400, "chỉ nhận URL https (repo công khai) hoặc file:// (repo mẫu trên máy)")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise HTTPException(400, "URL không được nhúng credential (dùng PAT riêng)")
    if parsed.scheme == "https" and (not parsed.netloc or not parsed.path.strip("/")):
        raise HTTPException(400, f"URL không hợp lệ: {url}")
    done = subprocess.run(["git", "clone", "--quiet", url, str(dest)], capture_output=True, text=True)
    if done.returncode != 0:
        raise HTTPException(400, "git clone lỗi: " + (done.stderr or done.stdout).strip()[-300:])


def _write_session_files(ctx: WebContext, kite_session_id: str, directory: Path) -> None:
    """opencode.json + AGENTS.md for one chat session, then `git init` (OpenCode only reads a
    project's opencode.json inside a Git repository). The MCP command carries --session-id so
    tasks started here are stamped with this session; the only allowed root at creation is the
    session folder itself — the repo copy's root is added by worker (b)'s reload path when the
    session first runs a task on a repo."""
    from ..config import gateway_models
    from ..mcp_server import chat_files

    config = ctx.config()
    models = gateway_models(config)
    if not models:
        raise HTTPException(409, "chưa có worker nào phục vụ model: thêm worker rồi tạo session")
    lead = "qwen3.6-fast" if "qwen3.6-fast" in models else models[0]
    command = [sys.executable, "-m", "poc.kaggle_worker.cli", "--config", str(ctx.config_path.resolve()),
               "mcp", "serve", "--session-id", kite_session_id, "--chat-dir", str(directory),
               "--allow-root", str(directory)]
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in chat_files(config, lead_model=lead, kite_command=command,
                                    allowed_roots=[directory], state_dir=ctx.state_dir).items():
        (directory / name).write_text(content, encoding="utf-8")
    done = subprocess.run(["git", "init", "-q", str(directory)], capture_output=True, text=True)
    if done.returncode != 0:
        raise HTTPException(500, "không tạo được thư mục chat của session")

log = logging.getLogger(__name__)


def _drop_orphan_knowledge_dirs(state_dir: Path, before: set[str]) -> None:
    """Remove knowledge directories created by a timed-out upload that never wrote meta.json.

    Best effort only: the conversion thread may still be running after the 120 s timeout
    (``asyncio.wait_for`` cannot stop it), so a directory it finishes later would keep its
    own meta.json and stay visible — it is a complete document then, not a half-saved one.
    """
    root = Path(state_dir) / "knowledge"
    if not root.is_dir():
        return
    for path in root.iterdir():
        if path.name not in before and path.is_dir() and not (path / "meta.json").is_file():
            shutil.rmtree(path, ignore_errors=True)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "body phải là JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body phải là một object JSON")
    return body


def _needs_rehash(hashed: str) -> bool:
    try:
        return sec._hasher().check_needs_rehash(hashed)
    except Exception:
        return False


def _auto_click(ctx: WebContext) -> Callable[[str], bool]:
    from ..browser import auto_click_on

    return lambda worker: auto_click_on(ctx.state_dir, worker)


def _bench_history_extra(state_dir: Path) -> tuple[dict, dict, list[str]]:
    """Criteria fields for the bench history (§9.2), read from the runs' ``results.json``.

    Returns ``(by_row, official, declared_hosts)``: ``by_row`` maps ``(folder, model)``
    to the run record's criteria fields (``criteria_version``, ``gate_set``,
    ``overall``, ``level``, ``level_unstable``, ``network``, ``axes``, ``radar``);
    ``official`` is the official summary over every run under ``<state>/bench``;
    ``declared_hosts`` is the union of the network hosts declared in ``task.yaml``
    (what the Bench page shows for the owner to confirm). Runs from before v1 have
    no criteria block, so they simply get no extra fields -- the old shape is kept.
    """
    from .. import bench_criteria
    from ..mcp_server import declared_network_hosts

    by_row: dict[tuple[str, str | None], dict] = {}
    records: list[dict] = []
    bench_dir = Path(state_dir) / "bench"
    if bench_dir.is_dir():
        for path in sorted(bench_dir.glob("*/results.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for record in (data.get("criteria") or {}).get("runs") or []:
                records.append(record)
                model = (record.get("system_config") or {}).get("model")
                by_row[(path.parent.name, model)] = {
                    "criteria_version": record.get("criteria_version"),
                    "gate_set": record.get("gate_set"),
                    "overall": record.get("overall"),
                    "level": record.get("level"),
                    "level_unstable": record.get("level_unstable"),
                    "network": record.get("network"),
                    "axes": record.get("axes"),
                    "radar": bench_criteria.radar_data(record.get("axes") or {}),
                }
    _annotated, official = bench_criteria.annotate_runs(records, gate_set="gate-v1")
    declared = declared_network_hosts(state_dir)
    hosts = sorted({h for hs in declared.values() for h in hs})
    return by_row, official, hosts


def _git_head(repo: Path) -> dict:
    import subprocess

    done = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), "log", "-1", "--format=%h%x09%s%x09%cI"],
                          capture_output=True, text=True)
    commit, _, rest = done.stdout.strip().partition("\t")
    subject, _, date = rest.partition("\t")
    return {"commit": commit, "subject": subject[:200], "date": date}


class JournalTail:
    """New lines of the journal files since the last call (starting at their current end)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.offsets = {name: self._size(name) for name in FILES}

    def _size(self, name: str) -> int:
        try:
            return (self.root / f"{name}.jsonl").stat().st_size
        except OSError:
            return 0

    def new_records(self) -> list[tuple[str, dict]]:
        out = []
        for name in FILES:
            path = self.root / f"{name}.jsonl"
            size = self._size(name)
            if size < self.offsets[name]:
                self.offsets[name] = 0                       # the file was replaced
            if size == self.offsets[name]:
                continue
            with path.open("rb") as stream:
                stream.seek(self.offsets[name])
                chunk = stream.read(min(size - self.offsets[name], 1 << 20))
            complete = chunk[:chunk.rfind(b"\n") + 1]
            self.offsets[name] += len(complete)
            for line in complete.splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    out.append((name, record))
        return out[-200:]


def task_activity(ctx: WebContext, tasks: list[dict]) -> list[dict]:
    """What each queued or running task is doing now, from its trace (for the Overview's 3D office)."""
    from ..backend import task_slug

    out = []
    for task in tasks:
        if task.get("state") not in ("QUEUED", "LEASED"):
            continue
        entry = {"task_id": task["task_id"], "state": task["state"], "goal": task.get("goal", "")[:120],
                 "step": None, "action": None, "phase": None, "since": None, "worker": None}
        trace = read_jsonl(ctx.state_dir / "tasks" / task_slug(task["task_id"]) / "trace.jsonl")
        for record in trace:
            event = record.get("event")
            if event == "step_start":
                entry.update(step=record.get("step"), since=record.get("ts"), action=None)
            elif event == "step":
                entry.update(step=None, action=None)
            elif event == "agent_action":
                entry["action"] = record.get("action")
            elif event in ("fast_start", "review_start"):
                entry["phase"] = "fast_path" if event == "fast_start" else "review"
            elif event in ("fast_path", "review_end"):
                entry["phase"] = None
            if record.get("worker"):
                entry["worker"] = record.get("worker")
        out.append(entry)
    return out


def daily(journal: Journal, since: float) -> list[dict]:
    """Per day: model requests, errors, tokens, GPU seconds (gateway), tasks and their verdicts, spend."""
    days: dict[str, dict[str, float]] = {}

    def bucket(record: dict) -> dict[str, float]:
        return days.setdefault(str(record.get("at", ""))[:10], {
            "requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0, "gpu_seconds": 0.0,
            "tasks": 0, "tasks_ok": 0, "usd": 0.0})

    for record in journal.read("gateway", since):
        entry = bucket(record)
        entry["requests"] += 1
        entry["errors"] += record.get("outcome") == "error"
        entry["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
        entry["completion_tokens"] += int(record.get("completion_tokens") or 0)
        entry["gpu_seconds"] += float(record.get("run_s") or 0)
    for record in journal.read("tasks", since):
        entry = bucket(record)
        entry["tasks"] += 1
        entry["tasks_ok"] += bool(record.get("verified") or record.get("outcome") in ("ok", "READY_FOR_REVIEW"))
    for record in journal.read("spend", since):
        bucket(record)["usd"] += float(record.get("usd") or 0)
    return [{"day": day, **{k: round(v, 4) if isinstance(v, float) else v for k, v in values.items()}}
            for day, values in sorted(days.items())]


MINI_PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><title>Kite</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:15px system-ui,sans-serif;background:#16181d;color:#e6e6e6;margin:0;padding:24px}
main{max-width:860px;margin:auto}input,button{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid #3d424d;
background:#22252c;color:#e6e6e6;margin:4px 0;width:100%;box-sizing:border-box}button{cursor:pointer;background:#2f5fd0;border:0}
pre{background:#20232a;padding:12px;border-radius:8px;overflow:auto;font-size:12px}#err{color:#f08a7a}
form{max-width:340px}.muted{color:#9aa0aa}
</style></head><body><main>
<h1>Kite</h1>
<p class="muted">Giao diện đầy đủ chưa được build trên máy này (chạy <code>kite web build</code>). Trang này để đăng nhập và xem trạng thái.</p>
<form id="login"><input id="u" placeholder="Tên đăng nhập" autocomplete="username">
<input id="p" type="password" placeholder="Mật khẩu" autocomplete="current-password">
<input id="c" placeholder="Mã 2FA (6 số)" inputmode="numeric" autocomplete="one-time-code">
<button>Đăng nhập</button><div id="err"></div></form>
<div id="app" hidden><button id="out" style="width:auto">Đăng xuất</button><pre id="data"></pre></div>
</main><script src="/kite-mini.js"></script></body></html>
"""

MINI_JS = """
let csrf = '';
const $ = id => document.getElementById(id);
async function show() {
  const me = await fetch('/api/auth/me');
  if (!me.ok) { $('login').hidden = false; $('app').hidden = true; return; }
  csrf = (await me.json()).csrf;
  $('login').hidden = true; $('app').hidden = false;
  const data = await fetch('/api/overview');
  $('data').textContent = JSON.stringify(await data.json(), null, 2);
}
$('login').onsubmit = async e => {
  e.preventDefault();
  const r = await fetch('/api/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username: $('u').value, password: $('p').value, code: $('c').value})});
  if (!r.ok) { $('err').textContent = (await r.json()).error || 'Không đăng nhập được'; return; }
  $('p').value = ''; $('c').value = ''; $('err').textContent = ''; show();
};
$('out').onclick = async () => { await fetch('/api/auth/logout', {method: 'POST', headers: {'X-CSRF-Token': csrf}}); show(); };
show(); setInterval(() => { if (!$('app').hidden) show(); }, 10000);
"""
