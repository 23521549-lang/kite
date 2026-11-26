"""Gói 5 (worker b): Connect API — the MCP + skill endpoint families, the enable gate,
sealed remote auth. The skill family mirrors MCP exactly (same flow the frontend uses
for both); a skill scan with an unknown scanner fails closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker import connect as engine  # noqa: E402
from poc.kaggle_worker.web import security as sec  # noqa: E402
from poc.kaggle_worker.web.app import WebContext, create_app  # noqa: E402
from poc.kaggle_worker.web.db import Store, connect, sqlite_url  # noqa: E402

from tests.poc.kaggle_worker.test_launcher import raw_config  # noqa: E402

SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "kham-pha-khoa-hoc"
NOW = 1_795_400_000.0


@pytest.fixture
def site(tmp_path):
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("w" * 32 + "\n")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw))
    clock = {"now": NOW}
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")), clock=lambda: clock["now"])
    store.migrate()
    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    store.create_user("han", sec.hash_password(PASSWORD), sealer.seal(SECRET))
    ctx = WebContext(config_path, store, sealer, ops_factory=lambda _config: object(),
                     gateway=lambda *a: {}, clock=lambda: clock["now"], events_every=0.05)
    client = TestClient(create_app(ctx), base_url="https://testserver")
    return {"client": client, "ctx": ctx, "store": store, "clock": clock,
            "path": tmp_path, "config": config_path}


def _code(clock, offset=0):
    return sec.totp_code(SECRET, int(clock["now"] // 30) + offset)


def _signed_in(site):
    response = site["client"].post(
        "/api/auth/login",
        json={"username": "han", "password": PASSWORD, "code": _code(site["clock"])})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


def _fake_pass_scan(state_dir, tool_id, collection="mcp"):
    """A stand-in for engine.scan_tool: records a passing report like the real one."""
    registry = engine.load_registry(state_dir)
    tool = engine.find_tool(registry, collection, tool_id)
    report = engine.ScanReport(verdict="pass", findings=[], scanner="fake",
                               scanner_version="0.1", scanned_at=engine.utcnow_iso(NOW))
    tool["last_scan"] = report.to_dict()
    engine.save_registry(state_dir, registry)
    return report


def test_connect_routes_need_a_session(site):
    assert site["client"].get("/api/connect/inventory").status_code == 401
    assert site["client"].post("/api/connect/mcp", json={}).status_code == 401


def test_create_mcp_rejects_unpinned_package(site):
    headers = _signed_in(site)
    response = site["client"].post(
        "/api/connect/mcp",
        json={"name": "x", "kind": "package",
              "config": {"ecosystem": "npm", "package": "some-mcp", "version": "latest",
                         "registry": engine.OFFICIAL_NPM_REGISTRY}},
        headers=headers)
    assert response.status_code == 400
    assert "latest" in response.text or "ghim" in response.text


def test_create_remote_mcp_seals_auth_and_never_returns_it(site):
    headers = _signed_in(site)
    response = site["client"].post(
        "/api/connect/mcp",
        json={"name": "remote", "kind": "remote",
              "config": {"url": "https://mcp.example.com/mcp"},
              "auth_secret": "tok-SECRET-VALUE"},
        headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "tok-SECRET-VALUE" not in json.dumps(body)
    assert body["has_auth"] is True
    assert body["config"].get("url") == "https://mcp.example.com/mcp"
    secret_file = engine.secret_path(site["path"], body["id"])
    assert secret_file.exists()
    assert "tok-SECRET-VALUE" not in secret_file.read_text(encoding="utf-8")
    inventory = site["client"].get("/api/connect/inventory", headers=headers).json()
    assert "tok-SECRET-VALUE" not in json.dumps(inventory)


def test_mcp_scan_enable_disable_delete_flow(site, monkeypatch):
    headers = _signed_in(site)
    created = site["client"].post(
        "/api/connect/mcp",
        json={"name": "local", "kind": "source",
              "config": {"path": str(site["path"]), "command": ["python3", "server.py"]}},
        headers=headers).json()
    tool_id = created["id"]
    assert created["enabled"] is False
    # Enable before any passing scan is refused.
    assert site["client"].post(f"/api/connect/mcp/{tool_id}/enable",
                               headers=headers).status_code == 400
    assert site["client"].get(f"/api/connect/mcp/{tool_id}/scan",
                              headers=headers).status_code == 404
    monkeypatch.setattr(engine, "scan_tool", _fake_pass_scan)
    scanned = site["client"].post(f"/api/connect/mcp/{tool_id}/scan", headers=headers)
    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["verdict"] == "pass"
    enabled = site["client"].post(f"/api/connect/mcp/{tool_id}/enable", headers=headers)
    assert enabled.status_code == 200 and enabled.json()["enabled"] is True
    monkeypatch.setattr(engine, "list_tools_stdio",
                        lambda command: [{"name": "add", "description": ""}])
    tested = site["client"].post(f"/api/connect/mcp/{tool_id}/test", headers=headers)
    assert tested.json() == {"ok": True, "tools": ["add"]}
    disabled = site["client"].post(f"/api/connect/mcp/{tool_id}/disable", headers=headers)
    assert disabled.json()["enabled"] is False
    deleted = site["client"].delete(f"/api/connect/mcp/{tool_id}", headers=headers)
    assert deleted.json() == {"deleted": tool_id}
    assert site["client"].get("/api/connect/inventory", headers=headers).json()["mcp"] == []


def test_skill_family_mirrors_mcp_scan_enable_delete(site):
    headers = _signed_in(site)
    skill_dir = site["path"] / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# skill\n")
    created = site["client"].post(
        "/api/connect/skills",
        json={"name": "my-skill", "path": str(skill_dir),
              # Explicit unknown scanner: the fail-closed path must not depend
              # on whether the real scanner binaries are installed here.
              "scanner": "no-such-scanner"}, headers=headers)
    assert created.status_code == 200, created.text
    skill_id = created.json()["id"]
    # The real scan runs and fails closed with a report, it does not crash.
    scanned = site["client"].post(f"/api/connect/skills/{skill_id}/scan", headers=headers)
    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["verdict"] == "fail"
    assert scanned.json()["findings"][0]["rule"] == "skill-scanner-missing"
    assert site["client"].post(f"/api/connect/skills/{skill_id}/enable",
                               headers=headers).status_code == 400
    latest = site["client"].get(f"/api/connect/skills/{skill_id}/scan", headers=headers)
    assert latest.json()["verdict"] == "fail"
    tested = site["client"].post(f"/api/connect/skills/{skill_id}/test", headers=headers)
    assert tested.json()["ok"] is False
    disabled = site["client"].post(f"/api/connect/skills/{skill_id}/disable",
                                   headers=headers)
    assert disabled.status_code == 200
    deleted = site["client"].delete(f"/api/connect/skills/{skill_id}", headers=headers)
    assert deleted.json() == {"deleted": skill_id}


def test_create_skill_rejects_missing_path(site):
    headers = _signed_in(site)
    response = site["client"].post(
        "/api/connect/skills",
        json={"name": "ghost", "path": str(site["path"] / "no-such-dir")},
        headers=headers)
    assert response.status_code == 400


def test_inventory_lists_platforms_with_status(site):
    headers = _signed_in(site)
    inventory = site["client"].get("/api/connect/inventory", headers=headers).json()
    assert set(inventory) == {"mcp", "skills", "platforms"}
    by_id = {p["id"]: p for p in inventory["platforms"]}
    assert set(by_id) >= {"github", "ntfy", "drive", "docs"}
    assert by_id["github"]["status"] == "not_configured"
    platforms = site["client"].get("/api/connect/platforms", headers=headers).json()
    assert [p["id"] for p in platforms] == [p["id"] for p in inventory["platforms"]]


def test_delete_remote_mcp_removes_its_sealed_secret(site):
    headers = _signed_in(site)
    created = site["client"].post(
        "/api/connect/mcp",
        json={"name": "remote", "kind": "remote",
              "config": {"url": "https://mcp.example.com/mcp"},
              "auth_secret": "tok-SECRET-VALUE"},
        headers=headers).json()
    secret_file = engine.secret_path(site["path"], created["id"])
    assert secret_file.exists()
    site["client"].delete(f"/api/connect/mcp/{created['id']}", headers=headers)
    assert not secret_file.exists()


def test_skill_created_without_scanner_uses_pair_default(site):
    headers = _signed_in(site)
    skill_dir = site["path"] / "skill-default"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# skill\n")
    created = site["client"].post(
        "/api/connect/skills",
        json={"name": "my-skill", "path": str(skill_dir)}, headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["config"]["scanner"] == engine.DEFAULT_SKILL_SCANNER
