"""GET /api/monitor: cost + quota per worker, read-only.

The page only rolls up numbers other modules already own — it never re-enters a price,
a quota or a cap (those are set when a worker is created/edited, Gói 1):

- per-worker spend, brand, label, caps and prices come from ``worker_rows`` (web/workers.py);
- Kaggle GPU seconds come from the gateway journal, last 7 days (``journal.py:
  request_record`` writes ``worker`` + ``run_s`` per finished request; the gateway writes
  them through ``Journal.write`` in cli.py:1253, so each line also carries ``ts``) — the
  configured quota is Kaggle's weekly budget (30 h/week), so "used" is the same window;
- RunPod hours come from the journal ``spend`` records ``kind == "pod"`` (pods.py ``_accrue``
  writes ``hours``; ``_record`` stamps ``worker`` + ``ts``);
- the GPU quota itself (``ctx.quota()``) is passed through untouched, as /api/overview does.

Alert evaluation lives in ``web/monitor_alerts.py`` (worker (c)); this module only hands it
the current metric values and reports how many rules are over their threshold.

The metric values are shared: :func:`metric_values` computes them without touching the
gateway or evaluating anything, so ``POST /api/monitor/alerts/check`` can evaluate a
rule on demand (including ``kaggle_quota_pct``) without re-running the lazy evaluation
inside ``build_monitor_payload``.
"""

from __future__ import annotations

import asyncio
import calendar
import time
from typing import Any, Callable

from fastapi import Request

from . import workers as wk

# Kaggle's GPU quota resets weekly (30 h/week); the configured quota is that weekly
# budget, so "used" is the last 7 days, not the whole journal. A record without a
# usable timestamp is counted (never silently drop usage); only records provably
# older than the window are left out.
KAGGLE_WINDOW_SECONDS = 7 * 86400


def _pct(spent: float, cap: float | None) -> float | None:
    """Spent as a percentage of a cap, one decimal; None when there is no usable cap."""
    if cap is None or cap <= 0:
        return None
    return round(spent / cap * 100, 1)


def _month_start_utc(now: float) -> float:
    """Epoch of the 1st of the current month, UTC (the same clock the spend ledger uses:
    relay.py ``_month`` formats with ``gmtime``)."""
    current = time.gmtime(now)
    return float(calendar.timegm((current.tm_year, current.tm_mon, 1, 0, 0, 0, 0, 0, 0)))


def _kaggle_used_seconds(journal: Any, worker_id: str, since: float) -> float:
    """GPU seconds one Kaggle worker spent since ``since`` (last 7 days), from the journal.

    Each finished request carries ``worker`` (the worker that ran it, gateway.py) and
    ``run_s`` (how long it ran, journal.py). Records without a worker (unassigned jobs,
    status notes written by ``journal_note``) carry no ``run_s`` and are skipped by the
    ``or 0`` below. Only records with a numeric ``ts`` older than ``since`` are left
    out; a record without a usable timestamp is counted rather than silently dropped.
    """
    total = 0.0
    for r in journal.read("gateway"):
        if r.get("worker") != worker_id:
            continue
        ts = r.get("ts")
        if isinstance(ts, (int, float)) and ts < since:
            continue
        total += r.get("run_s") or 0
    return round(total, 1)


def _runpod_hours_month(journal: Any, worker_id: str, month_start: float) -> float:
    """Hours a rented pod ran this month (UTC), from journal ``spend`` records.

    ``pods.py`` flushes ``{"kind": "pod", "hours": ...}`` (plus ``worker`` + ``ts``) only
    when the pod is stopped or the flush interval passes, so hours accrued since the last
    flush are not in the journal yet — the same lag the spend ledger has.
    """
    return round(sum(float(r.get("hours") or 0)
                     for r in journal.read("spend", month_start)
                     if r.get("kind") == "pod" and r.get("worker") == worker_id), 4)


