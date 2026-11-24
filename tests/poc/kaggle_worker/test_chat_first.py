"""The chat does what the terminal did on the full-test day (step 2): task IDs that tolerate a model's
copying, a real cancel, building on a previous result, memory, repo config, doctor, cleanup, worker
restart, update and bench, all as tools; plus `kite update`, cleanup of bench folders and history."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from poc.kaggle_worker import memory
from poc.kaggle_worker.backend import task_slug
from poc.kaggle_worker.mcp_server import KiteToolError, chat_files
from tests.poc.kaggle_worker.test_mcp_server import FakeClient, make_config, make_ops, make_repo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def finished_task(ops, repo: Path, head: str, task_id: str, patch: str) -> Path:
    """A task that ran on ``head`` of ``repo`` and left ``patch`` (as the backend writes them)."""
    import time

    from poc.kaggle_worker.protocol import ScheduledTask

    with ops._store() as store:
        try:
            store.enqueue(ScheduledTask(task_id, "coder", None, head, f"agent/{task_id}", "{}", 900, 0,
                                        time.time() - 60))
        except Exception:
            pass                                       # already queued by start_task
    root = ops.state_dir / "tasks" / task_slug(task_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.patch").write_text(patch)
    (root / "checkpoint.json").write_text(json.dumps({"task_id": task_id, "repo_path": str(repo),
                                                      "repo_head": head}))
    return root / "result.patch"


CALC_PATCH = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-x = 1\n+x = 3\n"
HELPER_PATCH = ("diff --git a/helpers.py b/helpers.py\nnew file mode 100644\n--- /dev/null\n+++ b/helpers.py\n"
                "@@ -0,0 +1 @@\n+ZERO = 0\n")


def test_task_ids_resolve_from_a_short_part_and_a_wrong_one_lists_the_recent_tasks(tmp_path):
    repo, _ = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    first = asyncio.run(ops.start_task(str(repo), "Sửa a"))
    second = asyncio.run(ops.start_task(str(repo), "Sửa b"))
    assert second["short_id"] == second["task_id"][-4:]
    assert ops.resolve("") == second["task_id"] and ops.resolve("mới nhất") == second["task_id"]
    assert ops.resolve(first["task_id"][-4:]) == first["task_id"]
    # Step 2: the model wrote chat-20261121-141857 for chat-20261121-143039-7f5d.
    with pytest.raises(KiteToolError) as wrong:
        ops.resolve("chat-20991231-141857")
    assert "Các task gần đây" in str(wrong.value) and second["task_id"] in str(wrong.value)
    status = asyncio.run(ops.task_status(second["short_id"]))
    assert status["task_id"] == second["task_id"]
    assert "đang chờ worker" in status["waiting_for"]        # QUEUED: it says what it waits for


def test_cancel_marks_the_task_first_and_a_finished_task_takes_no_instruction(tmp_path):
    repo, _ = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    started = asyncio.run(ops.start_task(str(repo), "Sửa a"))
    cancelled = asyncio.run(ops.cancel_task(started["short_id"]))
    assert cancelled["cancelled"] and client.signals[-1][1] == "cancel"
    assert (ops.state_dir / "tasks" / task_slug(started["task_id"]) / "cancel_requested").is_file()
    client.state[started["task_id"]] = "COMPLETED"
    with pytest.raises(KiteToolError, match="continue_from"):
        asyncio.run(ops.send_instruction(started["task_id"], "thêm test"))


def test_a_dirty_repo_is_refused_unless_told_and_a_follow_up_builds_on_the_previous_result(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    previous = finished_task(ops, repo, head, "chat-prev-0001", HELPER_PATCH + CALC_PATCH)
    # The previous result applied but not committed: a plain new task would silently miss it.
    subprocess.run(["git", "-C", str(repo), "apply", str(previous)], check=True)
    with pytest.raises(KiteToolError, match="chưa commit") as dirty:
        asyncio.run(ops.start_task(str(repo), "Sửa tiếp"))
    assert "calc.py" in str(dirty.value) and "continue_from" in str(dirty.value)
    ignored = asyncio.run(ops.start_task(str(repo), "Việc khác", ignore_uncommitted=True))
    assert "KHÔNG thấy" in ignored["notes"][0] and "helpers.py" in ignored["notes"][1]   # the new file: a note
    # "Sửa tiếp": allowed with the applied patch, and the task starts from it.
    follow = asyncio.run(ops.start_task(str(repo), "Thêm ONE = 1 vào helpers.py", continue_from="0001"))
    task_input = client.started[-1][0]
    assert follow["continue_from"] == "chat-prev-0001" and task_input.base_patch == str(previous)
    assert task_input.base_task == "chat-prev-0001" and "sửa tiếp kết quả của task chat-prev-0001" in task_input.goal
    # Once committed, the commit holds the previous result: nothing to apply on top.
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "helpers")
    asyncio.run(ops.start_task(str(repo), "Thêm TWO", continue_from="chat-prev-0001"))
    assert client.started[-1][0].base_patch == "" and "đã có trong commit hiện tại" in client.started[-1][0].goal


def test_applying_a_follow_up_result_swaps_out_the_previous_one(tmp_path):
    repo, head = make_repo(tmp_path)
    client = FakeClient()
    ops = make_ops(tmp_path, client)
    previous = finished_task(ops, repo, head, "chat-prev-0001", HELPER_PATCH)
    subprocess.run(["git", "-C", str(repo), "apply", str(previous)], check=True)
    follow = asyncio.run(ops.start_task(str(repo), "Thêm ONE", continue_from="0001"))
    task_id = follow["task_id"]
    both = HELPER_PATCH.replace("@@ -0,0 +1 @@\n+ZERO = 0\n", "@@ -0,0 +1,2 @@\n+ZERO = 0\n+ONE = 1\n")
    patch = finished_task(ops, repo, head, task_id, both)
    client.state[task_id] = "COMPLETED"
    client.outcomes[task_id] = SimpleNamespace(status="READY_FOR_REVIEW", review_bundle=None,
                                               checkpoint_path=str(patch.parent / "checkpoint.json"))
    from tests.poc.kaggle_worker.test_mcp_server import owner_code

    applied = asyncio.run(ops.apply_result(task_id, owner_code(ops, "apply", task_id)))
    assert "chat-prev-0001" in applied["replaced"]
    assert (repo / "helpers.py").read_text() == "ZERO = 0\nONE = 1\n"


def test_wait_task_answers_when_the_task_ends(tmp_path):
    repo, _ = make_repo(tmp_path)
    client = FakeClient()
    started = []

    async def sleep(_seconds):
        started.append(_seconds)
        client.state[task_id] = "COMPLETED"

    ops = make_ops(tmp_path, client)
    ops._sleep = sleep
    task_id = asyncio.run(ops.start_task(str(repo), "Sửa a"))["task_id"]
    report = asyncio.run(ops.wait_task("", minutes=3))
    assert report["done"] is True and report["workflow"] == "COMPLETED" and started == [15]


def test_remember_goes_into_the_chats_agents_file_and_a_repo_lesson_into_its_tasks(tmp_path):
    repo, _ = make_repo(tmp_path)
    chat = tmp_path / "kite-chat"
    chat.mkdir()
    files = chat_files(make_config(tmp_path), lead_model="qwen3.6", kite_command=["kite"], allowed_roots=[tmp_path],
                       state_dir=tmp_path / "state")
    (chat / "AGENTS.md").write_text(files["AGENTS.md"])
    ops = make_ops(tmp_path, FakeClient())
    ops.chat_dir = chat
    # What the model proposes is inert until the owner approves it in their own terminal.
    saved = asyncio.run(ops.remember("Luôn hỏi trước khi tạo task sửa tiếp"))
    assert saved["saved"] == "pending" and memory.preferences(ops.state_dir) == []
    assert "Luôn hỏi" not in (chat / "AGENTS.md").read_text()
    memory.approve_pending(ops.state_dir, saved["pending_id"])
    memory.refresh_agents_file(chat / "AGENTS.md", ops.state_dir)
    assert "Luôn hỏi trước khi tạo task sửa tiếp" in (chat / "AGENTS.md").read_text()
    proposed = asyncio.run(ops.remember("Dùng .get cho khóa tùy chọn", repo=str(repo)))
    assert "Dùng .get" not in memory.lessons_note(ops.state_dir, repo)
    memory.approve_pending(ops.state_dir, proposed["pending_id"])
    shown = asyncio.run(ops.memory_show(str(repo)))
    assert shown["chat"][0].startswith("1. ") and "Dùng .get" in shown["lessons"][0]
    assert shown["pending"] == []
    assert "Dùng .get" in memory.lessons_note(ops.state_dir, repo)
    asyncio.run(ops.memory_forget(1))
    assert "Luôn hỏi" not in (chat / "AGENTS.md").read_text() and memory.preferences(ops.state_dir) == []
    with pytest.raises(KiteToolError):
        asyncio.run(ops.memory_forget(9, repo=str(repo)))
    # A new chat setup keeps what the person asked to remember (once approved).
    saved = asyncio.run(ops.remember("Trả lời ngắn"))
    memory.approve_pending(ops.state_dir, saved["pending_id"])
    again = chat_files(make_config(tmp_path), lead_model="qwen3.6", kite_command=["kite"], allowed_roots=[tmp_path],
                       state_dir=ops.state_dir)
    assert "## Người dùng đã dặn" in again["AGENTS.md"] and "Trả lời ngắn" in again["AGENTS.md"]
    assert json.loads(again["opencode.json"])["permission"]["execute"] == "deny"     # no Code Mode


def test_repo_config_is_shown_checked_and_kept_by_kite_not_in_the_repo(tmp_path):
    repo, _ = make_repo(tmp_path)
    ops = make_ops(tmp_path, FakeClient())
    assert asyncio.run(ops.repo_config(str(repo)))["source"] == "chưa có"
    with pytest.raises(KiteToolError, match="quick_test"):
        asyncio.run(ops.repo_config(str(repo), "quick_test: pytest -q\n"))
    saved = asyncio.run(ops.repo_config(str(repo), "setup: pip install -q -r requirements.txt\ntest: pytest -q\n"))
    assert saved["saved"] and not (repo / ".kite.yml").exists()
    shown = asyncio.run(ops.repo_config(str(repo)))
    assert shown["source"] == "Kite lưu riêng" and "pip install -q -r requirements.txt" in shown["config"]


def test_machine_tools_ask_first_and_run_detached(tmp_path):
    repo, _ = make_repo(tmp_path)
    spawned = []
    ops = make_ops(tmp_path, FakeClient())
    ops._spawn = lambda command, log: spawned.append(command)
    assert asyncio.run(ops.worker_restart())["confirm_needed"] is True and not spawned
    assert asyncio.run(ops.worker_restart("kaggle-han", confirm=True))["started"]
    assert spawned[-1][-3:] == ["worker", "restart", "kaggle-han"]
    with pytest.raises(KiteToolError):
        asyncio.run(ops.worker_restart("kaggle-x", confirm=True))
    plan = asyncio.run(ops.bench_start(label="v6", repo_source=str(repo), worker="kaggle-han"))
    assert plan["confirm_needed"] and "--label v6" in plan["command"] and "--worker kaggle-han" in plan["command"]
    asyncio.run(ops.bench_start(confirm=True, label="v6", repo_source=str(repo)))
    assert spawned[-1][spawned[-1].index("--out") + 1] == str(ops.state_dir / "bench" / "v6")
    from poc.kaggle_worker import bench

    lock = bench.acquire_lock(ops.state_dir, label="v6", out=ops.state_dir / "bench" / "v6")
    with pytest.raises(KiteToolError, match="đang có một lần đo chạy"):      # one run at a time
        asyncio.run(ops.bench_start(confirm=True, label="v7", repo_source=str(repo)))
    bench.release_lock(lock)
    listed = asyncio.run(ops.cleanup())
    assert listed["removed"] is False and listed["folders"] == 0
    # An update while a task holds a worker would cut it off.
    asyncio.run(ops.start_task(str(repo), "Sửa a"))
    with ops._store() as store:
        store._connection.execute("update tasks set state = 'LEASED'")
        store._connection.commit()
    with pytest.raises(KiteToolError, match="đang có task chạy"):
        asyncio.run(ops.update_kite(confirm=True))


def test_kite_update_tests_the_new_code_and_goes_back_when_it_fails(tmp_path, monkeypatch):
    from poc.kaggle_worker import update

    units = tmp_path / "units"                     # not this machine's /etc/systemd/system
    units.mkdir()
    monkeypatch.setattr(update, "UNIT_DIR", units)
    origin, _ = make_repo(tmp_path, "origin")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    old = git(clone, "rev-parse", "HEAD")
    (origin / "calc.py").write_text("x = 2\n")
    git(origin, "commit", "-qam", "two")
    assert update.plan(clone)["behind"] == 1
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["-m", "pytest"]:
            return subprocess.CompletedProcess(cmd, 1, "E boom\n1 failed\n", "")
        return subprocess.run(cmd, **kwargs)

    failed = update.run(clone, python="python3", runner=runner)
    assert failed["updated"] is False and "quay lại bản cũ" in failed["error"]
    assert git(clone, "rev-parse", "HEAD") == old                        # the old Kite is back
    assert not any(c[0] == "sudo" for c in calls)                       # nothing restarted

    def passing(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["-m", "pytest"] or cmd[0] == "sudo":
            return subprocess.CompletedProcess(cmd, 0, "370 passed\n", "")
        return subprocess.run(cmd, **kwargs)

    done = update.run(clone, python="python3", runner=passing, after=lambda: {"chat": "ok"})
    assert done["updated"] and done["restarted"] and done["chat"] == "ok" and done["tests"] == "370 passed"
    assert calls[-1] == ["sudo", "-n", "systemctl", "restart", "kite-gateway", "kite-workflow-worker"]
    assert update.run(clone, python="python3", runner=passing)["note"] == "Kite đã là bản mới nhất"
    (units / "kite-web.service").write_text("[Unit]\n")                # the web is restarted once installed
    assert update.services() == ("kite-gateway", "kite-workflow-worker", "kite-web")


def test_cleanup_and_history_also_see_bench_folders_written_to_the_home_folder(tmp_path):
    import os
    import time as clock

    from poc.kaggle_worker import bench, cleanup

    home = tmp_path / "home"
    run = home / "bench-repo-v5" / "runs" / "bench-qwen-r1"
    (run / "work").mkdir(parents=True)
    (run / "work" / "big.bin").write_bytes(b"x" * 4096)
    (home / "bench-repo-v5" / "repos").mkdir()
    (home / "bench-repo-v5" / "results.json").write_text(json.dumps({"label": "v5", "started": "2026-11-21 08:01",
                                                                       "models": {}}))
    (home / "notes" / "runs" / "x" / "work").mkdir(parents=True)          # no results.json: not a bench run
    old = clock.time() - 10 * 86400
    for path in [p for p in home.rglob("*")] + [home]:
        os.utime(path, (old, old))
    items = cleanup.plan(tmp_path / "state", days=7, extra_roots=[home])
    assert {i.path.relative_to(home).as_posix() for i in items} == {
        "bench-repo-v5/runs/bench-qwen-r1/work", "bench-repo-v5/repos"}
    state_bench = tmp_path / "state" / "bench" / "v6"
    state_bench.mkdir(parents=True)
    (state_bench / "results.json").write_text(json.dumps({"label": "v6", "started": "2026-11-22 09:00", "models": {}}))
    text, rows = bench.history([tmp_path / "state" / "bench", home])
    assert "Kite bench" in text
