"""Per-task Docker sandbox for the coding agent.

Each task gets its own clone of the repository, mounted into throw-away containers:

1. **setup** (optional, with network): runs the operator-declared install command, for example
   ``pip install -r requirements.txt`` or ``npm ci``. Python packages go to ``/deps`` (a per-task
   directory shared with the next steps). Files the setup step creates in the repository are
   committed as a separate "setup" commit so they never end up in the agent's patch.
2. **agent** (no Internet): OpenCode (``opencode run``) on an *internal* Docker network whose only
   reachable service is the Kite gateway. No capabilities, no privilege gain, unprivileged user,
   CPU/memory/process limits, no secrets mounted, no Git remote.
3. **test** (no network at all): Kite runs the test command itself, so the result carries evidence
   instead of the model's claim that the tests pass.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess]

TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
JUNK_PATTERNS = ("__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
                 ".coverage", "node_modules/", ".venv/", ".opencode/", "*.egg-info/")
TAIL_CHARS = 4000
# Shared by every step: pip installs into /deps (user site), caches stay out of the repository.
DEPS_ENV = (
    ("PYTHONUSERBASE", "/deps/python"),
    ("PIP_USER", "1"),
    ("PIP_CACHE_DIR", "/deps/cache/pip"),
    ("NPM_CONFIG_CACHE", "/deps/cache/npm"),
    ("PATH", "/deps/python/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"),
)

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
    setup_command: str = ""
    setup_network: str = "bridge"
    setup_timeout_seconds: int = 1800
    docker_command: tuple[str, ...] = ("docker",)
    uid: int = field(default_factory=_default_uid)
    # After Kite's own test run fails, the agent gets the failing output and tries again this many times.
    fix_rounds: int = 2
    # After the tests pass (coding tasks): one more agent pass that re-checks the solution against the
    # goal and docstrings (edge cases, every caller). Kept only if the tests still pass afterwards.
    self_review: bool = True
    # Context window per served model (aliases included), told to OpenCode; missing models: 32768.
    model_context: dict[str, int] = field(default_factory=dict)


@dataclass
class SandboxRun:
    task_id: str
    repo: str
    base_head: str
    model: str
    goal: str
    workspace: str
    patch_base: str = ""
    setup_command: str = ""
    setup_exit_code: int | None = None
    setup_seconds: float = 0.0
    setup_output_tail: str = ""
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
    fix_rounds_used: int = 0
    review: str = "skipped"       # skipped | unchanged | kept | reverted

    @property
    def verified(self) -> bool:
        """Setup (if any) worked, the agent finished and changed something, Kite's tests passed."""
        return (self.setup_exit_code in (None, 0) and self.agent_status == "ok"
                and self.test_exit_code == 0 and bool(self.changed_files))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["verified"] = self.verified
        return data


