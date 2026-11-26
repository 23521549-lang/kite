"""Gói 8 (c): monitor alerts — rules, lazy evaluation, ntfy, routes.

Follows the ``test_web.py`` pattern: one owner account with password + TOTP, every route
behind the session, the fixture's own relay + pod + pull workers with a seeded ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")

from fastapi import HTTPException
from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker import notify  # noqa: E402
from poc.kaggle_worker.relay import SpendLedger  # noqa: E402
from poc.kaggle_worker.web import security as sec  # noqa: E402
from poc.kaggle_worker.web.app import WebContext, create_app  # noqa: E402
from poc.kaggle_worker.web.db import Store, connect, sqlite_url  # noqa: E402
from poc.kaggle_worker.web.monitor import metric_values  # noqa: E402
from poc.kaggle_worker.web.monitor_alerts import (  # noqa: E402
    MAX_HISTORY, create_rule, delete_rule, evaluate_and_notify, load_state,
    register_monitor_alert_routes, save_state)
from tests.poc.kaggle_worker.test_launcher import raw_config  # noqa: E402
from tests.poc.kaggle_worker.test_web import (  # noqa: E402
    NOW, PASSWORD, SECRET, FakeGateway, FakeOps, signed_in)

RELAY = "relay-openai"
POD = "relay-pod"
KAGGLE = "kaggle-han"


def _relay_provider(secrets: Path) -> dict:
    return {"id": RELAY, "adapter": "relay", "enabled": True, "owner": "han",
            "model": "gpt-x", "models": [], "capabilities": ["coding"],
            "quota": {"gpu_seconds": 0, "concurrency": 1},
            "relay": {"kind": "openai", "base_url": "https://api.example.test/v1",
                      "price": {"input": 1.0, "output": 2.0},
                      "caps": {"daily_usd": 10.0, "monthly_usd": 100.0}},
            "secrets": {"api_key": f"file:{secrets / 'api_key_relay'}"}}


def _pod_provider(secrets: Path) -> dict:
    return {"id": POD, "adapter": "relay", "enabled": True, "owner": "han",
            "model": "gpt-x", "models": [], "capabilities": ["coding"],
            "quota": {"gpu_seconds": 0, "concurrency": 1},
            "relay": {"kind": "openai", "base_url": "https://pod-1-8000.proxy.runpod.net/v1",
                      "price": {"input": 0.0, "output": 0.0},
                      "caps": {"daily_usd": 50.0, "monthly_usd": 500.0},
                      "pod": {"provider": "runpod", "id": "abc1234", "port": 8000,
                              "price_hour": 0.5}},
            "secrets": {"api_key": f"file:{secrets / 'api_key_relay'}",
                        "runpod_api_key": f"file:{secrets / 'runpod_api_key_relay-pod'}"}}


@pytest.fixture
def alert_site(tmp_path):
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})
    secrets = tmp_path / "secrets"
    (secrets / "api_key_relay").write_text("sk-test\n")
    (secrets / "runpod_api_key_relay-pod").write_text("rp-test\n")
    raw["providers"].append(_relay_provider(secrets))
    raw["providers"].append(_pod_provider(secrets))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw))
    clock = {"now": NOW}
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")), clock=lambda: clock["now"])
    store.migrate()
    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    store.create_user("han", sec.hash_password(PASSWORD), sealer.seal(SECRET))
    ledger = SpendLedger([], clock=lambda: clock["now"])
    ops, gateway = FakeOps(), FakeGateway()
    ctx = WebContext(config_path, store, sealer, ops_factory=lambda _config: ops, gateway=gateway,
                     clock=lambda: clock["now"], events_every=0.05,
                     ledger_factory=lambda: ledger)
    app = create_app(ctx)  # create_app already mounts the alert routes (app.py:568-569)
    client = TestClient(app, base_url="https://testserver")
    return {"client": client, "ctx": ctx, "store": store, "ops": ops, "gateway": gateway,
            "clock": clock, "path": tmp_path, "config": config_path, "ledger": ledger}


def _fake_ctx(tmp_path, *, ntfy=None, clock=None):
    config = SimpleNamespace(notify=SimpleNamespace(ntfy=ntfy))
    return SimpleNamespace(state_dir=tmp_path, clock=clock or (lambda: NOW),
                           config=lambda: config)


def _rule(ctx, worker_id=RELAY, metric="cost_day_pct", threshold=80.0):
    return create_rule(ctx.state_dir, worker_id, metric, threshold)


# ---------------------------------------------------------------- evaluation (no HTTP)
def test_no_rules_file_returns_empty_and_creates_nothing(tmp_path):
    ctx = _fake_ctx(tmp_path)
    assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 99.0}}) == ([], 0)
    assert not (tmp_path / "monitor" / "alerts.json").exists()


def test_fires_only_on_crossing_below_to_at_or_above(tmp_path):
    clock = {"now": NOW}
    ctx = _fake_ctx(tmp_path, clock=lambda: clock["now"])
    _rule(ctx)
    assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 50.0}}) == ([], 0)
    fired, active = evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 80.0}})  # exactly at it
    assert active == 1 and len(fired) == 1
    entry = fired[0]
    assert (entry["worker_id"], entry["metric"]) == (RELAY, "cost_day_pct")
    assert entry["value"] == 80.0 and entry["threshold"] == 80.0
    assert entry["notified"] is False and "80" in entry["message"]
    assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 85.0}}) == ([], 1)  # stays above: silent
    assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 70.0}}) == ([], 0)  # dips below
    fired, active = evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 90.0}})  # rises again: fires
    assert active == 1 and len(fired) == 1
    assert len(load_state(tmp_path)["history"]) == 2


def test_disabled_rule_never_fires_and_does_not_count_active(tmp_path):
    ctx = _fake_ctx(tmp_path)
    rule = _rule(ctx)
    state = load_state(tmp_path)
    state["rules"][0]["enabled"] = False
    save_state(tmp_path, state)
    assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 99.0}}) == ([], 0)
    assert load_state(tmp_path)["history"] == []
    assert rule["enabled"] is True  # the stored copy is what changed, not the returned dict


def test_unknown_worker_metric_or_value_is_skipped(tmp_path):
    ctx = _fake_ctx(tmp_path)
    _rule(ctx)
    assert evaluate_and_notify(ctx, {"someone-else": {"cost_day_pct": 99.0}}) == ([], 0)
    assert evaluate_and_notify(ctx, {RELAY: {"cost_month_pct": 99.0}}) == ([], 0)
    assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": "99"}}) == ([], 0)
    assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": float("nan")}}) == ([], 0)
    assert load_state(tmp_path)["history"] == []


def test_ntfy_sent_with_title_and_numbers_only(tmp_path, monkeypatch):
    (tmp_path / "ntfy_url").write_text("https://ntfy.sh/kite-test\n")
    ctx = _fake_ctx(tmp_path, ntfy=f"file:{tmp_path / 'ntfy_url'}")
    _rule(ctx)
    sent: list[tuple] = []
    monkeypatch.setattr(notify, "send", lambda url, title, message: sent.append((url, title, message)))
    fired, _active = evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 85.0}})
    assert len(sent) == 1
    url, title, message = sent[0]
    assert url == "https://ntfy.sh/kite-test" and title == "Kite: cảnh báo giám sát"
    assert RELAY in message and "85.0%" in message and "80.0%" in message
    assert fired[0]["notified"] is True


def test_ntfy_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    (tmp_path / "ntfy_url").write_text("https://ntfy.sh/kite-test\n")
    ctx = _fake_ctx(tmp_path, ntfy=f"file:{tmp_path / 'ntfy_url'}")
    _rule(ctx)

    def _boom(*args, **kwargs):
        raise RuntimeError("ntfy down")

    monkeypatch.setattr(notify, "send", _boom)
    fired, _active = evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 85.0}})
    assert len(fired) == 1 and fired[0]["notified"] is False


def test_history_keeps_at_most_200_records(tmp_path):
    clock = {"now": NOW}
    ctx = _fake_ctx(tmp_path, clock=lambda: clock["now"])
    _rule(ctx)
    for i in range(MAX_HISTORY + 5):
        clock["now"] += 1
        assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 10.0}}) == ([], 0)
        clock["now"] += 1
        fired, _active = evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 90.0}})
        assert len(fired) == 1
    history = load_state(tmp_path)["history"]
    assert len(history) == MAX_HISTORY
    assert history[0]["at"] < history[-1]["at"]  # oldest first, the first 5 firings dropped
    assert all(h["metric"] == "cost_day_pct" for h in history)


def test_last_alerted_persists_across_calls(tmp_path):
    ctx = _fake_ctx(tmp_path)
    rule = _rule(ctx)
    evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 90.0}})
    saved = load_state(tmp_path)["last_alerted"][rule["id"]]
    assert saved["value"] == 90.0 and saved["last_value"] == 90.0
    assert evaluate_and_notify(ctx, {RELAY: {"cost_day_pct": 95.0}}) == ([], 1)  # still no re-fire


def test_rule_storage_is_atomic_and_mode_0600(tmp_path):
    ctx = _fake_ctx(tmp_path)
    rule = _rule(ctx)
    path = tmp_path / "monitor" / "alerts.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert rule["enabled"] is True and rule["created_at"].startswith("2026-")
    assert delete_rule(tmp_path, rule["id"]) is True
    assert delete_rule(tmp_path, rule["id"]) is False
    assert load_state(tmp_path)["rules"] == []


# ---------------------------------------------------------------- routes
def test_alert_routes_need_a_session(alert_site):
    client = alert_site["client"]
    assert client.get("/api/monitor/alerts").status_code == 401
    assert client.post("/api/monitor/alerts", json={}).status_code == 401
    assert client.delete("/api/monitor/alerts/abc").status_code == 401
    assert client.post("/api/monitor/alerts/check", json={}).status_code == 401


def test_alert_crud(alert_site):
    client = alert_site["client"]
    headers = signed_in(alert_site)
    got = client.get("/api/monitor/alerts", headers=headers)
    assert got.status_code == 200
    assert got.json() == {"rules": [], "history": [], "ntfy_configured": False}
    created = client.post("/api/monitor/alerts", headers=headers,
                          json={"worker_id": RELAY, "metric": "cost_day_pct", "threshold": 80})
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["worker_id"] == RELAY and rule["metric"] == "cost_day_pct"
    assert rule["threshold"] == 80.0 and rule["enabled"] is True and rule["id"]
    listed = client.get("/api/monitor/alerts", headers=headers).json()
    assert [r["id"] for r in listed["rules"]] == [rule["id"]]
    assert client.delete(f"/api/monitor/alerts/{rule['id']}", headers=headers).status_code == 200
    assert client.get("/api/monitor/alerts", headers=headers).json()["rules"] == []
    missing = client.delete(f"/api/monitor/alerts/{rule['id']}", headers=headers)
    assert missing.status_code == 404 and "không có rule" in missing.json()["error"]


def test_create_rule_validation_is_vietnamese_400(alert_site):
    client = alert_site["client"]
    headers = signed_in(alert_site)
    bad_bodies = [
        ({"worker_id": RELAY, "metric": "nope", "threshold": 80},
         "metric phải là một trong"),
        ({"worker_id": RELAY, "metric": "cost_day_pct", "threshold": "abc"},
         "threshold phải là một số"),
        ({"worker_id": RELAY, "metric": "cost_day_pct", "threshold": True},
         "threshold phải là một số"),
        ({"worker_id": RELAY, "metric": "cost_day_pct", "threshold": -1},
         "threshold phải là số không âm"),
        ({"worker_id": "  ", "metric": "cost_day_pct", "threshold": 80},
         "thiếu worker_id"),
        ({"worker_id": RELAY, "metric": "cost_day_pct"},
         "threshold phải là một số"),
    ]
    for body, fragment in bad_bodies:
        response = client.post("/api/monitor/alerts", headers=headers, json=body)
        assert response.status_code == 400, (body, response.text)
        assert fragment in response.json()["error"], response.text
    not_json = client.post("/api/monitor/alerts", content="{oops",
                           headers={**headers, "Content-Type": "application/json"})
    assert not_json.status_code == 400 and "không phải JSON" in not_json.json()["error"]


def test_alert_writes_need_the_csrf_token(alert_site):
    client = alert_site["client"]
    signed_in(alert_site)
    body = {"worker_id": RELAY, "metric": "cost_day_pct", "threshold": 80}
    assert client.post("/api/monitor/alerts", json=body).status_code == 403
    assert client.post("/api/monitor/alerts", json=body,
                       headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_check_evaluates_from_ledger_and_dedupes(alert_site):
    client = alert_site["client"]
    headers = signed_in(alert_site)
    alert_site["ledger"].add(RELAY, 8.0, NOW)   # 8/10 USD today: 80% of the daily cap
    alert_site["ledger"].add(POD, 12.0, NOW)    # 12 USD this month on the pod
    for body in ({"worker_id": RELAY, "metric": "cost_day_pct", "threshold": 80},
                 {"worker_id": POD, "metric": "runpod_month_usd", "threshold": 10},
                 {"worker_id": KAGGLE, "metric": "kaggle_quota_pct", "threshold": 50}):
        assert client.post("/api/monitor/alerts", headers=headers, json=body).status_code == 201
    checked = client.post("/api/monitor/alerts/check", headers=headers, json={})
    assert checked.status_code == 200, checked.text
    data = checked.json()
    assert data["active_count"] == 2
    assert {f["metric"] for f in data["fired"]} == {"cost_day_pct", "runpod_month_usd"}
    assert data["values"][RELAY]["cost_day_pct"] == 80.0
    assert data["values"][POD]["runpod_month_usd"] == 12.0
    # The check now uses the shared values helper (web/monitor.py), so the Kaggle
    # quota % is a real number here too — 0.0 (no GPU seconds in the last 7 days),
    # below the rule's 50 threshold, so that rule correctly does not fire.
    assert data["values"][KAGGLE] == {"kaggle_quota_pct": 0.0}
    again = client.post("/api/monitor/alerts/check", headers=headers, json={})
    assert again.json() == {"fired": [], "active_count": 2,
                            "values": data["values"]}  # still above: no second message
    history = client.get("/api/monitor/alerts", headers=headers).json()["history"]
    assert len(history) == 2 and all(h["notified"] is False for h in history)


def test_check_sends_ntfy_when_configured(alert_site, monkeypatch):
    client = alert_site["client"]
    headers = signed_in(alert_site)
    (alert_site["path"] / "secrets" / "ntfy_url").write_text("https://ntfy.sh/kite-test\n")
    raw = json.loads(alert_site["config"].read_text(encoding="utf-8"))
    raw["notify"] = {"ntfy": f"file:{alert_site['path'] / 'secrets' / 'ntfy_url'}"}
    alert_site["config"].write_text(json.dumps(raw))
    sent: list[tuple] = []
    monkeypatch.setattr(notify, "send", lambda url, title, message: sent.append((url, title, message)))
    alert_site["ledger"].add(RELAY, 9.0, NOW)  # 90% of the daily cap
    assert client.post("/api/monitor/alerts", headers=headers,
                       json={"worker_id": RELAY, "metric": "cost_day_pct",
                             "threshold": 80}).status_code == 201
    assert client.get("/api/monitor/alerts", headers=headers).json()["ntfy_configured"] is True
    checked = client.post("/api/monitor/alerts/check", headers=headers, json={})
    assert checked.json()["fired"][0]["notified"] is True
    assert len(sent) == 1 and sent[0][0] == "https://ntfy.sh/kite-test"


def test_passed_auth_is_honoured_over_the_fallback(alert_site):
    # A bare app (no create_app wiring, no middleware): the only auth is the one passed in.
    from fastapi import FastAPI

    app = FastAPI()

    def _deny(request):
        raise HTTPException(403, "không được phép")

    register_monitor_alert_routes(app, alert_site["ctx"], auth=_deny)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/monitor/alerts").status_code == 403
    # And without a passed auth, the same bare app serves the route (the fallback only
    # guards writes, and only against a real session store).
    app2 = FastAPI()
    register_monitor_alert_routes(app2, alert_site["ctx"])
    assert TestClient(app2, base_url="https://testserver").get("/api/monitor/alerts").status_code == 200


def test_metric_values_come_from_ledger_config_and_journal(alert_site):
    alert_site["ledger"].add(RELAY, 5.0, NOW)   # 50% of the 10 USD daily cap, 5% monthly
    values = metric_values(alert_site["ctx"])
    assert values[RELAY] == {"cost_day_pct": 50.0, "cost_month_pct": 5.0}
    assert values[POD] == {"cost_day_pct": 0.0, "cost_month_pct": 0.0,
                           "runpod_month_usd": 0.0}
    # The pull worker has no relay caps — but it does have a Kaggle quota, so the
    # shared helper (unlike the old ledger-only current_values) reports its quota %.
    assert values[KAGGLE] == {"kaggle_quota_pct": 0.0}
