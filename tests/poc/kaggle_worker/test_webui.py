"""P4: the web page is built from web/ into web/dist (kite web build, kite update), and kite update installs
new libraries before it tests the new code."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from poc.kaggle_worker import update, webui
from tests.poc.kaggle_worker.test_chat_first import git, make_repo


def make_web(root: Path) -> Path:
    web = root / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text(json.dumps({"name": "kite-web", "scripts": {"build": "vite build"}}))
    (web / "package-lock.json").write_text("{}")
    (web / "index.html").write_text("<div id=root></div>")
    (web / "src" / "main.tsx").write_text("console.log(1)\n")
    return web


class FakeNpm:
    """npm ci / npm run build as subprocess calls; the build writes the page into KITE_WEB_OUT."""

    def __init__(self, fail_build: bool = False):
        self.calls: list[list[str]] = []
        self.fail_build = fail_build

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[1:3] == ["run", "build"]:
            assert Path(cmd[0]).parent.as_posix() in kwargs["env"]["PATH"]       # node next to npm is found
            if self.fail_build:
                return subprocess.CompletedProcess(cmd, 1, "", "src/App.tsx(3,1): error TS2304")
            out = Path(kwargs["env"]["KITE_WEB_OUT"])
            (out / "assets").mkdir(parents=True)
            (out / "index.html").write_text(f"build {len(self.calls)}")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def fake_python(tmp_path: Path) -> str:
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    npm = bin_dir / "npm"
    npm.write_text("#!/bin/sh\n")
    npm.chmod(0o755)
    return str(bin_dir / "python")


def test_the_page_builds_once_per_source_change_and_a_failed_build_keeps_the_last_one(tmp_path):
    web, python = make_web(tmp_path), fake_python(tmp_path)
    npm = FakeNpm()
    first = webui.build(web, python=python, runner=npm)
    assert first["ui"] == "đã build giao diện mới" and first["installed"]
    assert [c[1:3] for c in npm.calls] == [["ci", "--no-audit"], ["run", "build"]]
    assert npm.calls[0][0] == str(Path(python).parent / "npm")                  # the ui extra's npm
    assert (web / "dist" / "index.html").read_text() == "build 2"
    assert webui.status(web)["current"] is True
    # Nothing changed: no npm at all.
    assert webui.build(web, python=python, runner=npm)["ui"] == "giao diện đã là bản mới nhất" and len(npm.calls) == 2
    # A source change rebuilds; the packages are only installed again when the lock file changed.
    (web / "src" / "main.tsx").write_text("console.log(2)\n")
    assert webui.status(web)["current"] is False
    webui.build(web, python=python, runner=npm)
    assert [c[1] for c in npm.calls[2:]] == ["run"]
    (web / "src" / "main.tsx").write_text("console.log(3)\n")
    broken = webui.build(web, python=python, runner=FakeNpm(fail_build=True))
    assert "TS2304" in broken["ui_error"] and (web / "dist" / "index.html").read_text() == "build 3"
    assert not (web / ".dist-new").exists()


def test_without_node_the_build_says_how_to_get_it(tmp_path, monkeypatch):
    web = make_web(tmp_path)
    monkeypatch.setattr(webui.shutil, "which", lambda name: None)
    report = webui.build(web, python=str(tmp_path / "nowhere" / "python"), runner=FakeNpm())
    assert "[ui]" in report["ui_error"] and not (web / "dist").exists()
    assert webui.build(tmp_path / "missing")["ui"] == "không có thư mục web/"


def test_kite_update_installs_new_libraries_before_the_tests_and_builds_the_page_after(tmp_path):
    origin, _ = make_repo(tmp_path, "origin")
    (origin / "requirements.txt").write_text("-e .\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "requirements")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    old = git(clone, "rev-parse", "HEAD")
    (origin / "requirements.txt").write_text("-e .[web,ui]\n")
    make_web(origin)
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "web page")
    python = fake_python(tmp_path)
    calls = []

    def failing_install(cmd, **kwargs):
        calls.append(cmd)
        if "install" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "No solution found")
        return subprocess.run(cmd, **kwargs)

    failed = update.run(clone, python=python, runner=failing_install, restart=False)
    assert failed["updated"] is False and "cài thư viện mới lỗi" in failed["error"]
    assert git(clone, "rev-parse", "HEAD") == old and not any(c[1:3] == ["-m", "pytest"] for c in calls)

    npm = FakeNpm()
    order = []

    def passing(cmd, **kwargs):
        if "install" in cmd and "-r" in cmd:
            order.append("install")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:3] == ["-m", "pytest"]:
            order.append("tests")
            return subprocess.CompletedProcess(cmd, 0, "480 passed\n", "")
        if Path(cmd[0]).name == "npm":
            order.append("npm " + cmd[1])
            return npm(cmd, **kwargs)
        return subprocess.run(cmd, **kwargs)

    done = update.run(clone, python=python, runner=passing, restart=False)
    assert done["updated"] and done["libraries"].startswith("đã cài thư viện mới")
    assert order == ["install", "tests", "npm ci", "npm run"]
    assert done["ui"] == "đã build giao diện mới" and (clone / "web" / "dist" / "index.html").is_file()


def test_the_install_command_uses_uv_when_the_venv_has_no_pip(tmp_path, monkeypatch):
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    monkeypatch.setattr(update.shutil, "which", lambda name: str(uv) if name == "uv" else None)
    assert update.install_command("/venv/bin/python", tmp_path)[:5] == [str(uv), "pip", "install", "-q", "--python"]
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    monkeypatch.setattr(update.Path, "home", lambda: tmp_path / "nohome")
    assert update.install_command("/venv/bin/python", tmp_path)[:4] == ["/venv/bin/python", "-m", "pip", "install"]
