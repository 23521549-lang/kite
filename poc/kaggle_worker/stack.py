"""Kite in the Docker Compose deployment (deploy/stack): what changes inside the containers.

On the development machine Kite runs from a Git checkout under systemd: ``kite update`` pulls and tests
the code, the web restarts services with ``sudo systemctl``. In the Compose deployment every Kite
service runs from one image (KITE_DEPLOY=compose) and the host's own tool, ``kite-stack``
(deploy/stack/kite_stack.py, run by root through systemd), pulls new images, starts them, checks them
and puts the previous image back when the new one is not healthy. The containers never get Docker's
or root's power over the host for that: the web and the chat leave a request file in
``/var/lib/kite/stack/requests`` and read the result from ``/var/lib/kite/stack/status.json``.

This module also has what runs inside the image: the default config of a new machine (``init_config``),
the chat's OpenCode server start (``opencode_entry``), the services' health checks (``health``), and
moving a machine (``export_state`` on the old one, ``import_state`` on the new one).
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable
import urllib.error
import urllib.request

DATA_DIR = Path("/var/lib/kite")
HOME = DATA_DIR / "home"
IMAGE_ROOT = Path("/opt/kite")
HARNESS2_CHECKOUT = Path("/opt/harness2")
SERVICES = ("gateway", "worker", "web", "opencode")
# Names the web and the chat use (systemd units on the development machine) -> Compose services.
UNIT_SERVICES = {"kite-gateway": "gateway", "kite-workflow-worker": "worker", "kite-web": "web",
                 "kite-opencode": "opencode"}
ACTIONS = ("update", "check", "restart")
EXPORT_SKIP = ("harness2",)            # in the image already


class StackError(RuntimeError):
    pass


def in_compose() -> bool:
    return os.environ.get("KITE_DEPLOY") == "compose"


def stack_dir(data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / "stack"


def version() -> str:
    """The Kite revision this process runs: baked into the image, else the Git checkout's HEAD."""
    baked = os.environ.get("KITE_VERSION", "").strip()
    if baked:
        return baked[:12]
    try:
        done = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "--short=12",
                               "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


# ---------------------------------------------------------------------------- requests to the host
def status(data_dir: Path = DATA_DIR) -> dict:
    """What kite-stack last wrote: the running release, the newest one seen, the last update."""
    path = stack_dir(data_dir) / "status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "unknown", "error": "chưa có trạng thái từ kite-stack (dịch vụ cập nhật chưa chạy lần nào)"}
    except (OSError, ValueError) as exc:
        return {"state": "unknown", "error": f"không đọc được {path}: {exc}"}
    return data if isinstance(data, dict) else {"state": "unknown"}


def request(action: str, data_dir: Path = DATA_DIR, clock: Callable[[], float] = time.time, **fields: Any) -> dict:
    """Leave a request for kite-stack on the host (a systemd path unit starts it within seconds)."""
    if action not in ACTIONS:
        raise StackError(f"không có việc '{action}' (có: {', '.join(ACTIONS)})")
    if action == "restart":
        names = [UNIT_SERVICES.get(s, s) for s in fields.get("services") or []]
        unknown = [s for s in names if s not in SERVICES]
        if not names or unknown:
            raise StackError(f"dịch vụ không có: {', '.join(unknown) or '(trống)'}; có: {', '.join(SERVICES)}")
        fields["services"] = names
    folder = stack_dir(data_dir) / "requests"
    if not folder.is_dir():
        raise StackError(f"{folder} chưa có: máy này chưa cài bằng kite-stack")
    stamp = f"{clock():.3f}".replace(".", "-")
    record = {"action": action, "at": clock(), **fields}
    temp = folder / f".{stamp}-{action}.tmp"
    temp.write_text(json.dumps(record), encoding="utf-8")
    final = folder / f"{stamp}-{action}.json"
    temp.replace(final)                   # the path unit only sees whole files
    return {"queued": True, "request": final.name, **record}


def update_plan(data_dir: Path = DATA_DIR) -> dict:
    """``kite update`` / the Settings tab in the Compose deployment: the running and the newest release."""
    current = status(data_dir)
    running = current.get("current") or {}
    available = current.get("available") or {}
    # A release that already failed its health check is not "new": kite-stack went back from it.
    behind = (bool(available.get("digest")) and available.get("digest") != running.get("digest")
              and available.get("digest") not in (current.get("bad_digests") or []))
    return {"deploy": "compose", "head": (running.get("revision") or version())[:12],
            "target": (available.get("revision") or "")[:12], "behind": 1 if behind else 0, "commits": [],
            "dirty": [], "notebook_changed": False, "stack": current,
            **({"error": current["error"]} if current.get("error") else {})}


