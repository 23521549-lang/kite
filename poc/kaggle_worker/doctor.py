"""`kite doctor`: check that the Kite installation on this machine actually works.

Each check reads real state (services, sockets, gateway, Docker, files) and says what to run when
something is wrong. Nothing here changes the system, prints a secret, or starts a GPU session.
`kite sandbox doctor` is the deeper isolation test (it starts containers).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from typing import Any, Callable

from .config import KiteConfig

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
MARK = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


@dataclass
class Probes:
    """Everything that touches the machine, replaceable in tests."""
    run: Callable[[list[str], float], tuple[int, str]]
    http_json: Callable[[str, float], Any]
    tcp: Callable[[str, int, float], bool]
    disk_free: Callable[[Path], float]            # GiB
    which: Callable[[str], str | None]
    has_module: Callable[[str], bool]


def _run(command: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _http_json(url: str, timeout: float) -> Any:
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def _tcp(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def default_probes() -> Probes:
    return Probes(run=_run, http_json=_http_json, tcp=_tcp,
                  disk_free=lambda path: shutil.disk_usage(path).free / 1024**3,
                  which=shutil.which, has_module=lambda name: importlib.util.find_spec(name) is not None)


def _host_port(endpoint: str, default_port: int) -> tuple[str, int]:
    host, _, port = endpoint.rpartition(":")
    if not host:
        return endpoint, default_port
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return endpoint, default_port


def classify_address(address: str) -> str:
    """'loopback' | 'private' | 'all' (wildcard) | 'public' for a listening address."""
    address = address.strip("[]").split("%")[0]
    if address in ("*", "0.0.0.0", "::"):
        return "all"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "public"
    if ip.is_loopback:
        return "loopback"
    return "private" if ip.is_private or ip.is_link_local else "public"


def listeners(output: str) -> list[tuple[str, int]]:
    """Parse `ss -ltnH`: (address, port) of each listening TCP socket."""
    found = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        address, _, port = local.rpartition(":")
        if port.isdigit():
            found.append((address, int(port)))
    return found


def gpu_quota(p: Probes, command: str, token_file: str) -> tuple[str, str, str]:
    """(status, detail, fix) from ``kaggle quota -v``: GPU hours left this week (Kaggle CLI 2.x)."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="kite-kaggle-") as config_dir:
        code, output = p.run(["env", f"KAGGLE_CONFIG_DIR={config_dir}", f"KAGGLE_API_TOKEN={token_file}",
                              command, "quota", "-v"], 60)
    row = next((line.split(",") for line in output.splitlines() if line.startswith("GPU,")), None)
    if code != 0 or not row or len(row) < 4:
        return SKIP, "không đọc được (kaggle CLI cũ chưa có lệnh quota?)", ""
    remaining, total = row[2].strip(), row[3].strip()
    refresh = f", làm mới {row[4].strip()[:16]}" if len(row) > 4 and row[4].strip() else ""
    try:
        low = float(remaining.rstrip("h")) < 3
    except ValueError:
        low = False
    return (WARN if low else OK), f"còn {remaining} / {total}{refresh}", (
        "gần hết: bộ đo dài (polyglot ~7–11 giờ) để tuần sau" if low else "")


def browser_missing() -> str:
    """What Kite's browser still needs on this machine ("" when nothing): Python packages, Chromium."""
    import importlib.util

    lacking = [name for name in ("playwright", "cryptography") if importlib.util.find_spec(name) is None]
    if lacking:
        return "gói " + ", ".join(lacking)
    roots = [Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])] if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else []
    roots.append(Path.home() / ".cache" / "ms-playwright")
    if not any(root.is_dir() and any(child.name.startswith("chromium") for child in root.iterdir()) for root in roots):
        return "Chromium của Playwright"
    return ""


