"""HTTP API for plan schedules (Gói 4).

Mounted by the agent trưởng into ``create_app`` (``web/app.py``) as::

    from .schedules_api import create_router as create_schedules_router
    app.include_router(create_schedules_router(ctx, auth, audit))

``ctx`` is the ``WebContext`` of ``app.py`` (the schedules live in
``ctx.state_dir / "scheduler.sqlite3"``, the same file as the task store —
see ``cli._db_path``); ``auth`` is the ``auth(request)`` closure of
``create_app`` (raises 401/403); ``audit`` is its ``audit(session, action,
target, ok, detail)`` closure. Validation failures raise ``ValueError``,
which ``create_app`` already maps to HTTP 400.

No endpoint runs a plan immediately: plans only fire from the engine loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..schedules import Schedule, ScheduleStore
from ..store import StoreError

_UPDATE_FIELDS = {"plan_path", "kind", "run_at", "weekdays", "status"}


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "body phải là JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "body phải là một object JSON")
    return body


def _as_str(body: dict, field: str) -> str:
    value = body[field]
    if not isinstance(value, str):
        raise HTTPException(400, f"trường {field} phải là chuỗi")
    return value


def _parse_weekdays(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        type(day) is not int for day in value
    ):
        raise ValueError("weekdays phải là danh sách số nguyên 1 (Thứ 2) .. 7 (CN)")
    return tuple(value)


def _schedule_or_404(store: ScheduleStore, schedule_id: str) -> Schedule:
    schedule = store.get(schedule_id)
    if schedule is None:
        raise HTTPException(404, f"không có lịch {schedule_id}")
    return schedule


def create_router(
    ctx: Any,
    auth: Callable[[Request], Any],
    audit: Callable[..., None],
    *,
    store: ScheduleStore | None = None,
) -> APIRouter:
    """Build the schedules router.

    ``store`` is injectable for tests; in production it opens
    ``ctx.state_dir / "scheduler.sqlite3"``.
    """
    owned_store = store if store is not None else ScheduleStore(
        Path(ctx.state_dir) / "scheduler.sqlite3",
    )
    router = APIRouter()

    @router.get("/api/schedules")
    async def list_schedules(request: Request):
        auth(request)
        try:
            # Wrapped like the other list endpoints ({tasks: [...]}, {reports: [...]}):
            # the Workspace page reads ``data.schedules``.
            return {"schedules": [schedule.to_dict() for schedule in owned_store.list()]}
        except StoreError as exc:
            raise HTTPException(500, str(exc)) from exc

    @router.post("/api/schedules")
    async def create_schedule(request: Request):
        session = auth(request)
        body = await _body(request)
        for field in ("plan_path", "kind", "run_at"):
            if field not in body:
                raise HTTPException(400, f"thiếu trường {field}")
        schedule = owned_store.create(
            plan_path=_as_str(body, "plan_path"),
            kind=_as_str(body, "kind"),
            run_at=_as_str(body, "run_at"),
            weekdays=_parse_weekdays(body.get("weekdays")),
        )
        audit(session, "schedule_create", schedule.id)
        return JSONResponse(status_code=201, content=schedule.to_dict())

    @router.get("/api/schedules/{schedule_id}")
    async def get_schedule(schedule_id: str, request: Request):
        auth(request)
        return _schedule_or_404(owned_store, schedule_id).to_dict()

    @router.patch("/api/schedules/{schedule_id}")
    async def update_schedule(schedule_id: str, request: Request):
        session = auth(request)
        body = await _body(request)
        unknown = set(body) - _UPDATE_FIELDS
        if unknown:
            raise HTTPException(400, f"không sửa được trường: {', '.join(sorted(unknown))}")
        if "status" in body and body["status"] not in ("enabled", "disabled"):
            raise HTTPException(400, "status chỉ nhận 'enabled' hoặc 'disabled'")
        _schedule_or_404(owned_store, schedule_id)
        changes: dict[str, Any] = {}
        if "plan_path" in body:
            changes["plan_path"] = _as_str(body, "plan_path")
        if "kind" in body:
            changes["kind"] = _as_str(body, "kind")
        if "run_at" in body:
            changes["run_at"] = _as_str(body, "run_at")
        if "weekdays" in body:
            changes["weekdays"] = _parse_weekdays(body["weekdays"])
        if "status" in body:
            changes["status"] = _as_str(body, "status")
        updated = owned_store.update(schedule_id, **changes)
        audit(session, "schedule_update", schedule_id)
        return updated.to_dict()

    @router.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, request: Request):
        session = auth(request)
        _schedule_or_404(owned_store, schedule_id)
        owned_store.delete(schedule_id)
        audit(session, "schedule_delete", schedule_id)
        return {"deleted": True, "id": schedule_id}

    return router


__all__ = ["create_router"]
