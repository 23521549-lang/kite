"""Per-task Docker sandbox for the coding agent.

Each task gets its own clone of the repository, mounted into a throw-away container that runs
OpenCode non-interactively (``opencode run``). The container:

* has no Linux capabilities, cannot gain privileges, runs as an unprivileged user and has CPU,
  memory and process limits;
* sits on an *internal* Docker network: no Internet and therefore no ``git push``; the only
  reachable service is the Kite gateway, which also listens on the host's address there;
* sees only the task clone and a generated OpenCode config, never the operator's secrets.

After the agent finishes, Kite collects the patch itself and runs the test command in a second
container with no network at all, so the result carries evidence instead of the model's claim
that the tests pass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess]

TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
JUNK_PATTERNS = ("__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
                 ".coverage", "node_modules/", ".venv/", ".opencode/")
TAIL_CHARS = 4000

PROBE = r"""
import json, os, urllib.error, urllib.request
def get(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        return type(exc).__name__
print(json.dumps({
    "gateway": get(os.environ["KITE_GATEWAY_MODELS"]),
    "internet": get("https://example.com"),
    "host_secrets_visible": os.path.exists("/var/lib/kite"),
    "uid": os.getuid(),
}))
"""


class SandboxError(RuntimeError):
    pass


def _default_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 1000


@dataclass(frozen=True)
class SandboxSettings:
    image: str = "kite-sandbox:latest"
    network: str = "kite-sandbox-net"
    gateway_url: str = "http://172.19.0.1:8787/v1"
    memory: str = "1g"
    cpus: float = 1.0
    pids_limit: int = 512
    agent_timeout_seconds: int = 3600
    test_timeout_seconds: int = 900
    test_command: str = "python -m pytest -q"
    docker_command: tuple[str, ...] = ("docker",)
    uid: int = field(default_factory=_default_uid)


@dataclass
class SandboxRun:
    task_id: str
    repo: str
    base_head: str
    model: str
    goal: str
    workspace: str
    agent_status: str = "not_started"
    agent_exit_code: int | None = None
    agent_seconds: float = 0.0
    agent_output_tail: str = ""
    patch_path: str = ""
    changed_files: list[str] = field(default_factory=list)
    test_command: str = ""
    test_exit_code: int | None = None
    test_seconds: float = 0.0
    test_output_tail: str = ""

    @property
    def verified(self) -> bool:
        """The agent finished, changed something, and Kite's own test run passed."""
        return self.agent_status == "ok" and self.test_exit_code == 0 and bool(self.changed_files)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["verified"] = self.verified
        return data


def default_runner(cmd, *, timeout=None) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def _tail(text, limit: int = TAIL_CHARS) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return text[-limit:]


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SandboxError(f"git {' '.join(args)} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def container_name(kind: str, task_id: str) -> str:
    return f"kite-{kind}-{task_id}"


def docker_run_args(settings: SandboxSettings, *, name: str, workspace: Path, network: str,
                    config_file: Path | None = None, extra_env=()) -> list[str]:
    """``docker run`` prefix for a hardened, throw-away sandbox container (image name last)."""
    args = [
        *settings.docker_command, "run", "--rm", "--name", name,
        "--network", network,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", str(settings.pids_limit), "--memory", settings.memory,
        "--cpus", str(settings.cpus), "--user", f"{settings.uid}:{settings.uid}",
        "-e", "HOME=/home/kite", "-e", "OPENCODE_DISABLE_AUTOUPDATE=1",
        "-v", f"{Path(workspace).resolve()}:/work", "-w", "/work",
    ]
    for key, value in extra_env:
        args += ["-e", f"{key}={value}"]
    if config_file is not None:
        args += ["-v", f"{Path(config_file).resolve()}:/home/kite/.config/opencode/opencode.json:ro"]
    args.append(settings.image)
    return args


def agent_config(settings: SandboxSettings, *, model: str, models, small_model: str | None = None) -> dict:
    """OpenCode config for the sandbox: gateway provider, default model, no permission prompts."""
    from .gateway import opencode_provider_config

    base_url = settings.gateway_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    config = opencode_provider_config(base_url, list(models))
    config["model"] = f"kite/{model}"
    if small_model:
        config["small_model"] = f"kite/{small_model}"
    # The container is the safety boundary: inside it the agent may edit and run commands without
    # asking. An unanswered permission prompt used to hang the whole task (defect E1).
    config["permission"] = {"edit": "allow", "bash": "allow", "webfetch": "deny"}
    return config


def agent_prompt(goal: str, test_command: str) -> str:
    lines = [goal.strip(), "",
             "Làm việc trong thư mục hiện tại (/work). Môi trường không có Internet: "
             "không cài gói, không tải gì về."]
    if test_command:
        lines.append(f"Kiểm tra lại bằng lệnh: {test_command}")
    lines.append("Không commit, không push: Kite sẽ tự lấy thay đổi để người dùng duyệt.")
    return "\n".join(lines)


def prepare_clone(repo: Path, workspace: Path) -> str:
    """Clone the committed state of ``repo`` into ``workspace``; return the base commit."""
    repo = Path(repo).resolve()
    if _git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise SandboxError(f"{repo} is not a Git repository")
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise SandboxError("repository has uncommitted changes; commit or stash them first")
    head = _git(repo, "rev-parse", "HEAD")
    if workspace.exists():
        raise SandboxError(f"workspace {workspace} already exists; use another --task-id")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(workspace)],
                            capture_output=True, text=True)
    if cloned.returncode != 0:
        raise SandboxError(f"git clone failed: {cloned.stderr.strip()[:500]}")
    _git(workspace, "checkout", "--quiet", "--detach", head)
    # No remote inside the sandbox: nothing to push to, and no path back to the original repo.
    _git(workspace, "remote", "remove", "origin")
    exclude = workspace / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n# Kite sandbox: build/test artefacts are not part of the patch\n")
        stream.write("\n".join(JUNK_PATTERNS) + "\n")
    return head