def run_checks(config: KiteConfig, config_path: Path, probes: Probes | None = None) -> list[Check]:
    p = probes or default_probes()
    checks: list[Check] = []
    add = lambda *args: checks.append(Check(*args))   # noqa: E731
    state_dir = Path(config_path).parent
    pulls = [pr for pr in config.providers if pr.adapter == "pull" and pr.enabled]
    autostart = [pr.effective_worker_id for pr in pulls if pr.launch is not None]
    relays = [pr for pr in config.providers if pr.adapter == "relay" and pr.enabled and pr.relay is not None]

    add("cấu hình", OK, f"{config_path}: {len(pulls)} worker ({', '.join(pr.effective_worker_id for pr in pulls) or 'không có'})"
        + (f"; tự bật: {', '.join(autostart)}" if autostart else "")
        + (f"; worker API: {', '.join(pr.effective_worker_id for pr in relays)}" if relays else ""))
    if relays:
        from .journal import Journal
        from .relay import SpendLedger

        ledger = SpendLedger(Journal(state_dir / "journal").read("spend"))
        for provider in relays:
            r = provider.relay
            day, month = ledger.spent(provider.effective_worker_id)
            caps = [(day, r.daily_cap, "hôm nay"), (month, r.monthly_cap, "tháng này")]
            near = [f"{spent:.2f}/{cap:.2f} USD {when}" for spent, cap, when in caps if cap and spent >= 0.8 * cap]
            what = (f"máy {r.pod.provider} {r.pod.pod_id} (tự tắt sau {r.pod.idle_minutes} phút rảnh)" if r.pod
                    else f"{r.kind} {r.base_url}")
            detail = f"{what}; đã chi {day:.2f} USD hôm nay, {month:.2f} USD tháng này" + (
                f" (hạn mức {r.daily_cap or '-'}/ngày, {r.monthly_cap or '-'}/tháng)" if r.daily_cap or r.monthly_cap
                else ("" if not (r.price_in or r.price_out or r.pod) else "; chưa đặt hạn mức chi"))
            add(f"{'máy thuê' if r.pod else 'worker API'} {provider.effective_worker_id}", WARN if near else OK, detail,
                f"gần hoặc chạm hạn mức ({', '.join(near)}): nâng caps trong config hoặc chờ làm mới" if near else "")

    # ---- Kite's browser for kaggle.com (launch.via: browser, or Save Version by hand through Kite)
    browser_workers = [pr.effective_worker_id for pr in pulls if pr.launch is not None and pr.launch.via == "browser"]
    if browser_workers:
        missing = browser_missing()
        add("trình duyệt Kite", FAIL if missing else OK,
            f"thiếu {missing}" if missing else f"có Playwright và Chromium (cho {', '.join(browser_workers)})",
            "cd ~/kite && ~/.local/bin/uv pip install --python .venv/bin/python -e '.[browser]' && "
            "sudo apt-get install -y libatomic1 && sudo .venv/bin/playwright install-deps chromium && "
            ".venv/bin/playwright install chromium" if missing else "")

    # ---- the web page (only where the kite-web service is installed)
    from .update import UNIT_DIR

    if (UNIT_DIR / "kite-web.service").exists():
        from . import webui

        page = webui.status()
        if page.get("present"):
            add("giao diện web", OK if page["current"] else WARN,
                "đã build, đúng bản code" if page["current"] else
                ("đã build nhưng cũ hơn code" if page["built"] else "chưa build (web đang hiện trang rút gọn)"),
                "" if page["current"] else "kite web build")

    # ---- secrets: present, private
    for provider in config.providers:
        if not provider.enabled:
            continue
        for key, reference in sorted(provider.secrets.items()):
            label = f"secret {provider.id}.{key}"
            if reference.startswith("env:"):
                status = OK if os.environ.get(reference[4:]) else WARN
                add(label, status, f"biến môi trường {reference[4:]}" + ("" if status == OK else " chưa có trong shell này"),
                    "" if status == OK else "kiểm tra biến này trong unit systemd của dịch vụ")
                continue
            path = Path(reference[5:])
            if not path.exists():
                fix = (f"kite worker token {provider.effective_worker_id}" if key == "worker_token"
                       else f"kite worker autostart {provider.effective_worker_id} --kernel <user>/<slug>"
                       if key == "kaggle_api_token" else f"tạo file {path} (chmod 600)")
                add(label, FAIL, f"thiếu file {path}", fix)
            elif path.stat().st_mode & 0o077:
                add(label, WARN, f"{path} có quyền {path.stat().st_mode & 0o777:o}: người khác đọc được",
                    f"chmod 600 {path}")
            else:
                add(label, OK, f"{path} (0600)")

    # ---- gateway and workers
    gateway_url = f"http://{config.gateway.host}:{config.gateway.port}"
    status: dict | None = None
    if pulls or relays:
        try:
            status = p.http_json(gateway_url + "/status", 5)
        except Exception as exc:
            add("gateway", FAIL, f"không gọi được {gateway_url}/status: {exc}",
                "sudo systemctl status kite-gateway; journalctl -u kite-gateway -n 50")
    if status is not None:
        workers = status.get("workers") or []
        alive = [w for w in workers if w.get("alive")]
        add("gateway", OK, f"chạy; {len(alive)}/{len(workers)} worker sống, {status.get('queued_jobs', 0)} request chờ")
        for worker in workers:
            launch = worker.get("launch") or {}
            name = f"worker {worker.get('worker_id')}"
            if worker.get("alive"):
                from .pull_worker import WORKER_VERSION

                add(name, OK, f"sống, đã nạp {worker.get('loaded_model') or 'chưa có model'}"
                    + (f", {worker.get('running')}/{worker.get('slots')} request đang chạy" if worker.get("busy") else "")
                    + (f", {worker.get('slots')} request cùng lúc" if (worker.get("slots") or 1) > 1
                       and not worker.get("busy") else ""))
                if worker.get("version") != WORKER_VERSION and not str(worker.get("version")).startswith("relay-"):
                    # A notebook started by hand runs whatever was saved on Kaggle: step 2 of the full-test
                    # day had no streaming because the session predated the new worker code.
                    add(f"notebook {worker.get('worker_id')}", WARN,
                        f"chạy code worker cũ ({worker.get('version') or 'chưa báo phiên bản'}, mới là {WORKER_VERSION})",
                        f"kite worker restart {worker.get('worker_id')} (Kite đẩy notebook mới lên Kaggle rồi bật lại)")
            elif launch.get("state") == "error":
                add(name, FAIL, f"tự bật lỗi: {launch.get('detail')}",
                    f"sửa theo lỗi rồi: kite worker start {worker.get('worker_id')} --force")
            elif launch.get("state") in ("launching", "starting"):
                add(name, OK, f"đang bật trên Kaggle ({launch.get('state')}, {launch.get('seconds')} giây)")
            elif launch:
                add(name, OK, "chưa chạy; Kite tự bật khi có việc")
            elif alive or autostart:
                # Someone else's notebook (one Kaggle account per person) that is simply off: not a
                # problem while another worker is up or can be started (A1 of the full-test day).
                add(name, SKIP, "chưa kết nối (không có tự bật; bình thường khi chủ notebook chưa bật)")
            else:
                add(name, WARN, "chưa kết nối", "mở notebook Kaggle và chạy tới ô Worker mode "
                    f"(hoặc cấu hình tự bật: kite worker autostart {worker.get('worker_id')} --kernel <user>/<slug>)")
    if autostart:
        from .launcher import kaggle_command

        command = kaggle_command()
        found = Path(command).exists() or p.which(command)
        add("kaggle CLI", OK if found else FAIL, command if found else "chưa cài (cần cho tự bật worker)",
            "" if found else '"$(command -v uv || echo ~/.local/bin/uv)" pip install --python .venv/bin/python '
                             '-e ".[codemap,kaggle]"')
        for provider in pulls:
            if provider.launch is None:
                continue
            wid = provider.effective_worker_id
            if provider.launch.token == "kaggle-secret":
                add(f"tự bật {wid}", WARN, "notebook đọc Kaggle Secret, mà Kaggle không gắn Secrets cho bản Kite "
                    "đẩy qua API (lỗi HTTP 400): bản tự bật sẽ dừng ở ô worker",
                    f"kite worker autostart {wid} --kernel {provider.launch.kernel} --token per-launch")
            reference = provider.secrets.get("kaggle_api_token", "")
            if found and reference.startswith("file:"):
                add(f"quota GPU {wid}", *gpu_quota(p, command, reference[5:]))

    # ---- Temporal
    host, port = _host_port(config.temporal_endpoint, 7233)
    if p.tcp(host, port, 3):
        add("temporal", OK, f"{host}:{port} nhận kết nối")
    else:
        add("temporal", FAIL, f"không kết nối được {host}:{port}",
            "sudo docker ps --filter name=temporal; xem lại docker compose của Temporal")

    # ---- systemd services
    if p.which("systemctl"):
        for unit in ("kite-gateway", "kite-workflow-worker"):
            code, output = p.run(["systemctl", "is-active", unit], 10)
            state = output.strip().splitlines()[-1] if output.strip() else "unknown"
            add(f"dịch vụ {unit}", OK if code == 0 else FAIL, state,
                "" if code == 0 else f"sudo systemctl restart {unit}; journalctl -u {unit} -n 50")

    # ---- listening sockets: Kite's ports must not listen on public addresses
    if p.which("ss"):
        code, output = p.run(["ss", "-ltnH"], 10)
        ports = {config.gateway.port: "gateway", port: "temporal", config.opencode_port: "opencode"}
        if code == 0:
            for wanted, label in ports.items():
                found = [(addr, classify_address(addr)) for addr, prt in listeners(output) if prt == wanted]
                if not found:
                    continue
                public = [a for a, kind in found if kind == "public"]
                wide = [a for a, kind in found if kind == "all"]
                where = ", ".join(a for a, _ in found)
                if public:
                    add(f"cổng {wanted} ({label})", FAIL, f"nghe trên địa chỉ public {', '.join(public)}",
                        "chỉ cho nghe ở 127.0.0.1 hoặc mạng nội bộ")
                elif wide:
                    add(f"cổng {wanted} ({label})", WARN, f"nghe trên mọi địa chỉ ({where})",
                        "Docker publish cổng thì ufw không chặn: bind 127.0.0.1:<cổng> trong compose, "
                        "và giữ security group đóng cổng này")
                else:
                    add(f"cổng {wanted} ({label})", OK, f"chỉ nội bộ ({where})")

    # ---- sandbox: Docker, image, network
    if config.sandbox is not None:
        docker = list(config.sandbox.docker_command)
        code, output = p.run([*docker, "version", "--format", "{{.Server.Version}}"], 20)
        if code != 0:
            add("docker", FAIL, output.strip()[-200:] or "không chạy được docker",
                "sudo systemctl status docker; user phải thuộc nhóm docker")
        else:
            add("docker", OK, f"server {output.strip()}")
            code, _ = p.run([*docker, "image", "inspect", "-f", "{{.Id}}", config.sandbox.image], 20)
            add("sandbox image", OK if code == 0 else FAIL, config.sandbox.image,
                "" if code == 0 else "kite sandbox build")
            code, output = p.run([*docker, "network", "inspect", "-f", "{{.Internal}}", config.sandbox.network], 20)
            internal = code == 0 and output.strip() == "true"
            add("sandbox network", OK if internal else FAIL,
                f"{config.sandbox.network} " + ("internal (không Internet)" if internal else "thiếu hoặc không internal"),
                "" if internal else "kite sandbox build")
        if config.sandbox.code_map:
            ok = p.has_module("graphify")
            add("bản đồ code (graphify)", OK if ok else WARN, "đã cài" if ok else "chưa cài: task chạy không có bản đồ code",
                "" if ok else '"$(command -v uv || echo ~/.local/bin/uv)" pip install --python .venv/bin/python '
                              '-e ".[codemap,kaggle]"')
        if config.sandbox.deps_cache:
            cache = state_dir / "deps-cache"
            sets = [d for d in cache.iterdir() if d.is_dir()] if cache.is_dir() else []
            add("cache thư viện", OK, f"{cache}: {len(sets)} bộ đã lưu" if cache.is_dir()
                else f"{cache}: chưa có (tạo ở task đầu tiên)")

    # ---- disk
    for label, path in (("đĩa dữ liệu", state_dir), ("đĩa home", Path.home())):
        try:
            free = p.disk_free(path)
        except OSError as exc:
            add(label, WARN, f"không đọc được {path}: {exc}")
            continue
        status_ = FAIL if free < 1 else WARN if free < 5 else OK
        add(label, status_, f"{path}: còn {free:.1f} GiB",
            # Not `docker system prune`: it also deletes the sandbox network while no container uses it.
            "" if status_ == OK else "kite cleanup (xem trước, rồi --yes); docker image prune; docker builder prune")

    # ---- tools
    executable = config.opencode_executable
    found = Path(executable).exists() or p.which(executable)
    add("opencode", OK if found else FAIL, executable, "" if found else "cài OpenCode hoặc sửa opencode.executable")
    journal = state_dir / "journal"
    writable = os.access(journal if journal.exists() else state_dir, os.W_OK)
    add("journal", OK if writable else WARN, str(journal), "" if writable else f"sudo chown -R $USER {state_dir}")
    if not Path(config.harness2_checkout).exists():
        add("harness2", SKIP, f"{config.harness2_checkout} chưa có (chỉ cần cho Harness²)")
    return checks


def summary(checks: list[Check]) -> dict[str, Any]:
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    return {"status": "ok" if not fails else "needs_attention", "fail": fails, "warn": warns,
            "checks": [asdict(c) for c in checks]}


def render(checks: list[Check], config_path: Path) -> str:
    width = max((len(c.name) for c in checks), default=10) + 2
    lines = [f"Kite doctor — {config_path}"]
    for check in checks:
        lines.append(f"{MARK[check.status]} {check.name.ljust(width)}{check.detail}")
        if check.fix and check.status in (FAIL, WARN):
            lines.append(f"  {' ' * width}→ {check.fix}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("Kết quả: " + ("mọi thứ ổn" if not fails and not warns
                                 else f"{fails} lỗi, {warns} cảnh báo")
                 + ". Kiểm tra sâu sandbox (chạy container): kite sandbox doctor")
    return "\n".join(lines)
