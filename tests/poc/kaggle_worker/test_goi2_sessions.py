"""Gói 2 (worker (a)): chat sessions per environment, environments (register + clone),
and the origin stamp on task detail. Backend only; the frontend reads these routes."""

from __future__ import annotations

from pathlib import Path
import json
import os
import signal
import subprocess
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker.mcp_server import ApplyConflictError, KiteToolError  # noqa: E402
from poc.kaggle_worker.protocol import ScheduledTask  # noqa: E402
from poc.kaggle_worker.store import SQLiteSchedulerStore  # noqa: E402
from poc.kaggle_worker.web import security as sec  # noqa: E402
from poc.kaggle_worker.web.app import WebContext, create_app  # noqa: E402
from poc.kaggle_worker.web.db import Store, connect, sqlite_url  # noqa: E402

from tests.poc.kaggle_worker.test_launcher import raw_config  # noqa: E402

SECRET = "MFRGGZDFMZTWQ2LK"          # test-only TOTP secret (base32), not a real credential
PASSWORD = "Correct horse battery staple 7"
NOW = 1_795_400_000.0


def git(*args, path: Path | None = None, check: bool = True):
    return subprocess.run(["git", *(["-C", str(path)] if path else []), *args],
                          capture_output=True, text=True, check=check)


