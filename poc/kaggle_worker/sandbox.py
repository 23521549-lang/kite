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
from typing import Callable, Sequence

from . import codemap, editblocks

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
    host_gateway_url: str = ""       # the same gateway as Kite itself (on the host) reaches it; "" = gateway_url
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
    # After the tests pass: the repository's own static check (lint, types), from .kite.yml `check`.
    # {files} / {py_files} are filled with the changed files. Part of "verified", like the tests.
    check_command: str = ""
    # Coding tasks: before the agent, ask the model once for the whole fix as SEARCH/REPLACE blocks on
    # the code Kite located itself (Agentless-style). Kept only if Kite's tests and check pass; else
    # the clone is put back and the agent runs as usual (see try_fast_path).
    fast_path: bool = True
    # After a fast fix passed: review it with one model call too (approve, or SEARCH/REPLACE corrections
    # kept only if the tests and check still pass) instead of a whole agent session.
    fast_review: bool = True
    # Best-of-N on the fast path (Harness²-style: several cheap candidates, Kite's tests choose). After a
    # candidate fails, ask again this many times, told why the last one failed (never the reproduction
    # test itself: the fix stays independent of it).
    fast_retries: int = 1
    # With another worker idle on the same model, ask it for a second candidate at the same time.
    fast_parallel: bool = True
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
    check_command: str = ""
    check_exit_code: int | None = None    # None: no check ran (none declared, nothing to check)
    check_seconds: float = 0.0
    check_runs: int = 0
    check_output_tail: str = ""
    scratch_files: list[str] = field(default_factory=list)  # new files that look like experiments
    # off | skipped | passed | declined | not_applied | tests_failed | check_failed | error
    fast_path: str = "off"
    fast_seconds: float = 0.0
    review_mode: str = ""         # agent | one_call
    fast_attempts: int = 0        # fast-path candidates tried (best-of-N)
    repro: str = "off"            # the fast path's reproduction test: off | none | broken | not_reproduced
    #                               | reproduced

    @property
    def verified(self) -> bool:
        """Setup (if any) worked, the agent finished (or was stopped in a loop after its fix was in)
        and changed something, Kite's tests passed and so did the check (if any)."""
        return (self.setup_exit_code in (None, 0) and self.agent_status in ("ok", "stuck")
                and self.test_exit_code == 0 and self.check_exit_code in (None, 0) and bool(self.changed_files))

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
        "5. Trước khi kết thúc: xóa file thử bạn tạo (reproduce_*.py, debug_*.py...) và print gỡ lỗi bạn thêm. "
        "Rồi tóm tắt ngắn: đã đổi gì, vì sao, kết quả kiểm tra.",
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
                    "check": "check_command",
                    "setup_timeout": "setup_timeout_seconds", "test_timeout": "test_timeout_seconds"}


def repo_settings(settings: SandboxSettings, workspace: Path) -> SandboxSettings:
    """Settings for one repository: its committed ``.kite.yml`` overrides the operator's defaults.

    A real project knows how it is installed and tested (``pip install -r services/x/requirements.txt``,
    ``PYTHONPATH=. pytest services/x/tests``); one global ``sandbox.test_command`` cannot. The file
    is read from the task's own clone (the committed state), so it travels with the code::

        setup: pip install -r requirements.txt
        test: python -m pytest -q tests
        quick_test: python -m pytest -q {tests}   # optional: the same tests, only the given files
        check: ruff check --select E9,F63,F7,F82 {py_files}   # optional: after the tests pass
        test_timeout: 900          # seconds, optional (60-3600)
        setup_timeout: 900

    ``check`` (lint, type check) runs after the tests pass, without network; a failure goes back to the
    agent like a failing test. ``{files}`` / ``{py_files}`` become the changed (Python) files; with
    nothing to check there, the check is skipped. Its tool must be installed by ``setup``.
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
        elif key == "check" and value and "{tests}" in value:
            raise SandboxError(f"{REPO_CONFIG}: check takes {{files}} or {{py_files}}, not {{tests}}")
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
    ruff = "ruff.toml" in files or ".ruff.toml" in files or "[tool.ruff" in pyproject
    if not setup and not nested and not ruff:
        return ""
    lines = ["# Đề xuất của Kite: kiểm tra lại (nhất là biến môi trường test cần) rồi commit vào gốc repo"]
    if setup:
        lines.append(f"setup: {setup}")
    if nested:
        lines += [f"test: PYTHONPATH=. python -m pytest -q {' '.join(sorted(folders)[:6])}",
                  "quick_test: PYTHONPATH=. python -m pytest -q {tests}"]
    if ruff:
        lines += ["# setup phải cài ruff (vd. thêm `ruff` vào lệnh pip ở trên)",
                  "check: ruff check {py_files}"]
    return "\n".join(lines) + "\n"


REVIEW_CHECKLIST = [
    "- Phạm vi: chỉ đổi những gì yêu cầu cần; không đổi tên, định dạng lại hay \"dọn\" phần không liên quan.",
    "- Giao diện: không đổi tên/tham số của hàm, class, route, trường dữ liệu mà chỗ khác đang dùng, "
    "trừ khi yêu cầu bảo vậy.",
    "- Lỗi: không nuốt lỗi (`except: pass`, bắt rồi bỏ qua); lỗi phải báo đúng kiểu/mã mà đặc tả nói.",
    "- Bảo mật: không ghi cứng mật khẩu, token, khóa; không đưa dữ liệu từ ngoài vào lệnh shell, SQL, đường dẫn "
    "file hay eval mà không kiểm tra; không tắt kiểm tra TLS hay xác thực.",
    "- Sạch: không còn print/log gỡ lỗi, code chết, file thử (reproduce_*.py, debug_*.py, *.orig).",
]


def review_prompt(goal: str, test_command: str, impact: str = "", quick: str = "",
                  edited: list[str] = (), check: str = "", scratch: list[str] = ()) -> str:
    """Prompt for the self-review pass, after Kite's tests (and the check) already passed.

    ``impact`` (codemap.impact): the callers of what the agent changed, found by the code map.
    ``check``: the repository's check command (template); ``scratch``: new files that look like experiments.
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
        "4. Rà phần đã đổi theo danh sách dưới đây.",
        "5. Nếu thấy sai thì sửa code. Không sửa hay xóa file test có sẵn và conftest.py; được THÊM file test mới "
        "cho các trường hợp biên vừa kiểm tra.",
        f"6. Chạy lại `{test_command}`" + (f" và `{check_hint(check)}`" if check else "") + ", đảm bảo vẫn qua.",
        "7. Kết thúc bằng: các quy tắc đã kiểm tra, chỗ đã sửa thêm (hoặc \"không cần sửa\").",
    ]
    if quick_hint(quick):
        lines.append(quick_hint(quick))
    lines += ["", "Danh sách rà soát:", *REVIEW_CHECKLIST]
    if edited:
        lines += ["", "Bạn đã sửa hoặc xóa test có sẵn: " + ", ".join(edited) + ". Chỉ giữ nếu yêu cầu đổi đúng hành vi "
                  "mà test cũ kiểm tra; nếu chỉ để cho test qua thì trả file test về như cũ và sửa code."]
    if scratch:
        lines += ["", "File mới trông như file thử: " + ", ".join(scratch) + ". Xóa nếu nó không thuộc lời giải."]
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


