"""Clone/pull a GitHub repository with a per-environment PAT (Gói 2, Q4).

One PAT per environment, kept sealed in ``secrets/github_pat_<env_id>`` (see
``write_secret``/``read_secret`` in ``web/workers.py``). The PAT is handed to
git through a throw-away ``GIT_ASKPASS`` helper that reads it from an
environment variable of the child process only:

- the remote URL stays plain https — the PAT is never embedded in it (it
  would leak through ``.git/config`` and every copy of the repository);
- the PAT never appears in argv, in a file, or in an error message;
- the helper script itself contains no secret and is deleted with its temp
  directory as soon as git exits.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import stat
import subprocess
import tempfile
from urllib.parse import urlparse

SEALED_PREFIX = "kite-sealed:v1:"


class GitHubEnvError(RuntimeError):
    """A GitHub clone/pull failed, or its inputs were refused. Never carries a PAT."""


def read_pat(pat_path: str | Path, sealer=None) -> str:
    """What a PAT file holds. Mirrors ``read_secret`` in ``web/workers.py``:
    a sealed file needs the sealer that sealed it; a legacy plaintext file is
    read as-is."""
    path = Path(pat_path)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GitHubEnvError(f"không đọc được file PAT {path.name}: {exc}") from exc
    if not text:
        raise GitHubEnvError(f"file PAT {path.name} đang trống")
    if text.startswith(SEALED_PREFIX):
        if sealer is None:
            raise GitHubEnvError(
                f"{path.name} đã được niêm phong (sealed); cần khóa web để đọc")
        return sealer.unseal(text[len(SEALED_PREFIX):])
    return text


def _validate_url(url: str) -> str:
    """Plain https only, no credentials anywhere in the URL."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise GitHubEnvError("chỉ nhận URL https (không nhận http/ssh/file)")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise GitHubEnvError("URL không được nhúng credential (dùng file PAT riêng)")
    if not parsed.netloc or not parsed.path.strip("/"):
        raise GitHubEnvError(f"URL không hợp lệ: {url}")
    return url


@contextmanager
def _askpass_env(pat: str):
    """Environment for one git call: git asks the helper, the helper prints
    the PAT from its own environment, then everything is thrown away."""
    with tempfile.TemporaryDirectory(prefix="kite-askpass-") as tmp:
        helper = Path(tmp) / "askpass.sh"
        helper.write_text("#!/bin/sh\nprintf '%s\\n' \"$KITE_PAT_ASKPASS\"\n", encoding="utf-8")
        helper.chmod(stat.S_IRWXU)
        env = {**os.environ, "GIT_ASKPASS": str(helper), "KITE_PAT_ASKPASS": pat,
               "GIT_TERMINAL_PROMPT": "0"}
        yield env


def _run_git(args: list[str], pat: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    # credential.helper= (empty): never let git store the PAT anywhere.
    full = ["git", "-c", "credential.helper=", *args]
    with _askpass_env(pat) as env:
        return subprocess.run(full, capture_output=True, text=True, env=env,
                              cwd=str(cwd) if cwd else None)


def clone_with_pat(url: str, dest: str | Path, pat_path: str | Path,
                   sealer=None) -> Path:
    """``git clone`` a https repository with the PAT from ``pat_path``.
    Returns the clone directory. The PAT is never part of the URL."""
    url = _validate_url(url)
    pat = read_pat(pat_path, sealer=sealer)
    target = Path(dest).expanduser()
    if target.exists():
        raise GitHubEnvError(f"{target} đã tồn tại; xóa trước rồi clone lại")
    result = _run_git(["clone", "--quiet", url, str(target)], pat)
    if result.returncode != 0:
        raise GitHubEnvError("git clone lỗi: " + (result.stderr or result.stdout).strip()[-300:])
    return target


def pull_with_pat(repo_path: str | Path, pat_path: str | Path, sealer=None) -> str:
    """``git pull --ff-only`` on a repository, authenticating with the PAT
    from ``pat_path``. Returns the new HEAD. Refuses a non-fast-forward
    update instead of merging: the environment's clone must stay a faithful
    copy of the remote."""
    pat = read_pat(pat_path, sealer=sealer)
    repo = Path(repo_path).expanduser().resolve()
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode != 0:
        raise GitHubEnvError(f"{repo} không phải repo Git có ít nhất một commit")
    result = _run_git(["-C", str(repo), "pull", "--ff-only", "--quiet"], pat)
    if result.returncode != 0:
        raise GitHubEnvError("git pull lỗi: " + (result.stderr or result.stdout).strip()[-300:])
    after = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True)
    return after.stdout.strip()
