#!/usr/bin/env python3
"""kite-stack: install, update and run Kite's Docker Compose deployment on this machine (as root).

    curl -fsSL https://raw.githubusercontent.com/<owner>/kite/main/deploy/stack/install.sh | sudo bash
    sudo kite-stack admin                 # the owner account (password + 2FA app)
    sudo kite-stack status | update | logs [service] | restart [service...] | kite <args>
    sudo kite-stack import kite-export.tgz            # move a machine (kite stack export on the old one)

Releases. CI pushes ghcr.io/<owner>/kite and kite-sandbox for every green commit of main and moves their
``stable`` tag. ``update`` (a systemd timer, every 10 minutes) pulls ``stable``; when its digest is new it
copies the compose files out of the new image into ``releases/<id>`` (so compose files and image always go
together), pins both images by digest there (images.env), points ``current`` at it and starts it. If a
Kite service is not healthy within the deadline, ``current`` goes back to the previous release, which
starts again; that digest is not tried again automatically. A second timer, ``kite-stack-watchdog``
(every 5 minutes), restarts any service confirmed unhealthy on two ticks in a row, at most 3 restarts
of one service per hour — Docker's restart policy cannot fix a container that runs but stays unhealthy,
so the watchdog does. Nothing is updated while a task, a bench run
or a chat answer is in progress (``kite busy``), unless asked with --force. When stack.env carries
``KITE_VERIFY_IMAGE_SIGNATURES=1`` (stamped in at install), both new images must first prove the
cosign signature of this repository's CI (keyless, GitHub OIDC): an unsigned or wrongly signed image
is refused and the running release stays untouched.

The web and the chat cannot run Docker: they leave request files in ``requests/`` (a systemd path unit
starts ``kite-stack requests``) and read ``status.json``. stack.env (0600) keeps the secrets of the
machine (Postgres passwords) and its settings (address, access mode, auto update).

Standard library only (Python 3.10+ for Ubuntu 22.04): this runs on the host, outside the image.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import grp
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Any, Callable

DATA = Path("/var/lib/kite")             # fixed: the task sandboxes get host paths for their mounts
LIB = Path("/usr/local/lib/kite-stack")
BIN = Path("/usr/local/bin/kite-stack")
UNIT_DIR = Path("/etc/systemd/system")
UNITS = ("kite-stack-update.service", "kite-stack-update.timer",
         "kite-stack-requests.service", "kite-stack-requests.path",
         "kite-stack-watchdog.service", "kite-stack-watchdog.timer")
DEFAULT_IMAGE = "ghcr.io/23521549-lang/kite"
KITE_SERVICES = ("gateway", "worker", "web", "opencode")
INFRA_SERVICES = ("postgres", "temporal", "caddy")
ONE_SHOT = ("temporal-setup", "web-db")
WATCHDOG_SERVICES = (*KITE_SERVICES, *INFRA_SERVICES)
WATCHDOG_INTERVAL = 300.0        # the timer fires every 5 minutes
WATCHDOG_CONFIRM_TICKS = 2       # bad on 2 ticks in a row before a restart
WATCHDOG_COOLDOWN = 900.0        # 15 minutes between restarts of the same service
WATCHDOG_MAX_RESTARTS = 3        # at most 3 restarts of one service...
WATCHDOG_WINDOW = 3600.0         # ...in a rolling hour
UID = 1000
LOCAL_RELEASE = "kite-release"           # local tags that keep the images of kept releases
SANDBOX_TAG = "kite-sandbox:latest"      # the name Kite's config uses for the task sandbox
SANDBOX_NETWORK = "kite-sandbox-net"     # internal: the task agents reach only the gateway on it
SANDBOX_SUBNET, SANDBOX_GATEWAY = "172.30.230.0/24", "172.30.230.1"
SETTINGS = ("KITE_ACCESS", "KITE_DOMAIN", "KITE_HTTPS_PORT", "KITE_IMAGE_REPO", "KITE_SANDBOX_REPO",
            "KITE_CHANNEL", "KITE_AUTO_UPDATE", "TZ")

Runner = Callable[..., subprocess.CompletedProcess]


class StackError(RuntimeError):
    pass


def default_runner(cmd: list[str], *, timeout: float | None = None, capture: bool = True,
                   tty: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    if tty:
        return subprocess.run(cmd, timeout=timeout, env=env)
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout, env=env)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(path: Path, values: dict[str, str], header: str, mode: int = 0o600) -> None:
    lines = [f"# {line}" for line in header.splitlines()] + [f"{k}={v}" for k, v in values.items()]
    atomic_write(path, "\n".join(lines) + "\n", mode)


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(temp, mode)
    os.replace(temp, path)


def short(digest: str) -> str:
    return digest.split(":", 1)[-1][:12]


COSIGN_ISSUER = "https://token.actions.githubusercontent.com"
SECRET_KEYS = ("TEMPORAL_POSTGRES_PASSWORD", "KITE_WEB_DB_PASSWORD")


def find_cosign() -> str | None:
    """The cosign binary on this machine (a module-level seam: tests replace this)."""
    return shutil.which("cosign")


def signature_identity(image_repo: str) -> str:
    """The certificate identity CI signs with: this repository's ci.yml workflow on GitHub. The
    image repo is ghcr.io/<owner>/<repo>; both images are signed by that one workflow."""
    path = image_repo.split("/", 1)[1] if "/" in image_repo else image_repo
    return rf"^https://github\.com/{re.escape(path)}/\.github/workflows/ci\.yml@"


def generated_secrets(env: dict[str, str]) -> dict[str, str]:
    """The machine's passwords: the ones it already has are kept word for word, the missing ones
    are made here. One for the Postgres role Temporal signs in with (server, visibility store and
    schema setup are the same role, so one password), one for the web database's own role."""
    return {key: env.get(key) or secrets.token_urlsafe(32) for key in SECRET_KEYS}


def watchdog_classify(state: dict) -> str:
    """One service, one snapshot from services_state(): "good", "bad" or "unknown".

    The rule mirrors wait_healthy's: running and healthy (or no healthcheck) is good; running but
    unhealthy — or stuck starting, exited, dead, or restarting — is bad. Anything else (created,
    paused, missing) is unknown: the updater or an operator owns it, and the watchdog must not
    restart what it cannot see clearly. Docker's own restart policy owns crash loops; the watchdog
    owns the two states that policy cannot fix: running-but-unhealthy, and exited-and-not-restarted.
    """
    st, health = state.get("state", ""), state.get("health", "")
    if st == "running" and health in ("", "healthy"):
        return "good"
    if (st == "running" and health in ("unhealthy", "starting")) or st in ("exited", "dead", "restarting"):
        return "bad"
    return "unknown"