FAST_CONTEXT_CHARS = 40000     # code shown to the fast path (about 11K tokens)
FAST_FILE_CHARS = 16000        # a goal file this small is shown whole
FAST_TEST_CHARS = 6000         # one related test file, for reference
# Thinking models spend most of it before the edit blocks: in bench v4, 8192 ran out in 8 of 17 one-call
# requests (7k-8k tokens of thinking), leaving no answer or a cut block. 16384 leaves room for the answer.
FAST_MAX_TOKENS = 16384
FAST_DECLINE = "CẦN_AGENT"


def _span(lines: list[str], line: int, max_lines: int = 400) -> tuple[int, list[str]]:
    """The whole definition at 1-based ``line`` (decorators included, classes too), by indentation."""
    i = max(0, min(line - 1, len(lines) - 1))
    indent = len(lines[i]) - len(lines[i].lstrip())
    start = i
    while start > 0 and lines[start - 1].strip().startswith("@"):
        start -= 1
    end = i + 1
    while end < len(lines) and end - start < max_lines:
        text = lines[end]
        if text.strip() and len(text) - len(text.lstrip()) <= indent and not text.lstrip().startswith((")", "]", "}")):
            break
        end += 1
    while end > i + 1 and not lines[end - 1].strip():
        end -= 1
    return start + 1, lines[start:end]


def fast_context(workspace: Path, goal: str, graph: codemap.CodeGraph | None = None,
                 max_chars: int = FAST_CONTEXT_CHARS) -> str:
    """The code a one-shot fix needs, verbatim (no line numbers: SEARCH blocks copy it).

    In this order, within ``max_chars``: the files the goal names (whole when small), the
    definitions it names, the base classes of those classes (the helpers a new method will use),
    their callers (code map), one test file that uses them. Overlapping excerpts of a file are
    merged. "" when nothing could be located: then the agent explores on its own.
    """
    workspace = Path(workspace)
    files = [f for f in _git(workspace, "ls-files", check=False).splitlines() if f and not _is_noise(f)]
    mentioned = [f for f in files if f in goal or re.search(
        rf"(?<![\w./-]){re.escape(Path(f).name)}(?![\w-])", goal)]
    names = codemap.goal_names(goal)
    symbols: list = []
    if graph is not None:
        _, symbols = codemap.goal_map(graph, goal, mentioned, max_chars=CODE_MAP_CHARS, workspace=workspace)
        symbols = [s for s in symbols if s.kind != "module"]
        definitions = [(graph.qualified(s), s.file, s.line) for s in symbols]
    else:
        plain = [] if mentioned else [w for w in codemap.goal_words(goal) if w not in names]
        definitions = _grep_definitions(workspace, names, plain)
    cache: dict[str, list[str] | None] = {}

    def read(path: str) -> list[str] | None:
        if path not in cache:
            try:
                cache[path] = (workspace / path).read_text(encoding="utf-8").split("\n")
            except (OSError, UnicodeDecodeError):
                cache[path] = None
        return cache[path]

    whole = [f for f in mentioned if not codemap.is_test_path(f) and read(f) is not None
             and len("\n".join(read(f))) <= FAST_FILE_CHARS]
    spans: list[list] = []           # [path, first, last, labels]

    def want(path: str, line: int, label: str, head_only: int = 0) -> None:
        lines = read(path)
        if lines is None or path in whole or codemap.is_test_path(path):
            return
        if head_only:
            first, last = 1, min(len(lines), head_only)
        else:
            first, block = _span(lines, line)
            last = first + len(block) - 1
        for span in spans:
            if span[0] == path and first <= span[2] + 1 and last >= span[1] - 1:
                span[1], span[2] = min(span[1], first), max(span[2], last)
                span[3].append(label)
                return
        spans.append([path, first, last, [label]])

    for path in mentioned:
        if path not in whole and not any(d[1] == path for d in definitions):
            want(path, 1, f"file dài {len(read(path) or [])} dòng", head_only=150)
    for name, path, line in definitions[:8]:
        want(path, line, name)
    bases: list[tuple[str, str, int]] = []
    for name, path, line in definitions[:8]:
        head = (read(path) or [""])[max(0, line - 1)] if read(path) else ""
        match = re.match(r"\s*class\s+\w+\s*\(([^)]*)\)", head)
        if match:
            for base in re.findall(r"[A-Za-z_][\w.]*", match.group(1)):
                base = base.rsplit(".", 1)[-1]
                if base in ("object", "Exception", "BaseModel", "Enum", "ABC", "dict", "list"):
                    continue
                if graph is not None:
                    bases += [(base, s.file, s.line) for s in graph.lookup(base) if s.kind == "class"][:1]
                else:
                    bases += _grep_definitions(workspace, [base])[:1]
    for name, path, line in bases:
        want(path, line, f"lớp cha {name}")
    tests: list[str] = [f for f in mentioned if codemap.is_test_path(f)]
    if graph is not None:
        # What the named code itself uses (a schema it builds, a table it reads): a fix often has
        # to change that too, and the agent would have gone to read it.
        callees = 0
        for symbol in symbols[:6]:
            for relation, target, _path, _line in graph.out[symbol.id]:
                callee = graph.symbols.get(target)
                if (relation in codemap.CALL_RELATIONS and callee is not None and callee.kind != "module"
                        and callees < 4 and not codemap.is_test_path(callee.file)):
                    want(callee.file, callee.line, f"được {graph.qualified(symbol)} dùng")
                    callees += 1
        callers = 0
        for symbol in symbols[:6]:
            for _, user, path, _line in graph.users(symbol):
                if codemap.is_test_path(path):
                    if path.endswith(".py") and path not in tests:
                        tests.append(path)
                    continue
                if user is not None and user.kind != "module" and callers < 4 and path not in whole:
                    want(path, user.line, f"chỗ gọi {graph.qualified(symbol)}")
                    callers += 1
    if not whole and not spans:
        return ""
    merged: list[list] = []          # a later, wider excerpt (a whole class) can swallow earlier ones
    for span in spans:
        for kept in merged:
            if kept[0] == span[0] and span[1] <= kept[2] + 1 and span[2] >= kept[1] - 1:
                kept[1], kept[2] = min(kept[1], span[1]), max(kept[2], span[2])
                kept[3] += span[3]
                break
        else:
            merged.append(list(span))
    for kept in merged:
        for other in merged:
            if other is not kept and other[0] == kept[0] and other[1] >= kept[1] and other[2] <= kept[2]:
                kept[3] += other[3]
                other[2] = -1
    spans = [span for span in merged if span[2] >= 0]
    sections: list[str] = []
    budget = max_chars

    def add(title: str, text: str) -> None:
        nonlocal budget
        if len(text) + 200 <= budget:
            sections.append(f"### {title}\n`````\n{text.rstrip()}\n`````")
            budget -= len(text) + 200

    for path in whole:
        add(f"{path} (toàn bộ file, {len(chr(10).join(read(path)).splitlines())} dòng)", "\n".join(read(path)))
    for path, first, last, labels in spans:
        add(f"{path}, dòng {first}-{last} ({', '.join(dict.fromkeys(labels))})", "\n".join(read(path)[first - 1:last]))
    for path in tests[:1]:
        content = "\n".join(read(path) or [])
        if content:
            cut = content[:FAST_TEST_CHARS]
            add(f"{path} (test liên quan, chỉ để tham khảo{', đã cắt' if len(cut) < len(content) else ''})", cut)
    return "\n\n".join(sections)


