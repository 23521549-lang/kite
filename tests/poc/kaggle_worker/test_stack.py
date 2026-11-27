"""P6, inside the image: the config of a new machine, requests to kite-stack, health checks, the chat's
start, moving a machine (export on the old one, import on the new one), and the deployment files."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
import tarfile
import threading

import pytest
import yaml

from poc.kaggle_worker import bootstrap, cli, stack
from poc.kaggle_worker.config import load_config

ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------- a new machine's config
def test_a_new_machine_gets_a_config_kite_accepts_and_it_is_never_overwritten(tmp_path):
    config = tmp_path / "kite" / "config.yaml"
    report = stack.init_config(config, public="https://1-2-3-4.sslip.io", sandbox_gateway="172.30.230.1",
                               home=tmp_path / "home")
    assert report["written"] and (tmp_path / "home").is_dir()
    loaded = load_config(config)
    assert loaded.gateway.public_url == "https://1-2-3-4.sslip.io" and loaded.gateway.host == "127.0.0.1"
    assert loaded.sandbox.gateway_host == "172.30.230.1" and loaded.providers == ()
    assert loaded.harness2_checkout == "/opt/harness2"
    assert (config.parent / "secrets").stat().st_mode & 0o777 == 0o700
    raw = yaml.safe_load(config.read_text())
    raw["providers"] = []
    raw["routing"] = {"default_skill": 0.4}
    config.write_text(yaml.safe_dump(raw))
    again = stack.init_config(config, public="https://kite.example.com", sandbox_gateway="172.30.230.1")
    assert again == {"config": str(config), "written": False, "public_url_changed": True,
                     "sandbox_gateway_changed": False}
    kept = yaml.safe_load(config.read_text())
    assert kept["routing"] == {"default_skill": 0.4} and kept["gateway"]["public_url"] == "https://kite.example.com"
    assert stack.public_url("a.sslip.io") == "https://a.sslip.io"
    assert stack.public_url("a.sslip.io", 8443) == "https://a.sslip.io:8443"


# ---------------------------------------------------------------- requests and status
def test_requests_for_the_host_are_whole_files_with_known_actions_only(tmp_path):
    with pytest.raises(stack.StackError, match="chưa cài"):
        stack.request("update", data_dir=tmp_path)
    (tmp_path / "stack" / "requests").mkdir(parents=True)
    queued = stack.request("restart", data_dir=tmp_path, clock=lambda: 100.5,
                           services=["kite-gateway", "kite-workflow-worker"])
    assert queued["queued"] and queued["services"] == ["gateway", "worker"]
    files = list((tmp_path / "stack" / "requests").iterdir())
    assert [f.name for f in files] == ["100-500-restart.json"]
    assert json.loads(files[0].read_text())["action"] == "restart"
    with pytest.raises(stack.StackError):
        stack.request("rm -rf", data_dir=tmp_path)
    with pytest.raises(stack.StackError):
        stack.request("restart", data_dir=tmp_path, services=["postgres"])


def test_the_update_plan_comes_from_kite_stacks_status(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_VERSION", "abc123def4567890")
    plan = stack.update_plan(tmp_path)
    assert plan["deploy"] == "compose" and plan["head"] == "abc123def456" and "error" in plan
    (tmp_path / "stack").mkdir()
    (tmp_path / "stack" / "status.json").write_text(json.dumps({
        "state": "idle", "current": {"digest": "sha256:aa", "revision": "1111"},
        "available": {"digest": "sha256:bb", "revision": "2222"}}))
    plan = stack.update_plan(tmp_path)
    assert plan["behind"] == 1 and plan["head"] == "1111" and plan["target"] == "2222" and "error" not in plan
    status = json.loads((tmp_path / "stack" / "status.json").read_text())
    status["bad_digests"] = ["sha256:bb"]          # tried and rolled back: not offered again
    (tmp_path / "stack" / "status.json").write_text(json.dumps(status))
    assert stack.update_plan(tmp_path)["behind"] == 0


def test_kite_update_in_the_image_asks_kite_stack(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KITE_DEPLOY", "compose")
    monkeypatch.setattr(stack, "DATA_DIR", tmp_path)
    (tmp_path / "stack" / "requests").mkdir(parents=True)
    config = tmp_path / "kite" / "config.yaml"
    stack.init_config(config, public=None, home=tmp_path / "home")
    monkeypatch.setattr(stack, "request", lambda action, **kw: {"queued": True, "action": action})
    assert cli.main(["--config", str(config), "update", "--yes"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queued"] and out["action"] == "update"


def test_busy_lists_running_tasks_and_a_bench_run(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    stack.init_config(config, public=None, home=tmp_path / "home")
    assert cli.main(["--config", str(config), "busy"]) == 0
    assert json.loads(capsys.readouterr().out) == {"busy": False, "tasks": [], "chat": 0, "bench": None}
    from poc.kaggle_worker import bench

    # A lock file left by a bench that died is not a running bench.
    (tmp_path / bench.LOCK_NAME).write_text(json.dumps({"pid": 1, "label": "dead", "started": "x"}))
    assert cli.main(["--config", str(config), "busy"]) == 0
    assert json.loads(capsys.readouterr().out)["bench"] is None
    lock = bench.acquire_lock(tmp_path, label="v7", out=tmp_path / "v7")
    try:
        assert cli.main(["--config", str(config), "busy"]) == 0
        report = json.loads(capsys.readouterr().out)
    finally:
        bench.release_lock(lock)
    assert report["busy"] and report["bench"]["label"] == "v7"


def test_busy_counts_chat_sessions_in_every_chat_folder(tmp_path, monkeypatch, capsys):
    # Each chat session, the secretary and each patch review run in their own folder: a reply
    # being written in any of them must hold an update back, not only the old ~/kite-chat.
    pytest.importorskip("fastapi")
    from poc.kaggle_worker.store import SQLiteSchedulerStore

    config = tmp_path / "config.yaml"
    stack.init_config(config, public=None, home=tmp_path / "home")
    with SQLiteSchedulerStore(tmp_path / "scheduler.sqlite3") as store:
        store.create_chat_session("s1", "oc1", "default")
        store.create_chat_session("s2", "oc2", "default")
        store.set_chat_session_status("s2", "archived")
    for folder in ("chat-sessions/s1", "chat-sessions/s2", "review/t1"):
        (tmp_path / folder).mkdir(parents=True)
    asked = []

    class Sessions(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlsplit

            folder = parse_qs(urlsplit(self.path).query)["directory"][0]
            asked.append(Path(folder).name)
            busy = folder.endswith(("chat-sessions/s1", "review/t1"))
            body = json.dumps({"data": [{"id": "x", "time": {"busy": 2, "idle": 1}}] if busy else []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Sessions)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / "userhome"
    (home / ".local" / "state" / "opencode").mkdir(parents=True)
    (home / ".local" / "state" / "opencode" / "service.json").write_text(
        json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}", "password": "pw"}))
    monkeypatch.setenv("HOME", str(home))
    try:
        assert cli.main(["--config", str(config), "busy"]) == 0
    finally:
        server.shutdown()
    report = json.loads(capsys.readouterr().out)
    assert report["busy"] and report["chat"] == 2
    assert sorted(asked) == ["s1", "t1"]      # the archived session's folder is not asked


# ---------------------------------------------------------------- health checks
class Status(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path in ("/status", "/api/health") else 404
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def test_health_checks_ask_each_service_where_it_listens(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Status)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        config = tmp_path / "config.yaml"
        stack.init_config(config, public=None, home=tmp_path / "home")
        raw = yaml.safe_load(config.read_text())
        raw["gateway"]["port"] = port
        raw["temporal"]["endpoint"] = f"127.0.0.1:{port}"
        config.write_text(yaml.safe_dump(raw))
        assert stack.health("gateway", config)[0] and stack.health("worker", config)[0]
        monkeypatch.setenv("KITE_WEB_PORT", str(port))
        assert stack.health("web", config)[0]
        home = tmp_path / "home"
        assert not stack.health("opencode", config, home=home)[0]
        (home / ".local" / "state" / "opencode").mkdir(parents=True)
        (home / ".local" / "state" / "opencode" / "service.json").write_text(
            json.dumps({"url": f"http://127.0.0.1:{port}", "password": "p"}))
        assert stack.health("opencode", config, home=home)[0]
        raw["gateway"]["port"] = 1
        config.write_text(yaml.safe_dump(raw))
        assert not stack.health("gateway", config)[0]
    finally:
        server.shutdown()


def test_the_chat_container_rewrites_the_chat_files_then_becomes_opencode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    stack.opencode_entry(tmp_path / "c.yaml", lambda p: calls.append("refresh") or {},
                         lambda p: calls.append("setup") or {}, lambda: calls.append("serve"))
    assert calls == ["setup", "serve"]
    (tmp_path / "kite-chat").mkdir()
    (tmp_path / "kite-chat" / "opencode.json").write_text("{}")
    calls.clear()

    def broken(_path):
        raise RuntimeError("no gateway")

    stack.opencode_entry(tmp_path / "c.yaml", broken, broken, lambda: calls.append("serve"))
    assert calls == ["serve"]            # the server starts even when the files could not be written


# ---------------------------------------------------------------- moving a machine
def web_sqlite(path: Path, repo: Path) -> None:
    from poc.kaggle_worker.web.db import Store, connect, sqlite_url

    store = Store(connect(sqlite_url(path)))
    store.migrate()
    uid = store.create_user("han", "hash", "sealed")
    store.add_session("sid", uid, "csrf", "1.2.3.4", "ua")
    store.audit("han", "1.2.3.4", "login", "han", True, {"x": 1})
    with store.engine.begin() as conn:
        from sqlalchemy import insert

        from poc.kaggle_worker.web.db import repos

        conn.execute(insert(repos).values(name="sweetpaw", path=str(repo), url="", created_at=1.0))
    store.engine.dispose()


def test_a_machine_moves_with_its_state_chat_repositories_and_web_account(tmp_path):
    pytest.importorskip("sqlalchemy")            # the web extra (CI installs only the test extra)
    old_home = tmp_path / "old" / "home" / "ubuntu"
    old_state = tmp_path / "old" / "var" / "kite"
    (old_state / "secrets").mkdir(parents=True)
    (old_state / "secrets" / "worker_token_kaggle-han").write_text("tok\n")
    (old_state / "journal").mkdir()
    (old_state / "journal" / "gateway.jsonl").write_text("{}\n")
    (old_state / "deps-cache").mkdir()
    (old_state / "deps-cache" / "big").write_text("x" * 10)
    (old_state / "harness2").mkdir()
    (old_state / "harness2" / "skip").write_text("x")
    stack.init_config(old_state / "config.yaml", public="https://1-2-3-4.sslip.io:8443", home=old_home)
    raw = yaml.safe_load((old_state / "config.yaml").read_text())
    raw["harness2"] = {"checkout": str(old_state / "harness2"), "cli": str(old_home / "kite/.venv/bin/harness2")}
    raw["opencode"]["executable"] = str(old_home / ".opencode/bin/opencode")
    (old_state / "config.yaml").write_text(yaml.safe_dump(raw))
    (old_state / "scheduler.sqlite3").write_text("tasks")
    repo = old_home / "SweetPaw"
    (repo / ".git").mkdir(parents=True)
    (repo / "app.py").write_text("print(1)\n")
    outside = tmp_path / "old" / "srv" / "other"
    (outside / ".git").mkdir(parents=True)
    chat = old_home / "kite-chat"
    chat.mkdir(parents=True)
    (chat / "AGENTS.md").write_text("Han thích câu trả lời ngắn\n")
    (chat / "opencode.json").write_text(json.dumps({"mcp": {"kite": {"command": [
        str(old_home / "kite/.venv/bin/kite"), "--config", str(old_state / "config.yaml"), "mcp", "serve",
        "--allow-root", str(old_home)]}}}))
    web_sqlite(old_state / "web.sqlite3", repo)

    archive = tmp_path / "kite-export.tgz"
    report = stack.export_state(old_state, archive, home=old_home, repos=[repo, outside])
    assert archive.stat().st_mode & 0o777 == 0o600 and report["skipped"] == ["harness2"]
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "state/secrets/worker_token_kaggle-han" in names and "home/SweetPaw/app.py" in names
    assert "home/kite-chat/AGENTS.md" in names and "home/repos/other/.git" in names
    assert not any(n.startswith("state/harness2") for n in names)
    assert stack.registered_repos(old_state / "web.sqlite3") == [repo]

    new_state, new_home = tmp_path / "new" / "kite", tmp_path / "new" / "home"
    stack.init_config(new_state / "config.yaml", public="https://5-6-7-8.sslip.io", home=new_home)
    target = tmp_path / "new" / "web.sqlite3"
    result = stack.import_state(archive, new_state, home=new_home, web_db=f"sqlite:///{target}",
                                public="https://5-6-7-8.sslip.io", sandbox_gateway="172.30.230.1")
    config = load_config(new_state / "config.yaml")
    assert config.sandbox.gateway_host == "172.30.230.1"           # the new machine's sandbox network
    assert config.gateway.public_url == "https://5-6-7-8.sslip.io"
    assert config.harness2_checkout == "/opt/harness2" and config.opencode_executable == "opencode"
    assert (new_state / "secrets" / "worker_token_kaggle-han").stat().st_mode & 0o777 == 0o600
    assert (new_state / "journal" / "gateway.jsonl").exists() and (new_state / "deps-cache" / "big").exists()
    assert (new_home / "SweetPaw" / "app.py").read_text() == "print(1)\n"
    assert (new_home / "repos" / "other" / ".git").is_dir()
    command = json.loads((new_home / "kite-chat" / "opencode.json").read_text())["mcp"]["kite"]["command"]
    assert command[-1] == str(new_home) and str(old_home) not in json.dumps(command)
    assert result["config_backup"] and Path(result["config_backup"]).exists()
    assert result["web_db"] == {"users": 1, "audit_log": 1, "repos": 1, "settings": 0}
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT path FROM repos").fetchone()[0] == str(new_home / "SweetPaw")
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    with pytest.raises(stack.StackError, match="--force"):
        stack.import_state(archive, new_state, home=new_home)


# ---------------------------------------------------------------- the deployment files
def compose() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "stack" / "compose.yaml").read_text(encoding="utf-8"))


def test_kite_services_run_like_on_the_development_machine_and_only_caddy_faces_the_internet():
    services = compose()["services"]
    for name in ("gateway", "worker", "web", "opencode"):
        svc = services[name]
        assert svc["network_mode"] == "host" and svc["user"] == "1000:1000"
        # Only the worker shares the host's process space (its activities share PID-based
        # state with the chat processes); the chat runs in the web's, so the web can stop the
        # MCP servers of closed sessions; the gateway sees neither.
        assert svc.get("pid") == {"worker": "host", "opencode": "service:web"}.get(name)
        assert "ports" not in svc and svc["image"].startswith("${KITE_IMAGE")
        assert "/var/lib/kite:/var/lib/kite" in svc["volumes"]
        assert svc["healthcheck"]["test"] == ["CMD", "kite", "stack", "health", name]
        assert svc["restart"] == "unless-stopped"
    assert "/var/run/docker.sock:/var/run/docker.sock" in services["worker"]["volumes"]
    assert not any("docker.sock" in v for name in ("gateway", "web", "opencode") for v in services[name]["volumes"])
    for name, svc in services.items():
        for port in svc.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)
    assert services["web"]["environment"]["KITE_WEB_DB"].startswith("postgresql+psycopg://kite_web:")
    assert services["caddy"]["network_mode"] == "host"
    for access in ("public", "private"):
        assert (ROOT / "deploy" / "stack" / f"Caddyfile.{access}").is_file()
    private = "\n".join(line for line in (ROOT / "deploy" / "stack" / "Caddyfile.private").read_text().splitlines()
                        if not line.lstrip().startswith("#"))
    assert "8790" not in private and "8787" in private          # the web page is not published
    public = (ROOT / "deploy" / "stack" / "Caddyfile.public").read_text()
    assert re.search(r"@gateway_private path /v1/\* /kite/\*", public)
    for script in ("temporal-setup.sh", "web-db.sh", "install.sh", "kite_stack.py"):
        assert (ROOT / "deploy" / "stack" / script).is_file(), script     # run through sh/bash/python3
    for unit in ("kite-stack-update.service", "kite-stack-update.timer", "kite-stack-requests.path",
                 "kite-stack-requests.service"):
        assert (ROOT / "deploy" / "stack" / "systemd" / unit).is_file()


def test_the_installer_refuses_a_docker_key_with_the_wrong_fingerprint():
    script = (ROOT / "deploy" / "stack" / "install.sh").read_text(encoding="utf-8")
    # Docker's official apt signing key fingerprint, spaces removed: the download is only trusted
    # when the key that arrived carries exactly this fingerprint.
    assert "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" in script
    check = script.index("9DC858229FC7DD38854AE2D88D81803C0EBFCD88")
    assert "exit 1" in script[check:check + 400]                    # a mismatch aborts the install
    assert check < script.index("docker.sources")                   # and it happens before the repo is used


def test_the_installer_puts_the_ci_signers_cosign_release_on_the_machine():
    # A new machine verifies image signatures from its first release on, so the installer must bring
    # cosign along: the release CI signs with, checked against its SHA-256 before it is installed.
    script = (ROOT / "deploy" / "stack" / "install.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    version = re.search(r'^COSIGN_VERSION="(v[0-9.]+)"$', script, re.M).group(1)
    assert re.search(rf"cosign-release:\s*{re.escape(version)}\s*$", ci, re.M)    # signer = verifier
    for arch in ("amd64", "arm64"):
        assert re.search(rf'{arch}\) cosign_sha256="[0-9a-f]{{64}}"', script), arch
    check = script.index('!= "$cosign_sha256"')
    assert "exit 1" in script[check:check + 300]                    # a wrong download aborts the install
    assert check < script.index("/usr/local/bin/cosign")            # installed only after the check
    assert script.index("/usr/local/bin/cosign") < script.index('kite_stack.py" install')


def test_ci_moves_the_stable_tag_only_after_signing():
    # Machines pull :stable and refuse an unsigned image: stable must never point at one.
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    images = ci[ci.index("\n  images:"):ci.index("\n  images-check:")]
    assert images.index("cosign sign") < images.index(":stable")
    assert "imagetools inspect" in images                    # the tag still names the signed digest


def test_a_new_aws_machine_runs_the_installer_from_a_pinned_commit():
    # The first boot runs install.sh as root: from a fixed commit, never from the moving main branch.
    variables = (ROOT / "infra" / "aws" / "variables.tf").read_text(encoding="utf-8")
    assert re.search(r"raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/deploy/stack/install\.sh", variables)


def test_the_data_disk_carries_the_tag_the_daily_snapshots_target():
    # The DLM policy (bootstrap) and the disk (infra/aws) live in two Terraform roots: a renamed tag on
    # either side would silently stop the backups.
    bootstrap = (ROOT / "infra" / "aws" / "bootstrap" / "main.tf").read_text(encoding="utf-8")
    disk = (ROOT / "infra" / "aws" / "main.tf").read_text(encoding="utf-8")
    target = re.search(r"target_tags\s*=\s*\{\s*(\w+)\s*=\s*\"([^\"]+)\"\s*\}", bootstrap)
    assert target, "no target_tags in the DLM policy"
    volume = disk[disk.index('resource "aws_ebs_volume" "data"'):]
    volume = volume[:volume.index("\n}\n")]
    assert re.search(rf'{target.group(1)}\s*=\s*"{re.escape(target.group(2))}"', volume)
    assert re.search(r"retain_rule\s*\{\s*count\s*=\s*7\s*\}", bootstrap)


def test_third_party_images_are_pinned_by_digest_and_temporal_passwords_are_split():
    services = compose()["services"]
    for name in ("postgres", "web-db", "temporal", "temporal-setup", "caddy"):
        assert "@sha256:" in services[name]["image"], name
    setup_env = services["temporal-setup"]["environment"]
    assert "TEMPORAL_SETUP_POSTGRES_PASSWORD" in setup_env["POSTGRES_PWD"]
    temporal_env = services["temporal"]["environment"]
    assert "TEMPORAL_POSTGRES_PASSWORD" in temporal_env["SQL_PASSWORD"]
    assert "TEMPORAL_VISIBILITY_POSTGRES_PASSWORD" in temporal_env["VISIBILITY_SQL_PASSWORD"]
    assert "TEMPORAL_POSTGRES_PASSWORD" in services["postgres"]["environment"]["POSTGRES_PASSWORD"]


def test_pinned_versions_agree_across_the_image_and_the_code():
    app = (ROOT / "poc" / "kaggle_worker" / "web" / "app.py").read_text(encoding="utf-8")
    OPENCODE_TESTED = re.search(r'^OPENCODE_TESTED = "([^"]+)"', app, re.M).group(1)
    env = dict(line.split("=", 1) for line in (ROOT / "deploy" / "opencode.env").read_text().splitlines()
               if line and not line.startswith("#"))
    assert env["OPENCODE_VERSION"] == OPENCODE_TESTED and len(env["OPENCODE_SHA512"]) == 128
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert f"ARG HARNESS2_REVISION={bootstrap.HARNESS2_REVISION}" in dockerfile
    assert "USER 1000:1000" in dockerfile and "KITE_DEPLOY=compose" in dockerfile
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert {"web/node_modules", "web/dist", "**/secrets", "**/.env"} <= set(ignored)