def collect_patch(workspace: Path, base_head: str, patch_path: Path) -> list[str]:
    """Write every change since ``base_head`` (including new files) as a patch; return file names."""
    _git(workspace, "add", "-A")
    changed = [line for line in _git(workspace, "diff", "--cached", "--name-only", base_head).splitlines() if line]
    patch = subprocess.run(["git", "-C", str(workspace), "diff", "--cached", "--binary", base_head],
                           capture_output=True, text=True)
    if patch.returncode != 0:
        raise SandboxError(f"git diff failed: {patch.stderr.strip()[:500]}")
    patch_path.write_text(patch.stdout, encoding="utf-8")
    return changed


def run_task(settings: SandboxSettings, *, repo: Path, goal: str, model: str, models,
             state_dir: Path, task_id: str | None = None, small_model: str | None = None,
             test_command: str | None = None, runner: Runner = default_runner,
             log: Callable[[str], None] = print) -> SandboxRun:
    task_id = task_id or f"sbx-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    if not TASK_ID.fullmatch(task_id):
        raise SandboxError("task id must use lowercase letters, digits and dashes")
    if model not in models:
        raise SandboxError(f"model {model} is not served by the gateway ({', '.join(models)})")
    if not goal.strip():
        raise SandboxError("goal must not be empty")
    root = Path(state_dir) / task_id
    workspace = root / "work"
    base_head = prepare_clone(Path(repo), workspace)
    run = SandboxRun(task_id, str(Path(repo).resolve()), base_head, model, goal, str(workspace),
                     test_command=settings.test_command if test_command is None else test_command)

    config_file = root / "opencode.json"
    config_file.write_text(json.dumps(agent_config(settings, model=model, models=models,
                                                   small_model=small_model), indent=2), encoding="utf-8")
    log(f"[{task_id}] agent {model} đang làm trong sandbox (tối đa {settings.agent_timeout_seconds}s)...")
    name = container_name("agent", task_id)
    started = time.monotonic()
    try:
        result = runner(
            docker_run_args(settings, name=name, workspace=workspace, network=settings.network,
                            config_file=config_file)
            + ["opencode", "run", "-m", f"kite/{model}", agent_prompt(goal, run.test_command)],
            timeout=settings.agent_timeout_seconds)
        output = (result.stdout or "") + (result.stderr or "")
        run.agent_exit_code = result.returncode
        run.agent_status = "ok" if result.returncode == 0 else "failed"
    except subprocess.TimeoutExpired as exc:
        runner([*settings.docker_command, "kill", name], timeout=30)
        output = _tail(exc.stdout) + _tail(exc.stderr)
        run.agent_status = "timeout"
    run.agent_seconds = round(time.monotonic() - started, 1)
    run.agent_output_tail = _tail(output)
    (root / "agent.log").write_text(output, encoding="utf-8")

    patch_path = root / "result.patch"
    run.changed_files = collect_patch(workspace, base_head, patch_path)
    run.patch_path = str(patch_path)

    if run.test_command:
        log(f"[{task_id}] Kite tự chạy test (không mạng): {run.test_command}")
        name = container_name("test", task_id)
        started = time.monotonic()
        try:
            result = runner(docker_run_args(settings, name=name, workspace=workspace, network="none")
                            + ["sh", "-c", run.test_command], timeout=settings.test_timeout_seconds)
            test_output = (result.stdout or "") + (result.stderr or "")
            run.test_exit_code = result.returncode
        except subprocess.TimeoutExpired:
            runner([*settings.docker_command, "kill", name], timeout=30)
            test_output = "test command timed out"
            run.test_exit_code = 124
        run.test_seconds = round(time.monotonic() - started, 1)
        run.test_output_tail = _tail(test_output)
        (root / "test.log").write_text(test_output, encoding="utf-8")

    (root / "result.json").write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    return run