def fast_prompt(goal: str, context: str, test_command: str, check: str = "", feedback: str = "") -> str:
    checks = f"`{test_command}`" + (f" và `{check_hint(check)}`" if check else "")
    return "\n".join([
        goal.strip(), "",
        "Kite đã tìm sẵn phần code liên quan (bên dưới, đúng nội dung hiện tại của file). Hãy đưa ra bản sửa "
        "HOÀN CHỈNH bằng các khối SEARCH/REPLACE theo đúng mẫu sau, mỗi khối một chỗ sửa:",
        "",
        "đường/dẫn/tới/file.py",
        "<<<<<<< SEARCH",
        "các dòng chép NGUYÊN VĂN từ file hiện tại (cả thụt lề), vừa đủ để chỉ khớp một chỗ",
        "=======",
        "các dòng mới thay vào",
        ">>>>>>> REPLACE",
        "",
        "Quy tắc:",
        "- SEARCH chép đúng từng ký tự từ phần code bên dưới; giữ khối ngắn, chỉ quanh chỗ cần đổi.",
        "- Tạo file mới: để SEARCH trống.",
        "- Sửa đủ mọi chỗ yêu cầu cần (kể cả nơi gọi tới), không đổi thứ không liên quan.",
        "- Không sửa hay xóa test có sẵn; được thêm file test mới.",
        f"- Nếu phần code bên dưới chưa đủ để sửa chắc chắn đúng, chỉ trả lời một dòng: {FAST_DECLINE}: <lý do>.",
        "- Chỉ cần các khối sửa, kèm tối đa hai câu tóm tắt.",
        f"Kite sẽ tự áp các khối rồi chạy {checks} (không mạng).",
        *(["", feedback] if feedback else []),
        "",
        "Code liên quan:",
        context,
    ])


def retry_feedback(status: str, diff: str, reason: str, output: str) -> str:
    """Why the last candidate failed, for the next one. A failed reproduction test is described, never
    shown: the fix must stay independent of the test that judges it."""
    head = "LẦN THỬ TRƯỚC CỦA KITE CHƯA ĐẠT và đã được gỡ: code bên dưới vẫn là bản GỐC, sửa lại từ đầu."
    if status == "not_applied":
        return f"{head} Các khối sửa không áp được ({reason}): phần SEARCH phải chép NGUYÊN VĂN từ code bên dưới."
    shown = f"\nBản đã thử:\n```diff\n{diff.strip()[:3000]}\n```" if diff.strip() else ""
    if status == "repro_failed":
        return (f"{head} Bản đó chưa làm đúng yêu cầu: một kiểm tra độc lập Kite viết từ yêu cầu vẫn lỗi. "
                f"Đọc lại yêu cầu, nhất là các trường hợp biên và quy tắc nó nêu.{shown}")
    what = "làm test của repo lỗi" if status == "tests_failed" else "bị bước kiểm tra (lint) báo lỗi"
    return (f"{head} Bản đó {what}.{shown}\nKết quả:\n```\n{failure_digest(output, 1500).strip()}\n```")


