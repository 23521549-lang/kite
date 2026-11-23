"""P2: Kite's own browser for kaggle.com, its "Kite tự bấm" switch, and autostart through it."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from poc.kaggle_worker.browser import KiteBrowser, Vault, allowed_url, auto_click_on, set_auto_click
from poc.kaggle_worker.doctor import browser_missing
from poc.kaggle_worker.gateway import ModelGateway, WorkerAuth, create_app

from tests.poc.kaggle_worker.test_launcher import TOKEN_A, make


def test_only_kaggle_and_google_sign_in_open_in_the_main_frame():
    assert allowed_url("https://www.kaggle.com/code/hanle/kite-worker/edit")
    assert allowed_url("https://kaggle.com/account/login") and allowed_url("https://accounts.google.com/x")
    assert allowed_url("about:blank")
    for bad in ("http://www.kaggle.com/", "https://kaggle.com.evil.io/", "https://evilkaggle.com/",
                "https://example.com/", "file:///etc/passwd", "javascript:alert(1)", "chrome://settings"):
        assert not allowed_url(bad), bad


def test_the_saved_session_is_encrypted_and_private(tmp_path):
    pytest.importorskip("cryptography")
    vault = Vault(tmp_path / "secrets" / "browser_key", tmp_path / "browser" / "kaggle.bin")
    assert vault.load() is None
    state = {"cookies": [{"name": "KAGGLEID", "value": "very-secret-session"}], "origins": []}
    assert vault.save(state)
    assert vault.load() == state
    assert b"very-secret-session" not in vault.data_file.read_bytes()
    for path in (vault.key_file, vault.data_file):
        assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    vault.key_file.write_text("AAAA" * 11 + "\n")                     # another key: start signed out
    assert vault.load() is None and vault.error
    vault.forget()
    assert not vault.data_file.exists()


def test_kite_clicks_only_when_the_owner_switched_it_on(tmp_path):
    assert not auto_click_on(tmp_path, "kaggle-han")                    # off by default
    set_auto_click(tmp_path, "kaggle-han", True)
    assert auto_click_on(tmp_path, "kaggle-han") and not auto_click_on(tmp_path, "kaggle-an")
    set_auto_click(tmp_path, "kaggle-han", False)
    assert not auto_click_on(tmp_path, "kaggle-han")
    assert (tmp_path / "browser" / "auto_click.json").stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- the page and its WebSocket (no Chromium)
def browser_app(tmp_path):
    gateway = ModelGateway([WorkerAuth("kaggle-han", TOKEN_A, ("qwen3.6",))])
    browser = KiteBrowser(tmp_path, kernels={"kaggle-han": "hanle/kite-worker"})
    return create_app(gateway, browser=browser), browser


def test_the_browser_page_is_for_this_machine_only(tmp_path):
    async def scenario():
        app, browser = browser_app(tmp_path)
        async with TestClient(TestServer(app)) as client:
            page = await client.get("/kite/browser")
            html = await page.text()
            rebound = await client.get("/kite/browser", headers={"Host": "kite.attacker.example"})
            other_site = await client.get("/kite/browser", headers={"Origin": "https://attacker.example"})
            outside = await client.post("/kite/browser/open", json={"url": "https://example.com/"})
            return (page.status, page.headers.get("Content-Security-Policy", ""), browser.token in html,
                    rebound.status, other_site.status, outside.status, await outside.json())

    status, csp, has_token, rebound, other_site, outside, body = asyncio.run(scenario())
    assert status == 200 and has_token and "default-src 'none'" in csp
    assert rebound == 403 and other_site == 403
    assert outside == 400 and "kaggle.com" in body["error"]


def test_the_websocket_needs_the_page_token_and_a_local_origin(tmp_path):
    async def scenario():
        app, browser = browser_app(tmp_path)
        set_auto_click(tmp_path, "kaggle-han", False)
        async with TestClient(TestServer(app)) as client:
            refused = []
            for path, headers in (("/kite/browser/ws?k=wrong", {}),
                                  (f"/kite/browser/ws?k={browser.token}", {"Origin": "https://attacker.example"})):
                try:
                    await client.ws_connect(path, headers=headers)
                except Exception as exc:
                    refused.append(getattr(exc, "status", None))
            ws = await client.ws_connect(f"/kite/browser/ws?k={browser.token}",
                                         headers={"Origin": f"http://127.0.0.1:{client.port}"})
            first = json.loads((await ws.receive(timeout=5)).data)
            await ws.send_str(json.dumps({"t": "auto", "worker": "kaggle-han", "on": True}))
            second = json.loads((await ws.receive(timeout=5)).data)
            await ws.send_str(json.dumps({"t": "auto", "worker": "someone-else", "on": True}))
            await ws.close()
            return refused, first, second

    refused, first, second = asyncio.run(scenario())
    assert refused == [403, 403]
    assert first["t"] == "status" and not first["open"]
    assert first["workers"] == [{"id": "kaggle-han", "kernel": "hanle/kite-worker", "auto": False}]
    assert second["workers"][0]["auto"] is True and auto_click_on(tmp_path, "kaggle-han")
    assert not auto_click_on(tmp_path, "someone-else")                 # only workers with a notebook


# ---------------------------------------------------------------- autostart through the browser (fake browser)
class FakeBrowser:
    def __init__(self, result=None, fail=False):
        self.opened, self.clicked, self.result, self.fail = [], [], result or {"ok": True}, fail

    async def open(self, url):
        if self.fail:
            raise RuntimeError("Chromium is not installed")
        self.opened.append(url)
        return {}

    async def save_version(self, kernel):
        self.clicked.append(kernel)
        return self.result


def browser_launcher(tmp_path, *, clicks: bool, browser=None, fake=None):
    told, records = [], []
    config, gateway, launcher, fake, clock = make(
        tmp_path, launch={"kernel": "hanle/kite-worker", "via": "browser"}, fake=fake,
        browser=browser, auto_click=lambda worker: clicks,
        notify=lambda title, message, **kw: told.append((title, message)), journal=records.append)
    return gateway, launcher, fake, clock, told, records


def test_with_the_switch_off_kite_only_opens_the_notebook_and_waits_for_your_click(tmp_path):
    browser = FakeBrowser()

    async def scenario():
        gateway, launcher, fake, clock, told, records = browser_launcher(tmp_path, clicks=False, browser=browser)
        await launcher.wake(model="qwen3.6", reason="request")
        waiting = launcher.status()["kaggle-han"]
        note = launcher.wait_note("qwen3.6")
        starting = launcher.starting("qwen3.6")
        clock["now"] += 120
        await launcher.check_starting()                                 # still the old, finished run
        still = launcher.status()["kaggle-han"]["state"]
        fake.status = "QUEUED"                                          # you pressed Save Version
        clock["now"] += 60
        await launcher.check_starting()
        after = launcher.status()["kaggle-han"]
        await gateway.heartbeat("kaggle-han", {"ready": True, "loaded_model": "qwen3.6"})
        alive = launcher.status()["kaggle-han"]["state"]
        return fake, waiting, note, starting, still, after, alive, told, records

    fake, waiting, note, starting, still, after, alive, told, records = asyncio.run(scenario())
    assert browser.opened == ["https://www.kaggle.com/code/hanle/kite-worker/edit"] and browser.clicked == []
    assert [c[:2] for c in fake.calls if c[1] != "status"] == []       # nothing pushed through the API
    assert waiting["state"] == "waiting" and "bấm Save Version" in waiting["detail"]
    assert "/kite/browser" in note and starting
    assert told and "/kite/browser" in told[0][1]
    assert still == "waiting" and after["state"] == "starting" and alive == "alive"
    events = [(r["event"], r.get("clicked")) for r in records]
    assert ("waiting", None) in events and ("push", False) in events and events[-1][0] == "alive"


def test_with_the_switch_on_kite_presses_save_version(tmp_path):
    browser = FakeBrowser()

    async def scenario():
        _gateway, launcher, fake, _clock, told, records = browser_launcher(tmp_path, clicks=True, browser=browser)
        await launcher.wake(model="qwen3.6")
        return launcher.status()["kaggle-han"], told, records

    state, told, records = asyncio.run(scenario())
    assert browser.clicked == ["hanle/kite-worker"]
    assert state["state"] == "starting" and state["kaggle"] == "queued" and "Kite đã bấm" in state["detail"]
    assert not told and records[-1]["event"] == "push" and records[-1]["clicked"] is True


def test_a_sign_in_or_a_changed_page_falls_back_to_your_click(tmp_path):
    for result, words in (({"ok": False, "step": "login"}, "đăng nhập"),
                          ({"ok": False, "step": "save-run-all", "detail": "Timeout"}, "save-run-all")):
        browser = FakeBrowser(result)

        async def scenario():
            _gateway, launcher, _fake, _clock, told, _records = browser_launcher(
                tmp_path, clicks=True, browser=browser)
            await launcher.wake(model="qwen3.6", force=True)
            return launcher.status()["kaggle-han"], told

        state, told = asyncio.run(scenario())
        assert state["state"] == "waiting" and words in state["detail"] and told


def test_no_browser_installed_is_an_error_that_says_how_to_install_it(tmp_path):
    for browser in (None, FakeBrowser(fail=True)):
        async def scenario():
            _gateway, launcher, *_rest = browser_launcher(tmp_path, clicks=False, browser=browser)
            await launcher.wake(model="qwen3.6", force=True)
            return launcher.status()["kaggle-han"]

        state = asyncio.run(scenario())
        assert state["state"] == "error"
        assert ("pip install" in state["detail"]) if browser is None else ("Chromium" in state["detail"])


def test_nobody_clicks_for_an_hour_then_kite_stops_waiting(tmp_path):
    async def scenario():
        _gateway, launcher, _fake, clock, _told, _records = browser_launcher(tmp_path, clicks=False,
                                                                          browser=FakeBrowser())
        await launcher.wake(model="qwen3.6")
        clock["now"] += 3601
        await launcher.check_starting()
        return launcher.status()["kaggle-han"], launcher.starting("qwen3.6")

    state, starting = asyncio.run(scenario())
    assert state["state"] == "idle" and not starting


# ---------------------------------------------------------------- the real thing (Chromium on this machine)
EDITOR = """<html><head><title>Kite worker | Kaggle</title></head><body>
<button id="sv" onclick="document.getElementById('d').style.display='block'">Save Version</button>
<div id="d" role="dialog" style="display:none">
  <label><input type="radio" name="m" checked>Quick Save</label>
  <label><input type="radio" name="m" id="c">Save &amp; Run All (Commit)</label>
  <button onclick="window.saved = document.getElementById('c').checked ? 'commit' : 'quick';
                   document.getElementById('d').style.display='none'">Save</button>