def default_runner(cmd, *, timeout=None) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def clean_output(text) -> str:
    """Decode and strip terminal colour codes so logs and JSON stay readable."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return ANSI.sub("", text)


def _tail(text, limit: int = TAIL_CHARS) -> str:
    return clean_output(text)[-limit:]


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SandboxError(f"git {' '.join(args)} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def container_name(kind: str, task_id: str) -> str:
    return f"kite-{kind}-{task_id}"


def docker_run_args(settings: SandboxSettings, *, name: str, workspace: Path, network: str,
                    config_file: Path | None = None, deps: Path | None = None,
                    extra_env=()) -> list[str]:
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
    if deps is not None:
        args += ["-v", f"{Path(deps).resolve()}:/deps"]
        for key, value in DEPS_ENV:
            args += ["-e", f"{key}={value}"]
    for key, value in extra_env:
        args += ["-e", f"{key}={value}"]
    if config_file is not None:
        args += ["-v", f"{Path(config_file).resolve()}:/home/kite/.config/opencode/opencode.json:ro"]
    args.append(settings.image)
    return args


def agent_config(settings: SandboxSettings, *, model: str, models, small_model: str | None = None,
                 task_tag: str | None = None, worker_pin: str | None = None) -> dict:
    """OpenCode config for the sandbox: gateway provider, default model, no permission prompts.

    ``task_tag`` is sent as ``X-Kite-Task`` on every model request, so the gateway journal can tell
    which task each request belongs to. ``worker_pin`` (``X-Kite-Worker``) makes the gateway send
    the requests to the worker the task's route reserved.
    """
    from .gateway import opencode_provider_config

    base_url = settings.gateway_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    config = opencode_provider_config(base_url, list(models), context=dict(settings.model_context))
    config["model"] = f"kite/{model}"
    headers = {}
    if task_tag:
        headers["X-Kite-Task"] = task_tag
    if worker_pin:
        headers["X-Kite-Worker"] = worker_pin
    if headers:
        config["provider"]["kite"]["options"]["headers"] = headers
    if small_model:
        config["small_model"] = f"kite/{small_model}"
    # The container is the safety boundary: inside it the agent may edit and run commands without
    # asking. An unanswered permission prompt used to hang the whole task (defect E1).
    config["permission"] = {"edit": "allow", "bash": "allow", "webfetch": "deny"}
    return config


CONTEXT_FILES = 150          # paths listed from `git ls-files`
CONTEXT_CHARS = 8000         # content of files named in the goal (+ README), in total
FIX_OUTPUT_CHARS = 3000      # failing test output handed back to the agent

# How to work, per task type: the part of the harness that does not depend on the repository.
TASK_GUIDES = {
    "coding": [
        "1. Đọc các file liên quan và test hiện có trước khi sửa.",
        "2. Lập kế hoạch ngắn: cần đổi file nào, vì sao.",
        "3. Sửa đúng phạm vi yêu cầu. Không sửa, xóa hay bỏ qua test để cho qua, trừ khi yêu cầu nói vậy.",
        "4. Chạy lệnh kiểm tra; nếu lỗi thì đọc lỗi, tìm nguyên nhân gốc, sửa và chạy lại cho tới khi qua.",
        "5. Kết thúc bằng tóm tắt ngắn: đã đổi gì, vì sao, kết quả kiểm tra.",
    ],
    "analysis": [
        "1. Đọc code thật (chạy lệnh nếu cần) trước khi kết luận; không đoán nội dung file chưa đọc.",
        "2. Mỗi nhận định ghi file:dòng làm bằng chứng.",
        "3. Tách rõ điều đã kiểm chứng với điều còn là suy đoán.",
        "4. Không sửa code, trừ khi yêu cầu nói vậy.",
        "5. Trả lời bằng tiếng Việt, theo thứ tự: tóm tắt, chi tiết, đề xuất.",
    ],
    "review": [
        "1. Đọc kỹ phần code được hỏi và các chỗ gọi tới nó.",
        "2. Liệt kê vấn đề theo mức độ (nghiêm trọng, trung bình, nhỏ), mỗi vấn đề có file:dòng, "
        "tình huống gây lỗi và cách sửa.",
        "3. Chạy test hoặc lệnh để kiểm chứng khi có thể; ghi rõ vấn đề nào đã kiểm chứng.",
        "4. Không sửa code, trừ khi yêu cầu nói vậy. Trả lời bằng tiếng Việt.",
    ],
    "docs": [
        "1. Đọc code thật; mọi tên hàm, lệnh, tham số trong tài liệu phải khớp với code.",
        "2. Viết ngắn gọn, có ví dụ chạy được.",
        "3. Không đổi code, trừ khi yêu cầu nói vậy.",
    ],
}


def review_prompt(goal: str, test_command: str) -> str:
    """Prompt for the self-review pass, after Kite's tests already passed."""
    return "\n".join([
        "Test của Kite đã QUA. Đây là vòng RÀ SOÁT lời giải trước khi bàn giao (không làm lại từ đầu).",
        "",
        "Yêu cầu gốc:",
        goal.strip(),
        "",
        "Làm lần lượt:",
        "1. Đọc lại yêu cầu và docstring/đặc tả của mọi hàm, class bạn đã sửa hoặc viết. Liệt kê TỪNG quy tắc "
        "và ví dụ trong đó, kể cả trường hợp biên (số âm, 0, rỗng, ranh giới, kiểu đầu vào khác nhau, lỗi phải báo).",
        "2. Kiểm tra từng quy tắc bằng cách chạy thật, ví dụ `python -c \"...\"`. Test hiện có thường chưa đủ: "
        "qua test không có nghĩa là đúng đặc tả.",
        "3. Tìm mọi chỗ khác trong repo dùng tới phần đã đổi (`grep -rn <tên> .`) và sửa cho nhất quán.",
        "4. Nếu thấy sai thì sửa code. Không sửa hay xóa file test có sẵn và conftest.py; được THÊM file test mới "
        "cho các trường hợp biên vừa kiểm tra.",
        f"5. Chạy lại `{test_command}` và đảm bảo vẫn qua.",
        "6. Kết thúc bằng: các quy tắc đã kiểm tra, chỗ đã sửa thêm (hoặc \"không cần sửa\").",
    ])