def gateway_asker(settings: SandboxSettings, model: str, *, tag: str, pin: str | None = None,
                  timeout: int = 900) -> Callable[[str], str] | None:
    """One chat request through Kite's gateway (the same queue and worker as the agent's requests)."""
    url = (settings.host_gateway_url or settings.gateway_url).rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "X-Kite-Task": tag, **({"X-Kite-Worker": pin} if pin else {})}

    def ask(prompt: str) -> str:
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": FAST_MAX_TOKENS}
        request = urllib.request.Request(url, json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except Exception as exc:
            raise SandboxError(f"gateway: {exc}") from exc
        choice = (data.get("choices") or [{}])[0] or {}
        content = (choice.get("message") or {}).get("content") or ""
        if choice.get("finish_reason") == "length" and not content.strip():
            raise SandboxError(f"the model used all {FAST_MAX_TOKENS} tokens thinking and gave no answer")
        return content
    return ask


def idle_helpers(settings: SandboxSettings, model: str, *, exclude: str | None, limit: int = 1) -> list[str]:
    """Other workers that are connected, idle and already have ``model`` loaded: they can write a
    second fast-path candidate at the same time, at no cost in wall time (no model swap, no queue).
    Only used for a task pinned to a worker, so the two candidates never share one worker."""
    if not settings.fast_parallel or not exclude or limit <= 0:
        return []
    base = (settings.host_gateway_url or settings.gateway_url).rstrip("/")
    root = base[:-len("/v1")] if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(root + "/status", timeout=5) as response:
            status = json.loads(response.read())
    except Exception:
        return []
    return [w["worker_id"] for w in status.get("workers") or []
            if w.get("worker_id") != exclude and w.get("alive") and not w.get("busy") and not w.get("draining")
            and not w.get("stop_requested") and w.get("loaded_model") == model][:limit]


@dataclass
class FastAttempt:
    status: str                 # skipped | declined | not_applied | applied | error
    answer: str = ""
    reason: str = ""
    before: str = ""            # tree to go back to
    diff: str = ""
    files: list[str] = field(default_factory=list)
    context_chars: int = 0


def fast_attempt(workspace: Path, goal: str, graph: codemap.CodeGraph | None, ask: Callable[[str], str],
                 test_command: str, check: str = "", context: str | None = None, feedback: str = "") -> FastAttempt:
    """Locate the code, ask once, apply the edit blocks (all or nothing). Tests are the caller's."""
    context = fast_context(workspace, goal, graph) if context is None else context
    if not context:
        return FastAttempt("skipped", reason="không xác định được phần code liên quan")
    try:
        answer = ask(fast_prompt(goal, context, test_command, check, feedback))
    except Exception as exc:                    # gateway down, timeout: the agent still runs
        return FastAttempt("error", reason=str(exc)[:300], context_chars=len(context))
    return fast_apply(workspace, answer, len(context))


def fast_apply(workspace: Path, answer: str, context_chars: int = 0) -> FastAttempt:
    """Apply one fast answer's edit blocks (all or nothing); edits to existing tests are refused."""
    visible = re.sub(r"<think>.*?</think>", "", answer, flags=re.S)
    declined = re.search(rf"{FAST_DECLINE}\s*:?\s*(.*)", visible)
    if declined and "<<<<<<<" not in visible:
        return FastAttempt("declined", answer, declined.group(1).strip()[:300], context_chars=context_chars)
    before = snapshot_tree(workspace)
    try:
        files = editblocks.apply(workspace, editblocks.parse(answer))
        existing_tests = [f for f in files if codemap.is_test_path(f) and subprocess.run(
            ["git", "-C", str(workspace), "cat-file", "-e", f"{before}:{f}"], capture_output=True).returncode == 0]
        if existing_tests:
            raise editblocks.EditError(f"sửa test có sẵn ({', '.join(existing_tests)})")
    except editblocks.EditError as exc:
        restore_tree(workspace, before)
        return FastAttempt("not_applied", answer, str(exc)[:300], before, context_chars=context_chars)
    _git(workspace, "add", "-A")
    diff = _git(workspace, "diff", "--cached", before, check=False)
    return FastAttempt("applied", answer, "", before, diff, files, context_chars)


FAST_REASONS = {"tests_failed": "test của Kite lỗi", "check_failed": "bước kiểm tra lỗi",
                "not_applied": "không áp được vào code", "repro_failed": "test tái hiện lỗi trên bản sửa"}
REPRO_FILE = "test_kite_repro.py"


def repro_path(workspace: Path, goal: str, graph: codemap.CodeGraph | None = None) -> str:
    """Where the reproduction test goes: next to the tests of the code the goal names (so its
    conftest fixtures apply), else the nearest tests/ folder above the named files, else tests/."""
    workspace = Path(workspace)
    files = [f for f in _git(workspace, "ls-files", check=False).splitlines() if f]
    mentioned = [f for f in files if f in goal or re.search(
        rf"(?<![\w./-]){re.escape(Path(f).name)}(?![\w-])", goal)]
    folders = [str(Path(f).parent) for f in mentioned if codemap.is_test_path(f)]
    if graph is not None:
        _, symbols = codemap.goal_map(graph, goal, mentioned, max_chars=CODE_MAP_CHARS, workspace=workspace)
        folders += [str(Path(path).parent) for symbol in symbols[:6] for _, _, path, _ in graph.users(symbol)
                    if codemap.is_test_path(path) and path.endswith(".py")]
    for source in mentioned:
        here = Path(source).parent
        while True:
            for name in ("tests", "test"):
                if (workspace / here / name).is_dir():
                    folders.append(str(here / name))
            if here == here.parent:
                break
            here = here.parent
    folders += ["tests"] if (workspace / "tests").is_dir() else []
    folder = next((f for f in folders if f not in ("", ".")), "")
    name = REPRO_FILE
    for number in range(2, 20):
        if not (workspace / folder / name).exists():
            break
        name = f"test_kite_repro_{number}.py"
    return (Path(folder) / name).as_posix() if folder else name


def test_examples(workspace: Path, path: str, goal: str, max_chars: int = 4000) -> str:
    """The conftest.py that applies to ``path`` and one nearby test file: how this repository's tests
    build their data and fixtures (a reproduction test written without them rarely even runs)."""
    workspace, folder = Path(workspace), Path(path).parent
    sections = []
    here = folder
    while True:
        conftest = workspace / here / "conftest.py"
        if conftest.is_file():
            text = conftest.read_text(encoding="utf-8", errors="replace")
            sections.append(f"### {(here / 'conftest.py').as_posix()} (fixture có sẵn)\n`````\n{text[:max_chars]}\n`````")
            break
        if here == here.parent:
            break
        here = here.parent
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", goal)}
    candidates = sorted((f for f in (workspace / folder).glob("test_*.py") if not f.name.startswith("test_kite_repro")),
                        key=lambda f: (-sum(w in f.stem.lower() for w in words), f.stat().st_size))
    if candidates:
        text = candidates[0].read_text(encoding="utf-8", errors="replace")
        sections.append(f"### {candidates[0].relative_to(workspace).as_posix()} (test mẫu cùng thư mục)\n`````\n"
                        f"{text[:max_chars]}\n`````")
    return "\n\n".join(sections)


def repro_prompt(goal: str, context: str, path: str, command: str) -> str:
    return "\n".join([
        goal.strip(), "",
        "Bước 1 của Kite: TRƯỚC khi sửa, viết test kiểm tra đúng hành vi mà yêu cầu trên đòi hỏi.",
        "- Test phải THẤT BẠI với code hiện tại (bên dưới) và QUA khi code đã được sửa đúng yêu cầu.",
        "- Bao các quy tắc và trường hợp biên yêu cầu nêu; mỗi test kiểm một ý, tên test nói rõ ý đó.",
        "- Dựng dữ liệu và fixture giống file test liên quan bên dưới. Test chạy trong sandbox KHÔNG có mạng và "
        "KHÔNG có credential cloud (AWS...): mọi truy cập database/cloud phải qua fixture hoặc mock có sẵn "
        "của repo (xem conftest), nếu không test sẽ lỗi vì môi trường chứ không phải vì lỗi cần sửa.",
        "- Không sửa code, không sửa test có sẵn.",
        "Trả lời bằng đúng một khối tạo file mới, theo mẫu:", "",
        path, "<<<<<<< SEARCH", "=======", "<toàn bộ nội dung file test>", ">>>>>>> REPLACE", "",
        f"Kite sẽ chạy: `{command}`", "",
        "Code liên quan:", context,
    ])


# A reproduction test that fails because of its own setup or the sandbox (no network, no cloud
# credentials, a fixture that does not exist, a syntax error) proves nothing about the bug. Bench v4,
# gemma R1: the test wrote to real DynamoDB, failed with NoCredentialsError, counted as "reproduced",
# and the agent was then shown it as the reproduction. Such a test is "broken": no evidence, not shown.
# Import errors are not on the list: a test of a function the goal asks to add fails that way on purpose.
REPRO_BROKEN = re.compile(
    r"NoCredentialsError|NoRegionError|EndpointConnectionError|NewConnectionError|ConnectionRefusedError|"
    r"Temporary failure in name resolution|socket\.gaierror|fixture '[^']+' not found|ERROR at setup of|"
    r"SyntaxError|IndentationError")


def repro_outcome(code: int, output: str) -> str:
    """What running the reproduction test on the unchanged code shows."""
    if code == 0:
        return "not_reproduced"         # it passes already: it does not test what the goal changes
    if code == 5:
        return "none"                   # pytest collected no test
    return "broken" if REPRO_BROKEN.search(output or "") else "reproduced"


@dataclass
class FastResult:
    status: str                 # off | skipped | declined | not_applied | error | tests_failed | check_failed
    #                             | repro_failed | passed
    repro: str = "off"          # off | none | broken | not_reproduced | reproduced
    attempts: int = 0           # candidates tried (best-of-N)
    reason: str = ""
    answer: str = ""
    repro_answer: str = ""
    note: str = ""              # for the agent's prompt when the fast path did not hold
    test_output: str = ""
    check_code: int | None = None
    check_output: str = ""
    failing_output: str = ""    # for the agent's quick test selection
    files: list[str] = field(default_factory=list)
    context_chars: int = 0
    log: list[str] = field(default_factory=list)


def fast_path(workspace: Path, goal: str, graph: codemap.CodeGraph | None, ask: Callable[[str], str], *,
              test_command: str, check: str, template: str,
              run_tests: Callable[[str], tuple[int, str, str]],
              run_check: Callable[[str], tuple[int | None, str, str]],
              run_one: Callable[[str, str], tuple[int, str]],
              helpers: Sequence[Callable[[str], str]] = (), retries: int = 0) -> FastResult:
    """Agentless-style fix with evidence (Xia et al. 2024, github.com/OpenAutoCoder/Agentless).

    1. reproduction test written from the goal alone, run on the current code: it must fail;
    2. the fix, asked separately (it never sees that test), applied all-or-nothing;
    3. validation: the reproduction test now passes, and so do the repository's tests and check.
    The fix and the test come from the goal independently, so their agreement is evidence that a
    reading-only review can trust; without a reproduction ("not_reproduced"/"none") the caller
    reviews with the agent instead. Anything failing puts the clone back for the agent.

    Best-of-N (the idea of Harness², github.com/google-research/harness2: cheap candidates, a judge
    picks): ``helpers`` are askers on other idle workers, asked for candidates at the same time as
    ``ask``; candidates are checked one by one and the first that passes everything wins. Then up to
    ``retries`` more candidates from ``ask``, each told why the last one failed.
    """
    workspace = Path(workspace)
    context = fast_context(workspace, goal, graph)
    if not context:
        return FastResult("skipped", reason="không xác định được phần code liên quan")
    result = FastResult("error", context_chars=len(context))
    repro_text, repro_file, repro_output = "", "", ""
    if template:
        repro_file = repro_path(workspace, goal, graph)
        command = quick_test_command(template, [repro_file])
        try:
            examples = test_examples(workspace, repro_file, goal)
            reply = result.repro_answer = ask(repro_prompt(goal, context + ("\n\n" + examples if examples else ""),
                                                           repro_file, command))
            # A block with no file name line belongs to the file we asked for (bench v4, qwen R2).
            blocks = [b for b in editblocks.parse(reply, default_path=repro_file)
                      if not b.search.strip() and codemap.is_test_path(b.path)]
        except Exception as exc:                       # no test is no evidence, not a failure
            blocks, result.log = [], [f"không viết được test tái hiện: {str(exc)[:200]}"]
        if blocks and not (workspace / blocks[0].path).exists():
            repro_file = blocks[0].path
            try:
                editblocks.apply(workspace, blocks[:1])
            except editblocks.EditError:
                blocks = []
        if blocks and (workspace / repro_file).is_file():
            repro_text = (workspace / repro_file).read_text(encoding="utf-8")
            code, repro_output = run_one("repro", quick_test_command(template, [repro_file]))
            result.repro = repro_outcome(code, repro_output)
            (workspace / repro_file).unlink()
        else:
            result.repro = "none"
    repro_note = ("\nTest tái hiện Kite đã viết từ yêu cầu (có thể sai; yêu cầu gốc mới là chuẩn), "
                  f"`{repro_file}`:\n```python\n{repro_text.strip()[:3000]}\n```\n"
                  f"Kết quả trên code ban đầu:\n```\n{failure_digest(repro_output, 1500).strip()}\n```"
                  if result.repro == "reproduced" else "")

    def judge(fast: FastAttempt, label: str) -> tuple[str, str, int | None, str]:
        """Kite's tests on one applied candidate: (status, test output, check code, check output)."""
        if result.repro in ("reproduced", "not_reproduced"):
            target = workspace / repro_file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(repro_text, encoding="utf-8")
        code, output, _ = run_tests(label)
        check_code, check_output, _ = run_check(label) if code == 0 else (None, "", "")
        if code == 0 and check_code in (None, 0):
            return "passed", output, check_code, check_output
        restore_tree(workspace, fast.before)              # also drops the reproduction test
        if code != 0:
            status = "repro_failed" if repro_file and repro_file in output and result.repro != "off" \
                else "tests_failed"
            return status, output, None, ""
        return "check_failed", "", check_code, check_output

    prompt = fast_prompt(goal, context, test_command, check)
    askers = [ask, *helpers]
    if len(askers) > 1:                                    # one candidate per idle worker, at the same time
        from concurrent.futures import ThreadPoolExecutor

        def one(asker: Callable[[str], str]) -> str | Exception:
            try:
                return asker(prompt)
            except Exception as exc:
                return exc

        with ThreadPoolExecutor(len(askers)) as pool:
            replies = list(pool.map(one, askers))
    else:
        replies = [None]                                   # asked below, through fast_attempt
    last = best = None             # (status, attempt, test output, check code, check output)
    rank = {"tests_failed": 3, "repro_failed": 3, "check_failed": 3, "not_applied": 2}
    pending = len(replies) + max(0, retries)
    for n in range(1, pending + 1):
        if n <= len(replies) and replies[n - 1] is not None:
            reply = replies[n - 1]
            fast = (FastAttempt("error", reason=str(reply)[:300], context_chars=len(context))
                    if isinstance(reply, Exception) else fast_apply(workspace, reply, len(context)))
        else:
            why = best or last
            feedback = (retry_feedback(why[0], why[1].diff, why[1].reason, why[2] or why[4])
                        if why and why[0] in rank else "")
            fast = fast_attempt(workspace, goal, graph, ask, test_command, check, context=context, feedback=feedback)
        result.attempts = n
        if fast.status == "applied":
            status, output, check_code, check_output = judge(fast, "fast" if n == 1 else f"fast{n}")
        else:
            status, output, check_code, check_output = fast.status, "", None, ""
        result.answer, result.files, result.reason = fast.answer, fast.files, fast.reason
        if pending > 1:
            result.log.append(f"đường nhanh, bản {n}/{pending}: {status}"
                              + (f" ({fast.reason})" if fast.reason else ""))
        if status == "passed":
            result.status, result.test_output = "passed", output
            result.check_code, result.check_output = check_code, check_output
            return result
        last = (status, fast, output, check_code, check_output)
        if rank.get(status, 0) >= rank.get(best[0] if best else "", 0) and status in rank:
            best = last
        if fast.status in ("skipped", "declined", "error") and n >= len(replies):
            break                         # the model says it cannot, or cannot be asked: stop asking
    last = best or last
    status, fast, output, check_code, check_output = last
    result.status = status
    if status in ("tests_failed", "repro_failed", "check_failed"):
        if status != "check_failed":
            result.failing_output = output
        result.note = fast_note(FAST_REASONS[status], fast.diff,
                                output if status != "check_failed" else check_output) + repro_note
    elif status == "not_applied":
        result.note = fast_note(f"{FAST_REASONS['not_applied']} ({fast.reason})", "", "") + repro_note
    elif repro_note:
        result.note = "\n" + repro_note
    return result


def fast_review(workspace: Path, goal: str, base: str, graph: codemap.CodeGraph | None,
                ask: Callable[[str], str], test_command: str, check: str = "") -> tuple[int, str]:
    """One-call review of the change since ``base``: (0, answer) when it approved or its corrections
    were applied (the caller re-runs the tests); (1, reason) when it could not be asked or applied."""
    diff, context = review_context(workspace, base, graph)
    try:
        answer = ask(fast_review_prompt(goal, diff, context, test_command, check))
    except Exception as exc:
        return 1, f"không hỏi được model: {str(exc)[:200]}"
    try:
        blocks = editblocks.parse(answer)
    except editblocks.EditError as exc:
        return 1, f"câu trả lời rà soát hỏng: {exc}\n{answer}"
    if not blocks:
        return 0, answer
    try:
        files = editblocks.apply(workspace, blocks)
    except editblocks.EditError as exc:
        return 1, f"không áp được khối sửa của rà soát: {exc}\n{answer}"
    edited = [f for f in files if codemap.is_test_path(f) and subprocess.run(
        ["git", "-C", str(workspace), "cat-file", "-e", f"{base}:{f}"], capture_output=True).returncode == 0]
    if edited:
        return 1, f"rà soát sửa test có sẵn ({', '.join(edited)}): bỏ\n{answer}"
    return 0, answer


FAST_APPROVE = "ỔN"


def _new_lines(workspace: Path, base: str) -> dict[str, list[int]]:
    """New-side line numbers each file changed at since ``base`` (new files included: stage first)."""
    out = _git(Path(workspace), "diff", "--cached", "-U0", "--no-color", base, check=False)
    changed: dict[str, list[int]] = {}
    path = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) if match.group(2) is not None else 1)
                changed.setdefault(path, []).extend(range(start, start + max(count, 1)))
    return changed


