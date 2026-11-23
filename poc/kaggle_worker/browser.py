"""Kite's own browser for kaggle.com: a Chromium on the gateway machine, shown to the owner.

Kaggle starts a run at once when "Save Version" is pressed on kaggle.com, while a run pushed
through its API can wait in a queue for hours (22 Nov: version 11 queued 200+ minutes, version 12
saved by hand at the same moment ran at once). kaggle.com cannot be put in a frame of Kite's web
page, so Kite runs the browser itself (Playwright) and streams its screen:

* ``GET /kite/browser`` (loopback only; reach it through ``ssh -L``, later through Kite's web) shows
  the screen and sends the owner's mouse and keyboard back. The screen comes from Chromium's
  screencast (CDP ``Page.startScreencast``): JPEG frames over a WebSocket, no VNC server.
* The owner signs in to Kaggle once (email and password: Google's sign-in may refuse a browser that
  runs on a server). The session (cookies, local storage) is kept encrypted with a key in a 0600 file;
  Chromium keeps no profile on disk.
* Only kaggle.com pages (and Google's sign-in page) open in the main frame: any other address is
  refused, so the browser is no general proxy.
* "Kite tự bấm" (off by default, per worker): with it on, Kite presses Save Version -> Save & Run All
  -> Save itself when a worker must start. Kaggle's terms forbid automated access outside its API;
  switching it on is the owner's decision. With it off, Kite only opens the notebook's editor and
  the owner presses the buttons.
* Chromium starts when needed and closes after ``idle_close_seconds`` without a viewer (~0.5 GB RAM).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
from pathlib import Path
import re
import secrets as token_secrets
import sys
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

VIEWPORT = (1280, 800)
KAGGLE_HOSTS = ("kaggle.com",)
SIGN_IN_HOSTS = ("accounts.google.com",)
KEY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,19}$")
BUTTONS = ("left", "middle", "right")


def allowed_url(url: str) -> bool:
    """Pages the browser may open in its main frame: https kaggle.com (any subdomain) and Google sign-in."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme == "about":
        return url == "about:blank"
    if parts.scheme != "https" or not host:
        return False
    return any(host == h or host.endswith("." + h) for h in KAGGLE_HOSTS) or host in SIGN_IN_HOSTS


def editor_url(kernel: str) -> str:
    return f"https://www.kaggle.com/code/{kernel}/edit"


def needs_sign_in(url: str) -> bool:
    return "/account/login" in url or (urlsplit(url).hostname or "") in SIGN_IN_HOSTS


# ---------------------------------------------------------------- "Kite tự bấm" switch
def _auto_file(state_dir: Path) -> Path:
    return Path(state_dir) / "browser" / "auto_click.json"


