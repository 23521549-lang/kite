"""The built web page (web/dist) in a real browser: sign in through the login form, open every tab
from the menu, fail on a JavaScript error. The back end is the one test_web.py fakes (no Kite, no
gateway), served by uvicorn on a free local port.

Runs only when asked: KITE_WEB_PAGES=1 (CI's web-pages job builds the page and installs Playwright's
Chromium first). KITE_PAGES_BROWSER=/path/to/chrome uses a browser of your own instead of Playwright's."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("KITE_WEB_PAGES") != "1",
                                reason="KITE_WEB_PAGES=1 runs the page in a browser (CI's web-pages job)")

ROOT = Path(__file__).resolve().parents[3]
TABS = ("workspace", "chat", "tasks", "environments", "workers", "connect", "knowledge", "bench", "giam-sat",
        "settings", "")
# Counts the page's fetches; the page has settled once none ran for a few frames in a row.
SETTLE_SCRIPT = """(() => {
  let running = 0
  const fetch = window.fetch
  window.fetch = (...args) => { running++; return fetch(...args).finally(() => { running-- }) }
  window.__kiteQuietFrames = 0
  window.__kiteSettled = () => {
    window.__kiteQuietFrames = running === 0 ? window.__kiteQuietFrames + 1 : 0
    return window.__kiteQuietFrames >= 5
  }
})()"""


@pytest.fixture
def server(tmp_path):
    import uvicorn

    from poc.kaggle_worker.web import security as sec
    from poc.kaggle_worker.web.app import WebContext, create_app
    from poc.kaggle_worker.web.db import Store, connect, sqlite_url
    from tests.poc.kaggle_worker.test_launcher import raw_config
    from tests.poc.kaggle_worker.test_web import NOW, PASSWORD, SECRET, FakeGateway, FakeOps

    dist = ROOT / "web" / "dist"
    assert (dist / "index.html").is_file(), "build the page first: cd web && npm ci && npm run build"
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("w" * 32 + "\n")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw))
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")), clock=lambda: NOW)
    store.migrate()
    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    store.create_user("han", sec.hash_password(PASSWORD), sealer.seal(SECRET))
    # Plain http on localhost: the session cookie without the Secure flag (its name changes with it).
    ctx = WebContext(config_path, store, sealer, ops_factory=lambda _config: FakeOps(), gateway=FakeGateway(),
                     clock=lambda: NOW, events_every=0.05, secure_cookies=False, dist=dist)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    web = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=web.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not web.started:
        assert time.monotonic() < deadline and thread.is_alive(), "the web did not start"
        time.sleep(0.05)
    yield {"url": f"http://localhost:{port}", "code": sec.totp_code(SECRET, int(NOW // 30)), "password": PASSWORD}
    web.should_exit = True
    thread.join(10)


def test_every_tab_opens_without_a_javascript_error(server):
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as playwright:
        own = os.environ.get("KITE_PAGES_BROWSER")
        browser = playwright.chromium.launch(**({"executable_path": own, "args": ["--no-sandbox"]} if own else {}))
        try:
            page = browser.new_page()
            page.add_init_script(SETTLE_SCRIPT)
            page.on("pageerror", lambda error: errors.append(f"{page.url}: {error}"))
            # A status error of the fake back end (a route it does not fake) is the browser's console line
            # "Failed to load resource": not the page's fault. Any other console error is.
            page.on("console", lambda message: message.type == "error"
                    and not message.text.startswith("Failed to load resource")
                    and errors.append(f"{page.url}: console: {message.text}"))
            page.goto(server["url"] + "/")
            page.locator('input[autocomplete="username"]').fill("han")
            page.locator('input[autocomplete="current-password"]').fill(server["password"])
            page.locator('input[inputmode="numeric"]').first.click()
            page.keyboard.type(server["code"])
            page.keyboard.press("Enter")
            menu = page.get_by_role("navigation")
            menu.wait_for()
            for tab in TABS:
                menu.locator(f'a[href="/{tab}"]').click()
                page.wait_for_url(f"{server['url']}/{tab}")
                page.evaluate("window.__kiteQuietFrames = 0")
                page.wait_for_function("() => window.__kiteSettled()", polling="raf", timeout=15_000)
                if errors:
                    break
                assert page.locator("main").inner_text(timeout=5_000).strip(), f"/{tab} shows nothing"
        finally:
            browser.close()
    assert not errors, "\n".join(errors)