</div>
<input id="t"><button id="b" onclick="document.title='clicked'">Bấm</button>
<a id="out" href="https://example.com/">ra ngoài</a>
<script>document.cookie = "KAGGLEID=sess-123; max-age=3600; path=/";</script>
</body></html>"""


@pytest.fixture
def local_site():
    async def editor(_request):
        return web.Response(text=EDITOR, content_type="text/html")

    app = web.Application()
    app.router.add_get("/code/hanle/kite-worker/edit", editor)
    app.router.add_get("/account/login", editor)
    return app


chromium = pytest.mark.skipif(bool(browser_missing()), reason="Playwright Chromium not installed")


async def started(tmp_path, site):
    server = TestServer(site)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    browser = KiteBrowser(tmp_path, kernels={"kaggle-han": "hanle/kite-worker"},
                          allowed=lambda url: url.startswith(base) or allowed_url(url))
    browser.editor = lambda kernel: f"{base}/code/{kernel}/edit"
    return server, base, browser


@chromium
def test_the_owner_sees_the_screen_and_drives_mouse_and_keyboard(tmp_path, local_site):
    async def scenario():
        server, base, browser = await started(tmp_path, local_site)
        seen: list[dict] = []

        async def send(message):
            seen.append(json.loads(message))

        try:
            viewer = await browser.add_viewer(send)
            await browser.open(f"{base}/code/hanle/kite-worker/edit")
            box = await browser.page.locator("#b").bounding_box()
            for action in ("down", "up"):
                await browser.command({"t": "mouse", "a": action, "x": box["x"] + 5, "y": box["y"] + 5})
            field = await browser.page.locator("#t").bounding_box()
            await browser.command({"t": "mouse", "a": "down", "x": field["x"] + 5, "y": field["y"] + 5})
            await browser.command({"t": "mouse", "a": "up", "x": field["x"] + 5, "y": field["y"] + 5})
            await browser.command({"t": "text", "s": "xin chào"})
            await browser.page.click("#out")                            # a link out of kaggle.com
            for _ in range(50):
                if any(m["t"] == "frame" for m in seen) and browser.blocked:
                    break
                await asyncio.sleep(0.1)
            result = (await browser.page.title(), await browser.page.input_value("#t"), browser.page.url,
                      browser.blocked)
            await browser.remove_viewer(viewer)
        finally:
            await browser.close()
            await server.close()
        return result, seen

    (title, typed, url, blocked), seen = asyncio.run(scenario())
    assert title == "clicked" and typed == "xin chào"
    assert url.endswith("/code/hanle/kite-worker/edit") and blocked.startswith("https://example.com")
    frames = [m for m in seen if m["t"] == "frame"]
    assert frames and frames[0]["w"] == 1280 and len(frames[0]["d"]) > 1000
    assert any(m["t"] == "status" and m["open"] for m in seen)


@chromium
def test_kite_presses_save_version_and_keeps_the_session_encrypted(tmp_path, local_site):
    async def scenario():
        server, base, browser = await started(tmp_path, local_site)
        try:
            result = await browser.save_version("hanle/kite-worker", step_seconds=10)
            saved = await browser.page.evaluate("window.saved")
            await browser.close()
            blob = (tmp_path / "browser" / "kaggle.bin").read_bytes()
            again = KiteBrowser(tmp_path, allowed=browser.allowed)
            await again.ensure()
            cookies = await again.context.cookies()
            await again.close()
            browser.editor = lambda kernel: f"{base}/account/login?next=x"
            login = await browser.save_version("hanle/kite-worker", step_seconds=5)
        finally:
            await browser.close()
            await server.close()
        return result, saved, blob, cookies, login

    result, saved, blob, cookies, login = asyncio.run(scenario())
    assert result == {"ok": True} and saved == "commit"
    assert b"sess-123" not in blob and any(c["value"] == "sess-123" for c in cookies)
    assert login["ok"] is False and login["step"] == "login"
    assert os.stat(tmp_path / "browser" / "kaggle.bin").st_mode & 0o777 == 0o600


def test_kite_worker_browser_switches_auto_click_per_worker(tmp_path, capsys):
    from poc.kaggle_worker import cli
    from tests.poc.kaggle_worker.test_launcher import raw_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw_config(tmp_path, launch={"kernel": "hanle/kite-worker", "via": "browser"})))
    assert cli.main(["--config", str(path), "worker", "browser", "kaggle-han", "--auto-click", "on"]) == 0
    assert auto_click_on(tmp_path, "kaggle-han") and "rủi ro" in capsys.readouterr().out
    assert cli.main(["--config", str(path), "worker", "browser", "kaggle-han", "--auto-click", "off"]) == 0
    assert not auto_click_on(tmp_path, "kaggle-han")
