"""Connect API (Gói 5): the owner's tool store — MCP servers, skills, platforms.

The skill endpoints mirror the MCP family exactly (``POST /api/connect/skills``,
``POST /api/connect/skills/{id}/scan``, ``POST .../enable|disable``,
``DELETE /api/connect/skills/{id}``, ``GET /api/connect/skills/{id}/scan``) so the
frontend uses one flow for both: add -> scan -> read the report -> enable. A skill
scan fails closed until worker (a)'s evaluation registers a skill-scanner adapter.

Nothing secret ever leaves here: a remote MCP's Authorization token is sealed into
its own file with ``write_secret`` and only a ``has_auth`` flag reaches the browser.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import connect as engine
from .workers import read_secret, write_secret

COLLECTIONS = ("mcp", "skills")

PLATFORM_CATALOGUE = [
    {"id": "github", "name": "GitHub", "kind": "platform"},
    {"id": "ntfy", "name": "ntfy", "kind": "platform"},
    {"id": "drive", "name": "Google Drive", "kind": "platform"},
    {"id": "docs", "name": "Google Docs", "kind": "platform"},
]


class McpCreateIn(BaseModel):
    name: str = Field(max_length=80)
    kind: str = Field(max_length=20)          # local | source | package | remote
    config: dict = Field(default_factory=dict)
    auth_secret: str | None = Field(default=None, max_length=1000)


class SkillCreateIn(BaseModel):
    name: str = Field(max_length=80)
    path: str = Field(max_length=500)
    scanner: str | None = Field(default=None, max_length=80)


def build_router(ctx, auth) -> APIRouter:
    """Routes for one WebContext; ``auth`` is create_app's session/CSRF check."""
    router = APIRouter(prefix="/api/connect")

    def audit(session, action: str, target: str, detail: dict | None = None) -> None:
        ctx.store.audit(session.user.username, session.ip, action, target, True, detail or {})

    def load():
        return engine.load_registry(ctx.state_dir)

    def save(registry: dict) -> None:
        engine.save_registry(ctx.state_dir, registry)

    def get_tool(collection: str, tool_id: str) -> tuple[dict, dict]:
        registry = load()
        tool = engine.find_tool(registry, collection, tool_id)
        if tool is None:
            raise HTTPException(404, f"không có tool {tool_id}")
        return registry, tool

    def public_list(collection: str) -> list[dict]:
        return [engine.public_tool(t) for t in load().get(collection, [])]

    # ---------------------------------------------------------------- inventory
    @router.get("/inventory")
    async def inventory(request: Request):
        auth(request)
        registry = load()
        platforms = []
        for entry in PLATFORM_CATALOGUE:
            item = dict(entry)
            stored = next((p for p in registry.get("platforms", [])
                           if p.get("id") == entry["id"]), None)
            if stored:
                item.update({k: v for k, v in stored.items() if k != "id"})
            item["status"] = item.get("status") or _platform_status(ctx, entry["id"])
            platforms.append(item)
        return {"mcp": public_list("mcp"), "skills": public_list("skills"),
                "platforms": platforms}

    @router.get("/platforms")
    async def platforms(request: Request):
        return (await inventory(request))["platforms"]

    # ---------------------------------------------------------------- add
    @router.post("/mcp")
    async def add_mcp(body: McpCreateIn, request: Request):
        session = auth(request)
        name, kind, config = body.name.strip(), body.kind, dict(body.config)
        if not name:
            raise HTTPException(400, "cần tên cho MCP")
        if kind in ("local", "source"):
            command = config.get("command")
            if not command or not all(isinstance(p, str) and p for p in command):
                raise HTTPException(400, "MCP local cần config.command là danh sách lệnh")
        elif kind == "package":
            ecosystem = config.get("ecosystem", "npm")
            # The UI form never asks for a registry (limit 2: official only) —
            # a missing one defaults to the official registry of the ecosystem
            # instead of failing. An explicitly different registry still fails
            # in validate_package_spec below.
            registry = config.get("registry") or (
                engine.OFFICIAL_NPM_REGISTRY if ecosystem == "npm"
                else engine.OFFICIAL_PYPI_REGISTRY)
            config["registry"] = registry
            engine.validate_package_spec(ecosystem,
                                         config.get("package", ""), config.get("version", ""),
                                         registry)
        elif kind == "remote":
            url = config.get("url", "")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise HTTPException(400, "remote MCP phải dùng https://")
        else:
            raise HTTPException(400, f"kind MCP không hỗ trợ: {kind!r}")
        tool_id = engine.new_tool_id()
        tool = {"id": tool_id, "kind": kind, "name": name, "enabled": False,
                "config": config, "lock": None, "last_scan": None}
        if body.auth_secret:
            if kind != "remote":
                raise HTTPException(400, "auth_secret chỉ dùng cho remote MCP")
            write_secret(engine.secret_path(ctx.state_dir, tool_id), body.auth_secret,
                         sealer=ctx.sealer)
            tool["has_auth"] = True
        registry = load()
        registry["mcp"].append(tool)
        save(registry)
        audit(session, "connect.mcp.create", tool_id, {"kind": kind, "name": name})
        return engine.public_tool(tool)

    @router.post("/skills")
    async def add_skill(body: SkillCreateIn, request: Request):
        session = auth(request)
        name, path = body.name.strip(), body.path.strip()
        if not name:
            raise HTTPException(400, "cần tên cho skill")
        if not path or not Path(path).is_dir():
            raise HTTPException(400, f"skill path không phải thư mục: {path!r}")
        tool = {"id": engine.new_tool_id(), "kind": "skill", "name": name, "enabled": False,
                "config": {"path": path,
                           # Default: the evaluated pair gate (skillshield+argus).
                           # An explicit scanner stays explicit (tests use an unknown
                           # name to exercise the fail-closed path).
                           "scanner": body.scanner or engine.DEFAULT_SKILL_SCANNER},
                "lock": None,
                "last_scan": None}
        registry = load()
        registry["skills"].append(tool)
        save(registry)
        audit(session, "connect.skill.create", tool["id"], {"name": name})
        return engine.public_tool(tool)

    # ---------------------------------------------------------------- scan / test / enable / delete
    # One implementation serves both families; the frontend uses the same flow for
    # MCP and skills (add -> scan -> report -> enable).

    def _scan_routes(collection: str) -> None:
        singular = "mcp" if collection == "mcp" else "skill"

        @router.post(f"/{collection}/{{tool_id}}/scan")
        async def scan(tool_id: str, request: Request):
            session = auth(request)
            _registry, _tool = get_tool(collection, tool_id)
            report = await asyncio.to_thread(engine.scan_tool, ctx.state_dir, tool_id,
                                             collection=collection)
            audit(session, f"connect.{singular}.scan", tool_id,
                  {"verdict": report.verdict})
            return report.to_dict()

        @router.get(f"/{collection}/{{tool_id}}/scan")
        async def latest(tool_id: str, request: Request):
            auth(request)
            report = engine.latest_scan(ctx.state_dir, tool_id, collection=collection)
            if report is None:
                raise HTTPException(404, "tool này chưa quét lần nào")
            return report

        @router.post(f"/{collection}/{{tool_id}}/test")
        async def test(tool_id: str, request: Request):
            session = auth(request)
            _registry, tool = get_tool(collection, tool_id)
            try:
                tools = await asyncio.to_thread(_probe_tools, ctx, tool)
            except engine.McpClientError as exc:
                audit(session, f"connect.{singular}.test", tool_id, {"ok": False})
                return {"ok": False, "error": str(exc)}
            audit(session, f"connect.{singular}.test", tool_id, {"ok": True})
            return {"ok": True, "tools": [t["name"] for t in tools]}

        @router.post(f"/{collection}/{{tool_id}}/enable")
        async def enable(tool_id: str, request: Request):
            session = auth(request)
            registry, tool = get_tool(collection, tool_id)
            ok, reason = await asyncio.to_thread(engine.can_enable, tool)
            if not ok:
                raise HTTPException(400, f"không bật được: {reason}")
            tool["enabled"] = True
            save(registry)
            audit(session, f"connect.{singular}.enable", tool_id, {})
            return engine.public_tool(tool)

        @router.post(f"/{collection}/{{tool_id}}/disable")
        async def disable(tool_id: str, request: Request):
            session = auth(request)
            registry, tool = get_tool(collection, tool_id)
            tool["enabled"] = False
            save(registry)
            audit(session, f"connect.{singular}.disable", tool_id, {})
            return engine.public_tool(tool)

        @router.delete(f"/{collection}/{{tool_id}}")
        async def delete(tool_id: str, request: Request):
            session = auth(request)
            registry, tool = get_tool(collection, tool_id)
            registry[collection] = [t for t in registry[collection] if t["id"] != tool_id]
            save(registry)
            secret = engine.secret_path(ctx.state_dir, tool_id)
            try:
                secret.unlink()
            except OSError:
                pass
            audit(session, f"connect.{singular}.delete", tool_id, {})
            return {"deleted": tool_id}

    _scan_routes("mcp")
    _scan_routes("skills")
    return router