def watchdog_decide(states: dict[str, dict], history: dict[str, dict], now: float, *,
                    confirm_ticks: int = WATCHDOG_CONFIRM_TICKS,
                    cooldown: float = WATCHDOG_COOLDOWN,
                    max_restarts: int = WATCHDOG_MAX_RESTARTS,
                    window: float = WATCHDOG_WINDOW) -> tuple[list[dict], dict[str, dict]]:
    """What the watchdog does this tick, and the history to keep. Pure: no Docker, no restarts.

    history is {"services": {name: {"bad_ticks": int, "last_restart": float | None,
    "restarts": [float, ...], "alerted": bool}}}. A service restarts only after being bad on
    confirm_ticks ticks in a row, at least cooldown seconds after its last restart, and fewer than
    max_restarts successful restarts inside the rolling window. Anything else bad is watched (first
    ticks) or suppressed (limits hit): suppressed ticks also raise an alert, once per episode.
    A good tick resets the bad counter and the alert flag; an unknown tick changes nothing.
    Only successful restarts count towards the limit — a failed restart is retried next tick.
    Returns (actions, new_history); actions are {"service", "action", "reason"} with action in
    ("restart", "suppressed", "watch"). The caller records a restart in the history only after
    it really happened (see Stack.watchdog).
    """
    services = history.get("services", {})
    new_services: dict[str, dict] = {}
    actions: list[dict] = []
    for name in WATCHDOG_SERVICES:
        entry = dict(services.get(name, {}))
        entry.setdefault("bad_ticks", 0)
        entry.setdefault("last_restart", None)
        entry.setdefault("restarts", [])
        entry.setdefault("alerted", False)
        entry["restarts"] = [t for t in entry["restarts"] if now - t < window]
        new_services[name] = entry
        kind = watchdog_classify(states.get(name, {}))
        if kind == "good":
            entry["bad_ticks"] = 0
            entry["alerted"] = False
            continue
        if kind == "unknown":
            continue
        entry["bad_ticks"] = int(entry["bad_ticks"]) + 1
        if entry["bad_ticks"] < confirm_ticks:
            actions.append({"service": name, "action": "watch",
                            "reason": f"xấu {entry['bad_ticks']}/{confirm_ticks} lần kiểm liên tiếp"})
            continue
        last = entry["last_restart"]
        if last is not None and now - float(last) < cooldown:
            actions.append({"service": name, "action": "suppressed",
                            "reason": f"mới khởi động lại {int(now - float(last))}s trước, "
                                      f"chờ đủ {int(cooldown // 60)} phút"})
            continue
        if len(entry["restarts"]) >= max_restarts:
            actions.append({"service": name, "action": "suppressed",
                            "reason": f"đã khởi động lại {len(entry['restarts'])} lần trong "
                                      f"{int(window // 60)} phút qua (tối đa {max_restarts})"})
            continue
        actions.append({"service": name, "action": "restart",
                        "reason": f"xấu {entry['bad_ticks']} lần kiểm liên tiếp"})
    return actions, {**history, "services": new_services}


