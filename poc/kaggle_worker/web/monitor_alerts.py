"""Monitor alerts: owner-set thresholds on worker cost and quota.

A rule watches one worker and one metric (``cost_day_pct``, ``cost_month_pct``,
``kaggle_quota_pct`` or ``runpod_month_usd``). There are no default rules: the owner creates
them in the UI. Evaluation runs every 5 minutes in the web's scheduler (``scheduler_wiring.py``),
so an alert fires when nobody has the monitor page open, and also on every ``GET /api/monitor`` (see
``web/monitor.py``, which calls :func:`evaluate_and_notify` with the values it already computed)
and on ``POST /api/monitor/alerts/check``.

Firing rule: a rule fires only when its value crosses its threshold from below
(previous evaluated value under the threshold, current value at or above it). Staying
above the threshold does not fire again; dipping below and rising again fires again.
Deduplication state lives in ``last_alerted`` inside the state file, one entry per rule.

Storage is ``state_dir/monitor/alerts.json`` — ``{rules, history, last_alerted}`` — written
atomically under a lock (``joblock.updating`` / ``joblock.write_json``, mode 0600). History
keeps at most 200 records. Alert messages carry only a title and numbers, never code or
goal text (the ntfy discipline in ``notify.py``); without ntfy configured the firing is
still recorded in history with ``notified=false`` and nothing crashes.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request

from .. import joblock
from ..config import resolve_secret
from ..journal import iso
from . import security as sec

MAX_HISTORY = 200

METRICS = ("cost_day_pct", "cost_month_pct", "kaggle_quota_pct", "runpod_month_usd")

METRIC_LABELS = {
    "cost_day_pct": "chi phí ngày so với trần",
    "cost_month_pct": "chi phí tháng so với trần",
    "kaggle_quota_pct": "quota Kaggle đã dùng",
    "runpod_month_usd": "chi phí tháng RunPod",
}


# ------------------------------------------------------------------ storage
def alerts_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / "monitor" / "alerts.json"


def _empty_state() -> dict:
    return {"rules": [], "history": [], "last_alerted": {}}


def load_state(state_dir: str | Path) -> dict:
    """The stored ``{rules, history, last_alerted}`` (empty state when missing or broken)."""
    try:
        data = json.loads(alerts_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    rules = data.get("rules")
    history = data.get("history")
    last_alerted = data.get("last_alerted")
    return {"rules": rules if isinstance(rules, list) else [],
            "history": history if isinstance(history, list) else [],
            "last_alerted": last_alerted if isinstance(last_alerted, dict) else {}}


def save_state(state_dir: str | Path, state: dict) -> None:
    """Write the state atomically (mode 0600); call it inside ``joblock.updating(alerts_path(...))``."""
    joblock.write_json(alerts_path(state_dir), state, indent=1, ensure_ascii=False)


# ------------------------------------------------------------------ rules
def create_rule(state_dir: str | Path, worker_id: str, metric: str, threshold: float,
                *, clock: Callable[[], float] = time.time) -> dict:
    """Append a rule (already validated by the caller) and return it."""
    rule = {"id": uuid.uuid4().hex[:8], "worker_id": worker_id, "metric": metric,
            "threshold": float(threshold), "enabled": True, "created_at": iso(clock())}
    with joblock.updating(alerts_path(state_dir)):
        state = load_state(state_dir)
        state["rules"].append(rule)
        save_state(state_dir, state)
    return rule


def delete_rule(state_dir: str | Path, rule_id: str) -> bool:
    """Drop a rule (and its dedup entry); False when there is no such rule."""
    with joblock.updating(alerts_path(state_dir)):
        state = load_state(state_dir)
        before = len(state["rules"])
        state["rules"] = [r for r in state["rules"]
                          if not (isinstance(r, dict) and r.get("id") == rule_id)]
        if len(state["rules"]) == before:
            return False
        state["last_alerted"].pop(rule_id, None)
        save_state(state_dir, state)
    return True


def _validate_rule_body(body: Any) -> tuple[str, str, float]:
    """The ``{worker_id, metric, threshold}`` of a create request, or a Vietnamese 400."""
    if not isinstance(body, dict):
        raise HTTPException(400, "nội dung yêu cầu phải là một đối tượng JSON")
    worker_id = body.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise HTTPException(400, "thiếu worker_id: chọn một worker để đặt cảnh báo")
    metric = body.get("metric")
    if metric not in METRICS:
        raise HTTPException(400, "metric phải là một trong: " + ", ".join(METRICS))
    threshold = body.get("threshold")
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)):
        raise HTTPException(400, "threshold phải là một số")
    if threshold < 0:
        raise HTTPException(400, "threshold phải là số không âm")
    return worker_id.strip(), str(metric), float(threshold)


# ------------------------------------------------------------------ evaluation
def _format_value(metric: str, value: float) -> str:
    if metric == "runpod_month_usd":
        return f"{value:.2f} USD"
    return f"{value:.1f}%"


def alert_message(rule: dict, value: float) -> tuple[str, str]:
    """(title, message) for a firing: only the worker id and the numbers, no code or goal."""
    label = METRIC_LABELS.get(rule.get("metric"), str(rule.get("metric")))
    return ("Kite: cảnh báo giám sát",
            f"Worker {rule.get('worker_id')}: {label} {_format_value(rule['metric'], value)} "
            f"đã vượt ngưỡng {_format_value(rule['metric'], float(rule['threshold']))}.")


def _send_alert(ctx: Any, rule: dict, value: float) -> bool:
    """Send one ntfy message; False (never a crash) when ntfy is unset or sending fails."""
    try:
        config = ctx.config()
        ref = config.notify.ntfy if getattr(config, "notify", None) is not None else None
        if not ref:
            return False
        from .. import notify

        title, message = alert_message(rule, value)
        notify.send(resolve_secret(ref), title, message)
        return True
    except Exception:
        return False


def evaluate_and_notify(ctx: Any, values: dict) -> tuple[list[dict], int]:
    """Evaluate every enabled rule against ``values`` (``{worker_id: {metric: float}}``).

    Returns ``(fired, active_count)``: ``fired`` is the list of alerts that just fired
    (already written to history and sent over ntfy when configured), ``active_count`` is
    how many enabled rules are currently at or above their threshold. With no rules file
    (or no rules at all) this returns ``([], 0)`` without touching anything.

    A rule fires only on a crossing: its previous evaluated value was under the threshold
    and the current value is at or above it. The previous value is kept in
    ``last_alerted[rule_id]["last_value"]`` (``"value"``/``"at"`` there record the last
    firing itself). The caller owns the event loop: ntfy sends synchronously inside this
    function, so an async caller should run it in a thread (``asyncio.to_thread``).
    """
    state_dir = ctx.state_dir
    # The 5-minute job and an open monitor page evaluate at the same time: one at a time, or
    # both see the same crossing and send it twice.
    with joblock.updating(alerts_path(state_dir)):
        state = load_state(state_dir)
        rules = state["rules"]
        if not rules:
            return [], 0
        clock = getattr(ctx, "clock", time.time)
        now = clock() if callable(clock) else time.time()
        fired: list[dict] = []
        active = 0
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            worker_values = values.get(rule.get("worker_id")) if isinstance(values, dict) else None
            if not isinstance(worker_values, dict):
                continue
            value = worker_values.get(rule.get("metric"))
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                continue
            value = float(value)
            threshold = rule.get("threshold")
            if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                    or not math.isfinite(threshold)):
                continue
            threshold = float(threshold)
            previous = state["last_alerted"].get(rule["id"]) or {}
            previous_value = previous.get("last_value")
            if value >= threshold:
                active += 1
                if previous_value is None or previous_value < threshold:
                    entry = {"at": iso(now), "worker_id": rule["worker_id"], "metric": rule["metric"],
                             "value": value, "threshold": threshold,
                             "message": alert_message(rule, value)[1],
                             "notified": _send_alert(ctx, rule, value)}
                    state["history"].append(entry)
                    fired.append(entry)
                    state["last_alerted"][rule["id"]] = {
                        "value": value, "at": iso(now),
                        "last_value": value, "last_seen_at": iso(now)}
                    continue
            state["last_alerted"][rule["id"]] = {
                "value": previous.get("value"), "at": previous.get("at"),
                "last_value": value, "last_seen_at": iso(now)}
        if len(state["history"]) > MAX_HISTORY:
            state["history"] = state["history"][-MAX_HISTORY:]
        save_state(state_dir, state)
        return fired, active


# ------------------------------------------------------------------ routes
def register_monitor_alert_routes(app: Any, ctx: Any, auth: Callable | None = None) -> None:
    """Mount the alert routes on ``app`` (auth like the other routes).

    ``auth`` is ``app.py``'s ``auth(request)`` closure when the caller passes it (preferred:
    exactly the same session/CSRF/re-auth checks as every other route). When it is not
    passed, writes fall back to the same checks re-implemented against ``ctx.store``
    (the ``require_session`` middleware already enforces a live session for every
    ``/api/`` route either way).
    """

    def _write_auth(request: Request) -> None:
        if auth is not None:
            auth(request)
            return
        # Fallback: mirror auth()'s write checks. Imported lazily: this module is imported
        # by app.py, so importing app.py at module load would be circular.
        from .app import client_ip, same_origin

        origin = request.headers.get("origin")
        if origin is not None and not same_origin(request, origin):
            raise HTTPException(403, "yêu cầu từ trang khác")
        token = request.cookies.get(ctx.cookie)
        # The store keeps the digest of the cookie token, not the token itself
        # (app.py:460 stores sec.digest(token)); look it up the same way.
        row = ctx.store.session(sec.digest(token)) if token else None
        if row is None:
            raise HTTPException(401, "cần đăng nhập")
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row.csrf or ""):
            raise HTTPException(403, "thiếu mã CSRF")
        ip, agent = client_ip(request), request.headers.get("user-agent", "")
        if (row.ip and row.ip != ip) or (row.agent and row.agent != agent):
            raise HTTPException(403, "phiên đăng nhập đã đổi địa chỉ IP hoặc trình duyệt; "
                                     "nhập lại mật khẩu và mã 2FA ở trang tài khoản để tiếp tục")

    def _ntfy_configured() -> bool:
        try:
            config = ctx.config()
            return bool(getattr(config, "notify", None) is not None and config.notify.ntfy)
        except Exception:
            return False

    @app.get("/api/monitor/alerts")
    async def list_alerts(request: Request):
        if auth is not None:
            auth(request)
        state = load_state(ctx.state_dir)
        return {"rules": state["rules"], "history": state["history"],
                "ntfy_configured": _ntfy_configured()}

    @app.post("/api/monitor/alerts", status_code=201)
    async def create_alert(request: Request):
        _write_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "nội dung yêu cầu không phải JSON")
        worker_id, metric, threshold = _validate_rule_body(body)
        # In a thread: the 5-minute check holds the alerts lock while it sends ntfy.
        return await asyncio.to_thread(create_rule, ctx.state_dir, worker_id, metric, threshold,
                                       clock=ctx.clock)

    @app.delete("/api/monitor/alerts/{rule_id}")
    async def delete_alert(request: Request, rule_id: str):
        _write_auth(request)
        if not await asyncio.to_thread(delete_rule, ctx.state_dir, rule_id):
            raise HTTPException(404, f"không có rule cảnh báo {rule_id}")
        return {"ok": True, "deleted": rule_id}

    @app.post("/api/monitor/alerts/check")
    async def check_alerts(request: Request):
        _write_auth(request)
        # The shared values helper lives in web/monitor.py (imported lazily: that
        # module imports evaluate_and_notify from this one at its top level).
        from .monitor import metric_values

        values = metric_values(ctx)
        fired, active = await asyncio.to_thread(evaluate_and_notify, ctx, values)
        return {"fired": fired, "active_count": active, "values": values}
