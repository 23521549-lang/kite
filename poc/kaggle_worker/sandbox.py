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
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import tempfile
import time
import urllib.request
import uuid
from typing import Callable

from . import codemap

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
    # Let a repository declare its own setup/test commands in a committed `.kite.yml` (see repo_settings).
    repo_config: bool = True
    # Build a graphify code map of the task's clone (if graphify is installed) and use it in the
    # first prompt, the fix rounds and the self-review (see codemap.py).
    code_map: bool = True
    # Reuse what the setup step installed into /deps for later tasks of the same repository and
    # dependency files (a shared, read-only cache; see deps_key / store_deps).
    deps_cache: bool = True
    # Between agent rounds, run only the tests that matter (failing ones, the ones the agent wrote,
    # the ones the code map ties to the changed code) before the full suite (see quick_test_command).
    quick_tests: bool = True
    quick_test_command: str = ""     # from .kite.yml `quick_test`, with {tests}; derived for plain pytest
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
    setup_cached: bool = False    # setup skipped: the dependencies were reused from the cache
    quick_test_runs: int = 0
    quick_test_seconds: float = 0.0
    full_test_runs: int = 0
    stuck_stops: int = 0          # agent steps Kite stopped because they repeated themselves
    tests_edited: list[str] = field(default_factory=list)   # existing test files the agent changed

    @property
    def verified(self) -> bool:
        """Setup (if any) worked, the agent finished (or was stopped in a loop after its fix was in)
        and changed something, and Kite's tests passed."""
        return (self.setup_exit_code in (None, 0) and self.agent_status in ("ok", "stuck")
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
        # A link into the shared dependency cache is mounted read-only: no task may change it.
        args += ["-v", f"{Path(deps).resolve()}:/deps" + (":ro" if Path(deps).is_symlink() else "")]
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


CONTEXT_FILES = 150          # paths listed from `git ls-files` (fonts, images, lock files left out)
CONTEXT_CHARS = 10000        # excerpts: symbols and files the goal names (+ README), in total
CONTEXT_BLOCK_CHARS = 3000   # at most this much per excerpt
CODE_MAP_CHARS = 3500        # the graphify code map (codemap.py)
FIX_OUTPUT_CHARS = 3000      # failing test output handed back to the agent
NOISE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".bmp", ".woff", ".woff2", ".ttf",
                  ".otf", ".eot", ".pdf", ".zip", ".gz", ".tgz", ".jar", ".whl", ".so", ".dll", ".exe", ".bin",
                  ".pyc", ".map", ".min.js", ".min.css", ".lock", ".mp3", ".mp4", ".mov")
NOISE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "cargo.lock", "go.sum"}

