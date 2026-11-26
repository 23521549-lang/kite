"""Start the Gói 4 scheduler engine inside the web process (agent trưởng).

The engine is a periodic scan loop (``schedules.py``): due plan schedules plus two builtin
jobs — the 00:00 Asia/Bangkok secretary report and the 5-minute "Cần bạn quyết" scan.
It lives in the web process on purpose: schedules must still fire when Temporal is down,
and the triggers only need the SQLite store plus the OpenCode HTTP API.

Only ``cli.py`` starts it (``create_app(..., start_scheduler=True)`` in ``_web_serve``);
tests and other ``create_app`` callers never get an engine. Building the engine never
raises: when OpenCode cannot be reached the engine still starts with no starter — plan
triggers then fail with a journal note and the builtins still run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any

from .. import secretary as sec
from ..mcp_server import sweep_stale_mcp_processes
from ..needs_decision import notify_new_items
from ..schedules import SchedulerEngine, ScheduleStore, UrllibOpenCodeStarter, daily_at_bangkok
from .secretary_api import _make_notifier, _post_report_to_secretary
from .workspace_api import _scan

_SCAN_INTERVAL_SECONDS = 300  # "Cần bạn quyết" scan, same as the contract


def _note_fn(ctx: Any):
    """The engine's journal-note hook: engine events the secretary can read."""

    def note(record: dict) -> None:
        try:
            ctx.journal().write("notes", record)
        except Exception as exc:  # a note must never kill the engine
            print(f"kite schedules: không ghi được journal note ({exc})", file=sys.stderr, flush=True)

    return note


def _needs_decision_job(ctx: Any):
    """One scan cycle: read the current needs-decision items, ntfy the new ones once."""

    def job() -> None:
        items = _scan(ctx)
        notifier = _make_notifier(ctx)
        if notifier is None:
            return
        notify_new_items(notifier, items, ctx.state_dir / "needs_decision" / "notified.json")

    return job


def _mcp_sweep_job(ctx: Any):
    """One sweep cycle: SIGTERM orphaned ``kite mcp serve`` processes (Gói 2 teardown).

    A sweep cycle must never kill the engine: errors only print.
    """

    def job() -> None:
        try:
            sweep_stale_mcp_processes(ctx.state_dir, ctx.config_path)
        except Exception as exc:  # noqa: BLE001 - a sweep cycle must not kill the engine
            print(f"kite schedules: không quét được process MCP mồ côi ({exc})",
                  file=sys.stderr, flush=True)

    return job


def _daily_report_job(ctx: Any):
    """The 00:00 Asia/Bangkok secretary report: build, save, ntfy once, post to the bubble."""

    async def job() -> None:
        result = await asyncio.to_thread(
            sec.run_daily_report, ctx.state_dir, journal=ctx.journal(),
            scan_fn=lambda: _scan(ctx), notifier=_make_notifier(ctx))
        markdown = Path(result["path"]).read_text(encoding="utf-8")
        posted = await _post_report_to_secretary(ctx, markdown)
        if not posted["posted"]:
            print(f"kite secretary: không đăng được báo cáo ngày {result['date']} vào "
                  f"session thư ký: {posted.get('error')}", file=sys.stderr, flush=True)

    return job


def build_engine(ctx: Any) -> SchedulerEngine:
    """The engine with both builtin jobs registered (raises only on a bad store path)."""
    store = ScheduleStore(ctx.state_dir / "scheduler.sqlite3")
    chat_dir = ctx.chat_dir if getattr(ctx, "chat_dir", None) is not None else Path.home() / "kite-chat"
    try:
        url, password = ctx.opencode()
        starter: UrllibOpenCodeStarter | None = UrllibOpenCodeStarter(url, password)
    except Exception as exc:
        # No starter: plan triggers fail with a journal note, the builtins still run.
        print(f"kite schedules: không có OpenCode starter ({exc}); kế hoạch sẽ không kích "
              f"được cho tới khi kite-opencode chạy", file=sys.stderr, flush=True)
        starter = None
    engine = SchedulerEngine(store, chat_dir=Path(chat_dir), starter=starter, note=_note_fn(ctx))
    engine.register_builtin("needs_decision_scan", _SCAN_INTERVAL_SECONDS, _needs_decision_job(ctx))
    engine.register_builtin("mcp_sweep", _SCAN_INTERVAL_SECONDS, _mcp_sweep_job(ctx))
    engine.register_builtin("secretary_daily_report", daily_at_bangkok("00:00"),
                            _daily_report_job(ctx))
    return engine


def start_scheduler_engine(ctx: Any) -> asyncio.Task | None:
    """Create the engine's background task, or None when it cannot be built at all."""
    try:
        engine = build_engine(ctx)
    except Exception as exc:
        print(f"kite web: không khởi động được động cơ hẹn giờ ({type(exc).__name__}: {exc}); "
              f"lịch sẽ không chạy", file=sys.stderr, flush=True)
        return None
    return asyncio.create_task(engine.run_forever())