def _platform_status(ctx, platform_id: str) -> str:
    if platform_id == "github":
        secrets = ctx.state_dir / "secrets"
        return ("configured" if secrets.is_dir() and
                any(p.name.startswith("github_pat_") for p in secrets.iterdir())
                else "not_configured")
    if platform_id == "ntfy":
        try:
            return "configured" if ctx.config().notify.ntfy else "not_configured"
        except Exception:
            return "not_configured"
    return "not_configured"


def _probe_tools(ctx, tool: dict) -> list[dict]:
    """The connection check: local/source over stdio, remote over HTTP, package
    inside the scan container (download verified first, offline, scripts off)."""
    config = tool.get("config", {}) or {}
    kind = tool.get("kind")
    if kind in ("local", "source"):
        return engine.list_tools_stdio(list(config["command"]))
    if kind == "remote":
        headers = {}
        secret_file = engine.secret_path(ctx.state_dir, tool["id"])
        if secret_file.exists():
            headers["Authorization"] = ("Bearer " + read_secret(secret_file,
                                                               sealer=ctx.sealer))
        return engine.list_tools_remote(config["url"], headers)
    if kind == "package":
        return _probe_package_tools(ctx, tool, config)
    if kind == "skill":
        raise engine.McpClientError("skill không có tools/list — dùng scan thay cho test")
    raise engine.McpClientError(f"kind không hỗ trợ: {kind!r}")


def _probe_package_tools(ctx, tool: dict, config: dict) -> list[dict]:
    from ..sandbox import SandboxSettings

    ecosystem = config.get("ecosystem", "npm")
    name, version = config.get("package", ""), config.get("version", "")
    if ecosystem == "npm":
        meta = engine.npm_package_metadata(name, version)
        data = engine.download_package_file(meta["tarball_url"],
                                            expected_sha512=meta["integrity_sha512"])
        filename = "package.tgz"
    else:
        meta = engine.pypi_package_metadata(name, version)
        data = engine.download_package_file(meta["file_url"],
                                            expected_sha256=meta["integrity_sha256"])
        filename = ("package.whl" if meta.get("packagetype") == "bdist_wheel"
                    else "package.tar.gz")
    import tempfile

    server_command = config.get("server_command") or engine.default_server_command(
        ecosystem, name, version)
    with tempfile.TemporaryDirectory(prefix="kite-pkg-") as pkg_dir:
        (Path(pkg_dir) / filename).write_bytes(data)
        return engine.list_tools_in_container(
            SandboxSettings(), pkg_dir=Path(pkg_dir), pkg_filename=filename,
            ecosystem=ecosystem, server_command=server_command)