# How to work, per task type: the part of the harness that does not depend on the repository.
TASK_GUIDES = {
    "coding": [
        "1. Đọc các file liên quan và test hiện có trước khi sửa.",
        "2. Lập kế hoạch ngắn: cần đổi file nào, vì sao.",
        "3. Sửa đúng phạm vi yêu cầu. Không sửa, xóa hay bỏ qua test để cho qua, trừ khi yêu cầu nói vậy.",
        "4. Chạy lệnh kiểm tra; nếu lỗi thì đọc lỗi, tìm nguyên nhân gốc, sửa và chạy lại cho tới khi qua. "
        "Thử một cách hai lần mà kết quả không đổi thì đổi hướng, đừng lặp lại y nguyên.",
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


REPO_CONFIG = ".kite.yml"
REPO_CONFIG_KEYS = {"setup": "setup_command", "test": "test_command", "quick_test": "quick_test_command",
                    "setup_timeout": "setup_timeout_seconds", "test_timeout": "test_timeout_seconds"}


def repo_settings(settings: SandboxSettings, workspace: Path) -> SandboxSettings:
    """Settings for one repository: its committed ``.kite.yml`` overrides the operator's defaults.

    A real project knows how it is installed and tested (``pip install -r services/x/requirements.txt``,
    ``PYTHONPATH=. pytest services/x/tests``); one global ``sandbox.test_command`` cannot. The file
    is read from the task's own clone (the committed state), so it travels with the code::

        setup: pip install -r requirements.txt
        test: python -m pytest -q tests
        quick_test: python -m pytest -q {tests}   # optional: the same tests, only the given files
        test_timeout: 900          # seconds, optional (60-3600)
        setup_timeout: 900

    The commands still run in the sandbox (setup with the operator's setup network, tests with none).
    A malformed file is an error, not a silent fallback: it would otherwise test the wrong thing.
    """
    path = Path(workspace) / REPO_CONFIG
    if not settings.repo_config or not path.is_file():
        return settings
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise SandboxError(f"{REPO_CONFIG} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - set(REPO_CONFIG_KEYS):
        raise SandboxError(f"{REPO_CONFIG} accepts only {', '.join(sorted(REPO_CONFIG_KEYS))}")
    changes = {}
    for key, field_name in REPO_CONFIG_KEYS.items():
        if key not in data:
            continue
        value = data[key]
        if key.endswith("_timeout"):
            if not isinstance(value, int) or isinstance(value, bool) or not 60 <= value <= 3600:
                raise SandboxError(f"{REPO_CONFIG}: {key} must be seconds between 60 and 3600")
        elif not isinstance(value, str):
            raise SandboxError(f"{REPO_CONFIG}: {key} must be a shell command string ('' to skip)")
        elif key == "quick_test" and value and "{tests}" not in value:
            raise SandboxError(f"{REPO_CONFIG}: quick_test must contain {{tests}} (where Kite puts the test files)")
        changes[field_name] = value
    from dataclasses import replace

    return replace(settings, **changes)


def suggest_repo_config(files: list[str], pyproject: str = "") -> str:
    """A ``.kite.yml`` proposal from a repository's file list, or "" when Kite's defaults fit
    (nothing to install, tests found by ``python -m pytest -q``). For the user to check and commit:
    Kite never writes it into a repository itself."""
    requirements = sorted((f for f in files if re.search(r"(^|/)requirements[^/]*\.txt$", f)),
                          key=lambda f: (f.count("/"), f))[:4]
    setup = ""
    if requirements:
        setup = "pip install -q " + " ".join(f"-r {f}" for f in requirements)
    elif "pyproject.toml" in files or "setup.py" in files:
        extra = re.search(r"^\s*(test|tests|dev)\s*=", pyproject, re.M)
        setup = f"pip install -q -e '.[{extra.group(1)}]'" if extra else "pip install -q -e ."
    tests = [f for f in files if f.endswith(".py") and codemap.is_test_path(f) and not f.endswith("conftest.py")]
    folders = []
    for path in tests:
        parts = path.split("/")
        cut = next((i for i, part in enumerate(parts[:-1]) if part in ("tests", "test")), len(parts) - 1)
        folder = "/".join(parts[:cut + 1]) if cut < len(parts) - 1 else "/".join(parts[:-1])
        if folder and folder not in folders:
            folders.append(folder)
    nested = [f for f in folders if "/" in f]
    if not setup and not nested:
        return ""
    lines = ["# Đề xuất của Kite: kiểm tra lại (nhất là biến môi trường test cần) rồi commit vào gốc repo"]
    if setup:
        lines.append(f"setup: {setup}")
    if nested:
        lines += [f"test: PYTHONPATH=. python -m pytest -q {' '.join(sorted(folders)[:6])}",
                  "quick_test: PYTHONPATH=. python -m pytest -q {tests}"]
    return "\n".join(lines) + "\n"


def review_prompt(goal: str, test_command: str, impact: str = "", quick: str = "",
                  edited: list[str] = ()) -> str:
    """Prompt for the self-review pass, after Kite's tests already passed.

    ``impact`` (codemap.impact): the callers of what the agent changed, found by the code map.
    """
    lines = [
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
        "3. Tìm mọi chỗ khác trong repo dùng tới phần đã đổi (`grep -rn <tên> .`) và sửa cho nhất quán"
        + (" (bắt đầu từ danh sách bên dưới)." if impact else "."),
        "4. Nếu thấy sai thì sửa code. Không sửa hay xóa file test có sẵn và conftest.py; được THÊM file test mới "
        "cho các trường hợp biên vừa kiểm tra.",
        f"5. Chạy lại `{test_command}` và đảm bảo vẫn qua.",
        "6. Kết thúc bằng: các quy tắc đã kiểm tra, chỗ đã sửa thêm (hoặc \"không cần sửa\").",
    ]
    if quick_hint(quick):
        lines.append(quick_hint(quick))
    if edited:
        lines += ["", "Bạn đã sửa hoặc xóa test có sẵn: " + ", ".join(edited) + ". Chỉ giữ nếu yêu cầu đổi đúng hành vi "
                  "mà test cũ kiểm tra; nếu chỉ để cho test qua thì trả file test về như cũ và sửa code."]
    if impact:
        lines += ["", "Phần bạn đã đổi và những chỗ đang dùng nó (bản đồ code của Kite; có thể thiếu, vẫn grep thêm):",
                  impact]
    return "\n".join(lines)


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
                and status in ("ok", "stuck") and test_code == 0)


AGENT_STATUS = {0: "ok", 124: "timeout", 125: "stuck"}
STUCK_REPEATS = 4            # the same steps this many times in a row, with the same results: stuck
STUCK_CYCLE = 4              # longest repeating block of steps looked for


class StuckWatch:
    """Notices an agent going round in circles in ``opencode run`` output.

    Each step starts with a ``> agent · model`` header; a step is its action and what came back.
    When the last few steps are one block repeated ``STUCK_REPEATS`` times with identical text
    (re-reading a file, rewriting a script, running it, getting the same answer), more of the
    same will not help: v1 of the real-repo bench lost 50 minutes of a 60-minute step that way.
    """

    def __init__(self, repeats: int = STUCK_REPEATS, cycle: int = STUCK_CYCLE):
        self.repeats, self.cycle = repeats, cycle
        self.steps: list[str] = []
        self.current: list[str] | None = None
        self.reason = ""

    @staticmethod
    def _normal(line: str) -> str:
        return re.sub(r"\d+(\.\d+)?\s*(ms|s)\b", "<t>", ANSI.sub("", line).rstrip())

    def feed(self, line: str) -> str:
        """One output line; returns the reason once the agent is stuck (and keeps returning it)."""
        if self.reason:
            return self.reason
        line = self._normal(line)
        if line.startswith("> "):
            if self.current:
                self.steps.append("\n".join(self.current).strip())
                self.reason = self._check()
            self.current = []
        elif self.current is not None and line:
            self.current.append(line)
        return self.reason

    def _check(self) -> str:
        steps = self.steps
        for size in range(1, self.cycle + 1):
            span = size * self.repeats
            if len(steps) < span:
                break
            block = steps[-size:]
            if any(steps[-span + i] != block[i % size] for i in range(span)) or not any(block):
                continue
            actions = " → ".join(step.splitlines()[0][:80] for step in block if step)
            return f"lặp lại {self.repeats} lần cùng {size} bước với cùng kết quả ({actions})"
        return ""

    def scan(self, text: str) -> str:
        for line in clean_output(text).splitlines():
            self.feed(line)
        return self.feed("> end")


def edited_tests(workspace: Path, base: str) -> list[str]:
    """Test files that existed at ``base`` and the agent changed or deleted (adding tests is fine)."""
    rows = _git(Path(workspace), "diff", "--name-status", base, check=False).splitlines()
    return sorted(row.split("\t")[-1] for row in rows
                  if row[:1] in ("M", "D", "R") and codemap.is_test_path(row.split("\t")[1]))


def _is_noise(path: str) -> bool:
    low = path.lower()
    return low.rsplit("/", 1)[-1] in NOISE_NAMES or low.endswith(NOISE_SUFFIXES)


def _dir_summary(files: list[str], limit: int = 40) -> list[str]:
    counts: dict[str, int] = {}
    for f in files:
        folder = f.rsplit("/", 1)[0] + "/" if "/" in f else "./"
        counts[folder] = counts.get(folder, 0) + 1
    entries = sorted(counts.items())
    lines = [f"  {folder} ({count})" for folder, count in entries[:limit]]
    if len(entries) > limit:
        lines.append(f"  … và {len(entries) - limit} thư mục nữa")
    return lines


def _pick_files(files: list[str], keep: list[str], limit: int) -> list[str]:
    """At most ``limit`` paths: the ones that matter for the goal and their folders first, then
    top-level files, then source before tests and docs."""
    folders = {f.rsplit("/", 1)[0] for f in keep if "/" in f}
    first = [f for f in files if f in keep]
    first += [f for f in files if "/" not in f and f not in first]
    first += [f for f in files if "/" in f and f.rsplit("/", 1)[0] in folders and f not in first]
    chosen = first[:limit]
    taken = set(chosen)
    rest = sorted((f for f in files if f not in taken),
                  key=lambda f: (codemap.is_test_path(f), f.endswith((".md", ".rst", ".txt", ".html")), f))
    return sorted(chosen + rest[:max(0, limit - len(chosen))])


def _grep_definitions(workspace: Path, names: list[str], plain: list[str] = ()) -> list[tuple[str, str, int]]:
    """(name, file, line) of ``def``/``class`` definitions of the names (when there is no graph).

    ``Class.new_method`` falls back to the class; a ``plain`` word is tried only when nothing
    code-like was found, and counts only with one or two definitions outside the tests.
    """
    def hits(word: str) -> list[tuple[str, int]]:
        out = _git(Path(workspace), "grep", "-n", "-E",
                   rf"^[[:space:]]*(async[[:space:]]+)?(def|class)[[:space:]]+{re.escape(word)}([^A-Za-z0-9_]|$)",
                   "--", ":!*.md", ":!*.rst", ":!*.txt", ":!*.html", check=False)
        rows = [line.split(":", 2) for line in out.splitlines() if line.count(":") >= 2]
        return [(path, int(number)) for path, number, _ in rows if number.isdigit()]

    found = []
    for name in names:
        rows = hits(name.rsplit(".", 1)[-1])
        if not rows and "." in name:
            rows = hits(name.rsplit(".", 1)[0].rsplit(".", 1)[-1])
        if 0 < len(rows) <= 3:
            found += [(name, path, line) for path, line in rows]
    for word in plain:
        if found:
            break
        rows = [(path, line) for path, line in hits(word) if not codemap.is_test_path(path)]
        if 0 < len(rows) <= 2:
            found += [(word, path, line) for path, line in rows]
    return found


def _numbered(lines: list[str], first: int) -> str:
    return "\n".join(f"{first + i:>5}| {line}" for i, line in enumerate(lines))


def _block(lines: list[str], line: int, max_lines: int = 60) -> tuple[int, list[str]]:
    """The definition starting at 1-based ``line`` (decorators included), by indentation."""
    i = max(0, min(line - 1, len(lines) - 1))
    indent = len(lines[i]) - len(lines[i].lstrip())
    if lines[i].lstrip().startswith("class "):
        max_lines = min(max_lines, 30)      # a class: its head; the map and outline list its methods
    start = i
    while start > 0 and lines[start - 1].strip().startswith("@"):
        start -= 1
    end = i + 1
    while end < len(lines) and end - start < max_lines:
        text = lines[end]
        if text.strip() and len(text) - len(text.lstrip()) <= indent and not text.lstrip().startswith((")", "]", "}")):
            break
        end += 1
    return start + 1, lines[start:end]


def repo_context(workspace: Path, goal: str, *, max_files: int = CONTEXT_FILES,
                 max_chars: int = CONTEXT_CHARS, graph: codemap.CodeGraph | None = None) -> str:
    """What the agent should see before its first step.

    * the files (fonts, images and lock files left out); a large repository gets a folder summary
      and the files that matter for the goal first, instead of the first N paths alphabetically;
    * with a graphify ``graph`` (codemap.py): where the symbols the goal names are defined, who
      calls/imports/inherits them, and an outline of large files the goal names;
    * excerpts with line numbers: the definitions of those symbols (a method, not the first 4000
      characters of an 800-line file), the files the goal names, the README; within ``max_chars``.
    """
    workspace = Path(workspace)
    files = [f for f in _git(workspace, "ls-files", check=False).splitlines() if f]
    if not files:
        return ""
    listed = [f for f in files if not _is_noise(f)]
    skipped = len(files) - len(listed)
    mentioned = [f for f in listed if f in goal or re.search(
        rf"(?<![\w./-]){re.escape(Path(f).name)}(?![\w-])", goal)]
    names = codemap.goal_names(goal)
    code_map, symbols = codemap.goal_map(graph, goal, mentioned, max_chars=CODE_MAP_CHARS,
                                         workspace=workspace) if graph is not None else ("", [])
    if graph is not None:
        definitions = [(graph.qualified(s), s.file, s.line) for s in symbols if s.kind != "module"]
    else:
        plain = [] if mentioned else [w for w in codemap.goal_words(goal) if w not in names]
        definitions = _grep_definitions(workspace, names, plain)

    lines = [f"Repo có {len(files)} file" + (f" ({skipped} file font/ảnh/lock không liệt kê)" if skipped else "") + "."]
    if len(listed) > max_files:
        keep = mentioned + [path for _, path, _ in definitions]
        shown = _pick_files(listed, keep, max_files)
        lines += ["Thư mục (số file):", *_dir_summary(listed),
                  f"Các file trong repo ({len(shown)}/{len(listed)}, ưu tiên file liên quan; xem thêm: "
                  "`git ls-files <thư mục>`):", *shown]
    else:
        lines += [f"Các file trong repo ({len(listed)}/{len(listed)}):", *listed]
    if code_map:
        lines += ["", "Bản đồ code (graphify phân tích AST, có thể thiếu lời gọi động; vẫn đọc file trước khi sửa):",
                  code_map]

    budget = max_chars
    shown_files: set[str] = set()
    cache: dict[str, list[str] | None] = {}

    def read(path: str) -> list[str] | None:
        if path not in cache:
            try:
                cache[path] = (workspace / path).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                cache[path] = None
        return cache[path]

    def add(title: str, text: str) -> None:
        nonlocal budget
        chunk = text[:min(budget, CONTEXT_BLOCK_CHARS)]
        lines.extend(["", f"----- {title}{' (đã cắt)' if len(chunk) < len(text) else ''} -----", chunk.rstrip()])
        budget -= len(chunk)

    for name, path, line in definitions[:6]:
        content = read(path)
        if budget < 300 or content is None:
            continue
        first, block = _block(content, line)
        add(f"{path} dòng {first}-{first + len(block) - 1}: {name}", _numbered(block, first))
        shown_files.add(path)
    readme = next((f for f in files if f.lower() in ("readme.md", "readme.rst", "readme.txt", "readme")), None)
    for path in [*mentioned[:6], *([readme] if readme and readme not in mentioned else [])]:
        content = read(path)
        if budget < 300 or content is None or path in shown_files:
            continue
        text = "\n".join(content)
        if path != readme and len(text) > CONTEXT_BLOCK_CHARS:
            add(f"{path} (60 dòng đầu; file dài {len(content)} dòng)", _numbered(content[:60], 1))
        else:
            add(path, text if path == readme else _numbered(content, 1))
    return "\n".join(lines)


def code_graph(settings: SandboxSettings, workspace: Path, out_dir: Path,
               log: Callable[[str], None] = lambda _m: None) -> codemap.CodeGraph | None:
    """The task's code map: graphify's graph of the clone, built once into ``out_dir``; None if off,
    graphify is missing or it fails (Kite then works exactly as without it)."""
    if not settings.code_map:
        return None
    started = time.monotonic()
    path = codemap.build(workspace, out_dir)
    graph = codemap.CodeGraph.load(path, workspace)
    if graph is not None:
        log(f"bản đồ code (graphify): {len(graph.symbols)} ký hiệu, {time.monotonic() - started:.1f}s")
    return graph


QUICK_TEST_MAX = 40                 # more related tests than this: the full suite is as good
_PYTEST_VALUE_OPTIONS = {"-k", "-m", "-p", "-W", "-c", "-o", "-n", "--deselect", "--ignore", "--rootdir",
                         "--maxfail", "--confcutdir", "--basetemp", "--import-mode", "--tb"}


def quick_template(settings: SandboxSettings, test_command: str | None = None) -> str:
    """The quick-test command with a ``{tests}`` slot, or "" if there is none.

    From ``.kite.yml`` (``quick_test``), else derived from a plain pytest command that has only
    options (``python -m pytest -q``): appending files to a command that already names folders
    would still run everything, so such commands need an explicit ``quick_test``.
    """
    test_command = settings.test_command if test_command is None else test_command
    if not settings.quick_tests or not test_command:
        return ""
    if settings.quick_test_command:
        return settings.quick_test_command
    try:
        tokens = shlex.split(test_command)
    except ValueError:
        return ""
    start = next((i for i, t in enumerate(tokens) if t == "pytest" or t.endswith("/pytest")), None)
    if start is None:
        return ""
    takes_value = False
    for token in tokens[start + 1:]:
        if takes_value:
            takes_value = False
        elif token in _PYTEST_VALUE_OPTIONS:
            takes_value = True
        elif not token.startswith("-"):
            return ""                         # a path, an expression or a shell operator
    return f"{test_command} {{tests}}"


def select_tests(workspace: Path, base: str, graph: codemap.CodeGraph | None = None, last_output: str = "",
                 limit: int = QUICK_TEST_MAX) -> list[str]:
    """Tests worth running before the full suite: the ones that just failed, the test files the
    agent added or changed, and (with a code map) the tests that use the code it changed."""
    workspace = Path(workspace)
    failing, written, related = [], [], []
    for node in re.findall(r"^(?:FAILED|ERROR) ([\w./-]+\.py(?:::\S+)?)", clean_output(last_output), re.M):
        if (workspace / node.split("::", 1)[0]).is_file() and node not in failing:
            failing.append(node)
    changed = set(_git(workspace, "diff", "--name-only", base, check=False).splitlines())
    changed |= set(_git(workspace, "ls-files", "--others", "--exclude-standard", check=False).splitlines())
    for path in sorted(changed):
        if path.endswith(".py") and codemap.is_test_path(path) and not path.endswith("conftest.py") \
                and (workspace / path).is_file():
            written.append(path)
    if graph is not None:
        changed_symbols = [symbol for path, lines in codemap.changed_lines(workspace, base).items()
                           if not codemap.is_test_path(path)
                           for symbol in dict.fromkeys(graph.at(path, line) for line in lines) if symbol is not None]
        # Nearest first: tests that use the changed code, then tests of its callers, then (for a
        # method) tests that use its class.
        callers = [user for symbol in changed_symbols for _, user, path, _ in graph.users(symbol)
                   if user is not None and not codemap.is_test_path(path)]
        owners = [owner for symbol in changed_symbols if symbol.kind == "method"
                  for owner in [graph.parent(symbol)] if owner is not None and owner.kind == "class"]
        for symbol in changed_symbols + callers + owners:
            for _, _, user_path, _ in graph.users(symbol):
                if (codemap.is_test_path(user_path) and user_path.endswith(".py") and user_path not in related
                        and (workspace / user_path).is_file()):
                    related.append(user_path)
        # Last: tests that import the changed module (an HTTP app is tested through its routes, which
        # the graph cannot follow), but only when few do; many importers means "everything".
        for path in dict.fromkeys(s.file for s in changed_symbols):
            module = next((s for s in graph.by_file.get(path, []) if s.kind == "module"), None)
            importers = {p for _, _, p, _ in graph.users(module)} if module is not None else set()
            importers = sorted(p for p in importers if codemap.is_test_path(p) and p.endswith(".py")
                               and (workspace / p).is_file())
            if len(importers) <= 15:
                related += [p for p in importers if p not in related]
    picked = list(dict.fromkeys(failing + written))
    if len(picked) > limit:
        return []
    for path in related:
        if path not in picked and len(picked) < limit:
            picked.append(path)
    return picked


def quick_test_command(template: str, tests: list[str]) -> str:
    return template.replace("{tests}", " ".join(shlex.quote(t) for t in tests)) if template and tests else ""


def quick_hint(template: str) -> str:
    """The line telling the agent how to run only some tests while it works."""
    if not template:
        return ""
    return ("Trong lúc sửa, chạy riêng các file test liên quan cho nhanh: `" + template.replace("{tests}", "<file test>")
            + "`; chạy bộ đầy đủ một lần trước khi kết thúc (Kite cũng tự chạy lại).")


def agent_prompt(goal: str, test_command: str, *, task_type: str = "coding", context: str = "",
                 quick: str = "") -> str:
    lines = [goal.strip(), "", "Cách làm:", *TASK_GUIDES.get(task_type, TASK_GUIDES["coding"]), "",
             "Làm việc trong thư mục hiện tại (/work). Môi trường không có Internet: "
             "không cài gói, không tải gì về; thư viện cần thiết đã được cài sẵn."]
    if test_command:
        lines.append(f"Kiểm tra lại bằng lệnh: {test_command}")
        if quick_hint(quick):
            lines.append(quick_hint(quick))
    lines.append("Không commit, không push: Kite sẽ tự lấy thay đổi để người dùng duyệt.")
    if context:
        lines += ["", "Ngữ cảnh repo (Kite chuẩn bị sẵn; vẫn đọc lại file trước khi sửa):", context]
    return "\n".join(lines)


def failure_digest(output: str, limit: int = FIX_OUTPUT_CHARS) -> str:
    """Failing pytest output for the agent: the first failure's traceback, then the summary of all.

    The last N characters of a big suite are mostly the summary list (and the last traceback);
    the first traceback with its assertion is what the agent needs to see.
    """
    text = clean_output(output)
    failures = re.search(r"^=+ (FAILURES|ERRORS) =+$", text, re.M)
    if failures is None:
        return text[-limit:]
    summary = re.search(r"^=+ short test summary info =+$", text, re.M)
    tail = text[summary.start():] if summary and summary.start() > failures.start() else ""
    tail = tail[-(limit // 3):] if tail else text[-(limit // 4):]
    body = text[failures.start():summary.start() if tail and summary else len(text)]
    head = body[:max(0, limit - len(tail))]
    return head.rstrip() + ("\n…\n" if len(head) < len(body) else "\n") + tail.lstrip()


def fix_prompt(goal: str, test_command: str, test_output: str, attempt: int, rounds: int,
               targets: str = "", quick: str = "", stuck: str = "") -> str:
    """The next round after Kite's own test run failed: same goal, plus the real failing output.

    ``targets`` (codemap.failing_targets): what each failing test calls, from the code map.
    """
    lines = [
        goal.strip(), "",
        f"Kite đã tự chạy `{test_command}` (không mạng) sau lần làm trước và test THẤT BẠI "
        f"(vòng sửa {attempt}/{rounds}). Phần cuối output:",
        "```", failure_digest(test_output, FIX_OUTPUT_CHARS).strip(), "```",
        "Các thay đổi lần trước vẫn còn trong /work. Đọc lỗi, tìm nguyên nhân gốc trong code, sửa, rồi chạy lại "
        f"`{test_command}` cho tới khi qua. Không sửa, xóa hay bỏ qua test để cho qua, trừ khi yêu cầu nói vậy.",
        "Môi trường không có Internet. Không commit, không push.",
    ]
    if targets:
        lines[-2:-2] = ["Test thất bại gọi tới (bản đồ code của Kite, để biết nên đọc chỗ nào trước):", targets]
    if quick_hint(quick):
        lines.insert(-1, quick_hint(quick))
    if stuck:
        lines[1:1] = [f"Lần trước Kite đã dừng bạn vì {stuck}. Cách đó không dẫn tới đâu: đọc lại lỗi test bên "
                      "dưới, xem lại giả định của mình và đổi hướng.", ""]
    return "\n".join(lines)


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


DEPS_CACHE_KEEP = 6               # cached dependency sets kept (the most recently used)
DEP_MANIFESTS = re.compile(
    r"(^|/)(requirements[^/]*\.(txt|in)|constraints[^/]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|Pipfile(\.lock)?"
    r"|poetry\.lock|uv\.lock|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|go\.(mod|sum)|Cargo\.(toml|lock)"
    r"|Gemfile(\.lock)?|environment\.ya?ml)$")


def deps_key(settings: SandboxSettings, workspace: Path, setup_command: str) -> str | None:
    """Cache key of what the setup step installs: image, command, and the exact dependency files
    (by Git blob, including any tracked file the command names). None when there is no setup."""
    if not setup_command:
        return None
    named = {token for token in re.split(r"[\s=]+", setup_command) if token}
    rows = [line for line in _git(Path(workspace), "ls-files", "-s", check=False).splitlines()
            if "\t" in line and (DEP_MANIFESTS.search(line.split("\t", 1)[1]) or line.split("\t", 1)[1] in named)]
    digest = hashlib.sha256(f"{settings.image}\0{setup_command}\0".encode())
    for row in sorted(rows):
        digest.update(row.encode() + b"\n")
    return digest.hexdigest()[:24]


def cached_deps(cache_root: Path | None, key: str | None) -> Path | None:
    """The cached dependency set for ``key``, if a finished one exists (and mark it used)."""
    if cache_root is None or key is None:
        return None
    path = Path(cache_root) / key
    ready = path / ".kite-ready"
    if not ready.is_file():
        return None
    os.utime(ready)
    return path


def link_deps(deps: Path, cached: Path) -> None:
    """Make the task's /deps the cached set (a symlink; mounted read-only)."""
    deps = Path(deps)
    if deps.is_symlink():
        deps.unlink()
    elif deps.exists():
        shutil.rmtree(deps)
    deps.parent.mkdir(parents=True, exist_ok=True)
    deps.symlink_to(Path(cached).resolve(), target_is_directory=True)


def workspace_state(workspace: Path) -> str:
    """Everything in the clone except Python bytecode, ignored files included (to see if setup wrote there)."""
    lines = _git(Path(workspace), "status", "--porcelain", "--ignored", "--untracked-files=all", check=False)
    return "\n".join(line for line in lines.splitlines()
                     if "__pycache__/" not in line and not line.endswith((".pyc", ".pytest_cache/")))


def store_deps(cache_root: Path | None, key: str | None, deps: Path) -> bool:
    """After a successful setup that wrote only into /deps: move the task's /deps into the cache and
    link it back. Two tasks racing for the same key: the first one stores it, the other keeps its own."""
    deps = Path(deps)
    if cache_root is None or key is None or deps.is_symlink() or not deps.is_dir():
        return False
    cache_root = Path(cache_root)
    target = cache_root / key
    if target.exists():
        return False
    cache_root.mkdir(parents=True, exist_ok=True)
    staging = cache_root / f".{key}.{uuid.uuid4().hex[:8]}"
    moved = True
    try:
        os.rename(deps, staging)                  # same filesystem: instant
    except OSError as error:
        if error.errno != errno.EXDEV:
            return False
        moved = False                             # another disk (bench runs under ~/, the cache in
        try:                                      # the data volume): copy it over instead
            shutil.copytree(deps, staging, symlinks=True)
        except (OSError, shutil.Error):
            shutil.rmtree(staging, ignore_errors=True)
            return False
    (staging / ".kite-ready").write_text(time.strftime("%Y-%m-%d %H:%M:%S\n"), encoding="utf-8")
    try:
        os.rename(staging, target)
    except OSError:                               # someone stored the same key meanwhile
        if moved:
            os.rename(staging, deps)
            (deps / ".kite-ready").unlink(missing_ok=True)
        else:
            shutil.rmtree(staging, ignore_errors=True)
        return False
    link_deps(deps, target)                       # (a copied original is removed here)
    prune_deps(cache_root)
    return True


def prune_deps(cache_root: Path, keep: int = DEPS_CACHE_KEEP, idle_seconds: float = 6 * 3600) -> None:
    """Drop the least recently used sets beyond ``keep`` (only ones idle for a while)."""
    sets = []
    for path in Path(cache_root).iterdir():
        ready = path / ".kite-ready"
        if path.is_dir() and not path.name.startswith(".") and ready.is_file():
            sets.append((ready.stat().st_mtime, path))
    sets.sort(reverse=True)
    for used, path in sets[keep:]:
        if time.time() - used > idle_seconds:
            shutil.rmtree(path, ignore_errors=True)


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
              timeout: int, watch: StuckWatch | None = None) -> tuple[int, str]:
    """Run one container step; a timeout kills the container and returns exit code 124.

    With ``watch`` (agent steps), output is read as it comes and a stuck agent is stopped at once
    (exit code 125); a custom runner (tests) only gets its finished output checked.
    """
    if watch is not None and runner is default_runner:
        return _run_watched(settings, name=name, args=args, timeout=timeout, watch=watch)
    try:
        result = runner(args, timeout=timeout)
        code, output = result.returncode, clean_output(result.stdout) + clean_output(result.stderr)
    except subprocess.TimeoutExpired as exc:
        runner([*settings.docker_command, "kill", name], timeout=30)
        return 124, clean_output(exc.stdout) + clean_output(exc.stderr) + "\n[kite] step timed out\n"
    if watch is not None and watch.scan(output):
        return 125, output + f"\n[kite] stopped: {watch.reason}\n"
    return code, output


def _run_watched(settings: SandboxSettings, *, name: str, args: list[str], timeout: int,
                 watch: StuckWatch) -> tuple[int, str]:
    proc = subprocess.Popen(list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    lines: list[str] = []
    stopped = threading.Event()

    def kill() -> None:
        subprocess.run([*settings.docker_command, "kill", name], capture_output=True, timeout=30)

    def read() -> None:
        for line in proc.stdout:
            lines.append(line)
            if not stopped.is_set() and watch.feed(line):
                stopped.set()
                kill()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        kill()
        proc.wait(timeout=60)
    reader.join(timeout=30)
    output = clean_output("".join(lines))
    if stopped.is_set():
        return 125, output + f"\n[kite] stopped: {watch.reason}\n"
    if timed_out:
        return 124, output + "\n[kite] step timed out\n"
    return proc.returncode, output


def _write_result(root: Path, run: SandboxRun) -> SandboxRun:
    (root / "result.json").write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    return run


def run_task(settings: SandboxSettings, *, repo: Path, goal: str, model: str, models,
             state_dir: Path, task_id: str | None = None, small_model: str | None = None,
             test_command: str | None = None, setup_command: str | None = None, task_type: str = "coding",
             with_context: bool = True, runner: Runner = default_runner, log: Callable[[str], None] = print,
             worker_pin: str | None = None, deps_cache: Path | None = None) -> SandboxRun:
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
    settings = repo_settings(settings, workspace)
    deps = root / "deps"
    (deps / "cache").mkdir(parents=True, exist_ok=True)
    run = SandboxRun(
        task_id, str(Path(repo).resolve()), base_head, model, goal, str(workspace),
        patch_base=base_head,
        setup_command=settings.setup_command if setup_command is None else setup_command,
        test_command=settings.test_command if test_command is None else test_command)

    # 1. Setup: the only step with network access, and it runs only the declared command.
    #    Already installed for these dependency files (deps cache): reuse, no setup at all.
    key = deps_key(settings, workspace, run.setup_command) if settings.deps_cache else None
    cached = cached_deps(deps_cache, key)
    if cached is not None:
        link_deps(deps, cached)
        run.setup_exit_code, run.setup_cached = 0, True
        log(f"[{task_id}] dùng lại thư viện đã cài ({cached.name}), bỏ qua bước cài")
    elif run.setup_command:
        log(f"[{task_id}] cài thư viện (có mạng): {run.setup_command}")
        before = workspace_state(workspace)
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
        if workspace_state(workspace) == before and store_deps(deps_cache, key, deps):
            log(f"[{task_id}] đã lưu thư viện để task sau dùng lại ({key})")
        run.patch_base = commit_setup_changes(workspace)

    # 2. Agent (internal network, gateway only), then 3. Kite's own tests (no network at all).
    #    When the tests fail, the agent gets the failing output and tries again (settings.fix_rounds).
    config_file = root / "opencode.json"
    config_file.write_text(json.dumps(agent_config(settings, model=model, models=models, small_model=small_model,
                                                   task_tag=task_id, worker_pin=worker_pin), indent=2),
                           encoding="utf-8")
    graph = code_graph(settings, workspace, root / "graph", lambda m: log(f"[{task_id}] {m}")) if with_context else None
    template = quick_template(settings, run.test_command)
    prompt = agent_prompt(goal, run.test_command, task_type=task_type,
                          context=repo_context(workspace, goal, graph=graph) if with_context else "", quick=template)
    agent_log, test_log = [], []
    attempt, test_output = 0, ""

    def run_tests(after: str, last_output: str = "") -> tuple[int, str, str]:
        """Kite's test run: the related tests first (seconds), the full suite only if they pass."""
        quick = quick_test_command(template, select_tests(workspace, run.patch_base, graph, last_output))
        if quick:
            log(f"[{task_id}] test nhanh (không mạng): {quick}")
            name = container_name("quick", task_id)
            started = time.monotonic()
            code, output = _run_step(
                settings, runner, name=name, timeout=settings.test_timeout_seconds,
                args=docker_run_args(settings, name=name, workspace=workspace, network="none", deps=deps)
                + ["sh", "-c", quick])
            run.quick_test_runs += 1
            run.quick_test_seconds = round(run.quick_test_seconds + time.monotonic() - started, 1)
            test_log.append(f"===== quick after {after} exit={code} =====\n{output}")
            if code == 1:                  # tests failed: no need to run everything to know that
                return code, output, quick
        log(f"[{task_id}] Kite tự chạy test (không mạng): {run.test_command}")
        name = container_name("test", task_id)
        started = time.monotonic()
        code, output = _run_step(
            settings, runner, name=name, timeout=settings.test_timeout_seconds,
            args=docker_run_args(settings, name=name, workspace=workspace, network="none", deps=deps)
            + ["sh", "-c", run.test_command])
        run.full_test_runs += 1
        run.test_seconds = round(run.test_seconds + time.monotonic() - started, 1)
        test_log.append(f"===== after {after} exit={code} =====\n{output}")
        return code, output, run.test_command
    while True:
        kind = "agent" if attempt == 0 else f"fix{attempt}"
        log(f"[{task_id}] {'agent' if attempt == 0 else f'vòng sửa {attempt}/{settings.fix_rounds}:'} {model} "
            f"đang làm trong sandbox (tối đa {settings.agent_timeout_seconds}s)...")
        name = container_name(kind, task_id)
        started = time.monotonic()
        watch = StuckWatch()
        run.agent_exit_code, output = _run_step(
            settings, runner, name=name, timeout=settings.agent_timeout_seconds, watch=watch,
            args=docker_run_args(settings, name=name, workspace=workspace, network=settings.network,
                                 config_file=config_file, deps=deps)
            + ["opencode", "run", "-m", f"kite/{model}", prompt])
        run.agent_status = AGENT_STATUS.get(run.agent_exit_code, "failed")
        if run.agent_status == "stuck":
            run.stuck_stops += 1
            log(f"[{task_id}] Kite dừng agent: {watch.reason}")
        run.agent_seconds = round(run.agent_seconds + time.monotonic() - started, 1)
        run.agent_output_tail = _tail(output)
        agent_log.append(f"===== {kind} exit={run.agent_exit_code} =====\n{output}")
        if not run.test_command:
            break
        run.test_exit_code, test_output, ran = run_tests(kind, test_output)
        run.test_output_tail = _tail(test_output)
        if run.test_exit_code == 0 or run.agent_status not in ("ok", "stuck") or attempt >= settings.fix_rounds:
            break
        attempt += 1
        run.fix_rounds_used = attempt
        prompt = fix_prompt(goal, ran, test_output, attempt, settings.fix_rounds,
                            codemap.failing_targets(graph, test_output) if graph is not None else "", quick=template,
                            stuck=watch.reason)

    # 4. Self-review: tests pass, but passing tests is not the spec. Keep it only if tests still pass.
    if wants_review(settings, task_type, run.test_command, run.agent_status, run.test_exit_code):
        before = snapshot_tree(workspace)
        log(f"[{task_id}] vòng rà soát: {model} kiểm tra lại lời giải theo yêu cầu và docstring...")
        name = container_name("review", task_id)
        started = time.monotonic()
        review_code, output = _run_step(
            settings, runner, name=name, timeout=settings.agent_timeout_seconds, watch=StuckWatch(),
            args=docker_run_args(settings, name=name, workspace=workspace, network=settings.network,
                                 config_file=config_file, deps=deps)
            + ["opencode", "run", "-m", f"kite/{model}",
               review_prompt(goal, run.test_command,
                             codemap.impact(graph, workspace, run.patch_base) if graph is not None else "",
                             quick=template, edited=edited_tests(workspace, run.patch_base))])
        run.agent_seconds = round(run.agent_seconds + time.monotonic() - started, 1)
        agent_log.append(f"===== review exit={review_code} =====\n{output}")
        if snapshot_tree(workspace) == before:
            run.review = "unchanged"
        else:
            code, test_output, _ = run_tests("review")
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
    run.tests_edited = edited_tests(workspace, run.patch_base)
    if run.tests_edited:
        log(f"[{task_id}] lưu ý: agent đã sửa test có sẵn: {', '.join(run.tests_edited)}")
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