# ---------------------------------------------------------------------------- in the image
def public_url(domain: str, port: int | str = 443) -> str:
    port = int(port)
    return f"https://{domain}" + ("" if port == 443 else f":{port}")


def init_config(config_path: Path, *, public: str | None, sandbox_gateway: str | None = None,
                home: Path = HOME) -> dict:
    """The config of a new machine, written once; afterwards only the public URL and the sandbox network's
    address follow the machine (kite-stack configure, a moved config). Workers are added on the web."""
    import yaml

    from .config import load_config

    config_path = Path(config_path)
    if config_path.exists():
        changed = bool(public) and set_public_url(config_path, public)
        moved = bool(sandbox_gateway) and _set_sandbox_gateway(config_path, sandbox_gateway)
        return {"config": str(config_path), "written": False, "public_url_changed": changed,
                "sandbox_gateway_changed": moved}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    venv_bin = Path(sys.executable).parent
    raw: dict[str, Any] = {
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "kite-coding-tasks"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": str(HARNESS2_CHECKOUT), "cli": str(venv_bin / "harness2")},
        "gateway": {"host": "127.0.0.1", "port": 8787, **({"public_url": public} if public else {})},
        "sandbox": {"gateway_host": sandbox_gateway} if sandbox_gateway else {},
        "providers": [],
    }
    temp = config_path.with_name(config_path.name + ".tmp")
    temp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    load_config(temp)                     # never leave a config Kite itself refuses
    temp.replace(config_path)
    for folder in ("secrets", "journal", "tasks", "logs", "checkpoints", "evaluations"):
        (config_path.parent / folder).mkdir(exist_ok=True)
    (config_path.parent / "secrets").chmod(0o700)
    Path(home).mkdir(parents=True, exist_ok=True)
    return {"config": str(config_path), "written": True}


def set_public_url(config_path: Path, url: str) -> bool:
    """Point the Kaggle notebooks at this machine (a moved config still names the old one)."""
    import yaml

    from .config import load_config

    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    gateway = raw.setdefault("gateway", {})
    if gateway.get("public_url") == url:
        return False
    gateway["public_url"] = url
    temp = Path(config_path).with_name(Path(config_path).name + ".tmp")
    temp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    load_config(temp)
    temp.replace(config_path)
    return True


