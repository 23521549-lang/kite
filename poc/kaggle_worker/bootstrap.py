"""Safe, repeatable EC2 host bootstrap orchestration (no AWS API calls)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable

HARNESS2_REPOSITORY = "https://github.com/google-research/harness2.git"
HARNESS2_REVISION = "6868817518876de8fd170c6a8354daca273b6191"
OPENCODE_INSTALLER = "https://opencode.ai/v2/install"


class BootstrapError(RuntimeError):
    pass


CommandRunner = Callable[[list[str], Path | None], None]


def validate_host(*, system: str, version: str, git: bool, python312: bool,
                  docker: bool, compose: bool, data_dir: Path, available_bytes: int) -> None:
    if system != "Linux" or version not in {"22.04", "24.04"}:
        raise BootstrapError("supported host is Ubuntu LTS 22.04 or 24.04")
    if not git or not python312 or not docker or not compose:
        raise BootstrapError("Git, Python 3.12+, Docker, and Docker Compose v2 are required before bootstrap")
    if not data_dir.is_absolute():
        raise BootstrapError("KITE_DATA_DIR must be an absolute persistent EBS path")
    if available_bytes < 20 * 1024**3:
        raise BootstrapError("at least 20 GiB free space is required on the selected state filesystem")


def bootstrap(repo: Path, data_dir: Path, *, dry_run: bool = False,
              command_runner: CommandRunner | None = None,
              system: str = "Linux", version: str = "24.04", git: bool = True,
              python312: bool = True, docker: bool = True, compose: bool = True,
              distro: str = "ubuntu",
              python_executable: str = "python3.12",
              available_bytes: int = 40 * 1024**3, opencode_installed: bool = False) -> list[list[str]]:
    """Validate first, then execute a reproducible command list. Existing state is never removed."""
    validate_host(system=system, version=version, git=git, python312=python312,
                  docker=docker, compose=compose, data_dir=data_dir,
                  available_bytes=available_bytes)
    repo, data_dir = repo.resolve(), data_dir.resolve()
    if distro.lower() != "ubuntu":
        raise BootstrapError("supported host distribution is Ubuntu LTS")
    if (not (repo / "pyproject.toml").is_file()
            or not (repo / "requirements.txt").is_file()
            or not (repo / "deploy" / "compose.yaml").is_file()):
        raise BootstrapError("run bootstrap from the root of a cloned Kite repository")
    state = data_dir / "kite"
    harness_dir = state / "harness2"
    for path in (state, state / "checkpoints", state / "evaluations", state / "logs"):
        if path.exists() and not path.is_dir():
            raise BootstrapError(f"state path exists but is not a directory: {path}")
    if harness_dir.exists() and not harness_dir.is_dir():
        raise BootstrapError(f"state path exists but is not a directory: {harness_dir}")
    if harness_dir.exists() and not (harness_dir / ".git").exists():
        if any(harness_dir.iterdir()):
            raise BootstrapError("Harness² target exists and is not a Git checkout; refusing to overwrite")
    commands: list[list[str]] = []
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    venv_python = str(repo / ".venv" / "bin" / "python")
    if not opencode_installed:
        commands.append(["bash", "-lc", f"curl -fsSL {OPENCODE_INSTALLER} | bash"])
    commands.extend([
        [uv, "venv", "--python", python_executable, str(repo / ".venv")],
        [uv, "pip", "install", "--python", venv_python, "-r", str(repo / "requirements.txt"),
         *(["-c", str(repo / "requirements.lock")] if (repo / "requirements.lock").is_file() else [])],
    ])
    if not (harness_dir / ".git").is_dir():
        commands.extend([
            ["git", "clone", "--filter=blob:none", HARNESS2_REPOSITORY, str(harness_dir)],
            ["git", "-C", str(harness_dir), "checkout", "--detach", HARNESS2_REVISION],
        ])
    else:
        commands.append(["git", "-C", str(harness_dir), "checkout", "--detach", HARNESS2_REVISION])
    commands.extend([
        [uv, "pip", "install", "--python", venv_python, "-e", str(harness_dir)],
        ["sudo", "docker", "compose", "--env-file", str(repo / "deploy" / ".env"), "-f",
         str(repo / "deploy" / "compose.yaml"), "up", "-d", "postgres", "admin-tools", "temporal"],
    ])
    if dry_run:
        return commands
    state.mkdir(parents=True, exist_ok=True)
    for name in ("checkpoints", "evaluations", "logs"):
        (state / name).mkdir(exist_ok=True)
    config_path = state / "config.yaml"
    if not config_path.exists():
        config = {
            "schema_version": 1,
            "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "kite-coding-tasks"},
            "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
            "harness2": {"checkout": str(harness_dir), "cli": str(Path(venv_python).parent / "harness2")},
            "providers": [],
        }
        # JSON string quoting is valid YAML scalar syntax and keeps bootstrap
        # usable before the virtualenv/YAML dependency has been installed.
        yaml_text = (
            "schema_version: 1\n"
            "temporal:\n"
            f"  endpoint: {json.dumps(config['temporal']['endpoint'])}\n"
            f"  task_queue: {json.dumps(config['temporal']['task_queue'])}\n"
            "opencode:\n"
            f"  executable: {json.dumps(config['opencode']['executable'])}\n"
            "  server:\n"
            f"    host: {json.dumps(config['opencode']['server']['host'])}\n"
            f"    port: {config['opencode']['server']['port']}\n"
            "harness2:\n"
            f"  checkout: {json.dumps(config['harness2']['checkout'])}\n"
            f"  cli: {json.dumps(config['harness2']['cli'])}\n"
            "providers: []\n"
        )
        temp = config_path.with_suffix(".yaml.tmp")
        temp.write_text(yaml_text, encoding="utf-8")
        temp.replace(config_path)
    env_path = repo / "deploy" / ".env"
    if not env_path.exists():
        import secrets
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"TEMPORAL_POSTGRES_PASSWORD={secrets.token_urlsafe(36)}\nKITE_DATA_DIR={data_dir}\n")
    else:
        mode = env_path.stat().st_mode & 0o777
        if os.name == "posix" and mode & 0o077:
            raise BootstrapError("deploy/.env permissions must be 0600 or stricter; values were not read")
    runner = command_runner or _run_command
    for command in commands:
        runner(command, repo)
    return commands


def _run_command(command: list[str], cwd: Path | None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _host_version() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION_ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/kite"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        python_executable = os.environ.get("KITE_BOOTSTRAP_PYTHON", sys.executable)
        python_version = _python_version(python_executable)
        dry_run = args.dry_run
        cmds = bootstrap(args.repo, args.data_dir, dry_run=args.dry_run,
                         system="Linux" if dry_run else platform.system(),
                         version="24.04" if dry_run else _host_version(),
                         git=dry_run or shutil.which("git") is not None,
                         python312=dry_run or (3, 12) <= python_version < (3, 14),
                         python_executable=python_executable,
                         docker=dry_run or shutil.which("docker") is not None,
                         compose=dry_run or _compose_available(),
                         distro="ubuntu" if dry_run else _host_distro(),
                         available_bytes=shutil.disk_usage(_disk_check_path(args.data_dir)).free,
                         opencode_installed=dry_run or shutil.which("opencode") is not None)
        print(json.dumps({"dry_run": args.dry_run, "commands": cmds}, indent=2))
        return 0
    except (OSError, BootstrapError, subprocess.CalledProcessError) as exc:
        print(f"kite bootstrap: {exc}", file=sys.stderr)
        return 1


def _compose_available() -> bool:
    command = ["docker", "compose", "version"]
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0:
        command.insert(0, "sudo")
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _python_version(executable: str) -> tuple[int, int]:
    try:
        result = subprocess.run(
            [executable, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            check=True, capture_output=True, text=True,
        )
        major, minor = result.stdout.strip().split(".", 1)
        return int(major), int(minor)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0, 0


def _host_distro() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _disk_check_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


if __name__ == "__main__":
    raise SystemExit(main())