class Stack:
    def __init__(self, data: Path = DATA, *, run: Runner = default_runner, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print,
                 health_timeout: float = 300.0) -> None:
        self.data, self.run_cmd, self.clock, self.sleep, self.out = Path(data), run, clock, sleep, out
        self.health_timeout = health_timeout
        self.dir = self.data / "stack"
        self.env_file = self.dir / "stack.env"
        self.releases = self.dir / "releases"
        self.current_link = self.dir / "current"
        self.previous_link = self.dir / "previous"
        self.status_file = self.dir / "status.json"
        self.requests_dir = self.dir / "requests"

    # ------------------------------------------------------------------ small helpers
    def run(self, cmd: list[str], *, timeout: float | None = 600, check: bool = False, **kw) -> subprocess.CompletedProcess:
        done = self.run_cmd(cmd, timeout=timeout, **kw)
        if check and done.returncode != 0:
            detail = ((done.stderr or "") + (done.stdout or "")).strip()[-800:] if kw.get("capture", True) else ""
            raise StackError(f"{' '.join(cmd[:4])}... lỗi ({done.returncode}): {detail}")
        return done

    def env(self) -> dict[str, str]:
        return read_env(self.env_file)

    def release(self, link: Path | None = None) -> Path | None:
        link = link or self.current_link
        return link.resolve() if link.is_symlink() and link.resolve().is_dir() else None

    def release_info(self, release: Path | None) -> dict:
        if release is None:
            return {}
        try:
            return json.loads((release / "release.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def compose(self, *args: str, release: Path | None = None) -> list[str]:
        release = release or self.release()
        if release is None:
            raise StackError("chưa có bản nào được cài (sudo kite-stack install)")
        files = ["-f", str(release / "deploy" / "stack" / "compose.yaml")]
        for extra in filter(None, os.environ.get("KITE_STACK_OVERRIDE", "").split(":")):
            files += ["-f", extra]                     # tests and local experiments only
        return ["docker", "compose", "-p", "kite", "--project-directory", str(release / "deploy" / "stack"),
                "--env-file", str(self.env_file), "--env-file", str(release / "images.env"), *files, *args]

    # ------------------------------------------------------------------ status for the web
    def status(self) -> dict:
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_status(self, **fields: Any) -> dict:
        data = self.status()
        data.update(fields)
        data["updated"] = self.clock()
        data["auto"] = self.env().get("KITE_AUTO_UPDATE", "1") == "1"
        current = self.release_info(self.release())
        if current:
            data["current"] = {"digest": current.get("kite_digest"), "revision": current.get("revision"),
                               "created": current.get("created"), "id": current.get("id"),
                               "since": current.get("activated")}
        atomic_write(self.status_file, json.dumps(data, indent=2), 0o644)
        return data

    def finish(self, action: str, result: str, message: str = "", **extra: Any) -> dict:
        self.out(f"{action}: {result}" + (f" — {message}" if message else ""))
        return self.set_status(state="idle", last={"action": action, "result": result, "message": message[:1500],
                                                   "at": self.clock(), **extra})

    @contextlib.contextmanager
    def lock(self, wait: float = 0.0):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as handle:
            deadline = self.clock() + wait
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self.clock() >= deadline:
                        raise StackError("kite-stack khác đang chạy (cập nhật hoặc khởi động lại)") from None
                    self.sleep(5)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ images and releases
    def image_refs(self) -> tuple[str, str]:
        env = self.env()
        repo = env.get("KITE_IMAGE_REPO") or DEFAULT_IMAGE
        sandbox = env.get("KITE_SANDBOX_REPO") or f"{repo}-sandbox"
        channel = env.get("KITE_CHANNEL") or "stable"
        return f"{repo}:{channel}", f"{sandbox}:{channel}"

    def inspect(self, ref: str) -> dict:
        done = self.run(["docker", "image", "inspect", ref], timeout=60, check=True)
        data = json.loads(done.stdout)
        return data[0] if isinstance(data, list) and data else {}

    def digest_of(self, ref: str) -> str:
        """The registry digest of a pulled image (repo@sha256:...) for the repository in ``ref``."""
        repo = ref.rsplit("@", 1)[0]
        repo = repo.rsplit(":", 1)[0] if ":" in repo.rsplit("/", 1)[-1] else repo
        for item in self.inspect(ref).get("RepoDigests") or []:
            if item.startswith(repo + "@"):
                return item.split("@", 1)[1]
        raise StackError(f"{ref} không có digest của {repo} (image dựng tại chỗ?)")

    def pull(self) -> dict:
        kite_ref, sandbox_ref = self.image_refs()
        for ref in (kite_ref, sandbox_ref):
            self.run(["docker", "pull", "-q", ref], timeout=1800, check=True)
        info = self.inspect(kite_ref)
        labels = (info.get("Config") or {}).get("Labels") or {}
        return {"kite_ref": kite_ref, "kite_digest": self.digest_of(kite_ref),
                "sandbox_ref": sandbox_ref, "sandbox_digest": self.digest_of(sandbox_ref),
                "revision": (labels.get("org.opencontainers.image.revision") or "")[:12],
                "created": labels.get("org.opencontainers.image.created") or info.get("Created", "")}

    def cosign_env(self) -> dict[str, str]:
        """cosign under systemd: a unit without User= gets no HOME, and cosign needs one (Sigstore's
        trust root cache, registry logins in ~/.docker). HOME is root's when missing; the trust root
        cache (public data) sits in the stack folder."""
        env = dict(os.environ)
        if not env.get("HOME"):
            env["HOME"] = "/root"
        env["TUF_ROOT"] = str(self.dir / "sigstore")
        return env

    def verify_signatures(self, new: dict, env: dict[str, str] | None = None) -> tuple[bool, str]:
        """Both new images must carry this repo's CI signature before either may run. Only when
        stack.env asks for it (``KITE_VERIFY_IMAGE_SIGNATURES=1``): machines installed before CI
        signed its images keep updating until the owner turns it on. A failed check — or no cosign
        on the machine — refuses the images; nothing about the running release changes."""
        env = self.env() if env is None else env
        if env.get("KITE_VERIFY_IMAGE_SIGNATURES", "0") != "1":
            return True, ""
        cosign = find_cosign()
        if cosign is None:
            return False, ("máy chưa cài cosign: cài cosign rồi chạy lại, hoặc tắt "
                           "KITE_VERIFY_IMAGE_SIGNATURES trong stack.env")
        kite_repo = new["kite_ref"].rsplit(":", 1)[0]
        sandbox_repo = new["sandbox_ref"].rsplit(":", 1)[0]
        cosign_env = self.cosign_env()
        for pinned in (f"{kite_repo}@{new['kite_digest']}", f"{sandbox_repo}@{new['sandbox_digest']}"):
            done = self.run([cosign, "verify", "--certificate-identity-regexp",
                             signature_identity(kite_repo),
                             "--certificate-oidc-issuer", COSIGN_ISSUER, pinned], timeout=300, env=cosign_env)
            if done.returncode != 0:
                detail = ((done.stderr or "") + (done.stdout or "")).strip()[-400:]
                return False, f"{pinned} không đúng chữ ký của CI repo này: {detail}"
        return True, ""

    def prepare(self, new: dict) -> Path:
        """releases/<id>: the deploy/ folder of the new image, images.env with both images pinned."""
        kite_repo = new["kite_ref"].rsplit(":", 1)[0]
        sandbox_repo = new["sandbox_ref"].rsplit(":", 1)[0]
        pinned = f"{kite_repo}@{new['kite_digest']}"
        release_id = short(new["kite_digest"])
        target = self.releases / release_id
        if target.exists():
            shutil.rmtree(target)
        staging = self.releases / f".{release_id}.new"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        name = f"kite-stack-extract-{release_id}"
        self.run(["docker", "rm", "-f", name], timeout=60)
        self.run(["docker", "create", "--name", name, pinned], timeout=120, check=True)
        try:
            self.run(["docker", "cp", f"{name}:/opt/kite/deploy", str(staging / "deploy")], timeout=300, check=True)
        finally:
            self.run(["docker", "rm", "-f", name], timeout=60)
        write_env(staging / "images.env",
                  {"KITE_IMAGE": pinned, "KITE_SANDBOX_IMAGE": f"{sandbox_repo}@{new['sandbox_digest']}"},
                  "Images of this release, pinned by digest (kite-stack).", 0o644)
        record = {**new, "id": release_id, "prepared": self.clock()}
        atomic_write(staging / "release.json", json.dumps(record, indent=2), 0o644)
        staging.rename(target)
        for repo, digest, name in ((kite_repo, new["kite_digest"], "kite"),
                                   (sandbox_repo, new["sandbox_digest"], "sandbox")):
            self.run(["docker", "tag", f"{repo}@{digest}", f"{LOCAL_RELEASE}/{name}:{release_id}"], timeout=60)
        return target

    def activate(self, release: Path, *, previous: Path | None = None) -> None:
        """Point ``current`` at a release (``previous`` at the one it replaces) and tag its sandbox image
        with the name Kite's config uses."""
        info = self.release_info(release)
        sandbox = read_env(release / "images.env").get("KITE_SANDBOX_IMAGE")
        if sandbox:
            self.run(["docker", "tag", sandbox, SANDBOX_TAG], timeout=60, check=True)
        old = self.release()
        keep = previous if previous is not None else (old if old and old != release else None)
        for link, target in ((self.previous_link, keep), (self.current_link, release)):
            if target is None:
                continue
            temp = link.with_name(link.name + ".new")
            if temp.is_symlink() or temp.exists():
                temp.unlink()
            temp.symlink_to(target)
            os.replace(temp, link)
        info["activated"] = self.clock()
        atomic_write(release / "release.json", json.dumps(info, indent=2), 0o644)

    def prune(self) -> list[str]:
        keep = {p for p in (self.release(), self.release(self.previous_link)) if p is not None}
        removed = []
        for release in sorted(self.releases.glob("*")) if self.releases.is_dir() else []:
            if release in keep or release.name.startswith("."):
                continue
            for name in ("kite", "sandbox"):
                self.run(["docker", "rmi", f"{LOCAL_RELEASE}/{name}:{release.name}"], timeout=120)
            shutil.rmtree(release, ignore_errors=True)
            removed.append(release.name)
        self.run(["docker", "image", "prune", "-f"], timeout=600)
        return removed

    # ------------------------------------------------------------------ health
    def services_state(self) -> dict[str, dict]:
        done = self.run(self.compose("ps", "-a", "--format", "json"), timeout=60)
        states: dict[str, dict] = {}
        text = (done.stdout or "").strip()
        rows: list = []
        if text.startswith("["):
            with contextlib.suppress(ValueError):
                rows = json.loads(text)
        else:
            for line in text.splitlines():
                with contextlib.suppress(ValueError):
                    rows.append(json.loads(line))
        for row in rows:
            states[row.get("Service", "")] = {"state": row.get("State", ""), "health": row.get("Health", ""),
                                              "exit": row.get("ExitCode", 0)}
        return states

    def wait_healthy(self, timeout: float | None = None, services: tuple[str, ...] | None = None) -> tuple[bool, str]:
        """Every Kite service running and healthy, Postgres, Temporal and Caddy running, the one-shot
        setups finished well. Fails early when a service keeps failing."""
        wanted = services or (*KITE_SERVICES, *INFRA_SERVICES)
        deadline = self.clock() + (timeout or self.health_timeout)
        strikes: dict[str, int] = {}
        problems: list[str] = []
        while True:
            states = self.services_state()
            problems = []
            for name in ONE_SHOT if services is None else ():
                st = states.get(name)
                if st and st["state"] == "exited" and int(st.get("exit") or 0) != 0:
                    problems.append(f"{name}: lỗi (exit {st['exit']})")
                    strikes[name] = 99
            for name in wanted:
                st = states.get(name)
                if st is None:
                    problems.append(f"{name}: chưa có container")
                elif st["state"] != "running":
                    problems.append(f"{name}: {st['state']}")
                    if st["state"] in ("exited", "dead", "restarting"):
                        strikes[name] = strikes.get(name, 0) + 1
                elif st["health"] not in ("", "healthy"):
                    problems.append(f"{name}: {st['health']}")
                    if st["health"] == "unhealthy":
                        strikes[name] = strikes.get(name, 0) + 1
                else:
                    strikes.pop(name, None)
            if not problems:
                return True, "mọi dịch vụ khỏe"
            if any(v >= 4 for v in strikes.values()) or self.clock() >= deadline:
                break
            self.sleep(5)
        bad = [p.split(":", 1)[0] for p in problems]
        logs = ""
        kite_bad = [s for s in bad if s in (*KITE_SERVICES, *INFRA_SERVICES, *ONE_SHOT)]
        if kite_bad:
            done = self.run(self.compose("logs", "--no-color", "--tail", "25", *kite_bad[:3]), timeout=60)
            logs = (done.stdout or "")[-1500:]
        return False, "; ".join(problems) + (f"\n{logs}" if logs else "")

    # ------------------------------------------------------------------ watchdog
    def watchdog_history(self) -> dict:
        """The watchdog's memory: per-service bad ticks, restarts and alert flags. Only timestamps
        and service names — no secrets — so a corrupt file is simply forgotten, not fatal."""
        try:
            data = json.loads((self.dir / "watchdog.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _watchdog_save(self, history: dict, now: float) -> None:
        history["updated"] = now
        atomic_write(self.dir / "watchdog.json", json.dumps(history, indent=2), 0o644)

    def watchdog_notify(self, title: str, message: str, *, priority: int = 3,
                        opener: Callable = urllib.request.urlopen) -> bool:
        """Push one alert through the owner's ntfy topic, if `kite notify setup` was run: the topic
        URL lives in a 0600 secret file, referenced from the machine's config as file:PATH. Returns
        False when notifications are not set up or the push fails — never raises, so a dead alert
        channel cannot kill the watch. (Same JSON publishing as poc notify.send, reimplemented here
        because this file runs on the host, stdlib only, and cannot import the package.)"""
        try:
            config = (self.data / "kite" / "config.yaml").read_text(encoding="utf-8")
        except OSError:
            return False
        match = re.search(r"ntfy:\s*['\"]?file:([^\s'\"},]+)", config)
        if not match:
            return False
        try:
            url = Path(match.group(1)).read_text(encoding="utf-8").strip()
        except OSError:
            return False
        parsed = urllib.parse.urlsplit(url)
        topic = parsed.path.strip("/")
        if parsed.scheme not in ("https", "http") or not parsed.netloc or not topic or "/" in topic:
            return False
        body = json.dumps({"topic": topic, "title": title[:250], "message": message[:3000],
                           "tags": ["watchdog"], "priority": priority}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(f"{parsed.scheme}://{parsed.netloc}/", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with opener(request, timeout=10) as response:
                response.read()
        except OSError:
            return False
        return True

    def watchdog(self, *, check_only: bool = False, notify: Callable[..., bool] | None = None) -> dict:
        """One watch tick: snapshot every service, restart the ones confirmed unhealthy, within the
        flap limits. Returns a summary; every decision is also logged (self.out goes to the journal).
        check_only says what would happen and changes nothing: no restarts, no state file, no alerts.
        """
        send = notify or (lambda title, message, priority=3: self.watchdog_notify(title, message,
                                                                                  priority=priority))
        now = self.clock()
        summary: dict[str, Any] = {"checked_at": now, "restarted": [], "suppressed": [], "watching": [],
                                   "unknown": [], "would_restart": [], "failed": [], "alerts": 0, "error": ""}
        try:
            with self.lock():
                pass
        except StackError:
            # An update, a rollback or a restart is running: its services are down on purpose, and
            # a bad tick counted now would restart a freshly updated service on the next one.
            self.out("watchdog: kite-stack khác đang chạy (cập nhật hoặc khởi động lại) — bỏ qua lần kiểm này")
            summary["skipped"] = "kite-stack khác đang chạy"
            return summary
        history = self.watchdog_history()
        states = self.services_state()

        def alert(title: str, message: str, priority: int) -> bool:
            sent = send(title, message, priority=priority)
            summary["alerts"] += 1 if sent else 0
            if not sent:
                self.out("watchdog: chưa nối kênh cảnh báo (chạy `kite notify setup` để nhận push)")
            return sent

        if not states:
            self.out("watchdog: không đọc được trạng thái container (docker chưa chạy?)")
            summary["error"] = "không đọc được trạng thái container"
            if not history.get("alerted_no_states"):
                alert("Kite: watchdog không đọc được container",
                      "kite-stack watchdog không lấy được trạng thái dịch vụ — docker có thể đã chết.", 4)
                history["alerted_no_states"] = True
            if not check_only:
                self._watchdog_save(history, now)
            return summary
        history["alerted_no_states"] = False
        actions, history = watchdog_decide(states, history, now)
        for name in WATCHDOG_SERVICES:
            kind = watchdog_classify(states.get(name, {}))
            if kind == "unknown":
                summary["unknown"].append(name)
                history["services"][name]["last_result"] = "unknown"
            elif kind == "good":
                history["services"][name]["last_result"] = "ok"
        for action in actions:
            name, kind = action["service"], action["action"]
            entry = history["services"][name]
            if kind == "watch":
                summary["watching"].append(name)
                entry["last_result"] = "watching"
                self.out(f"watchdog: {name} {action['reason']} — theo dõi, chưa khởi động lại")
            elif kind == "suppressed":
                summary["suppressed"].append(name)
                entry["last_result"] = "suppressed"
                self.out(f"watchdog: {name} {action['reason']} — KHÔNG khởi động lại")
                if not entry["alerted"]:
                    alert(f"Kite: watchdog không khởi động lại được {name}",
                          f"{name} vẫn xấu nhưng {action['reason']}. Cần người xem.", 4)
                    # One attempt per episode, sent or not: otherwise an unconfigured ntfy would
                    # log "chưa nối kênh" every 5 minutes for as long as the service stays bad.
                    # A good tick (or a successful restart) clears the flag for the next episode.
                    entry["alerted"] = True
            else:  # restart
                if check_only:
                    summary["would_restart"].append(name)
                    self.out(f"watchdog: {name} {action['reason']} — sẽ khởi động lại (chỉ kiểm tra)")
                    continue
                self.out(f"watchdog: {name} {action['reason']} — khởi động lại")
                try:
                    report = self.restart([name], allow_infra=True)
                except (StackError, subprocess.SubprocessError, OSError) as exc:
                    entry["last_result"] = "failed"
                    summary["failed"].append(name)
                    self.out(f"watchdog: khởi động lại {name} lỗi: {exc}")
                    continue
                last = report.get("last") or {}
                if last.get("result") == "restarted":
                    entry["restarts"].append(now)
                    entry["last_restart"] = now
                    entry["bad_ticks"] = 0
                    entry["alerted"] = False
                    entry["last_result"] = "restarted"
                    summary["restarted"].append(name)
                    alert(f"Kite: đã khởi động lại {name}",
                          f"watchdog thấy {name} xấu {WATCHDOG_CONFIRM_TICKS} lần kiểm liên tiếp nên đã "
                          f"khởi động lại.", 3)
                elif last.get("restarted"):
                    # The container was restarted but is still not healthy: it counts as a restart
                    # (cooldown and the hourly limit apply), and the owner is told louder.
                    entry["restarts"].append(now)
                    entry["last_restart"] = now
                    entry["bad_ticks"] = 0
                    entry["last_result"] = "failed"
                    summary["failed"].append(name)
                    self.out(f"watchdog: {name} đã khởi động lại nhưng vẫn chưa khỏe: "
                             f"{str(last.get('message', ''))[:500]}")
                    alert(f"Kite: {name} khởi động lại rồi vẫn chưa khỏe",
                          f"watchdog đã khởi động lại {name} nhưng nó vẫn chưa khỏe. Cần người xem.", 4)
                else:
                    # The compose restart itself failed: nothing restarted, retry next tick.
                    entry["last_result"] = "failed"
                    summary["failed"].append(name)
                    self.out(f"watchdog: khởi động lại {name} lỗi: {str(last.get('message', ''))[:500]}")
        if not check_only:
            self._watchdog_save(history, now)
        return summary

    def sandbox_network(self) -> str:
        """The tasks' internal network, made before the gateway starts (it also listens on the host's
        address there). An existing one is kept: its address goes into Kite's config."""
        fmt = "{{.Internal}} {{(index .IPAM.Config 0).Gateway}}"
        done = self.run(["docker", "network", "inspect", "-f", fmt, SANDBOX_NETWORK], timeout=60)
        if done.returncode != 0:
            self.run(["docker", "network", "create", "--internal", "--subnet", SANDBOX_SUBNET,
                      "--gateway", SANDBOX_GATEWAY, SANDBOX_NETWORK], timeout=60, check=True)
            return SANDBOX_GATEWAY
        internal, _, gateway = done.stdout.strip().partition(" ")
        if internal != "true":
            raise StackError(f"mạng {SANDBOX_NETWORK} có sẵn nhưng không --internal: xóa nó rồi chạy lại")
        return gateway or SANDBOX_GATEWAY

    def up(self, release: Path | None = None) -> tuple[bool, str]:
        self.sandbox_network()
        done = self.run(self.compose("up", "-d", "--remove-orphans", release=release), timeout=1800)
        if done.returncode != 0:
            return False, "docker compose up lỗi: " + ((done.stderr or "") + (done.stdout or "")).strip()[-1200:]
        return self.wait_healthy()

    # ------------------------------------------------------------------ busy?
    def busy(self) -> dict:
        """Running tasks, a bench run, a chat answering: asked inside the running gateway (else a new
        container of the current release)."""
        for cmd in (self.compose("exec", "-T", "gateway", "kite", "busy"),
                    self.compose("run", "--rm", "--no-deps", "-T", "gateway", "kite", "busy")):
            done = self.run(cmd, timeout=180)
            if done.returncode == 0:
                with contextlib.suppress(ValueError):
                    return json.loads(done.stdout)
        return {"busy": False, "unknown": True}

    # ------------------------------------------------------------------ update
    def update(self, *, force: bool = False, timer: bool = False, check_only: bool = False) -> dict:
        try:
            with self.lock(wait=0 if timer else 900):
                return self._update(force=force, timer=timer, check_only=check_only)
        except StackError as exc:
            if timer and "đang chạy" in str(exc):
                return {"skipped": str(exc)}
            raise

    def _update(self, *, force: bool, timer: bool, check_only: bool) -> dict:
        env = self.env()
        if timer and env.get("KITE_AUTO_UPDATE", "1") != "1":
            return self.set_status(state="idle")
        self.set_status(state="checking")
        try:
            new = self.pull()
        except (StackError, subprocess.SubprocessError) as exc:
            return self.finish("update", "failed", f"không tải được image: {exc}")
        self.set_status(available={"digest": new["kite_digest"], "revision": new["revision"],
                                   "created": new["created"], "checked": self.clock()})
        current = self.release_info(self.release())
        if current.get("kite_digest") == new["kite_digest"] and current.get("sandbox_digest") == new["sandbox_digest"]:
            return self.finish("update", "up_to_date", f"đang chạy {new['revision'] or short(new['kite_digest'])}")
        if check_only:
            return self.finish("check", "available", f"có bản mới {new['revision'] or short(new['kite_digest'])}")
        bad = self.status().get("bad_digests") or []
        if new["kite_digest"] in bad and not force:
            return self.finish("update", "failed", f"bản {new['revision']} đã lỗi lúc thử trước; chờ bản sau "
                                                   "(hoặc kite-stack update --force)")
        if not force and current:
            busy = self.busy()
            if busy.get("busy"):
                what = ", ".join(filter(None, [
                    f"task {', '.join(busy.get('tasks') or [])}" if busy.get("tasks") else "",
                    f"lần đo {busy['bench'].get('label')}" if busy.get("bench") else "",
                    "chat đang trả lời" if busy.get("chat") else ""]))
                return self.finish("update", "busy", f"hoãn vì đang chạy: {what}; thử lại lần sau")
        signed, why = self.verify_signatures(new, env)
        if not signed:
            return self.finish("update", "failed", f"bản mới bị từ chối, bản đang chạy giữ nguyên: {why}")
        old = self.release()
        release = self.prepare(new)
        self.set_status(state="updating")
        self.activate(release)
        ok, detail = self.up()
        if ok:
            self.adopt(release)
            removed = self.prune()
            return self.finish("update", "updated", f"đã chạy {new['revision'] or release.name}", removed=removed)
        bad = (self.status().get("bad_digests") or [])[-9:] + [new["kite_digest"]]
        self.set_status(state="rolling_back", bad_digests=bad)
        if old is None:
            return self.finish("update", "failed", f"bản mới không khỏe: {detail}")
        self.activate(old, previous=release)
        ok_back, back = self.up()
        message = f"bản {new['revision'] or release.name} không khỏe: {detail}"
        if not ok_back:
            message += f"\nbản cũ cũng chưa khỏe: {back}"
        return self.finish("update", "rolled_back", message)

    def adopt(self, release: Path) -> None:
        """Use this release's kite-stack (and systemd units) from now on: only once it ran healthy."""
        source = release / "deploy" / "stack"
        LIB.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "kite_stack.py", LIB / "kite_stack.py.new")
        os.replace(LIB / "kite_stack.py.new", LIB / "kite_stack.py")
        os.chmod(LIB / "kite_stack.py", 0o755)
        if not BIN.exists():
            BIN.symlink_to(LIB / "kite_stack.py")
        changed = False
        for unit in UNITS:
            new = (source / "systemd" / unit).read_text(encoding="utf-8")
            path = UNIT_DIR / unit
            if not path.exists() or path.read_text(encoding="utf-8") != new:
                atomic_write(path, new, 0o644)
                changed = True
        if changed and shutil.which("systemctl"):
            self.run(["systemctl", "daemon-reload"], timeout=60)

    # ------------------------------------------------------------------ requests from the web and the chat
    def requests(self) -> list[dict]:
        done = []
        for path in sorted(self.requests_dir.glob("*.json")) if self.requests_dir.is_dir() else []:
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                request = {}
            path.unlink(missing_ok=True)
            action = request.get("action")
            try:
                if action == "update":
                    result = self.update(force=bool(request.get("force")))
                elif action == "check":
                    result = self.update(check_only=True)
                elif action == "restart":
                    result = self.restart(list(request.get("services") or []))
                else:
                    result = {"ignored": f"việc lạ: {action!r}"}
            except (StackError, subprocess.SubprocessError, OSError) as exc:
                result = self.finish(str(action), "failed", str(exc))
            done.append({"request": path.name, "action": action, "result": (result.get("last") or result)})
        return done

    def restart(self, services: list[str], *, allow_infra: bool = False) -> dict:
        """Restart services, then wait until they are healthy again. By default only the Kite services:
        the web's request files must not be able to restart the infrastructure (postgres, Temporal,
        Caddy) — that stays the watchdog's job, which passes allow_infra=True."""
        allowed = (*KITE_SERVICES, *INFRA_SERVICES) if allow_infra else KITE_SERVICES
        names = [s for s in services if s in allowed]
        if not names or len(names) != len(services):
            raise StackError(f"chỉ khởi động lại được: {', '.join(allowed)}")
        if ("gateway" in names or "web" in names) and "opencode" not in names:
            # the chat reads the workers again (new or changed ones) / lives in the web's process space
            names.append("opencode")
        with self.lock(wait=600):
            self.set_status(state="restarting")
            done = self.run(self.compose("restart", *names), timeout=600)
            if done.returncode != 0:
                return self.finish("restart", "failed", (done.stderr or done.stdout or "").strip()[-800:],
                                   restarted=False)
            ok, detail = self.wait_healthy(services=tuple(names))
            return self.finish("restart", "restarted" if ok else "failed",
                               ", ".join(names) + ("" if ok else f": {detail}"), restarted=True)

    # ------------------------------------------------------------------ install
    def install(self, *, access: str, domain: str | None, https_port: int, image: str, sandbox: str | None,
                channel: str, auto_update: bool | None, timezone: str | None,
                ghcr_token_file: Path | None) -> dict:
        if os.geteuid() != 0:
            raise StackError("chạy bằng root: sudo kite-stack install ...")
        if not self.data.is_dir():
            raise StackError(f"{self.data} chưa có: tạo (hoặc gắn ổ dữ liệu vào đó) rồi chạy lại")
        if not os.path.ismount(self.data):
            self.out(f"lưu ý: {self.data} không phải ổ riêng; dữ liệu nằm trên ổ hệ thống")
        for folder, owner, mode in ((self.data / "kite", UID, 0o755), (self.data / "home", UID, 0o755),
                                    (self.data / "postgres", None, 0o700), (self.data / "caddy", None, 0o755),
                                    (self.dir, 0, 0o755), (self.releases, 0, 0o755), (self.requests_dir, UID, 0o700)):
            folder.mkdir(parents=True, exist_ok=True)
            if owner is not None:
                os.chown(folder, owner, owner)
            if owner is not None or not any(folder.iterdir()):
                os.chmod(folder, mode)
        env = self.env()
        public_ip = None
        if not domain and not env.get("KITE_DOMAIN"):
            public_ip = self.public_ip()
            domain = f"{public_ip.replace('.', '-')}.sslip.io"
        values = {
            "KITE_ACCESS": access or env.get("KITE_ACCESS", "public"),
            "KITE_DOMAIN": domain or env["KITE_DOMAIN"],
            "KITE_HTTPS_PORT": str(https_port or env.get("KITE_HTTPS_PORT", "443")),
            "KITE_IMAGE_REPO": image or env.get("KITE_IMAGE_REPO", DEFAULT_IMAGE),
            "KITE_SANDBOX_REPO": sandbox or env.get("KITE_SANDBOX_REPO") or f"{image or DEFAULT_IMAGE}-sandbox",
            "KITE_CHANNEL": channel or env.get("KITE_CHANNEL", "stable"),
            "KITE_AUTO_UPDATE": {True: "1", False: "0"}.get(auto_update, env.get("KITE_AUTO_UPDATE", "1")),
            "KITE_VERIFY_IMAGE_SIGNATURES": env.get("KITE_VERIFY_IMAGE_SIGNATURES", "1"),
            "TZ": timezone or env.get("TZ") or self.host_timezone(),
            "DOCKER_GID": str(grp.getgrnam("docker").gr_gid),
            **generated_secrets(env),
        }
        if values["KITE_ACCESS"] not in ("public", "private"):
            raise StackError("--access là public hoặc private")
        write_env(self.env_file, values, "Kite on this machine (kite-stack). Secrets: keep this file 0600, root only.")
        if ghcr_token_file:
            user = values["KITE_IMAGE_REPO"].split("/")[1]
            token = Path(ghcr_token_file).read_text(encoding="utf-8").strip()
            done = subprocess.run(["docker", "login", "ghcr.io", "-u", user, "--password-stdin"], input=token,
                                  text=True, capture_output=True, timeout=60)
            if done.returncode != 0:
                raise StackError("docker login ghcr.io lỗi: " + done.stderr.strip()[-300:])
        with self.lock(wait=900):
            if self.release() is None:
                self.set_status(state="updating")
                new = self.pull()
                signed, why = self.verify_signatures(new, values)
                if not signed:
                    raise StackError(f"image đầu tiên bị từ chối: {why}")
                release = self.prepare(new)
                self.activate(release)
            release = self.release()
            url = self.public_url()
            init = self.run(self.compose("run", "--rm", "--no-deps", "-T", "gateway", "kite", "stack", "init",
                                         "--public-url", url, "--sandbox-gateway", self.sandbox_network()),
                            timeout=600)
            if init.returncode != 0:
                raise StackError("kite stack init lỗi: " + (init.stderr or init.stdout).strip()[-800:])
            ok, detail = self.up()
            self.adopt(release)
            for unit in ("kite-stack-update.timer", "kite-stack-requests.path", "kite-stack-watchdog.timer"):
                self.run(["systemctl", "enable", "--now", unit], timeout=60)
            result = self.finish("install", "updated" if ok else "failed", detail if not ok else f"Kite chạy ở {url}")
        return {"url": url, "healthy": ok, "detail": detail, "access": values["KITE_ACCESS"],
                "next": ["sudo kite-stack admin   # tạo tài khoản chủ (mật khẩu + app 2FA)",
                         f"mở {url}" if values["KITE_ACCESS"] == "public"
                         else "ssh -L 8790:127.0.0.1:8790 <máy> rồi mở http://127.0.0.1:8790"],
                "status": result.get("last")}

    def public_url(self) -> str:
        env = self.env()
        port = int(env.get("KITE_HTTPS_PORT", "443"))
        return f"https://{env['KITE_DOMAIN']}" + ("" if port == 443 else f":{port}")

    def public_ip(self) -> str:
        """This machine's public IPv4: AWS's metadata service, else an echo service."""
        try:
            token_req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                               headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
            token = urllib.request.urlopen(token_req, timeout=2).read().decode()
            ip_req = urllib.request.Request("http://169.254.169.254/latest/meta-data/public-ipv4",
                                            headers={"X-aws-ec2-metadata-token": token})
            ip = urllib.request.urlopen(ip_req, timeout=2).read().decode().strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                return ip
        except OSError:
            pass
        for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
            with contextlib.suppress(OSError):
                ip = urllib.request.urlopen(url, timeout=10).read().decode().strip()
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                    return ip
        raise StackError("không tìm được IP public: chạy lại với --domain <tên miền>")

    @staticmethod
    def host_timezone() -> str:
        with contextlib.suppress(OSError):
            name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
            if name:
                return name
        with contextlib.suppress(OSError):
            target = os.readlink("/etc/localtime")
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
        return "UTC"

    def configure(self, **settings: str | None) -> dict:
        """Change the address, the access mode or the auto update; Caddy and the config follow."""
        env = self.env()
        if not env:
            raise StackError("chưa cài: sudo kite-stack install")
        for key, value in settings.items():
            if value is not None:
                env[key] = value
        if env.get("KITE_ACCESS") not in ("public", "private"):
            raise StackError("access là public hoặc private")
        write_env(self.env_file, env, "Kite on this machine (kite-stack). Secrets: keep this file 0600, root only.")
        with self.lock(wait=600):
            url = self.public_url()
            self.run(self.compose("exec", "-T", "gateway", "kite", "stack", "init", "--public-url", url), timeout=300)
            ok, detail = self.up()
            self.finish("configure", "updated" if ok else "failed", detail)
        return {"url": url, "access": env["KITE_ACCESS"], "auto_update": env.get("KITE_AUTO_UPDATE") == "1",
                "healthy": ok, "detail": detail,
                "note": "đổi địa chỉ thì notebook Kaggle cần stub mới: kite-stack kite worker stub <worker>"}

    # ------------------------------------------------------------------ moving a machine
    def import_state(self, archive: Path, *, force: bool) -> dict:
        archive = Path(archive).resolve()
        if not archive.is_file():
            raise StackError(f"không có file {archive}")
        staged = self.dir / "import.tgz"
        with self.lock(wait=600):
            shutil.copy2(archive, staged)
            os.chown(staged, UID, UID)
            os.chmod(staged, 0o600)
            try:
                self.run(self.compose("stop", *KITE_SERVICES), timeout=300)
                args = ["run", "--rm", "--no-deps", "-T", "-e", f"KITE_PUBLIC_URL={self.public_url()}", "web",
                        "kite", "stack", "import", "/var/lib/kite/stack/import.tgz",
                        "--sandbox-gateway", self.sandbox_network(), *(["--force"] if force else [])]
                done = self.run(self.compose(*args), timeout=3600)
            finally:
                staged.unlink(missing_ok=True)
            if done.returncode != 0:
                self.up()
                raise StackError("import lỗi: " + ((done.stderr or "") + (done.stdout or "")).strip()[-1200:])
            ok, detail = self.up()
            self.finish("import", "updated" if ok else "failed", detail)
        report = {}
        with contextlib.suppress(ValueError):
            report = json.loads(done.stdout[done.stdout.find("{"):])
        return {"imported": report, "healthy": ok, "detail": detail,
                "next": ["mở trang web, đăng nhập bằng tài khoản của máy cũ",
                         "notebook Kaggle trỏ về địa chỉ mới: kite-stack kite worker restart <worker> "
                         "(hoặc dán lại: kite-stack kite worker stub <worker>)"]}


# ---------------------------------------------------------------------- command line
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if argv and argv[0] in ("kite", "compose"):      # everything after them goes on as it is (-h, --json...)
        argv, passthrough = argv[:1], argv[1:]
    parser = argparse.ArgumentParser(prog="kite-stack", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="install (or repair) Kite on this machine")
    install.add_argument("--access", choices=["public", "private"], default=None,
                         help="public: web page and /worker/* over HTTPS; private: only /worker/*, the web page "
                              "through ssh -L or tailscale (default: public)")
    install.add_argument("--domain", default=None, help="DNS name of this machine (default: <ip>.sslip.io)")
    install.add_argument("--https-port", type=int, default=None, help="default 443")
    install.add_argument("--image", default=None, help=f"Kite image repository (default {DEFAULT_IMAGE})")
    install.add_argument("--sandbox-image", default=None, help="sandbox image repository (default <image>-sandbox)")
    install.add_argument("--channel", default=None, help="tag to follow (default stable)")
    install.add_argument("--no-auto-update", action="store_true", help="update only when asked")
    install.add_argument("--timezone", default=None)
    install.add_argument("--ghcr-token-file", type=Path, default=None,
                         help="only for private images: a file with a GitHub token (read:packages)")
    update = commands.add_parser("update", help="pull the stable image and run it if it is new")
    update.add_argument("--force", action="store_true", help="also while something runs, or a digest that failed")
    update.add_argument("--timer", action="store_true", help=argparse.SUPPRESS)
    update.add_argument("--check", action="store_true", help="only look for a new image")
    commands.add_parser("requests", help="carry out the web's and the chat's requests (systemd path unit)")
    commands.add_parser("status", help="running release, newest one, last update, containers")
    watchdog = commands.add_parser("watchdog", help="check every service once; restart the unhealthy ones")
    watchdog.add_argument("--check", action="store_true", help="only say what would happen, restart nothing")
    restart = commands.add_parser("restart", help="restart Kite services")
    restart.add_argument("services", nargs="*", default=list(KITE_SERVICES))
    logs = commands.add_parser("logs", help="logs of the services")
    logs.add_argument("services", nargs="*")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--tail", default="200")
    commands.add_parser("up", help="start every service of the current release")
    commands.add_parser("down", help="stop every service (data stays)")
    admin = commands.add_parser("admin", help="create the owner account, or --reset its password and 2FA")
    admin.add_argument("--username", default=None)
    admin.add_argument("--reset", action="store_true")
    commands.add_parser("kite", help="run a kite command in the gateway's container: kite-stack kite doctor")
    commands.add_parser("compose", help="docker compose with this machine's files: kite-stack compose ps")
    configure = commands.add_parser("configure", help="change the address, the access mode, the auto update")
    configure.add_argument("--access", choices=["public", "private"], default=None)
    configure.add_argument("--domain", default=None)
    configure.add_argument("--https-port", type=int, default=None)
    configure.add_argument("--auto-update", choices=["on", "off"], default=None)
    imp = commands.add_parser("import", help="move a machine: an export made with `kite stack export`")
    imp.add_argument("archive", type=Path)
    imp.add_argument("--force", action="store_true", help="replace the tasks this machine already has")
    args = parser.parse_args(argv)

    stack = Stack()
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        if args.command == "install":
            report = stack.install(access=args.access or "", domain=args.domain, https_port=args.https_port or 0,
                                   image=args.image or "", sandbox=args.sandbox_image, channel=args.channel or "",
                                   auto_update=False if args.no_auto_update else None, timezone=args.timezone,
                                   ghcr_token_file=args.ghcr_token_file)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["healthy"] else 1
        if args.command == "update":
            report = stack.update(force=args.force, timer=args.timer, check_only=args.check)
            print(json.dumps(report.get("last", report), indent=2, ensure_ascii=False))
            return 0 if (report.get("last") or {}).get("result") not in ("failed", "rolled_back") else 1
        if args.command == "requests":
            print(json.dumps(stack.requests(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "status":
            print(json.dumps(stack.status(), indent=2, ensure_ascii=False))
            if stack.release():
                stack.run(stack.compose("ps"), timeout=60, capture=False)
            return 0
        if args.command == "restart":
            report = stack.restart(args.services)
            print(json.dumps(report.get("last", report), indent=2, ensure_ascii=False))
            return 0
        if args.command == "watchdog":
            report = stack.watchdog(check_only=args.check)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        if args.command == "logs":
            cmd = stack.compose("logs", "--no-color", "--tail", args.tail, *(["-f"] if args.follow else []),
                                *args.services)
            return stack.run(cmd, timeout=None, capture=False).returncode
        if args.command == "up":
            with stack.lock(wait=600):
                ok, detail = stack.up()
            print(detail)
            return 0 if ok else 1
        if args.command == "down":
            with stack.lock(wait=600):
                return stack.run(stack.compose("down"), timeout=600, capture=False).returncode
        if args.command == "admin":
            cmd = stack.compose("run", "--rm", "--no-deps", *([] if tty else ["-T"]), "web", "kite", "web", "admin",
                                *(["--username", args.username] if args.username else []),
                                *(["--reset"] if args.reset else []))
            return stack.run(cmd, timeout=None, tty=True).returncode
        if args.command == "kite":
            running = stack.services_state().get("gateway", {}).get("state") == "running"
            base = ("exec",) if running else ("run", "--rm", "--no-deps")
            cmd = stack.compose(*base, *([] if tty else ["-T"]), "gateway", "kite", *passthrough)
            return stack.run(cmd, timeout=None, tty=True).returncode
        if args.command == "compose":
            return stack.run(stack.compose(*passthrough), timeout=None, tty=True).returncode
        if args.command == "configure":
            report = stack.configure(KITE_ACCESS=args.access, KITE_DOMAIN=args.domain,
                                     KITE_HTTPS_PORT=str(args.https_port) if args.https_port else None,
                                     KITE_AUTO_UPDATE={"on": "1", "off": "0"}.get(args.auto_update or ""))
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["healthy"] else 1
        if args.command == "import":
            report = stack.import_state(args.archive, force=args.force)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["healthy"] else 1
    except (StackError, subprocess.SubprocessError, OSError) as exc:
        print(f"kite-stack: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