def ensure_network(settings: SandboxSettings, runner: Runner = default_runner) -> str:
    """Create the internal sandbox network if needed; return the host's address on it."""
    docker = list(settings.docker_command)
    fmt = "{{.Internal}} {{(index .IPAM.Config 0).Gateway}}"
    inspect = runner([*docker, "network", "inspect", "-f", fmt, settings.network], timeout=30)
    if inspect.returncode != 0:
        created = runner([*docker, "network", "create", "--internal", settings.network], timeout=60)
        if created.returncode != 0:
            raise SandboxError(f"cannot create network {settings.network}: {created.stderr.strip()[:300]}")
        inspect = runner([*docker, "network", "inspect", "-f", fmt, settings.network], timeout=30)
    internal, _, gateway = inspect.stdout.strip().partition(" ")
    if internal != "true":
        raise SandboxError(f"network {settings.network} exists but is not --internal; remove it and rebuild")
    return gateway


def build_image(settings: SandboxSettings, *, dockerfile: Path, opencode_binary: Path,
                opencode_cache: Path | None, runner: Runner = default_runner) -> str:
    """Build the sandbox image with the host's OpenCode binary and package cache baked in."""
    with tempfile.TemporaryDirectory(prefix="kite-sandbox-build-") as tmp:
        context = Path(tmp)
        shutil.copy2(dockerfile, context / "Dockerfile")
        shutil.copy2(opencode_binary, context / "opencode")
        cache = context / "opencode-cache"
        if opencode_cache is not None and opencode_cache.is_dir():
            shutil.copytree(opencode_cache, cache, symlinks=True)
        else:
            cache.mkdir()
        result = runner([*settings.docker_command, "build", "--build-arg", f"KITE_UID={settings.uid}",
                         "-t", settings.image, str(context)], timeout=1800)
        if result.returncode != 0:
            raise SandboxError("docker build failed:\n" + _tail(result.stdout + result.stderr, 2000))
    return settings.image


def doctor(settings: SandboxSettings, runner: Runner = default_runner) -> dict[str, dict]:
    """Check the isolation promises: internal network, gateway reachable, no Internet, no secrets."""
    checks: dict[str, dict] = {}

    def record(name: str, ok: bool, detail: object) -> None:
        checks[name] = {"ok": bool(ok), "detail": str(detail)[:300]}

    docker = list(settings.docker_command)
    version = runner([*docker, "version", "--format", "{{.Server.Version}}"], timeout=30)
    record("docker", version.returncode == 0, (version.stdout or version.stderr).strip() or "no output")
    if version.returncode != 0:
        return checks
    image = runner([*docker, "image", "inspect", settings.image], timeout=30)
    record("image", image.returncode == 0,
           settings.image if image.returncode == 0 else "missing: run `kite sandbox build`")
    network = runner([*docker, "network", "inspect", "-f", "{{.Internal}}", settings.network], timeout=30)
    record("network_internal", network.stdout.strip() == "true",
           network.stdout.strip() or network.stderr.strip() or "missing")
    if not all(check["ok"] for check in checks.values()):
        return checks

    with tempfile.TemporaryDirectory(prefix="kite-doctor-") as tmp:
        models_url = settings.gateway_url.rstrip("/") + "/models"
        probe = runner(docker_run_args(settings, name=f"kite-doctor-{uuid.uuid4().hex[:8]}",
                                       workspace=Path(tmp), network=settings.network,
                                       extra_env=[("KITE_GATEWAY_MODELS", models_url)])
                       + ["python", "-c", PROBE], timeout=120)
        opencode = runner(docker_run_args(settings, name=f"kite-doctor-{uuid.uuid4().hex[:8]}",
                                          workspace=Path(tmp), network="none")
                          + ["opencode", "--version"], timeout=60)
    try:
        data = json.loads((probe.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        record("probe", False, (probe.stderr or probe.stdout or "no output").strip())
        return checks
    internet = data.get("internet")
    record("gateway_reachable", data.get("gateway") == 200, data.get("gateway"))
    record("internet_blocked", not (isinstance(internet, int) and 200 <= internet < 400), internet)
    record("host_secrets_hidden", data.get("host_secrets_visible") is False, data.get("host_secrets_visible"))
    record("non_root", data.get("uid") not in (0, None), data.get("uid"))
    record("opencode", opencode.returncode == 0, (opencode.stdout or opencode.stderr).strip())
    return checks