def auto_click_on(state_dir: Path, worker: str) -> bool:
    """The owner's "Kite tự bấm" switch for ``worker``; off unless switched on."""
    try:
        saved = json.loads(_auto_file(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(saved, dict) and saved.get(worker) is True


def set_auto_click(state_dir: Path, worker: str, on: bool) -> None:
    path = _auto_file(state_dir)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    saved[worker] = bool(on)
    _write_private(path, json.dumps(saved, indent=1).encode("utf-8"))


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.replace(temp, path)


# ---------------------------------------------------------------- the saved session
class Vault:
    """The browser session (Playwright storage state) encrypted with AES-GCM; the key is a 0600 file."""

    def __init__(self, key_file: Path, data_file: Path) -> None:
        self.key_file, self.data_file = Path(key_file), Path(data_file)
        self.error: str | None = None

    def _key(self, create: bool) -> bytes | None:
        try:
            key = base64.b64decode(self.key_file.read_text(encoding="ascii").strip())
            if len(key) == 32:
                return key
        except (OSError, ValueError):
            pass
        if not create:
            return None
        key = token_secrets.token_bytes(32)          # none yet, or damaged: a new key (sign in again)
        _write_private(self.key_file, base64.b64encode(key) + b"\n")
        return key

    @staticmethod
    def _aes(key: bytes):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            return None
        return AESGCM(key)

    def save(self, state: dict) -> bool:
        aes = self._aes(self._key(create=True) or b"")
        if aes is None:
            self.error = "thiếu gói cryptography: phiên đăng nhập không được lưu (pip install -e .[browser])"
            return False
        nonce = token_secrets.token_bytes(12)
        blob = aes.encrypt(nonce, json.dumps(state).encode("utf-8"), b"kite-browser")
        _write_private(self.data_file, nonce + blob)
        return True

    def load(self) -> dict | None:
        key = self._key(create=False)
        if not self.data_file.exists():
            return None
        if key is None:
            self.error = "không đọc được khóa của phiên đăng nhập đã lưu; đăng nhập lại"
            return None
        aes = self._aes(key)
        if aes is None:
            return None
        raw = self.data_file.read_bytes()
        try:
            state = json.loads(aes.decrypt(raw[:12], raw[12:], b"kite-browser"))
        except Exception:                      # another key, a damaged file: start signed out
            self.error = "không đọc được phiên đăng nhập đã lưu; đăng nhập lại"
            return None
        return state if isinstance(state, dict) else None

    def forget(self) -> None:
        self.data_file.unlink(missing_ok=True)


# ---------------------------------------------------------------- viewers
class Viewer:
    """One open page of the owner: gets the newest frame and status, never a backlog."""

    def __init__(self, send: Callable[[str], Awaitable[Any]]) -> None:
        self.send = send
        self.pending: dict[str, str] = {}
        self.event = asyncio.Event()
        self.task: asyncio.Task | None = None

    def offer(self, kind: str, message: str) -> None:
        self.pending[kind] = message
        self.event.set()

    async def pump(self) -> None:
        while True:
            await self.event.wait()
            self.event.clear()
            for kind in ("note", "status", "frame"):
                message = self.pending.pop(kind, None)
                if message is not None:
                    await self.send(message)


async def launch_chromium(headless: bool = True):
    """Start Playwright and Chromium (the full build in new headless mode when it is installed)."""
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    args = ["--disable-dev-shm-usage"]
    try:
        browser = await playwright.chromium.launch(headless=headless, channel="chromium", args=args)
    except Exception:
        browser = await playwright.chromium.launch(headless=headless, args=args)
    return playwright, browser


class KiteBrowser:
    def __init__(self, state_dir: Path, *, kernels: dict[str, str] | None = None,
                 start: Callable[[], Awaitable[tuple[Any, Any]]] | None = None,
                 allowed: Callable[[str], bool] = allowed_url, viewport: tuple[int, int] = VIEWPORT,
                 idle_close_seconds: float = 600.0, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = Path(state_dir)
        self.kernels = dict(kernels or {})          # worker id -> "<user>/<slug>" of its notebook
        self.editor = editor_url                    # kernel -> the notebook editor's address
        self.vault = Vault(self.state_dir / "secrets" / "browser_key", self.state_dir / "browser" / "kaggle.bin")
        self._start = start or launch_chromium
        self.allowed = allowed
        self.viewport = viewport
        self.idle_close_seconds = idle_close_seconds
        self.clock = clock
        self.token = token_secrets.token_urlsafe(24)   # the WebSocket needs it: only the page Kite served has it
        self.viewers: set[Viewer] = set()
        self.lock = asyncio.Lock()
        self.busy = 0
        self.last_used = clock()
        self.url, self.title, self.blocked, self.error = "", "", "", ""
        self._pw = self._browser = self.context = self.page = self._cdp = None
        self._casting = False
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    @property
    def is_open(self) -> bool:
        return self.page is not None

    async def ensure(self):
        async with self.lock:
            if self.page is not None:
                return self.page
            try:
                self._pw, self._browser = await self._start()
            except Exception as exc:
                self.error = ("không bật được Chromium: " + f"{type(exc).__name__}: {exc}"[:200]
                              + " (cài: pip install -e .[browser] rồi playwright install --with-deps chromium)")
                raise RuntimeError(self.error) from exc
            width, height = self.viewport
            self.context = await self._browser.new_context(
                viewport={"width": width, "height": height}, storage_state=self.vault.load(), locale="en-US",
                service_workers="block")
            await self.context.route("**/*", self._guard)
            self.page = await self.context.new_page()
            self.page.on("framenavigated", self._navigated)
            self.page.on("popup", self._popup)
            self.error = ""
            if self.viewers:
                await self._cast(True)
            return self.page

    async def close(self) -> None:
        async with self.lock:
            if self.page is None:
                return
            await self.save_state()
            await self._cast(False)
            for closing in (self.context, self._browser):
                try:
                    await closing.close()
                except Exception:
                    pass
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = self._browser = self.context = self.page = self._cdp = None
            self.url = self.title = ""
        await self._broadcast_status()

    async def save_state(self) -> None:
        if self.context is None:
            return
        try:
            self.vault.save(await self.context.storage_state())
        except Exception as exc:
            print(f"kite browser: could not save the session: {type(exc).__name__}", file=sys.stderr)

    async def watch(self, every: float = 30.0) -> None:
        """Keep the session saved; close Chromium once nobody watches and nothing runs for a while."""
        while True:
            await asyncio.sleep(every)
            try:
                if self.page is None:
                    continue
                if not self.viewers and not self.busy and self.clock() - self.last_used >= self.idle_close_seconds:
                    await self.close()
                else:
                    await self.save_state()
            except Exception as exc:
                print(f"kite browser: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------ page events
    async def _guard(self, route, request) -> None:
        try:
            main = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:                     # a service worker's request has no frame
            main = False
        if main and not self.allowed(request.url):
            self.blocked = request.url[:200]
            # 204 No Content: the browser stays on the page it shows (an abort would show an error page).
            await route.fulfill(status=204, body="")
            self._spawn(self._broadcast_status())
            return
        await route.continue_()

    def _navigated(self, frame) -> None:
        if self.page is None or frame != self.page.main_frame:
            return
        self.url = frame.url

        async def refresh() -> None:
            try:
                self.title = (await self.page.title())[:200] if self.page is not None else ""
            except Exception:
                pass
            await self._broadcast_status()

        self._spawn(refresh())

    def _popup(self, popup) -> None:
        # One tab only: a link that opens a new tab opens in this one instead (when allowed).
        async def fold() -> None:
            url = popup.url
            try:
                await popup.close()
            except Exception:
                pass
            if url and url != "about:blank" and self.allowed(url) and self.page is not None:
                await self.page.goto(url, wait_until="domcontentloaded")

        self._spawn(fold())

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ screen
    async def _cast(self, on: bool) -> None:
        if on and not self._casting and self.page is not None:
            self._cdp = await self.context.new_cdp_session(self.page)
            self._cdp.on("Page.screencastFrame", lambda params: self._spawn(self._frame(params)))
            width, height = self.viewport
            await self._cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 70,
                                                          "maxWidth": width, "maxHeight": height})
            self._casting = True
        elif not on and self._casting:
            self._casting = False
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:
                pass

    async def _frame(self, params: dict) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass
        meta = params.get("metadata") or {}
        message = json.dumps({"t": "frame", "d": params.get("data", ""),
                              "w": meta.get("deviceWidth") or self.viewport[0],
                              "h": meta.get("deviceHeight") or self.viewport[1]})
        for viewer in list(self.viewers):
            viewer.offer("frame", message)

    def status(self) -> dict[str, Any]:
        return {"t": "status", "open": self.is_open, "url": self.url, "title": self.title,
                "blocked": self.blocked, "busy": bool(self.busy), "error": self.error or self.vault.error or "",
                "workers": [{"id": wid, "kernel": kernel, "auto": auto_click_on(self.state_dir, wid)}
                            for wid, kernel in sorted(self.kernels.items())]}

    async def _broadcast_status(self) -> None:
        message = json.dumps(self.status(), ensure_ascii=False)
        for viewer in list(self.viewers):
            viewer.offer("status", message)

    def _note(self, viewer: Viewer | None, text: str) -> None:
        message = json.dumps({"t": "note", "s": text}, ensure_ascii=False)
        for target in ([viewer] if viewer is not None else list(self.viewers)):
            target.offer("note", message)

    async def add_viewer(self, send: Callable[[str], Awaitable[Any]]) -> Viewer:
        viewer = Viewer(send)
        viewer.task = asyncio.ensure_future(viewer.pump())
        self.viewers.add(viewer)
        self.last_used = self.clock()
        viewer.offer("status", json.dumps(self.status(), ensure_ascii=False))
        if self.page is not None:
            await self._cast(True)
        return viewer

    async def remove_viewer(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)
        if viewer.task is not None:
            viewer.task.cancel()
        self.last_used = self.clock()
        if not self.viewers:
            await self._cast(False)

    # ------------------------------------------------------------------ actions
    async def open(self, url: str) -> dict[str, Any]:
        if not self.allowed(url):
            raise ValueError("trình duyệt Kite chỉ mở trang kaggle.com")
        page = await self.ensure()
        self.last_used = self.clock()
        self.blocked = ""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:                  # a slow page is still shown; the owner sees it
            self.error = f"{type(exc).__name__}: {exc}"[:200]
        await self.save_state()
        await self._broadcast_status()
        return self.status()

    async def open_worker(self, worker: str) -> dict[str, Any]:
        if worker not in self.kernels:
            raise ValueError(f"worker {worker} chưa có notebook (kite worker autostart {worker} --kernel ...)")
        return await self.open(self.editor(self.kernels[worker]))

    async def save_version(self, kernel: str, *, step_seconds: float = 60.0) -> dict[str, Any]:
        """Press Save Version -> Save & Run All -> Save in the notebook's editor. Only the launcher calls
        this, and only while the owner's "Kite tự bấm" switch is on for that worker."""
        import re as regex

        self.busy += 1
        await self._broadcast_status()
        ms = step_seconds * 1000
        try:
            page = await self.ensure()
            url = self.editor(kernel)
            if not page.url.startswith(url):
                await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            if needs_sign_in(page.url):
                return {"ok": False, "step": "login", "detail": "Kaggle đòi đăng nhập"}

            async def save_button() -> None:
                await page.get_by_role("button", name=regex.compile(r"save version", regex.I)).first.click(timeout=ms)

            async def run_all() -> None:
                await page.get_by_text(regex.compile(r"save\s*&\s*run all", regex.I)).first.click(timeout=ms / 2)

            async def save() -> None:
                exact = regex.compile(r"^\s*save\s*$", regex.I)
                dialog = page.get_by_role("dialog")
                try:
                    await dialog.get_by_role("button", name=exact).last.click(timeout=5000)
                except Exception:
                    await page.get_by_role("button", name=exact).last.click(timeout=ms / 2)
                try:
                    await dialog.first.wait_for(state="hidden", timeout=30_000)
                except Exception:
                    pass

            for name, step in (("save-version", save_button), ("save-run-all", run_all), ("save", save)):
                try:
                    await step()
                except Exception as exc:
                    if needs_sign_in(page.url):
                        return {"ok": False, "step": "login", "detail": "Kaggle đòi đăng nhập"}
                    return {"ok": False, "step": name, "detail": f"{type(exc).__name__}: {exc}"[:300]}
            return {"ok": True}
        finally:
            self.busy -= 1
            self.last_used = self.clock()
            await self.save_state()
            await self._broadcast_status()

    async def command(self, message: dict, viewer: Viewer | None = None) -> None:
        """One message from the owner's page: mouse, keyboard, navigation, switches."""
        kind = message.get("t")
        self.last_used = self.clock()
        try:
            if kind == "open":
                await self.open_worker(str(message.get("worker")))
            elif kind == "goto":
                await self.open(str(message.get("u", ""))[:2000])
            elif kind == "close":
                await self.close()
            elif kind == "logout":
                await self.close()
                self.vault.forget()
                self._note(viewer, "đã xóa phiên đăng nhập Kaggle đã lưu")
            elif kind == "auto":
                worker = str(message.get("worker"))
                if worker in self.kernels:
                    set_auto_click(self.state_dir, worker, bool(message.get("on")))
                    await self._broadcast_status()
            elif self.page is not None:
                await self._input(kind, message)
        except ValueError as exc:
            self._note(viewer, str(exc))
        except Exception as exc:
            self._note(viewer, f"lỗi: {type(exc).__name__}: {exc}"[:300])

    async def _input(self, kind: Any, message: dict) -> None:
        page = self.page
        width, height = self.viewport

        def number(key: str, high: float) -> float:
            value = message.get(key, 0)
            value = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
            return max(-high, min(high, value))

        x, y = max(0.0, number("x", width)), max(0.0, number("y", height))
        if kind == "mouse":
            action = message.get("a")
            button = message.get("b") if message.get("b") in BUTTONS else "left"
            count = int(number("c", 3)) or 1
            if action == "move":
                await page.mouse.move(x, y)
            elif action == "down":
                await page.mouse.move(x, y)
                await page.mouse.down(button=button, click_count=count)
            elif action == "up":
                await page.mouse.move(x, y)
                await page.mouse.up(button=button, click_count=count)
            elif action == "wheel":
                await page.mouse.move(x, y)
                await page.mouse.wheel(number("dx", 5000), number("dy", 5000))
        elif kind == "key":
            key = str(message.get("k", ""))
            if KEY_NAME.fullmatch(key):
                await (page.keyboard.down(key) if message.get("a") == "down" else page.keyboard.up(key))
        elif kind == "text":
            text = str(message.get("s", ""))[:5000]
            if text:
                await page.keyboard.insert_text(text)
        elif kind == "nav":
            action = message.get("a")
            if action == "back":
                await page.go_back()
            elif action == "forward":
                await page.go_forward()
            elif action == "reload":
                await page.reload()


# ---------------------------------------------------------------- HTTP routes (gateway, loopback only)
def add_routes(app, browser: KiteBrowser, local_only: Callable[[Any], None]) -> None:
    from aiohttp import WSMsgType, web

    async def page(request: web.Request) -> web.Response:
        local_only(request)
        return web.Response(text=PAGE.replace("__TOKEN__", browser.token), content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                     "Referrer-Policy": "no-referrer",
                                     "Content-Security-Policy": "default-src 'none'; img-src data:; "
                                     "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                     "connect-src ws://127.0.0.1:* ws://localhost:* ws://[::1]:*"})

    async def socket(request: web.Request) -> web.StreamResponse:
        local_only(request)               # a browser always sends Origin on a WebSocket: other sites fail here
        if not hmac.compare_digest(request.query.get("k", ""), browser.token):
            raise web.HTTPForbidden(text="open /kite/browser first")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
        await ws.prepare(request)
        viewer = await browser.add_viewer(ws.send_str)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    await browser.command(data, viewer)
        finally:
            await browser.remove_viewer(viewer)
        return ws

    async def open_page(request: web.Request) -> web.Response:
        local_only(request)
        try:
            body = await request.json() if request.can_read_body else {}
        except json.JSONDecodeError:
            body = {}
        body = body if isinstance(body, dict) else {}
        try:
            if body.get("close"):
                await browser.close()
                return web.json_response(browser.status())
            report = (await browser.open_worker(str(body["worker"])) if body.get("worker")
                      else await browser.open(str(body.get("url") or "https://www.kaggle.com/")))
        except (ValueError, RuntimeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(report)

    app.router.add_get("/kite/browser", page)
    app.router.add_get("/kite/browser/ws", socket)
    app.router.add_post("/kite/browser/open", open_page)


PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><title>Trình duyệt Kite</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{margin:0;font:14px system-ui,sans-serif;background:#16181d;color:#e6e6e6}
header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 12px;background:#20232a}
button,select{background:#2d313a;color:#e6e6e6;border:1px solid #3d424d;border-radius:6px;padding:4px 10px;cursor:pointer}
button:hover{background:#3a3f4a}#where{flex:1;min-width:200px;color:#9aa0aa;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
#screen{display:block;margin:8px auto;max-width:calc(100vw - 16px);max-height:calc(100vh - 110px);background:#000;outline:none}
#screen:focus{box-shadow:0 0 0 2px #4c8bf5}#note{padding:0 12px;color:#f0b85a;min-height:18px}
label{display:flex;gap:4px;align-items:center}.off{color:#9aa0aa;text-align:center;padding:60px}
</style></head><body>
<header>
<button id="back" title="Lùi">◀</button><button id="reload" title="Tải lại">⟳</button>
<select id="worker"></select><button id="open">Mở notebook</button>
<label title="Kite tự bấm Save Version khi cần bật worker. Điều khoản Kaggle cấm thao tác tự động; bật là bạn chấp nhận rủi ro."><input type="checkbox" id="auto"> Kite tự bấm</label>
<span id="where">Trình duyệt Kite đang tắt</span>
<button id="close">Tắt trình duyệt</button>
</header>
<div id="note">Đăng nhập Kaggle bằng email và mật khẩu. Bật worker: Save Version → Save &amp; Run All → Save.</div>
<canvas id="screen" tabindex="0" width="1280" height="800"></canvas>
<script>
const ws = new WebSocket(`ws://${location.host}/kite/browser/ws?k=__TOKEN__`);
const cv = document.getElementById('screen'), g = cv.getContext('2d'), img = new Image();
let W = 1280, H = 800, workers = [], lastMove = 0;
const $ = id => document.getElementById(id);
const send = o => { if (ws.readyState === 1) ws.send(JSON.stringify(o)); };
img.onload = () => { if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; } g.drawImage(img, 0, 0, W, H); };
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.t === 'frame') { W = m.w || W; H = m.h || H; img.src = 'data:image/jpeg;base64,' + m.d; }
  else if (m.t === 'note') { $('note').textContent = m.s; }
  else if (m.t === 'status') {
    $('where').textContent = m.open ? ((m.busy ? '[Kite đang bấm] ' : '') + (m.title || '') + ' — ' + (m.url || ''))
                                    : 'Trình duyệt Kite đang tắt: bấm "Mở notebook"';
    if (m.blocked) $('note').textContent = 'Đã chặn trang ngoài kaggle.com: ' + m.blocked;
    if (m.error) $('note').textContent = m.error;
    const keep = $('worker').value; workers = m.workers || [];
    $('worker').innerHTML = workers.map(w => `<option value="${w.id}">${w.id}</option>`).join('');
    if (keep) $('worker').value = keep;
    syncAuto();
    if (!m.open) { g.fillStyle = '#000'; g.fillRect(0, 0, cv.width, cv.height); }
  }
};
ws.onclose = () => { $('note').textContent = 'Mất kết nối với Kite: tải lại trang.'; };
function syncAuto() { const w = workers.find(x => x.id === $('worker').value); $('auto').checked = !!(w && w.auto); $('auto').disabled = !w; }
$('worker').onchange = syncAuto;
$('open').onclick = () => { send({t: 'open', worker: $('worker').value}); cv.focus(); };
$('auto').onchange = () => {
  if ($('auto').checked && !confirm('Bật "Kite tự bấm"? Điều khoản Kaggle cấm thao tác tự động ngoài API; bạn chịu rủi ro tài khoản.')) { $('auto').checked = false; return; }
  send({t: 'auto', worker: $('worker').value, on: $('auto').checked});
};
$('back').onclick = () => send({t: 'nav', a: 'back'});
$('reload').onclick = () => send({t: 'nav', a: 'reload'});
$('close').onclick = () => send({t: 'close'});
const pos = e => { const r = cv.getBoundingClientRect(); return {x: (e.clientX - r.left) * W / r.width, y: (e.clientY - r.top) * H / r.height}; };
const BTN = ['left', 'middle', 'right'];
cv.addEventListener('mousemove', e => { const t = performance.now(); if (t - lastMove < 40) return; lastMove = t; send({t: 'mouse', a: 'move', ...pos(e)}); });
cv.addEventListener('mousedown', e => { e.preventDefault(); cv.focus(); send({t: 'mouse', a: 'down', b: BTN[e.button] || 'left', c: e.detail || 1, ...pos(e)}); });
cv.addEventListener('mouseup', e => send({t: 'mouse', a: 'up', b: BTN[e.button] || 'left', c: e.detail || 1, ...pos(e)}));
cv.addEventListener('wheel', e => { e.preventDefault(); send({t: 'mouse', a: 'wheel', dx: e.deltaX, dy: e.deltaY, ...pos(e)}); }, {passive: false});
cv.addEventListener('contextmenu', e => e.preventDefault());
const printable = e => e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
cv.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'v') return; e.preventDefault(); send(printable(e) ? {t: 'text', s: e.key} : {t: 'key', a: 'down', k: e.key}); });
cv.addEventListener('keyup', e => { if (!printable(e)) { e.preventDefault(); send({t: 'key', a: 'up', k: e.key}); } });
document.addEventListener('paste', e => { if (document.activeElement === cv) { e.preventDefault(); send({t: 'text', s: e.clipboardData.getData('text')}); } });
</script></body></html>
"""
