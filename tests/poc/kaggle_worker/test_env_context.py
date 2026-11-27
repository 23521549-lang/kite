"""F4: Environment Context tab (Điểm 9) — description field, codemap build,
AGENTS.md, and the "Kite thấy gì" preview.

Backend only; the frontend reads these routes. The fixtures mirror
``test_goi2_sessions.py`` (same TestClient + FakeOps shape); the context
endpoint itself reads the REAL lesson/experience stores, because the preview
must be exactly what a task's goal would be loaded with — so lessons and
experience docs are seeded as real files, not via FakeOps.
"""

from __future__ import annotations

from pathlib import Path
import json
import stat
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker import codemap as codemap_mod  # noqa: E402
from poc.kaggle_worker import memory as mem  # noqa: E402
from poc.kaggle_worker.cli import main  # noqa: E402
from poc.kaggle_worker.experience_store import experience_path  # noqa: E402
from poc.kaggle_worker.web import codemap_job, env_context  # noqa: E402
from poc.kaggle_worker.web import security as sec  # noqa: E402
from poc.kaggle_worker.web.app import WebContext, create_app  # noqa: E402
from poc.kaggle_worker.web.db import Store, connect, sqlite_url  # noqa: E402

from tests.poc.kaggle_worker.test_goi2_sessions import (  # noqa: E402
    FakeGateway,
    FakeOps,
    git,
    make_env,
    make_repo,
    signed_in,
)
from tests.poc.kaggle_worker.test_launcher import raw_config  # noqa: E402

SECRET = "MFRGGZDFMZTWQ2LK"          # test-only TOTP secret (base32), not a real credential
PASSWORD = "Correct horse battery staple 7"
NOW = 1_795_400_000.0

STUB_GRAPH = {
    "nodes": [
        {"id": "m1", "label": "app.py", "source_file": "app.py",
         "source_location": "app.py:1", "file_type": "code"},
        {"id": "c1", "label": "Shop", "source_file": "app.py",
         "source_location": "app.py:5", "file_type": "code", "_callable_class": True},
        {"id": "f1", "label": ".checkout()", "source_file": "app.py",
         "source_location": "app.py:9", "file_type": "code"},
    ],
    "links": [],
}


def _fake_graphify(tmp_path: Path) -> Path:
    """A stand-in `graphify` binary: writes STUB_GRAPH into --out/graphify-out."""
    script = tmp_path / "fake-graphify"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "out = Path(sys.argv[sys.argv.index('--out') + 1])\n"
        "d = out / 'graphify-out'\n"
        "d.mkdir(parents=True, exist_ok=True)\n"
        f"(d / 'graph.json').write_text(json.dumps({STUB_GRAPH!r}))\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


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
    ops, gateway = FakeOps(tmp_path), FakeGateway()
    ctx = WebContext(config_path, store, sealer, ops_factory=lambda _config: ops, gateway=gateway,
                     clock=lambda: clock["now"], events_every=0.05)
    client = TestClient(create_app(ctx), base_url="https://testserver")
    return {"client": client, "ctx": ctx, "store": store, "ops": ops, "clock": clock,
            "path": tmp_path, "config": config_path}