def review_context(workspace: Path, base: str, graph: codemap.CodeGraph | None = None,
                   max_chars: int = FAST_CONTEXT_CHARS) -> tuple[str, str]:
    """(diff, current code) for a one-call review: the changed files as they are now (whole when
    small, else the definitions around the changed lines) and the callers of the changed code."""
    workspace = Path(workspace)
    _git(workspace, "add", "-A")
    diff = _git(workspace, "diff", "--cached", "--no-color", base, check=False)
    sections, budget = [], max_chars

    def add(title: str, text: str) -> None:
        nonlocal budget
        if len(text) + 200 <= budget:
            sections.append(f"### {title}\n`````\n{text.rstrip()}\n`````")
            budget -= len(text) + 200

    spans: list[tuple[str, int, int]] = []

    def covered(path: str, line: int) -> bool:
        return any(p == path and first <= line <= last for p, first, last in spans)

    changed = _new_lines(workspace, base)
    for path, lines in changed.items():
        try:
            content = (workspace / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(content) <= FAST_FILE_CHARS:
            add(f"{path} (toàn bộ file hiện tại)", content)
            continue
        rows = content.split("\n")
        for line in lines:
            if not covered(path, line):
                first, block = _span(rows, line)
                spans.append((path, first, first + len(block) - 1))
                add(f"{path}, dòng {first}-{first + len(block) - 1} (hiện tại)", "\n".join(block))
    if graph is not None:
        callers = 0
        for path, lines in codemap.changed_lines(workspace, base).items():
            for symbol in dict.fromkeys(graph.at(path, line) for line in lines):
                if symbol is None or symbol.kind == "module":
                    continue
                for _, user, user_path, _line in graph.users(symbol):
                    if (user is None or user.kind == "module" or codemap.is_test_path(user_path)
                            or user_path in changed or callers >= 4 or covered(user_path, user.line)):
                        continue
                    try:
                        rows = (workspace / user_path).read_text(encoding="utf-8").split("\n")
                    except (OSError, UnicodeDecodeError):
                        continue
                    first, block = _span(rows, user.line)
                    spans.append((user_path, first, first + len(block) - 1))
                    add(f"{user_path}, dòng {first}-{first + len(block) - 1} (chỗ gọi {graph.qualified(symbol)})",
                        "\n".join(block))
                    callers += 1
    return diff, "\n\n".join(sections)


def fast_review_prompt(goal: str, diff: str, context: str, test_command: str, check: str = "") -> str:
    checks = f"`{test_command}`" + (f" và `{check_hint(check)}`" if check else "")
    return "\n".join([
        goal.strip(), "",
        f"Kite đã sửa code cho yêu cầu trên và {checks} đã QUA. Đây là bước RÀ SOÁT cuối, trả lời một lần.",
        "Thay đổi so với ban đầu:", "```diff", diff.strip()[:8000], "```", "",
        "Rà soát:",
        "- Đủ mọi quy tắc và ví dụ trong yêu cầu và docstring, kể cả trường hợp biên (rỗng, None, 0, thiếu trường, "
        "ranh giới, lỗi phải báo).",
        "- Mọi chỗ gọi tới phần đã đổi vẫn đúng (code bên dưới).",
        *REVIEW_CHECKLIST, "",
        f"Nếu bản sửa đã đúng và đủ: chỉ trả lời một dòng: {FAST_APPROVE}",
        "Nếu còn sai hoặc thiếu: đưa các khối sửa trên code HIỆN TẠI bên dưới, đúng mẫu",
        "đường/dẫn/file.py", "<<<<<<< SEARCH", "các dòng chép nguyên văn từ code hiện tại", "=======",
        "các dòng mới", ">>>>>>> REPLACE",
        "Không sửa hay xóa test có sẵn. Kite sẽ chạy lại test; khối sửa làm hỏng test sẽ bị bỏ.", "",
        "Code hiện tại:", context,
    ])


def fast_note(reason: str, diff: str, output: str) -> str:
    """What the agent hears about a fast attempt that did not hold."""
    lines = ["", f"Kite đã thử một bản sửa nhanh (một lần gọi model) nhưng {reason}. Bản đó đã được bỏ, /work đang "
             "như ban đầu. Tham khảo để tránh lặp lại y nguyên:"]
    if diff.strip():
        lines += ["```diff", diff.strip()[:3000], "```"]
    if output.strip():
        lines += ["Kết quả kiểm tra của bản đó:", "```", failure_digest(output, 2000).strip(), "```"]
    return "\n".join(lines)


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


SCRATCH_FILE = re.compile(
    r"(^|/)((repro|reproduce|reproduction|debug|scratch|tmp|temp|playground)([_.-][^/]*)?\.(py|sh|js|ts|txt|json)"
    r"|[^/]+\.(orig|rej|bak|log))$", re.I)
CHECK_UNAVAILABLE = (126, 127)     # the check's tool is not installed or not runnable: setup's job, not the agent's


def changed_files(workspace: Path, base: str) -> list[str]:
    """Files added or modified since ``base`` that exist now (new, untracked files included)."""
    workspace = Path(workspace)
    tracked = _git(workspace, "diff", "--name-only", "--diff-filter=AMR", base, check=False).splitlines()
    new = _git(workspace, "ls-files", "--others", "--exclude-standard", check=False).splitlines()
    return [path for path in dict.fromkeys(tracked + new)
            if path and "__pycache__/" not in path and (workspace / path).is_file()]


def scratch_files(workspace: Path, base: str) -> list[str]:
    """New files that look like the agent's own experiments (reproduce_x.py, debug.log, a.py.orig)."""
    workspace = Path(workspace)
    added = _git(workspace, "diff", "--name-only", "--diff-filter=A", base, check=False).splitlines()
    added += _git(workspace, "ls-files", "--others", "--exclude-standard", check=False).splitlines()
    return sorted(path for path in set(added)
                  if path and SCRATCH_FILE.search(path) and not codemap.is_test_path(path)
                  and (workspace / path).is_file())


def syntax_errors(workspace: Path, files: list[str], limit: int = 5) -> str:
    """Changed Python files that do not even parse. Kite checks this itself: compile only, nothing runs."""
    problems = []
    for path in files:
        if not path.endswith(".py"):
            continue
        try:
            compile((Path(workspace) / path).read_bytes(), path, "exec", dont_inherit=True)
        except SyntaxError as exc:
            problems.append(f"{path}:{exc.lineno}: SyntaxError: {exc.msg}")
        except (OSError, ValueError):
            continue
        if len(problems) >= limit:
            break
    return "\n".join(problems)


def syntax_command(problems: str) -> str:
    """The command that shows the same syntax errors (what the agent can run to check its fix)."""
    files = dict.fromkeys(line.split(":", 1)[0] for line in problems.splitlines() if line)
    return "python -m py_compile " + " ".join(shlex.quote(path) for path in files)


def check_command(template: str, files: list[str]) -> str:
    """The repository's check with {files} / {py_files} filled in; "" when it needs files and has none."""
    if not template:
        return ""
    python = [path for path in files if path.endswith((".py", ".pyi"))]
    for slot, chosen in (("{files}", files), ("{py_files}", python)):
        if slot in template:
            if not chosen:
                return ""
            template = template.replace(slot, " ".join(shlex.quote(path) for path in chosen))
    return template


def check_hint(template: str) -> str:
    return template.replace("{files}", "<file đã sửa>").replace("{py_files}", "<file .py đã sửa>")


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
                 quick: str = "", check: str = "") -> str:
    lines = [goal.strip(), "", "Cách làm:", *TASK_GUIDES.get(task_type, TASK_GUIDES["coding"]), "",
             "Làm việc trong thư mục hiện tại (/work). Môi trường không có Internet: "
             "không cài gói, không tải gì về; thư viện cần thiết đã được cài sẵn."]
    if test_command:
        lines.append(f"Kiểm tra lại bằng lệnh: {test_command}")
        if quick_hint(quick):
            lines.append(quick_hint(quick))
        if check:
            lines.append(f"Sau khi test qua, Kite còn kiểm tra bằng `{check_hint(check)}`; chạy lệnh này trước khi kết thúc.")
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
               targets: str = "", quick: str = "", stuck: str = "", check: str = "") -> str:
    """The next round after Kite's own test run failed: same goal, plus the real failing output.

    ``targets`` (codemap.failing_targets): what each failing test calls, from the code map.
    ``check``: the tests passed but this check failed; ``test_output`` is then the check's output.
    """
    if check:
        lines = [
            goal.strip(), "",
            f"Test đã QUA, nhưng Kite chạy kiểm tra `{check}` (không mạng) và có lỗi "
            f"(vòng sửa {attempt}/{rounds}). Output:",
            "```", clean_output(test_output)[-FIX_OUTPUT_CHARS:].strip(), "```",
            "Các thay đổi lần trước vẫn còn trong /work. Sửa đúng các lỗi được báo trong code; không tắt quy tắc, "
            "không thêm `# noqa` hay `type: ignore` để lách. Rồi chạy lại lệnh kiểm tra đó và "
            f"`{test_command}` cho tới khi cả hai qua.",
            "Môi trường không có Internet. Không commit, không push.",
        ]
        if stuck:
            lines[1:1] = [f"Lần trước Kite đã dừng bạn vì {stuck}. Đổi hướng.", ""]
        return "\n".join(lines)
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
             worker_pin: str | None = None, deps_cache: Path | None = None,
             asker: Callable[[str], str] | None = None) -> SandboxRun:
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
    run.check_command = settings.check_command if run.test_command else ""
    prompt = agent_prompt(goal, run.test_command, task_type=task_type,
                          context=repo_context(workspace, goal, graph=graph) if with_context else "", quick=template,
                          check=run.check_command)
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

    def run_check(after: str) -> tuple[int | None, str, str]:
        """After the tests pass: Python syntax of the changed files (Kite itself), then the repo's check."""
        files = changed_files(workspace, run.patch_base)
        problems = syntax_errors(workspace, files)
        if problems:
            test_log.append(f"===== syntax after {after} =====\n{problems}")
            return 1, problems, syntax_command(problems)
        command = check_command(run.check_command, files)
        if not command:
            return None, "", ""
        log(f"[{task_id}] Kite kiểm tra (không mạng): {command}")
        name = container_name("check", task_id)
        started = time.monotonic()
        code, output = _run_step(
            settings, runner, name=name, timeout=settings.test_timeout_seconds,
            args=docker_run_args(settings, name=name, workspace=workspace, network="none", deps=deps)
            + ["sh", "-c", command])
        run.check_runs += 1
        run.check_seconds = round(run.check_seconds + time.monotonic() - started, 1)
        test_log.append(f"===== check after {after} exit={code} =====\n{output}")
        return code, output, command
    # 2a. Fast path (Agentless-style): a reproduction test and a one-call fix on code Kite located
    #     itself, checked against each other and the repository's tests (see fast_path). Kept only if
    #     everything passes; otherwise the clone goes back and the agent runs, told what was tried.
    ask = None

    def run_one(kind: str, command: str) -> tuple[int, str]:
        name = container_name(kind, task_id)
        code, output = _run_step(
            settings, runner, name=name, timeout=settings.test_timeout_seconds,
            args=docker_run_args(settings, name=name, workspace=workspace, network="none", deps=deps)
            + ["sh", "-c", command])
        test_log.append(f"===== {kind} exit={code} =====\n{output}")
        return code, output

    if settings.fast_path and with_context and task_type == "coding" and run.test_command:
        ask = asker if asker is not None else gateway_asker(settings, model, tag=task_id, pin=worker_pin)
        if ask is not None:
            started = time.monotonic()
            helpers = [] if asker is not None else [
                gateway_asker(settings, model, tag=f"{task_id}-b{i + 2}", pin=other)
                for i, other in enumerate(idle_helpers(settings, model, exclude=worker_pin))]
            log(f"[{task_id}] đường nhanh: viết test tái hiện, rồi hỏi {model} một lần để có cả bản sửa"
                + (f" (thêm một bản song song trên worker rảnh)" if helpers else "") + "...")
            fast = fast_path(workspace, goal, graph, ask, test_command=run.test_command, check=run.check_command,
                             template=template, run_tests=run_tests, run_check=run_check, run_one=run_one,
                             helpers=helpers, retries=settings.fast_retries)
            agent_log.append(f"===== fast repro {fast.repro} =====\n{fast.repro_answer}")
            agent_log.append(f"===== fast {fast.status} ({fast.attempts} bản) =====\n{fast.answer}")
            run.fast_path, run.repro, run.fast_attempts = fast.status, fast.repro, fast.attempts
            if fast.status == "passed":
                run.agent_status, run.agent_exit_code, run.agent_output_tail = "ok", 0, _tail(fast.answer)
                run.test_exit_code, run.test_output_tail = 0, _tail(fast.test_output)
                run.check_exit_code, run.check_output_tail = fast.check_code, _tail(fast.check_output)
            else:
                prompt += fast.note
                test_output = fast.failing_output
            run.fast_seconds = round(time.monotonic() - started, 1)
            for line in fast.log:
                log(f"[{task_id}] {line}")
            log(f"[{task_id}] đường nhanh: {run.fast_path} (bản {fast.attempts}), test tái hiện: {run.repro}"
                f"{f' ({fast.reason})' if fast.reason else ''} sau {run.fast_seconds}s"
                + ("; bỏ qua agent, sang rà soát" if run.fast_path == "passed" else "; chuyển sang agent"))

    while run.fast_path != "passed":
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
        failed_check, check_output = "", ""
        run.check_exit_code, run.check_output_tail = None, ""      # the check only counts on passing tests
        if run.test_exit_code == 0:
            run.check_exit_code, check_output, checked = run_check(kind)
            run.check_output_tail = _tail(check_output)
            if run.check_exit_code in CHECK_UNAVAILABLE:
                log(f"[{task_id}] lệnh check không chạy được (exit {run.check_exit_code}): setup đã cài công cụ "
                    "đó chưa? Không đưa lỗi này cho agent")
                break
            if run.check_exit_code not in (None, 0):
                failed_check = checked
                log(f"[{task_id}] test qua nhưng kiểm tra lỗi (exit {run.check_exit_code}): {checked}")
        if ((run.test_exit_code == 0 and not failed_check) or run.agent_status not in ("ok", "stuck")
                or attempt >= settings.fix_rounds):
            break
        attempt += 1
        run.fix_rounds_used = attempt
        if failed_check:
            prompt = fix_prompt(goal, run.test_command, check_output, attempt, settings.fix_rounds, quick=template,
                                stuck=watch.reason, check=failed_check)
        else:
            prompt = fix_prompt(goal, ran, test_output, attempt, settings.fix_rounds,
                                codemap.failing_targets(graph, test_output) if graph is not None else "",
                                quick=template, stuck=watch.reason)

    # 4. Self-review: tests pass, but passing tests is not the spec. Keep it only if tests (and the
    #    check) still pass.
    if (wants_review(settings, task_type, run.test_command, run.agent_status, run.test_exit_code)
            and run.check_exit_code in (None, 0)):
        before = snapshot_tree(workspace)
        started = time.monotonic()
        if run.fast_path == "passed" and run.repro == "reproduced" and settings.fast_review and ask is not None:
            # A one-call fix gets a one-call review: approve, or corrections kept only if tests still pass.
            run.review_mode = "one_call"
            log(f"[{task_id}] vòng rà soát (một lần gọi): {model} đọc lại bản sửa theo yêu cầu và chỗ gọi...")
            review_code, output = fast_review(workspace, goal, run.patch_base, graph, ask, run.test_command,
                                              run.check_command)
            if review_code != 0:
                log(f"[{task_id}] rà soát một lần gọi: {output.splitlines()[0] if output else 'lỗi'}")
                restore_tree(workspace, before)
        else:
            run.review_mode = "agent"
            log(f"[{task_id}] vòng rà soát: {model} kiểm tra lại lời giải theo yêu cầu và docstring...")
            name = container_name("review", task_id)
            review_code, output = _run_step(
                settings, runner, name=name, timeout=settings.agent_timeout_seconds, watch=StuckWatch(),
                args=docker_run_args(settings, name=name, workspace=workspace, network=settings.network,
                                     config_file=config_file, deps=deps)
                + ["opencode", "run", "-m", f"kite/{model}",
                   review_prompt(goal, run.test_command,
                                 codemap.impact(graph, workspace, run.patch_base) if graph is not None else "",
                                 quick=template, edited=edited_tests(workspace, run.patch_base),
                                 check=run.check_command, scratch=scratch_files(workspace, run.patch_base))])
        run.agent_seconds = round(run.agent_seconds + time.monotonic() - started, 1)
        agent_log.append(f"===== review ({run.review_mode}) exit={review_code} =====\n{output}")
        if snapshot_tree(workspace) == before:
            run.review = "unchanged"
        else:
            code, test_output, _ = run_tests("review")
            check_code, check_output, _ = run_check("review") if code == 0 else (None, "", "")
            if review_code == 0 and code == 0 and check_code in (None, 0):
                run.review, run.agent_output_tail, run.test_output_tail = "kept", _tail(output), _tail(test_output)
                run.check_exit_code, run.check_output_tail = check_code, _tail(check_output)
            else:
                restore_tree(workspace, before)
                run.review = "reverted"
                log(f"[{task_id}] rà soát làm hỏng (agent exit {review_code}, test exit {code}, kiểm tra "
                    f"{check_code}): trả lại bản trước")
    (root / "agent.log").write_text("\n".join(agent_log), encoding="utf-8")
    if test_log:
        (root / "test.log").write_text("\n".join(test_log), encoding="utf-8")

    patch_path = root / "result.patch"
    run.tests_edited = edited_tests(workspace, run.patch_base)
    if run.tests_edited:
        log(f"[{task_id}] lưu ý: agent đã sửa test có sẵn: {', '.join(run.tests_edited)}")
    run.scratch_files = scratch_files(workspace, run.patch_base)
    if run.scratch_files:
        log(f"[{task_id}] lưu ý: có file trông như file thử: {', '.join(run.scratch_files)}")
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