def make_repo(path: Path, *, branch: str | None = None) -> dict[str, str]:
    """A sample repo with a commit on main and, optionally, a second branch."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git("config", "user.email", "t@t", path=path)
    git("config", "user.name", "t", path=path)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    git("add", ".", path=path)
    git("commit", "-qm", "init", path=path)
    heads = {"main": git("rev-parse", "HEAD", path=path).stdout.strip()}
    if branch:
        git("checkout", "-qb", branch, path=path)
        (path / "feat.txt").write_text("f\n", encoding="utf-8")
        git("add", ".", path=path)
        git("commit", "-qm", "feat", path=path)
        heads[branch] = git("rev-parse", "HEAD", path=path).stdout.strip()
        git("checkout", "-q", "main", path=path)
    return heads


class FakeOps:
    """KiteOps as the routes use it, plus a real scheduler store for the registry."""

    def __init__(self, tmp_path: Path):
        self.calls: list[tuple] = []
        self.allowed_roots = [tmp_path]
        self._db_path = tmp_path / "scheduler.sqlite3"

    def _store(self):
        return SQLiteSchedulerStore(self._db_path)

    async def logged(self, tool, method, *args, **kwargs):
        self.calls.append(("logged", tool))
        return await method(*args, **kwargs)

    async def list_tasks(self, limit=10):
        return []

    async def task_log(self, task_id="", limit=80):
        return {"task_id": task_id, "progress": []}

    async def task_status(self, task_id=""):
        return {"task_id": task_id, "workflow": "COMPLETED"}

    async def task_result(self, task_id=""):
        return {"task_id": task_id, "ready": True}

    async def rerun_task(self, task_id):
        self.calls.append(("rerun_task", task_id))
        return {"task_id": "task-new", "reran_from": task_id}

    async def apply_result(self, task_id="", confirmation_code=""):
        raise ApplyConflictError(task_id, "deadbeef", ["a.py"], "patch failed: a.py")

    async def repo_status(self, repo):
        if "outside" in repo:
            raise KiteToolError(f"{repo} nằm ngoài các thư mục được phép")
        path = Path(repo)
        if (path / ".git").is_dir():
            status = git("status", "--short", path=path, check=False).stdout.strip()
            return {"repo": str(path), "branch": git("rev-parse", "--abbrev-ref", "HEAD", path=path).stdout.strip(),
                    "head": git("rev-parse", "HEAD", path=path).stdout.strip(),
                    "changes": status or "(không có thay đổi)", "diff_stat": "",
                    "recent_commits": git("log", "-8", "--oneline", path=path).stdout.strip(),
                    "kite_yml": None}
        return {"repo": repo, "clean": True}

    async def memory_show(self, repo=""):
        return {"chat": [], "repo": repo, "lessons": ["1. test lesson"] if repo else []}


class FakeGateway:
    async def __call__(self, method, path, body):
        return {"ok": True}


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


def signed_in(site):
    code = sec.totp_code(SECRET, int(site["clock"]["now"] // 30))
    response = site["client"].post("/api/auth/login",
                                   json={"username": "han", "password": PASSWORD, "code": code})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


def make_env(site, headers, name="demo", path=None, **extra):
    body = {"name": name, "path": str(path) if path else ""}
    body.update(extra)
    response = site["client"].post("/api/environments", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def make_session(site, headers, **body):
    response = site["client"].post("/api/chat/sessions", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ store registry
def test_registry_create_read_move_archive_keeps_row(tmp_path):
    store = SQLiteSchedulerStore(tmp_path / "s.sqlite3")
    try:
        row = store.create_chat_session("abc123", None, "default", "hello", kind="chat")
        assert row["status"] == "active" and row["kind"] == "chat" and row["environment_id"] == "default"
        assert row["opencode_session_id"] is None
        move = store.record_session_move("abc123", "default", "repo-x", "picker", note="t")
        assert move["trigger"] == "picker" and move["from_env"] == "default" and move["to_env"] == "repo-x"
        assert store.get_chat_session("abc123")["environment_id"] == "repo-x"
        assert len(store.list_session_moves("abc123")) == 1
        assert len(store.list_chat_sessions(environment_id="repo-x")) == 1
        store.set_chat_session_status("abc123", "deleted")
        kept = store.get_chat_session("abc123")
        assert kept is not None and kept["status"] == "deleted"   # row kept, only status changed
        assert store.list_session_moves("abc123")[0]["note"] == "t"
        with pytest.raises(ValueError):
            store.create_chat_session("bad", None, "default", None, kind="nope")
        with pytest.raises(ValueError):
            store.record_session_move("abc123", "a", "b", "other")
    finally:
        store.close()


def test_task_origin_columns_survive_reopen(tmp_path):
    path = tmp_path / "s.sqlite3"
    task = ScheduledTask(task_id="t1", role="coder", model_id=None, repo_head="h", branch="b",
                         payload_json='{"goal":"g"}', estimated_gpu_seconds=60, priority=1,
                         created_at=100.0, chat_session_id="abc123", environment_id="repo-x",
                         main_repo="/r")
    first = SQLiteSchedulerStore(path)
    first.enqueue(task)
    first.enqueue(task)                       # re-enqueue with the same content is a no-op
    first.close()
    reopened = SQLiteSchedulerStore(path)
    try:
        got = reopened.get_task("t1")
        assert got["chat_session_id"] == "abc123" and got["environment_id"] == "repo-x"
        assert got["main_repo"] == "/r" and got["last_apply_error"] is None
        old = ScheduledTask(task_id="t0", role="coder", model_id=None, repo_head="h", branch="b",
                            payload_json='{"goal":"g"}', estimated_gpu_seconds=60, priority=1,
                            created_at=99.0)
        reopened.enqueue(old)
        assert reopened.get_task("t0")["chat_session_id"] is None   # old task: unknown origin
    finally:
        reopened.close()


# ------------------------------------------------------------------ chat sessions
def test_create_session_writes_uuid_dir_and_mcp_command(site):
    headers = signed_in(site)
    created = make_session(site, headers, title="hello")
    kite_id = created["kite_session_id"]
    assert len(kite_id) == 32 and all(c in "0123456789abcdef" for c in kite_id)
    assert created["opencode_session_id"] is None
    directory = Path(created["directory"])
    assert directory == site["path"] / "chat-sessions" / kite_id
    assert (directory / "AGENTS.md").is_file()
    command = json.loads((directory / "opencode.json").read_text(encoding="utf-8"))["mcp"]["kite"]["command"]
    assert "--session-id" in command and kite_id in command
    assert command[command.index("--chat-dir") + 1] == str(directory)
    assert (directory / ".git").is_dir()
    listed = site["client"].get("/api/chat/sessions", headers=headers).json()["sessions"]
    assert [s["kite_session_id"] for s in listed] == [kite_id]
    assert listed[0]["environment_id"] == "default" and listed[0]["title"] == "hello"


def test_move_rules_one_way_default_to_repo(site):
    headers = signed_in(site)
    repo = make_repo(site["path"] / "sample")
    env = make_env(site, headers, name="demo", path=site["path"] / "sample")
    created = make_session(site, headers)
    sid = created["kite_session_id"]
    base = f"/api/chat/sessions/{sid}"
    assert site["client"].post(f"{base}/move", json={"to_env": "999"}, headers=headers).status_code == 404
    assert site["client"].post(f"{base}/move", json={"to_env": "default"}, headers=headers).status_code == 409
    moved = site["client"].post(f"{base}/move", json={"to_env": env["id"]}, headers=headers)
    assert moved.status_code == 200, moved.text
    assert moved.json()["environment_id"] == env["id"]
    moves = site["client"].get(f"{base}/moves", headers=headers).json()["moves"]
    assert len(moves) == 1 and moves[0]["from_env"] == "default" and moves[0]["to_env"] == env["id"]
    assert moves[0]["trigger"] == "picker"
    assert site["client"].post(f"{base}/move", json={"to_env": "default"}, headers=headers).status_code == 409
    site["client"].post(f"{base}/archive", headers=headers)
    assert site["client"].post(f"{base}/move", json={"to_env": env["id"]}, headers=headers).status_code == 409


def test_delete_session_keeps_registry_row(site):
    headers = signed_in(site)
    created = make_session(site, headers)
    sid = created["kite_session_id"]
    assert site["client"].delete(f"/api/chat/sessions/{sid}", headers=headers).status_code == 200
    listed = site["client"].get("/api/chat/sessions", headers=headers).json()["sessions"]
    assert [s["status"] for s in listed] == ["deleted"]
    assert site["client"].get("/api/chat/sessions/nope/moves", headers=headers).status_code == 404


def test_tenth_open_session_warns_many_sessions(site):
    headers = signed_in(site)
    for n in range(10):
        created = make_session(site, headers, title=f"s{n}")
        if n < 9:
            assert "warning" not in created, created
        else:
            assert created["warning"] == "many_sessions"


def test_rerun_calls_ops_rerun_task(site):
    headers = signed_in(site)
    response = site["client"].post("/api/tasks/task-old/rerun", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"task_id": "task-new"}


def test_apply_conflict_maps_to_structured_400(site):
    headers = signed_in(site)
    response = site["client"].post("/api/tasks/task-old/apply", headers=headers)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "apply_conflict" and body["head"] == "deadbeef"
    assert body["failed_files"] == ["a.py"] and body["task_id"] == "task-old"


# ------------------------------------------------------------------ environments
@pytest.fixture
def stub_github_env(monkeypatch):
    """A stand-in for worker (b)'s github_env: records calls, clones for real with plain git."""
    import types

    calls: dict = {}
    module = types.ModuleType("poc.kaggle_worker.github_env")

    class GitHubEnvError(RuntimeError):
        pass

    def clone_with_pat(url, dest, pat_path, sealer=None):
        calls["clone"] = {"url": url, "dest": str(dest), "pat_path": str(pat_path),
                          "sealer": sealer is not None}
        if "fail" in url:
            raise GitHubEnvError("clone boom")
        done = subprocess.run(["git", "clone", "--quiet", url, str(dest)],
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise GitHubEnvError("git clone lỗi: " + (done.stderr or done.stdout).strip()[-200:])
        return Path(dest)

    def pull_with_pat(repo_path, pat_path, sealer=None):
        calls["pull"] = {"repo_path": str(repo_path), "pat_path": str(pat_path),
                         "sealer": sealer is not None}
        return "newhead123"

    module.GitHubEnvError = GitHubEnvError
    module.clone_with_pat = clone_with_pat
    module.pull_with_pat = pull_with_pat
    monkeypatch.setitem(sys.modules, "poc.kaggle_worker.github_env", module)
    return calls


def test_add_environment_local_path(site):
    headers = signed_in(site)
    make_repo(site["path"] / "sample")
    env = make_env(site, headers, name="demo", path=site["path"] / "sample",
                   url="https://github.com/x/y")
    assert env["cloned"] is False and env["path"] == str(site["path"] / "sample")
    listed = site["client"].get("/api/environments", headers=headers).json()["environments"]
    assert [e["id"] for e in listed] == ["default", env["id"]]
    assert listed[1]["kind"] == "github" and listed[1]["url"] == "https://github.com/x/y"


def test_add_environment_clone_file_url_and_branch(site):
    headers = signed_in(site)
    heads = make_repo(site["path"] / "sample", branch="feature-x")
    url = f"file://{site['path'] / 'sample'}"
    env = make_env(site, headers, name="cloned", url=url, branch="feature-x")
    assert env["cloned"] is True
    dest = Path(env["path"])
    assert dest == site["path"] / "envs" / dest.parent.name / "repo"
    assert (dest / ".git").is_dir() and (dest / "feat.txt").is_file()
    assert git("rev-parse", "HEAD", path=dest).stdout.strip() == heads["feature-x"]
    assert git("rev-parse", "--abbrev-ref", "HEAD", path=dest).stdout.strip() == "feature-x"
    assert site["client"].post("/api/environments", json={"name": "bad", "url": "ssh://x/y"},
                               headers=headers).status_code == 400
    assert site["client"].post("/api/environments", json={"name": "bad"},
                               headers=headers).status_code == 400
    assert site["client"].post("/api/environments", json={"name": "bad", "pat": "x"},
                               headers=headers).status_code == 400
    assert site["client"].post("/api/environments", json={"name": "bad", "branch": "main"},
                               headers=headers).status_code == 400


def test_add_environment_clone_with_pat_seals_it(site, stub_github_env):
    headers = signed_in(site)
    make_repo(site["path"] / "sample")
    url = f"file://{site['path'] / 'sample'}"
    response = site["client"].post("/api/environments",
                                   json={"name": "priv", "url": url, "pat": "ghp_testpat123"},
                                   headers=headers)
    assert response.status_code == 200, response.text
    assert "ghp_testpat123" not in response.text
    env = response.json()
    assert stub_github_env["clone"]["url"] == url and stub_github_env["clone"]["sealer"] is True
    sealed = (site["path"] / "secrets" / f"github_pat_{env['id']}").read_text(encoding="utf-8")
    assert sealed.startswith("kite-sealed:v1:") and "ghp_testpat123" not in sealed
    assert site["client"].post(f"/api/environments/{env['id']}/pat",
                               json={"pat": "ghp_other"}, headers=headers).status_code == 200
    assert site["client"].delete(f"/api/environments/{env['id']}/pat", headers=headers).status_code == 200
    assert site["client"].delete(f"/api/environments/{env['id']}/pat", headers=headers).status_code == 404


def test_add_environment_clone_failure_leaves_nothing(site, stub_github_env):
    headers = signed_in(site)
    response = site["client"].post("/api/environments",
                                   json={"name": "boom", "url": "https://fail.example/x/y"},
                                   headers=headers)
    assert response.status_code == 400, response.text
    assert "boom" not in [e["name"] for e in
                          site["client"].get("/api/environments", headers=headers).json()["environments"]]
    assert list((site["path"] / "envs").glob("clone-*")) == []
    assert list((site["path"] / "secrets").glob("github_pat_*")) == []


def test_environments_card_and_detail(site):
    headers = signed_in(site)
    heads = make_repo(site["path"] / "sample")
    fetch_head = site["path"] / "sample" / ".git" / "FETCH_HEAD"
    fetch_head.write_text(heads["main"] + "\n", encoding="utf-8")
    os.utime(fetch_head, (1_700_000_000.0, 1_700_000_000.0))
    env = make_env(site, headers, name="demo", path=site["path"] / "sample")
    make_session(site, headers, environment_id=env["id"], title="in env")
    with site["ops"]._store() as store:
        store.enqueue(ScheduledTask(task_id="t-env", role="coder", model_id=None, repo_head="h",
                                    branch="b", payload_json='{"goal":"g"}', estimated_gpu_seconds=60,
                                    priority=1, created_at=100.0, environment_id=env["id"]))
    # Spend tagged to t-env: 0.5 this month (NOW is 2026-11-23 UTC), 2.0 last
    # month. The card's cost_usd is the all-time lower bound (2.5) while
    # spend_month_usd is the current-UTC-month slice (0.5) the card displays.
    journal_dir = site["path"] / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    with (journal_dir / "spend.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"ts": NOW, "tag": "t-env", "usd": 0.5, "worker": "w"}) + "\n")
        stream.write(json.dumps({"ts": NOW - 40 * 86400, "tag": "t-env", "usd": 2.0,
                                 "worker": "w"}) + "\n")
    cards = site["client"].get("/api/environments", headers=headers).json()["environments"]
    card = next(e for e in cards if e["id"] == env["id"])
    assert card["branch"] == "main" and card["head"] == heads["main"] and card["clean"] is True
    assert card["last_sync"] == 1_700_000_000.0
    assert card["session_count"] == 1 and card["task_count"] == 1 and card["lesson_count"] == 1
    # cost_usd is the all-time lower bound (0.5 this month + 2.0 last month);
    # spend_month_usd is only the current-UTC-month slice the card displays.
    assert card["cost_usd"] == 2.5 and card["spend_month_usd"] == 0.5
    assert card["recent_commits"] and "init" in card["recent_commits"][0]
    default = next(e for e in cards if e["id"] == "default")
    assert default["branch"] is None and default["session_count"] == 0
    detail = site["client"].get(f"/api/environments/{env['id']}", headers=headers).json()
    assert detail["id"] == env["id"] and detail["recent_commits"] == card["recent_commits"]
    assert site["client"].get("/api/environments/999", headers=headers).status_code == 404


def test_sync_uses_sealed_pat(site, stub_github_env):
    headers = signed_in(site)
    make_repo(site["path"] / "sample")
    env = make_env(site, headers, name="demo", path=site["path"] / "sample",
                   url="https://github.com/x/y")
    response = site["client"].post(f"/api/environments/{env['id']}/sync", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "newhead123"
    assert stub_github_env["pull"]["sealer"] is True
    assert stub_github_env["pull"]["pat_path"].endswith(f"github_pat_{env['id']}")
    local = make_env(site, headers, name="local", path=site["path"] / "sample")
    assert site["client"].post(f"/api/environments/{local['id']}/sync", headers=headers).status_code == 200
    assert site["client"].post("/api/environments/default/sync", headers=headers).status_code == 409


def test_env_delete_blocked_by_active_session(site):
    headers = signed_in(site)
    make_repo(site["path"] / "sample")
    env = make_env(site, headers, name="demo", path=site["path"] / "sample")
    created = make_session(site, headers, environment_id=env["id"])
    assert site["client"].delete(f"/api/environments/{env['id']}", headers=headers).status_code == 409
    site["client"].post(f"/api/chat/sessions/{created['kite_session_id']}/archive", headers=headers)
    assert site["client"].delete(f"/api/environments/{env['id']}", headers=headers).status_code == 200
    assert site["client"].delete("/api/environments/default", headers=headers).status_code == 409


# ------------------------------------------------------------------ task detail
def test_task_detail_carries_origin_fields(site):
    headers = signed_in(site)
    with site["ops"]._store() as store:
        store.enqueue(ScheduledTask(task_id="t-detail", role="coder", model_id=None, repo_head="h",
                                    branch="b", payload_json='{"goal":"g"}', estimated_gpu_seconds=60,
                                    priority=1, created_at=100.0, chat_session_id="sess1",
                                    environment_id="env1", main_repo="/main"))
    detail = site["client"].get("/api/tasks/t-detail", headers=headers).json()
    assert detail["chat_session_id"] == "sess1" and detail["environment_id"] == "env1"
    assert detail["main_repo"] == "/main" and detail["last_apply_error"] is None
    missing = site["client"].get("/api/tasks/nope", headers=headers).json()
    assert missing["chat_session_id"] is None and missing["last_apply_error"] is None


# ------------------------------------------------------------------ apply-error column + repo -> env lookup
def test_last_apply_error_column_round_trips(tmp_path):
    import sqlite3

    path = tmp_path / "s.sqlite3"
    store = SQLiteSchedulerStore(path)
    try:
        cols = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(tasks)").fetchall()}
        assert "last_apply_error" in cols
        store.enqueue(ScheduledTask(task_id="t1", role="coder", model_id=None, repo_head="h",
                                    branch="b", payload_json='{"goal":"g"}', estimated_gpu_seconds=60,
                                    priority=1, created_at=100.0))
        db = sqlite3.connect(path)
        db.execute("UPDATE tasks SET last_apply_error=? WHERE task_id=?",
                   ('{"code": "apply_conflict", "head": "abc", "failed_files": ["a.py"]}', "t1"))
        db.commit()
        db.close()
        assert json.loads(store.get_task("t1")["last_apply_error"])["code"] == "apply_conflict"
    finally:
        store.close()


def test_environment_for_repo(tmp_path):
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    make_repo(repo1)
    make_repo(repo2)
    git("remote", "add", "origin", "git@github.com:owner/sample.git", path=repo2)
    web_store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")), clock=lambda: NOW)
    web_store.migrate()
    id1 = web_store.add_repo("r1", str(repo1), "")
    id2 = web_store.add_repo("gh", str(repo2), "https://github.com/owner/sample")
    store = SQLiteSchedulerStore(tmp_path / "scheduler.sqlite3")
    try:
        assert store.environment_for_repo(str(repo1)) == str(id1)                    # path match
        assert store.environment_for_repo(str(repo2)) == str(id2)                    # origin, ssh -> https
        assert store.environment_for_repo("https://github.com/owner/sample") == str(id2)
        assert store.environment_for_repo("https://github.com/owner/sample.git") == str(id2)
        assert store.environment_for_repo(str(tmp_path / "nope")) is None
        assert store.environment_for_repo("") is None
    finally:
        store.close()
    bare = SQLiteSchedulerStore(tmp_path / "elsewhere" / "s.sqlite3")
    try:
        assert bare.environment_for_repo(str(repo1)) is None     # no web.sqlite3 sibling
    finally:
        bare.close()


def test_first_task_of_locked_session_refuses_wrong_repo(tmp_path):
    from tests.poc.kaggle_worker.test_goi2_copies import (
        FakeClient,
        make_ops,
        make_repo as make_repo_b,
        seed_contract_schema,
        seed_session,
    )

    repo1, _head1 = make_repo_b(tmp_path, "repo1")
    repo2, _head2 = make_repo_b(tmp_path, "repo2")
    state = tmp_path / "state"
    db_path = state / "scheduler.sqlite3"
    seed_contract_schema(db_path)
    web_store = Store(connect(sqlite_url(state / "web.sqlite3")), clock=lambda: NOW)
    web_store.migrate()
    id1 = web_store.add_repo("r1", str(repo1), "")
    web_store.add_repo("r2", str(repo2), "")
    seed_session(db_path, "sess1", environment_id=str(id1))
    ops = make_ops(tmp_path, FakeClient(), session_id="sess1", roots=[tmp_path])
    with pytest.raises(KiteToolError, match="không đổi repo"):
        ops._resolve_task_repo(str(repo2), task_id="t1", session_id="sess1")
    copy, _head, main_repo, env_id = ops._resolve_task_repo(str(repo1), task_id="t2",
                                                            session_id="sess1")
    assert env_id == str(id1) and main_repo == repo1
    assert copy != repo1 and (copy / "calc.py").is_file()


def test_cli_mcp_serve_session_id_reaches_kiteops():
    import inspect

    from poc.kaggle_worker.cli import _parser
    from poc.kaggle_worker.mcp_server import KiteOps

    args = _parser().parse_args(["--config", "/tmp/x.yaml", "mcp", "serve", "--session-id", "abc"])
    assert args.session_id == "abc"
    assert "session_id" in inspect.signature(KiteOps.__init__).parameters


# ------------------------------------------------------------------ web teardown (review (c) 5b)

def _dummy_mcp_process(config_path: Path, session_id: str):
    """A real process whose argv matches `stale_chat_processes` for this config+session:
    `kite --config <resolved> mcp serve --session-id <id>` (here `python -c` sleeps
    instead of serving, but the argv shape is what the teardown matches on)."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)",
         "--config", str(config_path.resolve()), "mcp", "serve", "--session-id", session_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_dead(proc, timeout: float = 5.0) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_delete_session_stops_its_mcp_process(site):
    headers = signed_in(site)
    created = make_session(site, headers)
    sid = created["kite_session_id"]
    proc = _dummy_mcp_process(site["config"], sid)
    try:
        assert site["client"].delete(f"/api/chat/sessions/{sid}", headers=headers).status_code == 200
        assert _wait_dead(proc), "process MCP của session đã xóa vẫn còn sống"
        assert proc.returncode == -signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_archive_session_stops_only_its_own_mcp_process(site):
    headers = signed_in(site)
    sid = make_session(site, headers)["kite_session_id"]
    other = make_session(site, headers)["kite_session_id"]
    mine = _dummy_mcp_process(site["config"], sid)
    theirs = _dummy_mcp_process(site["config"], other)
    try:
        response = site["client"].post(f"/api/chat/sessions/{sid}/archive", headers=headers)
        assert response.status_code == 200, response.text
        assert _wait_dead(mine), "process MCP của session đã lưu trữ vẫn còn sống"
        assert mine.returncode == -signal.SIGTERM
        assert theirs.poll() is None, "process của session khác bị giết oan"
    finally:
        for proc in (mine, theirs):
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def test_scheduler_engine_registers_mcp_sweep(site, monkeypatch):
    from poc.kaggle_worker.web.scheduler_wiring import _mcp_sweep_job, build_engine

    engine = build_engine(site["ctx"])
    assert engine._jobs["mcp_sweep"].interval_seconds == 300

    calls: list[tuple] = []
    monkeypatch.setattr("poc.kaggle_worker.web.scheduler_wiring.sweep_stale_mcp_processes",
                        lambda *args: calls.append(args))
    _mcp_sweep_job(site["ctx"])()
    assert calls == [(site["ctx"].state_dir, site["ctx"].config_path)]


def test_web_startup_sweeps_orphaned_mcp_processes(site, monkeypatch):
    from poc.kaggle_worker.web import app as appmod

    calls: list[tuple] = []
    monkeypatch.setattr(appmod, "sweep_stale_mcp_processes", lambda *args: calls.append(args))
    with TestClient(create_app(site["ctx"], start_scheduler=True), base_url="https://testserver"):
        pass
    assert calls == [(site["ctx"].state_dir, site["ctx"].config_path)]
