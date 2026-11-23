"""Update Kite on the control plane: pull, test, restart, from the chat or ``kite update``.

Step 2 of the full-test day: every change meant SSH, ``git pull``, ``pytest``, ``systemctl restart``,
``kite chat setup --force``, and a Kaggle notebook started by hand that still ran the old worker.
``run`` does the EC2 part in one go and refuses to leave a broken Kite behind: the tests run on the
new code first, and a failure puts the old commit back before anything is restarted.

Kite still never pushes: the code reaches GitHub from the person's own machine.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = "poc/kaggle_worker/kaggle_model_worker.ipynb"
SERVICES = ("kite-gateway", "kite-workflow-worker")
OPTIONAL_SERVICES = ("kite-web",)          # restarted too once installed (/etc/systemd/system/<name>.service)
UNIT_DIR = Path("/etc/systemd/system")


def services(unit_dir: Path = UNIT_DIR) -> tuple[str, ...]:
    return SERVICES + tuple(s for s in OPTIONAL_SERVICES if (unit_dir / f"{s}.service").exists())
Runner = Callable[..., subprocess.CompletedProcess]


def _run(runner: Runner, cmd: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return runner(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def plan(repo: Path = REPO, runner: Runner = subprocess.run) -> dict:
    """What an update would bring: the new commits on the branch's upstream, and whether the worker
    notebook changed (then the Kaggle worker needs a restart to run it)."""
    git = lambda *args: _run(runner, ["git", "-c", "core.fsmonitor=false", *args], repo)   # noqa: E731
    fetched = git("fetch", "-q")
    if fetched.returncode != 0:
        return {"error": "git fetch lỗi: " + fetched.stderr.strip()[-300:]}
    head = git("rev-parse", "HEAD").stdout.strip()
    upstream = git("rev-parse", "@{u}")
    if upstream.returncode != 0:
        return {"error": "nhánh hiện tại không theo dõi nhánh nào trên GitHub"}
    target = upstream.stdout.strip()
    commits = [line for line in git("log", "--oneline", f"{head}..{target}").stdout.splitlines() if line]
    dirty = [line[3:] for line in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines() if line]
    notebook = git("diff", "--quiet", head, target, "--", NOTEBOOK).returncode != 0 if commits else False
    return {"head": head[:12], "target": target[:12], "commits": [c[:120] for c in commits[:20]],
            "behind": len(commits), "dirty": dirty, "notebook_changed": notebook}


def run(repo: Path = REPO, *, python: str = sys.executable, runner: Runner = subprocess.run,
        restart: bool = True, after: Callable[[], dict] | None = None) -> dict:
    """Pull the new commits, test them, restart Kite's services. ``after`` runs once the services
    are back (the chat files are rewritten by the new code there)."""
    report = plan(repo, runner)
    if report.get("error") or not report["behind"]:
        return {**report, "updated": False, **({} if report.get("error") else {"note": "Kite đã là bản mới nhất"})}
    if report["dirty"]:
        return {**report, "updated": False,
                "error": "repo Kite trên EC2 có file đang sửa: " + ", ".join(report["dirty"][:5])}
    git = lambda *args: _run(runner, ["git", "-c", "core.fsmonitor=false", *args], repo)   # noqa: E731
    old = git("rev-parse", "HEAD").stdout.strip()
    pulled = git("pull", "--ff-only", "-q")
    if pulled.returncode != 0:
        return {**report, "updated": False, "error": "git pull lỗi: " + pulled.stderr.strip()[-300:]}
    tests = _run(runner, [python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], repo, timeout=1800)
    summary = (tests.stdout.strip().splitlines() or [""])[-1][-200:]
    if tests.returncode != 0:
        git("reset", "--hard", "-q", old)          # never leave a Kite whose tests fail
        return {**report, "updated": False, "tests": summary,
                "error": "test của bản mới lỗi nên đã quay lại bản cũ: " + tests.stdout.strip()[-600:]}
    result = {**report, "updated": True, "tests": summary, "restarted": False}
    if restart:
        names = services()
        done = _run(runner, ["sudo", "-n", "systemctl", "restart", *names], repo)
        result["restarted"] = done.returncode == 0
        if done.returncode != 0:
            result["restart_with"] = f"sudo systemctl restart {' '.join(names)}"
            result["restart_error"] = done.stderr.strip()[-300:]
    if after is not None:
        try:
            result.update(after())
        except Exception as exc:             # the update itself is done; report what did not follow
            result["after_error"] = str(exc)[:300]
    return result