def _repo_with_agents(tmp_path: Path, *, agents: str | None = "# Quy uoc\nKhong commit truc tiep.\n") -> Path:
    repo = tmp_path / "demo-repo"
    make_repo(repo)
    (repo / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    if agents is not None:
        (repo / "AGENTS.md").write_text(agents, encoding="utf-8")
    git("add", ".", path=repo)
    git("commit", "-qm", "add app", path=repo)
    return repo


def _seed_lesson(state_dir: Path, repo: str, text: str = "- Khong commit truc tiep\n") -> None:
    path = mem.memory_path(state_dir, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_experience(state_dir: Path, repo: str, text: str = "Quy tac: test truoc khi commit.\n") -> None:
    path = experience_path(state_dir, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ migration (Mảnh 1)
def test_migration_fresh_db_has_description(site):
    assert site["store"].migrate() == 2  # migrate() returns the current version
    assert site["store"].set_repo_description("nope", "x") is False
    env = make_env(site, signed_in(site), path=str(site["path"] / "demo-repo"))
    assert site["store"].set_repo_description(env["id"], "mo ta") is True
    rows = {str(r["id"]): r for r in site["store"].repo_rows()}
    assert rows[env["id"]]["description"] == "mo ta"


def test_migration_v1_db_preserves_rows(tmp_path):
    from sqlalchemy import text

    db_path = tmp_path / "web.sqlite3"
    store = Store(connect(sqlite_url(db_path)), clock=lambda: NOW)
    # Build a v1 database by hand: no description column, version row = 1.
    with store.engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_version (version INTEGER NOT NULL)"))
        conn.execute(text("INSERT INTO schema_version VALUES (1)"))
        conn.execute(text(
            "CREATE TABLE repos (id INTEGER PRIMARY KEY, name VARCHAR(100) UNIQUE NOT NULL, "
            "path TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', created_at FLOAT NOT NULL)"))
        conn.execute(text("INSERT INTO repos VALUES (7, 'old', '/r', '', 1.0)"))
    assert store.migrate() == 2
    rows = store.repo_rows()
    assert len(rows) == 1 and rows[0]["name"] == "old" and rows[0]["description"] == ""


# ------------------------------------------------------------------ PATCH description (Mảnh 1)
def test_patch_description_round_trip_and_validation(site):
    headers = signed_in(site)
    repo = _repo_with_agents(site["path"])
    env = make_env(site, headers, path=str(repo))
    env_id = env["id"]
    r = site["client"].patch(f"/api/environments/{env_id}",
                             json={"description": "Repo demo cho shop"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "Repo demo cho shop"
    # Clearing with an empty string.
    r = site["client"].patch(f"/api/environments/{env_id}", json={"description": "  "},
                             headers=headers)
    assert r.status_code == 200 and r.json()["description"] == ""
    # Too long, and the Default environment cannot be described.
    r = site["client"].patch(f"/api/environments/{env_id}", json={"description": "x" * 2001},
                             headers=headers)
    assert r.status_code == 422, r.text
    r = site["client"].patch("/api/environments/default", json={"description": "x"}, headers=headers)
    assert r.status_code == 409, r.text
    r = site["client"].patch("/api/environments/nope", json={"description": "x"}, headers=headers)
    assert r.status_code == 404, r.text


# ------------------------------------------------------------------ GET context (Mảnh 5)
def test_get_context_full(site):
    headers = signed_in(site)
    repo = _repo_with_agents(site["path"])
    _seed_lesson(site["path"], str(repo))
    env = make_env(site, headers, path=str(repo))
    site["client"].patch(f"/api/environments/{env['id']}",
                         json={"description": "Repo demo cho shop"}, headers=headers)
    r = site["client"].get(f"/api/environments/{env['id']}/context", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["env_id"] == env["id"]
    assert data["description"] == "Repo demo cho shop"
    assert data["agents_md"]["present"] is True
    assert "Quy uoc" in data["agents_md"]["content"]
    assert data["agents_md"]["truncated"] is False
    # No graphify in this venv: status is not_built (or unavailable on a
    # machine where the extra is absent) — never "ready" without a graph.
    assert data["codemap"]["status"] in ("not_built", "unavailable"), data["codemap"]
    assert data["codemap"]["graphify_available"] is (codemap_mod.graphify_cli() is not None)
    # The preview is what the agent gets: description + lessons fallback.
    assert "Repo demo cho shop" in data["preview"]
    assert "Khong commit truc tiep" in data["preview"]
    assert "AGENTS.md" in data["preview"]


def test_get_context_empty_repo(site):
    headers = signed_in(site)
    repo = _repo_with_agents(site["path"], agents=None)
    env = make_env(site, headers, path=str(repo))
    r = site["client"].get(f"/api/environments/{env['id']}/context", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["agents_md"]["present"] is False
    assert "(chưa có mô tả" in data["preview"]
    assert "(chưa có bài học nào" in data["preview"]


def test_get_context_experience_beats_lessons(site):
    headers = signed_in(site)
    repo = _repo_with_agents(site["path"])
    _seed_lesson(site["path"], str(repo), "- Bai hoc cu\n")
    _seed_experience(site["path"], str(repo), "Quy tac: test truoc khi commit.\n")
    env = make_env(site, headers, path=str(repo))
    r = site["client"].get(f"/api/environments/{env['id']}/context", headers=headers)
    assert r.status_code == 200, r.text
    assert "Quy tac: test truoc khi commit." in r.json()["preview"]
    assert "Bai hoc cu" not in r.json()["preview"]  # experience doc replaces raw lessons


def test_get_context_default_has_no_codemap(site):
    headers = signed_in(site)
    r = site["client"].get("/api/environments/default/context", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["codemap"]["status"] == "none"
    r = site["client"].get("/api/environments/nope/context", headers=headers)
    assert r.status_code == 404, r.text


def test_get_context_codemap_ready_from_stub_graph(site):
    headers = signed_in(site)
    repo = _repo_with_agents(site["path"])
    env = make_env(site, headers, path=str(repo))
    env_id = str(env["id"])
    out_dir = env_context.codemap_dir(site["path"], env_id)
    graph_dir = out_dir / "graphify-out"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.json").write_text(json.dumps(STUB_GRAPH), encoding="utf-8")
    env_context.write_codemap_meta(site["path"], env_id,
                                   {"status": "ready", "built_at": 1_795_400_000.0, "error": None})
    r = site["client"].get(f"/api/environments/{env_id}/context", headers=headers)
    assert r.status_code == 200, r.text
    codemap = r.json()["codemap"]
    assert codemap["status"] == "ready"
    assert codemap["files"] == 1
    assert codemap["symbols"]["total"] == 3
    assert codemap["symbols"]["classes"] == 1
    assert codemap["symbols"]["methods"] == 1
    assert codemap["top_files"][0]["path"] == "app.py"


# ------------------------------------------------------------------ env_context helpers (Mảnh 2)
def test_codemap_status_rejects_escaping_env_id(tmp_path):
    with pytest.raises(ValueError):
        env_context.codemap_dir(tmp_path, "../escape")


def test_read_agents_md_truncates(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("x" * (env_context.AGENTS_MD_MAX_CHARS + 10), encoding="utf-8")
    got = env_context.read_agents_md(repo)
    assert got["present"] is True and got["truncated"] is True
    assert got["chars"] == env_context.AGENTS_MD_MAX_CHARS + 10  # chars = full file length
    assert len(got["content"]) == env_context.AGENTS_MD_MAX_CHARS  # content is capped
    assert env_context.read_agents_md(tmp_path / "nope")["present"] is False


# ------------------------------------------------------------------ codemap rebuild (Mảnh 3)
def test_request_rebuild_refuses_without_graphify(tmp_path, monkeypatch):
    monkeypatch.setattr(codemap_mod, "graphify_cli", lambda: None)
    with pytest.raises(ValueError, match="chưa cài graphify"):
        codemap_job.request_rebuild(tmp_path, "1", "/repo")
    with pytest.raises(ValueError, match="Default"):
        codemap_job.request_rebuild(tmp_path, "default", None)


def test_run_rebuild_with_stub_cli(site):
    headers = signed_in(site)
    repo = _repo_with_agents(site["path"])
    env = make_env(site, headers, path=str(repo))
    env_id = str(env["id"])
    fake = _fake_graphify(site["path"])
    result = codemap_job.run_rebuild(site["path"], env_id, cli=[str(fake)])
    assert result["status"] == "ready", result
    assert (env_context.codemap_dir(site["path"], env_id) / "graphify-out" / "graph.json").is_file()
    assert env_context.read_codemap_meta(site["path"], env_id)["status"] == "ready"
    r = site["client"].get(f"/api/environments/{env_id}/context", headers=headers)
    assert r.json()["codemap"]["symbols"]["total"] == 3


def test_run_rebuild_failure_keeps_old_graph(site, monkeypatch):
    headers = signed_in(site)
    repo = _repo_with_agents(site["path"])
    env = make_env(site, headers, path=str(repo))
    env_id = str(env["id"])
    fake = _fake_graphify(site["path"])
    codemap_job.run_rebuild(site["path"], env_id, cli=[str(fake)])
    old_graph = (env_context.codemap_dir(site["path"], env_id) / "graphify-out" / "graph.json")
    old_text = old_graph.read_text(encoding="utf-8")
    # A CLI that exits non-zero: build returns None, meta becomes failed,
    # and the previous graph.json survives untouched.
    monkeypatch.setattr(codemap_mod, "build", lambda *a, **k: None)
    result = codemap_job.run_rebuild(site["path"], env_id)
    assert result["status"] == "failed", result
    assert old_graph.read_text(encoding="utf-8") == old_text
    assert env_context.read_codemap_meta(site["path"], env_id)["status"] == "failed"


def test_rebuild_endpoint_without_graphify_is_503(site, monkeypatch):
    monkeypatch.setattr(codemap_mod, "graphify_cli", lambda: None)
    headers = signed_in(site)
    repo = _repo_with_agents(site["path"])
    env = make_env(site, headers, path=str(repo))
    r = site["client"].post(f"/api/environments/{env['id']}/codemap/rebuild", headers=headers)
    assert r.status_code == 503, r.text
    r = site["client"].post("/api/environments/default/codemap/rebuild", headers=headers)
    assert r.status_code == 409, r.text


# ------------------------------------------------------------------ CLI (Mảnh 4)
def test_cli_codemap_build_with_stub_graphify(site, monkeypatch, capsys):
    repo = _repo_with_agents(site["path"])
    env = make_env(site, signed_in(site), path=str(repo))
    fake = _fake_graphify(site["path"])
    monkeypatch.setattr(codemap_mod, "graphify_cli", lambda: [str(fake)])
    code = main(["--config", str(site["config"]), "codemap", "build", str(env["id"])])
    assert code == 0, capsys.readouterr()
    assert env_context.read_codemap_meta(site["path"], str(env["id"]))["status"] == "ready"


def test_cli_codemap_build_without_graphify_exits_1(site, monkeypatch, capsys):
    repo = _repo_with_agents(site["path"])
    env = make_env(site, signed_in(site), path=str(repo))
    monkeypatch.setattr(codemap_mod, "graphify_cli", lambda: None)
    code = main(["--config", str(site["config"]), "codemap", "build", str(env["id"])])
    assert code == 1
    assert "chưa cài graphify" in capsys.readouterr().err