def snapshot_tree(workspace: Path) -> str:
    """Git tree of the whole workspace right now (new files included), to compare or restore later."""
    _git(workspace, "add", "-A")
    return _git(workspace, "write-tree")


def restore_tree(workspace: Path, tree: str) -> None:
    """Put the workspace back exactly as it was at ``snapshot_tree`` (drops files created since)."""
    _git(workspace, "read-tree", "-u", "--reset", tree)
    _git(workspace, "clean", "-fdq")


def wants_review(settings: SandboxSettings, task_type: str, test_command: str, status: str, test_code) -> bool:
    return bool(settings.self_review and task_type == "coding" and test_command
                and status == "ok" and test_code == 0)


def repo_context(workspace: Path, goal: str, *, max_files: int = CONTEXT_FILES,
                 max_chars: int = CONTEXT_CHARS) -> str:
    """What the agent should see before its first step: the file list, then the content of the
    files the goal names (by path or file name) and the README, within a character budget."""
    files = [f for f in _git(Path(workspace), "ls-files", check=False).splitlines() if f]
    if not files:
        return ""
    shown = files[:max_files]
    lines = [f"Các file trong repo ({len(shown)}/{len(files)}):", *shown]
    mentioned = [f for f in files if f in goal or re.search(
        rf"(?<![\w./-]){re.escape(Path(f).name)}(?![\w-])", goal)]
    readme = next((f for f in files if f.lower() in ("readme.md", "readme.rst", "readme.txt", "readme")), None)
    budget = max_chars
    for name in [*mentioned[:6], *([readme] if readme and readme not in mentioned else [])]:
        if budget < 200:
            break
        try:
            text = (Path(workspace) / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunk = text[:min(budget, 4000)]
        lines += ["", f"----- {name}{' (đã cắt)' if len(chunk) < len(text) else ''} -----", chunk.rstrip()]
        budget -= len(chunk)
    return "\n".join(lines)


def agent_prompt(goal: str, test_command: str, *, task_type: str = "coding", context: str = "") -> str:
    lines = [goal.strip(), "", "Cách làm:", *TASK_GUIDES.get(task_type, TASK_GUIDES["coding"]), "",
             "Làm việc trong thư mục hiện tại (/work). Môi trường không có Internet: "
             "không cài gói, không tải gì về; thư viện cần thiết đã được cài sẵn."]
    if test_command:
        lines.append(f"Kiểm tra lại bằng lệnh: {test_command}")
    lines.append("Không commit, không push: Kite sẽ tự lấy thay đổi để người dùng duyệt.")
    if context:
        lines += ["", "Ngữ cảnh repo (Kite chuẩn bị sẵn; vẫn đọc lại file trước khi sửa):", context]
    return "\n".join(lines)


def fix_prompt(goal: str, test_command: str, test_output: str, attempt: int, rounds: int) -> str:
    """The next round after Kite's own test run failed: same goal, plus the real failing output."""
    return "\n".join([
        goal.strip(), "",
        f"Kite đã tự chạy `{test_command}` (không mạng) sau lần làm trước và test THẤT BẠI "
        f"(vòng sửa {attempt}/{rounds}). Phần cuối output:",
        "```", _tail(test_output, FIX_OUTPUT_CHARS).strip(), "```",
        "Các thay đổi lần trước vẫn còn trong /work. Đọc lỗi, tìm nguyên nhân gốc trong code, sửa, rồi chạy lại "
        f"`{test_command}` cho tới khi qua. Không sửa, xóa hay bỏ qua test để cho qua, trừ khi yêu cầu nói vậy.",
        "Môi trường không có Internet. Không commit, không push.",
    ])


def prepare_clone(repo: Path, workspace: Path, *, head: str | None = None,
                  require_clean: bool = True) -> str:
    """Clone the committed state of ``repo`` into ``workspace``; return the base commit.

    ``head`` pins a specific commit (Temporal tasks pin it when they are created); by default the
    repository's current HEAD is used and uncommitted changes are refused.
    """
    repo = Path(repo).resolve()
    if _git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise SandboxError(f"{repo} is not a Git repository")
    if require_clean and _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise SandboxError("repository has uncommitted changes; commit or stash them first")
    head = head or _git(repo, "rev-parse", "HEAD")
    if _git(repo, "cat-file", "-t", head, check=False) != "commit":
        raise SandboxError(f"commit {head} does not exist in {repo}")
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


def commit_setup_changes(workspace: Path) -> str:
    """Commit whatever the setup step created so the agent's patch excludes it; return HEAD."""
    if _git(workspace, "status", "--porcelain"):
        _git(workspace, "add", "-A")
        _git(workspace, "-c", "user.name=Kite sandbox", "-c", "user.email=kite@sandbox.invalid",
             "commit", "--quiet", "--no-verify", "-m", "kite: setup artefacts")
    return _git(workspace, "rev-parse", "HEAD")


def collect_patch(workspace: Path, base: str, patch_path: Path) -> list[str]:
    """Write every change since ``base`` (including new files) as a patch; return file names."""
    _git(workspace, "add", "-A")
    changed = [line for line in _git(workspace, "diff", "--cached", "--name-only", base).splitlines() if line]
    patch = subprocess.run(["git", "-C", str(workspace), "diff", "--cached", "--binary", base],
                           capture_output=True, text=True)
    if patch.returncode != 0:
        raise SandboxError(f"git diff failed: {patch.stderr.strip()[:500]}")
    patch_path.write_text(patch.stdout, encoding="utf-8")
    return changed


def _run_step(settings: SandboxSettings, runner: Runner, *, name: str, args: list[str],
              timeout: int) -> tuple[int, str]:
    """Run one container step; a timeout kills the container and returns exit code 124."""
    try:
        result = runner(args, timeout=timeout)
        return result.returncode, clean_output(result.stdout) + clean_output(result.stderr)
    except subprocess.TimeoutExpired as exc:
        runner([*settings.docker_command, "kill", name], timeout=30)
        return 124, clean_output(exc.stdout) + clean_output(exc.stderr) + "\n[kite] step timed out\n"


def _write_result(root: Path, run: SandboxRun) -> SandboxRun:
    (root / "result.json").write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    return run


def run_task(settings: SandboxSettings, *, repo: Path, goal: str, model: str, models,
             state_dir: Path, task_id: str | None = None, small_model: str | None = None,
             test_command: str | None = None, setup_command: str | None = None, task_type: str = "coding",
             with_context: bool = True, runner: Runner = default_runner, log: Callable[[str], None] = print) -> SandboxRun:
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
    deps = root / "deps"
    (deps / "cache").mkdir(parents=True, exist_ok=True)
    run = SandboxRun(
        task_id, str(Path(repo).resolve()), base_head, model, goal, str(workspace),
        patch_base=base_head,
        setup_command=settings.setup_command if setup_command is None else setup_command,
        test_command=settings.test_command if test_command is None else test_command)

    # 1. Setup: the only step with network access, and it runs only the declared command.
    if run.setup_command:
        log(f"[{task_id}] cài thư viện (có mạng): {run.setup_command}")
        name = container_name("setup", task_id)
        started = time.monotonic()
        run.setup_exit_code, output = _run_step(
            settings, runner, name=name, timeout=settings.setup_timeout_seconds,
            args=docker_run_args(settings, name=name, workspace=workspace,
                                 network=settings.setup_network, deps=deps)
            + ["sh", "-c", run.setup_command])
        run.setup_seconds = round(time.monotonic() - started, 1)
        run.setup_output_tail = _tail(output)
        (root / "setup.log").write_text(output, encoding="utf-8")
        if run.setup_exit_code != 0:
            run.agent_status = "skipped"
            log(f"[{task_id}] setup lỗi (exit {run.setup_exit_code}); không chạy agent. Xem setup.log")
            return _write_result(root, run)
        run.patch_base = commit_setup_changes(workspace)

    # 2. Agent (internal network, gateway only), then 3. Kite's own tests (no network at all).
    #    When the tests fail, the agent gets the failing output and tries again (settings.fix_rounds).
    config_file = root / "opencode.json"
    config_file.write_text(json.dumps(agent_config(settings, model=model, models=models, small_model=small_model,
                                                   task_tag=task_id), indent=2), encoding="utf-8")
    prompt = agent_prompt(goal, run.test_command, task_type=task_type, context=repo_context(workspace, goal) if with_context else "")
    agent_log, test_log = [], []
    attempt = 0
    while True:
        kind = "agent" if attempt == 0 else f"fix{attempt}"
        log(f"[{task_id}] {'agent' if attempt == 0 else f'vòng sửa {attempt}/{settings.fix_rounds}:'} {model} "
            f"đang làm trong sandbox (tối đa {settings.agent_timeout_seconds}s)...")
        name = container_name(kind, task_id)
        started = time.monotonic()
        run.agent_exit_code, output = _run_step(
            settings, runner, name=name, timeout=settings.agent_timeout_seconds,
            args=docker_run_args(settings, name=name, workspace=workspace, network=settings.network,
                                 config_file=config_file, deps=deps)
            + ["opencode", "run", "-m", f"kite/{model}", prompt])
        run.agent_status = {0: "ok", 124: "timeout"}.get(run.agent_exit_code, "failed")
        run.agent_seconds = round(run.agent_seconds + time.monotonic() - started, 1)
        run.agent_output_tail = _tail(output)
        agent_log.append(f"===== {kind} exit={run.agent_exit_code} =====\n{output}")
        if not run.test_command:
            break
        log(f"[{task_id}] Kite tự chạy test (không mạng): {run.test_command}")
        name = container_name("test", task_id)
        started = time.monotonic()
        run.test_exit_code, test_output = _run_step(
            settings, runner, name=name, timeout=settings.test_timeout_seconds,
            args=docker_run_args(settings, name=name, workspace=workspace, network="none", deps=deps)
            + ["sh", "-c", run.test_command])
        run.test_seconds = round(run.test_seconds + time.monotonic() - started, 1)
        run.test_output_tail = _tail(test_output)
        test_log.append(f"===== after {kind} exit={run.test_exit_code} =====\n{test_output}")
        if run.test_exit_code == 0 or run.agent_status != "ok" or attempt >= settings.fix_rounds:
            break
        attempt += 1
        run.fix_rounds_used = attempt
        prompt = fix_prompt(goal, run.test_command, test_output, attempt, settings.fix_rounds)

    # 4. Self-review: tests pass, but passing tests is not the spec. Keep it only if tests still pass.
    if wants_review(settings, task_type, run.test_command, run.agent_status, run.test_exit_code):
        before = snapshot_tree(workspace)
        log(f"[{task_id}] vòng rà soát: {model} kiểm tra lại lời giải theo yêu cầu và docstring...")
        name = container_name("review", task_id)
        started = time.monotonic()
        review_code, output = _run_step(
            settings, runner, name=name, timeout=settings.agent_timeout_seconds,
            args=docker_run_args(settings, name=name, workspace=workspace, network=settings.network,
                                 config_file=config_file, deps=deps)
            + ["opencode", "run", "-m", f"kite/{model}", review_prompt(goal, run.test_command)])
        run.agent_seconds = round(run.agent_seconds + time.monotonic() - started, 1)
        agent_log.append(f"===== review exit={review_code} =====\n{output}")
        if snapshot_tree(workspace) == before:
            run.review = "unchanged"
        else:
            name = container_name("test", task_id)
            started = time.monotonic()
            code, test_output = _run_step(
                settings, runner, name=name, timeout=settings.test_timeout_seconds,
                args=docker_run_args(settings, name=name, workspace=workspace, network="none", deps=deps)
                + ["sh", "-c", run.test_command])
            run.test_seconds = round(run.test_seconds + time.monotonic() - started, 1)
            test_log.append(f"===== after review exit={code} =====\n{test_output}")
            if review_code == 0 and code == 0:
                run.review, run.agent_output_tail, run.test_output_tail = "kept", _tail(output), _tail(test_output)
            else:
                restore_tree(workspace, before)
                run.review = "reverted"
                log(f"[{task_id}] rà soát làm hỏng (agent exit {review_code}, test exit {code}): trả lại bản trước")
    (root / "agent.log").write_text("\n".join(agent_log), encoding="utf-8")
    if test_log:
        (root / "test.log").write_text("\n".join(test_log), encoding="utf-8")

    patch_path = root / "result.patch"
    run.changed_files = collect_patch(workspace, run.patch_base, patch_path)
    run.patch_path = str(patch_path)
    return _write_result(root, run)


def require_worker(settings: SandboxSettings, model: str, *, fetch=None) -> dict:
    """Fail fast when no live worker serves ``model``.

    Without this check the agent would sit on the gateway until the job timeout (30 minutes)
    whenever the Kaggle notebook is not running.
    """
    base = settings.gateway_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = base + "/status"
    if fetch is None:
        def fetch(target: str) -> dict:
            with urllib.request.urlopen(target, timeout=10) as response:
                return json.loads(response.read())
    try:
        status = fetch(url)
    except Exception as exc:
        raise SandboxError(f"cannot reach the gateway at {url} ({exc}); is kite-gateway running?") from exc
    for worker in status.get("workers", []):
        if worker.get("alive") and not worker.get("draining") and model in worker.get("models", []):
            return worker
    raise SandboxError(
        f"no live worker serves {model}: start the Kaggle notebook (Worker mode), wait for "
        "'Worker đã kết nối', then retry")


def _stream(cmd: list[str], timeout: int, log: Callable[[str], None]) -> tuple[int, str]:
    """Run a long command, echo its output line by line, keep the last lines for errors."""
    tail: deque[str] = deque(maxlen=200)
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    assert process.stdout is not None
    for line in process.stdout:
        line = clean_output(line.rstrip("\n"))
        tail.append(line)
        log(line)
        if time.monotonic() > deadline:
            process.kill()
            tail.append("[kite] build timed out")
            break
    return process.wait(), "\n".join(tail)


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
                opencode_cache: Path | None, runner: Runner = default_runner,
                log: Callable[[str], None] | None = None) -> str:
    """Build the sandbox image with the host's OpenCode binary and package cache baked in.

    With ``log`` the build output is streamed line by line (the build takes several minutes).
    """
    with tempfile.TemporaryDirectory(prefix="kite-sandbox-build-") as tmp:
        context = Path(tmp)
        shutil.copy2(dockerfile, context / "Dockerfile")
        shutil.copy2(opencode_binary, context / "opencode")
        cache = context / "opencode-cache"
        if opencode_cache is not None and opencode_cache.is_dir():
            shutil.copytree(opencode_cache, cache, symlinks=True)
        else:
            cache.mkdir()
        cmd = [*settings.docker_command, "build", "--progress=plain",
               "--build-arg", f"KITE_UID={settings.uid}", "-t", settings.image, str(context)]
        if log is not None and runner is default_runner:
            returncode, output = _stream(cmd, 1800, log)
        else:
            result = runner(cmd, timeout=1800)
            returncode, output = result.returncode, (result.stdout or "") + (result.stderr or "")
        if returncode != 0:
            raise SandboxError("docker build failed:\n" + _tail(output, 2000))
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
    record("opencode", opencode.returncode == 0, clean_output(opencode.stdout or opencode.stderr).strip())
    return checks
