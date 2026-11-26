"""Gói 8 (a): GET /api/monitor — cost + quota per worker, read-only roll-up.

Fixture pattern follows tests/poc/kaggle_worker/test_web.py (site + login + signed_in),
with three providers: a Kaggle pull worker, a relay with caps, and a RunPod pod worker.
Spend and gateway lines are written straight to the journal the endpoint reads.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker.journal import Journal  # noqa: E402
from poc.kaggle_worker.relay import SpendLedger  # noqa: E402
from poc.kaggle_worker.web import security as sec  # noqa: E402
from poc.kaggle_worker.web.app import WebContext, create_app  # noqa: E402
from poc.kaggle_worker.web.db import Store, connect, sqlite_url  # noqa: E402

from tests.poc.kaggle_worker.test_launcher import raw_config  # noqa: E402

SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "correct horse battery staple"
NOW = 1_795_400_000.0                    # 2026-11-23 (Monday), UTC — the frozen clock
LAST_MONTH = NOW - 40 * 86400            # 2026-10-14: same journal, previous month


class FakeOps:
    async def list_tasks(self, limit=10):
        return []


class FakeGateway:
    """Gateway /status: Kaggle + pod alive, relay not in the gateway (not alive)."""

    def __init__(self):
        self.down = False

    async def __call__(self, method, path, body):
        if self.down:
            from poc.kaggle_worker.web.app import GatewayDown

            raise GatewayDown("ConnectionRefused")
        return {"workers": [{"worker_id": "kaggle-han", "alive": True, "busy": False, "running": 0,
                             "slots": 2, "loaded_model": "qwen3.6", "version": "abc",
                             "switch": {"on": True, "auto": True}},
                            {"worker_id": "runpod", "alive": True, "busy": False, "running": 0,
                             "slots": 1, "loaded_model": "qwen3-32b", "version": "abc",
                             "switch": {"on": True, "auto": True}}],
                "queued_jobs": 0, "running_jobs": 0}


def _raw(tmp_path: Path, *, relay_caps: dict | None = None, kaggle_quota: int = 108000) -> dict:
    """Three providers: Kaggle (30 h quota) + relay with caps + RunPod pod."""
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})
    raw["providers"][0]["quota"] = {"gpu_seconds": kaggle_quota, "concurrency": 1}
    secrets = tmp_path / "secrets"
    (secrets / "api_key_openrouter").write_text("sk-or-secret\n")
    (secrets / "runpod_api_key_runpod").write_text("rp-secret\n")
    (secrets / "api_key_runpod").write_text("vllm-secret\n")
    relay: dict = {"kind": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "upstream": {"gpt": "openai/gpt-5-mini"}, "price": {"input": 0.25, "output": 2}}
    if relay_caps is not None:
        relay["caps"] = relay_caps
    raw["providers"].append({
        "id": "openrouter", "adapter": "relay", "enabled": True, "model": "gpt", "models": ["claude"],
        "capabilities": ["coding"], "quota": {"gpu_seconds": 0, "concurrency": 3},
        "secrets": {"api_key": f"file:{secrets / 'api_key_openrouter'}"}, "relay": relay})
    raw["providers"].append({
        "id": "runpod", "adapter": "relay", "enabled": True, "owner": "self", "model": "qwen3-32b",
        "capabilities": ["coding"], "quota": {"gpu_seconds": 0, "concurrency": 1},
        "secrets": {"runpod_api_key": f"file:{secrets / 'runpod_api_key_runpod'}",
                    "api_key": f"file:{secrets / 'api_key_runpod'}"},
        "relay": {"kind": "openai", "pod": {"id": "abc123xyz", "price_hour": 0.79},
                  "caps": {"daily_usd": 5}}})
    return raw


def _make_site(tmp_path: Path, raw: dict) -> dict:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw))
    clock = {"now": NOW}
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")), clock=lambda: clock["now"])
    store.migrate()
    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    store.create_user("han", sec.hash_password(PASSWORD), sealer.seal(SECRET))
    gateway = FakeGateway()
    # The ledger's day/month totals use its own clock (real time in production); freeze
    # it to the fixture clock or "today" would be the real date, not the fixture's.
    ledger_factory = lambda: SpendLedger(Journal(tmp_path / "journal").read("spend"),
                                         clock=lambda: clock["now"])
    ctx = WebContext(config_path, store, sealer, ops_factory=lambda _config: FakeOps(), gateway=gateway,
                     clock=lambda: clock["now"], events_every=0.05, ledger_factory=ledger_factory)
    client = TestClient(create_app(ctx), base_url="https://testserver")
    return {"client": client, "ctx": ctx, "store": store, "gateway": gateway,
            "clock": clock, "path": tmp_path, "config": config_path}


@pytest.fixture
def site(tmp_path):
    return _make_site(tmp_path, _raw(tmp_path, relay_caps={"daily_usd": 1, "monthly_usd": 10}))


def _code(clock, offset=0):
    return sec.totp_code(SECRET, int(clock["now"] // 30) + offset)


def signed_in(site):
    response = site["client"].post("/api/auth/login", json={
        "username": "han", "password": PASSWORD, "code": _code(site["clock"])})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


def _spend(site, worker, usd, ts=NOW, **extra):
    site["ctx"].journal().write("spend", {"worker": worker, "usd": usd, "ts": ts, **extra})


def _gateway_run(site, worker, run_s, ts=NOW):
    site["ctx"].journal().write("gateway", {"worker": worker, "run_s": run_s, "ts": ts})


def _monitor(site):
    response = site["client"].get("/api/monitor", headers=signed_in(site))
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(payload):
    return {w["id"]: w for w in payload["workers"]}


# ------------------------------------------------------------------ shape + auth
def test_monitor_returns_the_contract_shape(site):
    payload = _monitor(site)
    assert set(payload) == {"workers", "gpu_quota", "totals", "alerts_active"}
    assert payload["gpu_quota"] is None and payload["alerts_active"] == 0
    assert payload["totals"] == {"today_usd": 0.0, "month_usd": 0.0}
    workers = _by_id(payload)
    assert set(workers) == {"kaggle-han", "openrouter", "runpod"}
    for worker in workers.values():
        assert set(worker) == {"id", "label", "type", "brand", "enabled", "alive",
                               "spend", "relay", "kaggle", "runpod"}
        assert set(worker["spend"]) == {"today_usd", "month_usd"}
    kaggle, relay, pod = workers["kaggle-han"], workers["openrouter"], workers["runpod"]
    assert (kaggle["type"], kaggle["brand"], kaggle["alive"]) == ("kaggle", "Kaggle", True)
    assert kaggle["relay"] is None and kaggle["runpod"] is None and kaggle["kaggle"] is not None
    assert (relay["type"], relay["brand"], relay["alive"]) == ("openai", "OpenRouter", False)
    assert relay["kaggle"] is None and relay["runpod"] is None and relay["relay"] is not None
    assert (pod["type"], pod["brand"], pod["alive"]) == ("runpod", "RunPod", True)
    assert pod["kaggle"] is None and pod["relay"] is not None and pod["runpod"] is not None
    # Gói 1 (label) is not in this tree yet: the field is present, just unset.
    assert all(w["label"] is None for w in workers.values())


def test_monitor_needs_a_session(site):
    assert site["client"].get("/api/monitor").status_code == 401


def test_monitor_totals_are_the_sum_of_the_workers(site):
    _spend(site, "kaggle-han", 0.5)
    _spend(site, "openrouter", 1.25)
    _spend(site, "runpod", 2.0)
    _spend(site, "openrouter", 10.0, ts=LAST_MONTH)          # last month: not in the totals
    payload = _monitor(site)
    assert payload["totals"] == {"today_usd": 3.75, "month_usd": 3.75}
    assert _by_id(payload)["openrouter"]["spend"] == {"today_usd": 1.25, "month_usd": 1.25}


# ------------------------------------------------------------------ relay caps
def test_relay_cap_percentages(site):
    _spend(site, "openrouter", 0.5)                          # day 0.50 / cap 1, month 0.50 / cap 10
    _spend(site, "openrouter", 1.5, ts=NOW - 86400)          # yesterday, same month: month 2.00
    relay = _by_id(_monitor(site))["openrouter"]["relay"]
    assert relay["price"] == {"input": 0.25, "output": 2.0}
    assert relay["caps"] == {"daily_usd": 1.0, "monthly_usd": 10.0}
    assert (relay["cap_pct_day"], relay["cap_pct_month"]) == (50.0, 20.0)


def test_relay_cap_percentages_are_null_without_a_cap(tmp_path):
    site = _make_site(tmp_path, _raw(tmp_path, relay_caps=None))
    _spend(site, "openrouter", 0.5)
    relay = _by_id(_monitor(site))["openrouter"]["relay"]
    assert relay["caps"] == {"daily_usd": None, "monthly_usd": None}
    assert (relay["cap_pct_day"], relay["cap_pct_month"]) == (None, None)


def test_relay_cap_zero_is_null_not_a_crash(tmp_path):
    site = _make_site(tmp_path, _raw(tmp_path, relay_caps={"daily_usd": 0, "monthly_usd": 10}))
    _spend(site, "openrouter", 0.5)
    relay = _by_id(_monitor(site))["openrouter"]["relay"]
    assert relay["cap_pct_day"] is None and relay["cap_pct_month"] == 5.0


# ------------------------------------------------------------------ Kaggle quota
def test_kaggle_used_seconds_come_from_gateway_journal(site):
    _gateway_run(site, "kaggle-han", 3600.0)
    _gateway_run(site, "kaggle-han", 1800.0)
    _gateway_run(site, "kaggle-han", None)                   # unassigned job: no run time
    _gateway_run(site, None, 9999.0)                         # no worker: belongs to nobody
    _gateway_run(site, "openrouter", 100.0)                  # another worker: not Kaggle's
    kaggle = _by_id(_monitor(site))["kaggle-han"]["kaggle"]
    assert kaggle == {"quota_gpu_seconds": 108000, "used_gpu_seconds": 5400.0,
                      "remaining_gpu_seconds": 102600.0, "pct_used": 5.0,
                      "used_source": "journal"}


def test_kaggle_used_seconds_count_only_the_last_7_days(site):
    # The configured quota is Kaggle's weekly budget, so "used" is the last 7 days.
    _gateway_run(site, "kaggle-han", 3600.0, ts=NOW - 8 * 86400)  # 8 days ago: outside
    _gateway_run(site, "kaggle-han", 1800.0)                     # today: inside
    kaggle = _by_id(_monitor(site))["kaggle-han"]["kaggle"]
    assert kaggle == {"quota_gpu_seconds": 108000, "used_gpu_seconds": 1800.0,
                      "remaining_gpu_seconds": 106200.0, "pct_used": 1.7,
                      "used_source": "journal"}


def test_kaggle_pct_is_null_without_a_quota(tmp_path):
    site = _make_site(tmp_path, _raw(tmp_path, kaggle_quota=0))
    kaggle = _by_id(_monitor(site))["kaggle-han"]["kaggle"]
    assert kaggle == {"quota_gpu_seconds": 0, "used_gpu_seconds": 0.0,
                      "remaining_gpu_seconds": 0.0, "pct_used": None, "used_source": "journal"}


# ------------------------------------------------------------------ RunPod hours
def test_runpod_hours_and_cost_come_from_pod_spend_records(site):
    _spend(site, "runpod", 1.975, kind="pod", hours=2.5)
    _spend(site, "runpod", 1.185, kind="pod", hours=1.5)
    _spend(site, "runpod", 9.99, ts=LAST_MONTH, kind="pod", hours=3.0)   # last month: excluded
    _spend(site, "runpod", 0.5)                                 # a relay charge, not pod hours
    pod = _by_id(_monitor(site))["runpod"]["runpod"]
    assert pod == {"price_hour": 0.79, "running_hours_month": 4.0,
                   "cost_month_usd": 3.66, "running": True}


def test_runpod_not_running_when_the_gateway_is_down(site):
    site["gateway"].down = True
    pod = _by_id(_monitor(site))["runpod"]["runpod"]
    assert pod["running"] is False


# ------------------------------------------------------------------ gpu_quota + alerts
def test_gpu_quota_is_passed_through_untouched(site):
    quota = {"kaggle-han": {"status": "ok", "detail": "còn 12h / 30h"}}

    async def fake_quota():
        return quota

    site["ctx"].quota = fake_quota
    assert _monitor(site)["gpu_quota"] == quota


def test_gpu_quota_failures_read_as_null(site):
    async def broken_quota():
        raise RuntimeError("kaggle CLI missing")

    site["ctx"].quota = broken_quota
    assert _monitor(site)["gpu_quota"] is None


def test_alerts_active_comes_from_the_alerts_module(site, monkeypatch):
    seen: dict = {}

    def evaluate_and_notify(ctx, values):
        seen.update(values)
        return [{"rule_id": "r1"}], 2

    module = types.ModuleType("poc.kaggle_worker.web.monitor_alerts")
    module.evaluate_and_notify = evaluate_and_notify
    monkeypatch.setitem(sys.modules, "poc.kaggle_worker.web.monitor_alerts", module)
    _spend(site, "openrouter", 0.5)
    _spend(site, "openrouter", 1.5, ts=NOW - 86400)
    _gateway_run(site, "kaggle-han", 5400.0)
    _spend(site, "runpod", 1.975, kind="pod", hours=2.5)
    payload = _monitor(site)
    assert payload["alerts_active"] == 2
    assert seen["openrouter"] == {"cost_day_pct": 50.0, "cost_month_pct": 20.0}
    assert seen["kaggle-han"] == {"kaggle_quota_pct": 5.0}
    # RunPod: relay cap ngày 5 USD (tháng không có trần) + tiền thuê tháng.
    assert seen["runpod"] == {"cost_day_pct": 39.5, "runpod_month_usd": 1.975}


def test_alerts_guard_when_the_alerts_module_is_missing(site, monkeypatch):
    monkeypatch.setitem(sys.modules, "poc.kaggle_worker.web.monitor_alerts", None)
    payload = _monitor(site)
    assert payload["alerts_active"] == 0