def _set_sandbox_gateway(config_path: Path, address: str) -> bool:
    import yaml

    from .config import load_config

    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    sandbox = raw.get("sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("gateway_host") == address:
        return False
    sandbox["gateway_host"] = address
    temp = Path(config_path).with_name(Path(config_path).name + ".tmp")
    temp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    load_config(temp)
    temp.replace(config_path)
    return True


def health(service: str, config_path: Path, *, timeout: float = 5.0, home: Path | None = None) -> tuple[bool, str]:
    """The Compose health check of one Kite service (its HEALTHCHECK runs ``kite stack health <name>``)."""
    if service == "gateway":
        from .config import load_config

        config = load_config(config_path)
        return _http_ok(f"http://{config.gateway.host}:{config.gateway.port}/status", timeout)
    if service == "web":
        return _http_ok(f"http://127.0.0.1:{os.environ.get('KITE_WEB_PORT', '8790')}/api/health", timeout)
    if service == "worker":
        from .config import load_config

        endpoint = load_config(config_path).temporal_endpoint
        host, _, port = endpoint.rpartition(":")
        return _tcp_ok(host or "127.0.0.1", int(port or 7233), timeout)
    if service == "opencode":
        info = Path(home or Path.home()) / ".local" / "state" / "opencode" / "service.json"
        try:
            url = json.loads(info.read_text(encoding="utf-8"))["url"]
        except (OSError, ValueError, KeyError, TypeError):
            return False, f"chưa có {info}"
        match = re.match(r"https?://([^:/]+):(\d+)", str(url))
        if not match:
            return False, f"địa chỉ lạ trong {info}: {url}"
        return _tcp_ok(match.group(1), int(match.group(2)), timeout)
    raise StackError(f"không có dịch vụ {service}")


def _http_ok(url: str, timeout: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
            return 200 <= response.status < 300, f"{url}: HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"{url}: HTTP {exc.code}"
    except (OSError, urllib.error.URLError) as exc:
        return False, f"{url}: {exc}"


def _tcp_ok(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} nhận kết nối"
    except OSError as exc:
        return False, f"{host}:{port}: {exc}"


def opencode_entry(config_path: Path, refresh: Callable[[Path], dict], setup: Callable[[Path], dict],
                   exec_server: Callable[[], None]) -> None:
    """Start of the chat's container: write the chat's files with this image's Kite (the old ones name
    the previous image's tools), then become ``opencode serve --service``. A machine without a worker
    yet has no chat folder: it is made once a worker serves a model (the container restarts with the
    gateway after a worker is added)."""
    chat = Path.home() / "kite-chat" / "opencode.json"
    try:
        report = refresh(config_path) if chat.exists() else setup(config_path)
    except Exception as exc:              # the chat page says what is missing; the server still starts
        report = {"chat": f"{type(exc).__name__}: {exc}"[:300]}
    print(json.dumps({"opencode_entry": report}, ensure_ascii=False), flush=True)
    exec_server()


# ---------------------------------------------------------------------------- moving a machine
def export_state(state_dir: Path, out: Path, *, home: Path | None = None, repos: list[Path] = (),
                 web_db: Path | None = None, with_cache: bool = True, chat_history: bool = True) -> dict:
    """One file with what a new machine needs: the state folder (config, secrets, journal, memory, task
    records, the library cache), the chat folder and OpenCode's sessions, and the repositories Kite
    works on (they may hold commits that are only on this machine: Kite never pushes)."""
    state_dir, home = Path(state_dir), Path(home or Path.home())
    skip = set(EXPORT_SKIP) | (set() if with_cache else {"deps-cache"})
    repos = [Path(r).resolve() for r in repos] or registered_repos(web_db or state_dir / "web.sqlite3")
    manifest = {"format": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "state_dir": str(state_dir),
                "home": str(home), "version": version(), "repos": []}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for item in sorted(state_dir.iterdir()):
            if item.name in skip or item.name.endswith(".tmp"):
                continue
            tar.add(item, arcname=f"state/{item.name}")
        for rel in ("kite-chat", *((".local/share/opencode",) if chat_history else ())):
            if (home / rel).exists():
                tar.add(home / rel, arcname=f"home/{rel}")
        for repo in repos:
            if not (repo / ".git").exists():
                continue
            try:
                rel = repo.relative_to(home)
            except ValueError:
                rel = Path("repos") / repo.name     # outside the home folder: put it in ~/repos
            tar.add(repo, arcname=f"home/{rel}")
            manifest["repos"].append({"from": str(repo), "to": str(rel)})
        data = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
        tar.addfile(info, io.BytesIO(data))
    out.chmod(0o600)                      # it holds the secrets
    return {"file": str(out), "bytes": out.stat().st_size, "repos": manifest["repos"],
            "skipped": sorted(skip)}


def registered_repos(web_db: Path) -> list[Path]:
    """Repository folders the web knows (Repo tab); none when there is no web database."""
    if not Path(web_db).is_file():
        return []
    import sqlite3

    try:
        with sqlite3.connect(f"file:{web_db}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT path FROM repos").fetchall()
    except sqlite3.Error:
        return []
    return [Path(row[0]) for row in rows if row and row[0]]


def import_state(archive: Path, state_dir: Path, *, home: Path = HOME, web_db: str | None = None,
                 public: str | None = None, sandbox_gateway: str | None = None, force: bool = False) -> dict:
    """Put an export into this machine: files into the state and home folders (paths of the old machine
    rewritten), the web's tables into this machine's database, the gateway's public URL to this one."""
    state_dir, home = Path(state_dir), Path(home)
    if not force and (state_dir / "scheduler.sqlite3").exists():
        raise StackError("máy này đã có task (scheduler.sqlite3): thêm --force để ghi đè bằng bản chuyển sang")
    report: dict[str, Any] = {"state": str(state_dir), "home": str(home)}
    state_dir.mkdir(parents=True, exist_ok=True)
    # Inside the state folder: same disk (files are moved, not copied), and writable by Kite's user.
    with tempfile.TemporaryDirectory(prefix=".kite-import-", dir=state_dir) as tmp:
        root = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(root, filter="data")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        old_home, old_state = manifest.get("home", ""), manifest.get("state_dir", "")
        backup = None
        if (state_dir / "config.yaml").exists():
            backup = state_dir / f"config.yaml.before-import-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(state_dir / "config.yaml", backup)
        for source, target in ((root / "state", state_dir), (root / "home", home)):
            if source.is_dir():
                _merge(source, target)
        report["config_backup"] = str(backup) if backup else None
        report["repos"] = manifest.get("repos", [])
    config = state_dir / "config.yaml"
    replacements = [(old, new) for old, new in ((old_state, str(state_dir)), (old_home, str(home))) if old and old != new]
    report["rewritten"] = _rewrite_paths(config, home / "kite-chat" / "opencode.json", replacements)
    report["harness2"] = _image_harness2(config)
    if public:
        report["public_url"] = public if set_public_url(config, public) else "không đổi"
    if sandbox_gateway and config.is_file():        # this machine's sandbox network, not the old one's
        report["sandbox_gateway"] = sandbox_gateway if _set_sandbox_gateway(config, sandbox_gateway) else "không đổi"
    sqlite_db = state_dir / "web.sqlite3"
    if web_db and sqlite_db.exists():
        report["web_db"] = copy_web_tables(sqlite_db, web_db, replacements)
    secrets = state_dir / "secrets"
    if secrets.is_dir():
        secrets.chmod(0o700)
        for item in secrets.iterdir():
            if item.is_file():
                item.chmod(0o600)
    return report


def _merge(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        dest = target / item.name
        if item.is_dir() and not item.is_symlink():
            if dest.exists() and not dest.is_dir():
                dest.unlink()
            _merge(item, dest)
        else:
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            shutil.move(str(item), str(dest))


def _rewrite_paths(config: Path, chat: Path, replacements: list[tuple[str, str]]) -> list[str]:
    """Old machine paths (home folder, state folder, Kite's checkout) in the config and the chat's
    opencode.json; the chat's own command is written again by the opencode container at start."""
    done = []
    for path in (config, chat):
        if not path.is_file():
            continue
        text = original = path.read_text(encoding="utf-8")
        for old, new in replacements:
            text = re.sub(re.escape(old.rstrip("/")) + r"(?=[/\"'\s:,\]]|$)", new.rstrip("/"), text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            done.append(str(path))
    return done


def _image_harness2(config: Path) -> str:
    import yaml

    if not config.is_file():
        return "không có config"
    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    wanted = {"checkout": str(HARNESS2_CHECKOUT), "cli": str(Path(sys.executable).parent / "harness2")}
    opencode = raw.setdefault("opencode", {})
    if raw.get("harness2") == wanted and opencode.get("executable") == "opencode":
        return "không đổi"
    # Harness² and OpenCode of the image (the old machine had them in its home folder or checkout).
    raw["harness2"] = wanted
    opencode["executable"] = "opencode"
    config.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return "đã trỏ vào bản trong image"


def copy_web_tables(sqlite_db: Path, target_url: str, replacements: list[tuple[str, str]] = ()) -> dict:
    """The web's account, audit log, repositories and settings from the old machine's SQLite into this
    machine's database (Postgres). Sessions and sign-in attempts stay behind: every browser signs in
    again on the new address anyway."""
    from sqlalchemy import Integer, delete, insert, select

    from .web import db

    source = db.Store(db.connect(db.sqlite_url(sqlite_db)))
    target = db.Store(db.connect(target_url))
    target.migrate()
    counts = {}
    tables = [db.users, db.audit_log, db.repos, db.settings]
    with source.engine.connect() as src, target.engine.begin() as dst:
        for table in reversed(tables):
            dst.execute(delete(table))
        for table in tables:
            rows = [dict(row._mapping) for row in src.execute(select(table))]
            if table is db.repos:
                for row in rows:
                    for old, new in replacements:
                        if row["path"] == old or row["path"].startswith(old.rstrip("/") + "/"):
                            row["path"] = new.rstrip("/") + row["path"][len(old.rstrip("/")):]
            if rows:
                dst.execute(insert(table), rows)
            counts[table.name] = len(rows)
        if target.engine.dialect.name == "postgresql":
            for table in tables:   # the copied ids moved past the sequences Postgres keeps for them
                if "id" in table.c and isinstance(table.c.id.type, Integer):
                    dst.exec_driver_sql(f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                                        f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)")
    return counts
