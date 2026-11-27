"""Scheduler wiring (Gói 4, agent trưởng): the engine inside the web process.

The wiring is the only piece that knows how the three workers' modules plug together:
``schedules`` (engine) + ``secretary`` (00:00 report) + ``needs_decision`` (scan) +
the two routers' helpers. The fake ctx here is deliberately minimal — no OpenCode
server, no chat dir, no ntfy — so these tests prove the engine starts and both builtin
jobs run (or fail gracefully) with nothing behind them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from poc.kaggle_worker import secretary as sec
from poc.kaggle_worker.journal import Journal
from poc.kaggle_worker.relay import SpendLedger
from poc.kaggle_worker.web.scheduler_wiring import build_engine


def _fake_ctx(tmp_path):
    config = SimpleNamespace(notify=SimpleNamespace(ntfy=None, near_cap_pct=80))
    ops = SimpleNamespace(db_path=tmp_path / "scheduler.sqlite3")
    return SimpleNamespace(
        state_dir=tmp_path,
        config_path=tmp_path / "config.yaml",
        chat_dir=None,
        opencode=lambda: ("http://127.0.0.1:4096", "pw"),
        config=lambda: config,
        ops=lambda: ops,
        journal=lambda: Journal(tmp_path / "journal"),
        ledger=lambda: SpendLedger([]),
    )


def test_build_engine_registers_builtins(tmp_path):
    engine = build_engine(_fake_ctx(tmp_path))
    assert set(engine._jobs) == {"needs_decision_scan", "secretary_daily_report", "mcp_sweep",
                                 "monitor_alerts"}
    assert engine._jobs["mcp_sweep"].interval_seconds == 300
    assert engine._jobs["monitor_alerts"].interval_seconds == 300
    assert engine.starter is not None  # loopback OpenCode URL, no server needed to build
    assert engine.chat_dir == Path.home() / "kite-chat"  # no chat dir: triggers note + retry


def test_engine_run_once_does_not_crash_with_no_data(tmp_path):
    engine = build_engine(_fake_ctx(tmp_path))
    summary = asyncio.run(engine.run_once())
    assert summary["errors"] == []


def test_scan_job_runs_with_no_data(tmp_path):
    engine = build_engine(_fake_ctx(tmp_path))
    engine._jobs["needs_decision_scan"].func()  # no scheduler db, no ntfy: a silent no-op


def test_report_job_builds_report_and_fails_post_gracefully(tmp_path):
    engine = build_engine(_fake_ctx(tmp_path))
    asyncio.run(engine._jobs["secretary_daily_report"].func())
    # The report is built and saved even though there is no OpenCode server to post to:
    # posting must never fail the report itself.
    day = sec.default_report_date()
    path = tmp_path / "secretary" / "reports" / f"{day.isoformat()}.md"
    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith("# Báo cáo ngày")


def test_monitor_alerts_fire_from_the_scheduler_without_the_page_open(tmp_path, monkeypatch):
    from poc.kaggle_worker.web import scheduler_wiring
    from poc.kaggle_worker.web.monitor_alerts import create_rule, load_state

    ctx = _fake_ctx(tmp_path)
    ctx.clock = lambda: 1_790_000_000.0
    engine = build_engine(ctx)
    asked = []
    monkeypatch.setattr(scheduler_wiring, "metric_values", lambda c: asked.append(c) or {"w1": {"cost_day_pct": 95.0}})
    asyncio.run(engine._jobs["monitor_alerts"].func())
    assert asked == []                                    # no rules: the spend is not even read
    create_rule(tmp_path, "w1", "cost_day_pct", 80.0)
    asyncio.run(engine._jobs["monitor_alerts"].func())
    history = load_state(tmp_path)["history"]
    assert len(history) == 1 and history[0]["worker_id"] == "w1" and history[0]["notified"] is False