def _workers_and_values(config: Any, ledger: Any, journal: Any, now: float,
                        month_start: float, status: dict | None
                        ) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    """(per-worker cost rows, alert metric values) — the single computation behind both
    ``build_monitor_payload`` and :func:`metric_values`. Pure: no gateway call, no alert
    evaluation, no writes."""
    providers = {p.effective_worker_id: p for p in config.providers}

    # worker_rows is the single source for brand/label/spend/caps/prices; the auto-click
    # flag it also returns is not surfaced here, so a stub is enough.
    rows = wk.worker_rows(config, status, ledger, lambda _wid: False)

    workers: list[dict[str, Any]] = []
    values: dict[str, dict[str, float]] = {}
    for row in rows:
        wid = str(row["id"])
        kind = str(row["type"])
        spend = row["spend"]
        provider = providers.get(wid)
        metrics: dict[str, float] = {}

        relay: dict[str, Any] | None = None
        api = row.get("api")
        if isinstance(api, dict):
            caps = api.get("caps") or {}
            cap_day = _pct(float(spend["today_usd"]), caps.get("daily_usd"))
            cap_month = _pct(float(spend["month_usd"]), caps.get("monthly_usd"))
            relay = {"price": {"input": api["price"]["input"], "output": api["price"]["output"]},
                     "caps": {"daily_usd": caps.get("daily_usd"), "monthly_usd": caps.get("monthly_usd")},
                     "cap_pct_day": cap_day, "cap_pct_month": cap_month}
            if cap_day is not None:
                metrics["cost_day_pct"] = cap_day
            if cap_month is not None:
                metrics["cost_month_pct"] = cap_month

        kaggle: dict[str, Any] | None = None
        if kind == "kaggle":
            quota = int(provider.quota_gpu_seconds) if provider is not None else 0
            used = _kaggle_used_seconds(journal, wid, now - KAGGLE_WINDOW_SECONDS)
            pct = _pct(used, quota if quota > 0 else None)
            kaggle = {"quota_gpu_seconds": quota, "used_gpu_seconds": used,
                      "remaining_gpu_seconds": round(quota - used, 1),
                      "pct_used": pct, "used_source": "journal"}
            if pct is not None:
                metrics["kaggle_quota_pct"] = pct

        runpod: dict[str, Any] | None = None
        pod = row.get("pod")
        if isinstance(pod, dict):
            hours = _runpod_hours_month(journal, wid, month_start)
            runpod = {"price_hour": pod.get("price_hour"), "running_hours_month": hours,
                      "cost_month_usd": round(float(spend["month_usd"]), 4),
                      "running": bool(row.get("alive"))}
            metrics["runpod_month_usd"] = float(spend["month_usd"])

        values[wid] = metrics
        workers.append({"id": wid, "label": row.get("label"), "type": kind,
                        "brand": row.get("brand"), "enabled": bool(row.get("enabled")),
                        "alive": bool(row.get("alive")),
                        "spend": {"today_usd": round(float(spend["today_usd"]), 4),
                                  "month_usd": round(float(spend["month_usd"]), 4)},
                        "relay": relay, "kaggle": kaggle, "runpod": runpod})

    return workers, values


def _evaluate_alerts(ctx: Any, values: dict[str, dict[str, float]]) -> tuple[list[dict], int]:
    """(fired alerts, rules currently over threshold), via worker (c)'s module.

    Imported lazily on purpose: the import is re-resolved on every call, so tests can
    substitute the alerts module (``sys.modules``) and so importing this module alone
    never fails when the alerts module is absent. In the real app this guard never
    hides a broken alerts module — ``app.py`` imports it at startup with a plain
    import, so a broken module fails loudly there. This module never evaluates a rule
    itself.
    """
    try:
        from .monitor_alerts import evaluate_and_notify
    except ImportError:
        return [], 0
    return evaluate_and_notify(ctx, values)


def metric_values(ctx: Any) -> dict[str, dict[str, float]]:
    """Alert metric values ``{worker_id: {metric: float}}``, without side effects.

    The same numbers ``build_monitor_payload`` reports, computed without calling the
    gateway and without evaluating any rule — so the "check now" endpoint can evaluate
    on demand. ``status`` is None here: liveness is not part of any metric value.
    """
    config = ctx.config()
    now = ctx.clock()
    _workers, values = _workers_and_values(config, ctx.ledger(), ctx.journal(), now,
                                           _month_start_utc(now), None)
    return values


async def build_monitor_payload(ctx: Any) -> dict[str, Any]:
    """The GET /api/monitor response, exactly the shape in HOP-DONG-GIAO-TIEP.md (Gói 8)."""
    config = ctx.config()
    ledger = ctx.ledger()
    journal = ctx.journal()
    now = ctx.clock()
    month_start = _month_start_utc(now)

    status: dict | None = None
    try:
        status = await ctx.gateway("GET", "/status", None)
    except Exception:                                  # gateway down: every worker reads not alive
        status = None

    workers, values = _workers_and_values(config, ledger, journal, now, month_start, status)

    # Evaluation runs in a thread: ntfy sends synchronously inside evaluate_and_notify,
    # and a slow send must not block the event loop (see monitor_alerts.py).
    _fired, active = await asyncio.to_thread(_evaluate_alerts, ctx, values)

    gpu_quota: dict | None = None
    if ctx.quota is not None:
        try:
            gpu_quota = await ctx.quota()
        except Exception:
            gpu_quota = None

    return {"workers": workers, "gpu_quota": gpu_quota,
            "totals": {"today_usd": round(sum(w["spend"]["today_usd"] for w in workers), 4),
                       "month_usd": round(sum(w["spend"]["month_usd"] for w in workers), 4)},
            "alerts_active": active}


def register_monitor_routes(app: Any, ctx: Any,
                            auth: Callable[[Request], Any] | None = None) -> None:
    """Mount GET /api/monitor. ``auth`` is the session check from create_app (app.py):
    passed in because it is a closure there, not an importable helper. When it is not
    passed, the require_session middleware in create_app still returns 401 for any
    /api/ route without a session — the per-handler call is the same check the other
    read endpoints make, kept here so this route matches them exactly.
    """

    @app.get("/api/monitor")
    async def monitor(request: Request) -> dict[str, Any]:
        if auth is not None:
            auth(request)
        return await build_monitor_payload(ctx)
