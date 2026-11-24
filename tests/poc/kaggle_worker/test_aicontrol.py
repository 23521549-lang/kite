"""Phase 2 gates: confirmation codes for apply/commit, full patch paging, pristine-test verification."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from poc.kaggle_worker import confirm
from poc.kaggle_worker.mcp_server import KiteToolError
from test_mcp_server import FakeClient, finished_task, git_out, make_ops, make_repo


def owner_code(ops, action: str, task_id: str, **kwargs) -> str:
    """What `kite tasks confirm` does in the owner's terminal (the model cannot call this)."""
    from poc.kaggle_worker.backend import task_slug

    patch = ops.state_dir / "tasks" / task_slug(task_id) / "result.patch"
    return confirm.issue_code(ops.state_dir, action, task_id,
                              confirm.patch_sha256(patch.read_bytes()), **kwargs)


def applied_task(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    return repo, head, ops, task_id


def test_apply_result_refuses_without_a_code_and_with_a_wrong_one(tmp_path):
    repo, _head, ops, task_id = applied_task(tmp_path)
    with pytest.raises(KiteToolError, match="thiếu mã xác nhận"):
        asyncio.run(ops.apply_result(task_id))
    with pytest.raises(KiteToolError, match="không đúng"):
        asyncio.run(ops.apply_result(task_id, "ma-khong-co-that"))


def test_apply_result_refuses_a_code_minted_for_another_patch(tmp_path):
    repo, _head, ops, task_id = applied_task(tmp_path)
    foreign = confirm.issue_code(ops.state_dir, "apply", task_id, "0" * 64)
    with pytest.raises(KiteToolError, match="không khớp"):
        asyncio.run(ops.apply_result(task_id, foreign))
    asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    with pytest.raises(KiteToolError, match="không khớp"):      # an apply code is not a commit code
        asyncio.run(ops.commit_result(task_id, "Add median", owner_code(ops, "apply", task_id)))


def test_apply_result_refuses_an_expired_code(tmp_path):
    repo, _head, ops, task_id = applied_task(tmp_path)
    stale = owner_code(ops, "apply", task_id, clock=lambda: time.time() - 2000)
    with pytest.raises(KiteToolError, match="hết hạn"):
        asyncio.run(ops.apply_result(task_id, stale))


def test_apply_and_commit_happy_path_and_codes_are_single_use(tmp_path):
    repo, head = make_repo(tmp_path)
    for key, value in (("user.name", "t"), ("user.email", "t@e.com")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    apply_code = owner_code(ops, "apply", task_id)
    applied = asyncio.run(ops.apply_result(task_id, apply_code))
    assert applied["applied"] and (repo / "calc.py").read_text() == "x = 2\n"
    with pytest.raises(KiteToolError, match="đã được dùng"):
        asyncio.run(ops.apply_result(task_id, apply_code))
    commit_code = owner_code(ops, "commit", task_id)
    committed = asyncio.run(ops.commit_result(task_id, "Add median", commit_code))
    assert committed["committed"] and git_out(repo, "log", "-1", "--format=%s") == "Add median"
    with pytest.raises(KiteToolError, match="đã được dùng"):
        asyncio.run(ops.commit_result(task_id, "Again", commit_code))


def test_apply_and_commit_refuse_an_empty_task_id(tmp_path):
    repo, _head, ops, _task_id = applied_task(tmp_path)
    with pytest.raises(KiteToolError, match="ghi rõ task_id"):
        asyncio.run(ops.apply_result(""))
    with pytest.raises(KiteToolError, match="ghi rõ task_id"):
        asyncio.run(ops.commit_result("", "Add median", "whatever"))


def test_task_result_pages_cover_the_whole_patch_and_name_its_hash(tmp_path):
    from poc.kaggle_worker.backend import task_slug
    from poc.kaggle_worker.integration import ReviewBundle
    from poc.kaggle_worker.temporal_workflow import CodingTaskOutcome

    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa calc.py"))["task_id"]
    patch = "".join(f"+dòng {i}\n" for i in range(300))            # 300 lines, well over one page
    patch_path = tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(patch)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repo_path": str(repo)}))
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = CodingTaskOutcome(task_id, "READY_FOR_REVIEW", str(checkpoint), ReviewBundle(
        task_id, "lease", "kaggle-han", head, patch, "Kite test run: PASSED", 20))
    seen, offset = "", 0
    for _ in range(10):
        page = asyncio.run(ops.task_result(task_id, patch_offset=offset, patch_chars=1000))
        assert page["patch_sha256"] == confirm.patch_sha256(patch)
        assert page["patch_chars_total"] == len(patch)
        chunk = page["patch"].split("\n... (còn ")[0]
        seen += chunk
        offset += len(chunk)
        if offset >= len(patch):
            break
    assert seen == patch


def test_cli_tasks_confirm_mints_codes_bound_to_the_patch_and_memory_approval_flow(tmp_path, capsys):
    from poc.kaggle_worker import cli, memory
    from poc.kaggle_worker.backend import task_slug

    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    task_id = finished_task(tmp_path, client, ops, repo, head)
    config = tmp_path / "state" / "config.json"
    config.write_text("{}")
    assert cli.main(["--config", str(config), "tasks", "confirm", task_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    patch = (tmp_path / "state" / "tasks" / task_slug(task_id) / "result.patch").read_bytes()
    assert shown["patch_sha256"] == confirm.patch_sha256(patch)
    assert sorted(shown["files"]) == ["calc.py", "notes.md"] and shown["lines"] == "+2/-1"
    applied = asyncio.run(ops.apply_result(task_id, shown["apply_code"]))
    assert applied["applied"]
    # The memory queue is owner-driven from the same CLI.
    entry = memory.propose_preference(tmp_path / "state", "Trả lời ngắn")
    assert cli.main(["--config", str(config), "memory", "list-pending"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"][0]["id"] == entry["id"]
    assert cli.main(["--config", str(config), "memory", "approve", entry["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["approved"] is True
    assert "Trả lời ngắn" in memory.preferences_block(tmp_path / "state")


# --------------------------------------------------------------------- pristine-test verification

CP = subprocess.CompletedProcess


class WeakeningDocker:
    """The agent "fixes" the failing test by weakening it; test steps run the real pytest."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[1] in ("rm", "kill"):
            return CP(cmd, 0, "", "")
        kind = cmd[cmd.index("--name") + 1].split("-")[1]
        work = Path(cmd[cmd.index("-v") + 1].rsplit(":/work", 1)[0])
        if kind == "setup":
            (work / "generated_by_setup.txt").write_text("artefact\n")
            return CP(cmd, 0, "installed\n", "")
        if kind == "agent" or kind.startswith("fix"):
            (work / "tests" / "test_calc.py").write_text(
                "from calc import average\n\n\ndef test_average():\n    assert average([2, 4]) == 2\n")
            return CP(cmd, 0, "Đã nới lỏng test\n", "")
        if kind in ("test", "quick"):
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"],
                                  cwd=work, capture_output=True, text=True)
            return CP(cmd, done.returncode, done.stdout + done.stderr, "")
        return CP(cmd, 0, "", "")                                   # check / review: nothing to say


def test_a_patch_that_weakens_a_test_is_not_verified_and_pristine_tests_catch_it(tmp_path):
    from test_backend import SETTINGS, make_backend, make_store, make_task

    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                                     "-c", "user.email=t@example.com", *a],
                                    check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (repo / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) - 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import average\n\n\ndef test_average():\n    assert average([2, 4]) == 3\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    head = git("rev-parse", "HEAD")
    store = make_store(tmp_path)
    backend = make_backend(tmp_path, store, WeakeningDocker(), SETTINGS)
    task = make_task(store, repo, head, "task-weaken")

    async def scenario():
        work = await backend.prepare_workspace(task, "k1")
        route = await backend.reserve_route(task, "k2")
        session = await backend.prepare_opencode_session(task, route, work, None, "k3")
        return await backend.run_opencode(task, route, work, "", session, "k4", lambda beat: None)

    result = asyncio.run(scenario())
    saved = json.loads((backend.task_dir("task-weaken") / "result.json").read_text())
    assert saved["verified"] is False and "tests/test_calc.py" in saved["verified_reason"]
    assert saved["tests_edited"] == ["tests/test_calc.py"]
    assert saved["test_exit_code"] == 1                            # the pristine test fails on the bug
    assert "NOT VERIFIED" in result.summary and "FAILED" in result.summary
    assert "assert average([2, 4]) == 2" in result.patch            # the patch keeps the weakened test
