"""Runnable evidence for the 2026-11-18 Kite review (defects E1-E6). Run from the repo root:

    python -m pytest docs/reviews/2026-11-18-phase1/evidence/test_evidence.py -q -rA

Each test asserts the DEFECTIVE behaviour. A passing test means the defect is still present;
a failing test means it has been fixed. This file lives outside ``tests/`` on purpose, so the
normal test run (testpaths = ["tests"]) does not collect it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd() / "tests" / "poc" / "kaggle_worker"))
from test_opencode import FakeOpenCode  # noqa: E402  (reuse the repo's own fake server)

from poc.kaggle_worker.opencode import OpenCodeClient, OpenCodeTaskAdapter
from poc.kaggle_worker.protocol import OwnerPolicy, ScheduledTask, WorkerProfile, WorkerResult
from poc.kaggle_worker.routing import RoutingRequest
from poc.kaggle_worker.store import SQLiteSchedulerStore
from poc.kaggle_worker.temporal_workflow import CodingTaskInput, RouteLease


def _git_repo(path: Path) -> str:
    path.mkdir()
    for cmd in (["init"], ["config", "user.name", "t"], ["config", "user.email", "t@x"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True, capture_output=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "b"], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def _store(tmp_path: Path, quota: int = 3600) -> SQLiteSchedulerStore:
    store = SQLiteSchedulerStore(tmp_path / "s.sqlite3")
    store.register_owner_policy(OwnerPolicy("han", True, True, 1, quota, 1000.0))
    store.register_worker(WorkerProfile("kaggle-1", "han", "kaggle", ("coder",), ("qwen",), True, True, 1000.0, 1))
    return store


def _task(task_id: str) -> ScheduledTask:
    return ScheduledTask(task_id, "coder", None, "h" * 40, f"agent/{task_id}",
                         json.dumps({"goal": "g"}), 900, 0, 1000.0)


# E1 ---------------------------------------------------------------------------
def test_E1_permission_request_is_never_answered_and_run_times_out(tmp_path):
    """OpenCode sessions are created with shell=ask, but nothing replies to permission.asked."""
    fake = FakeOpenCode()
    fake.terminal_event = "permission.asked"          # model wants to run e.g. `pytest`
    try:
        head = _git_repo(tmp_path / "wt")
        task = CodingTaskInput("t1", "run tests", str(tmp_path / "wt"), head, "agent/t1", ("coding",), None, 60)
        route = RouteLease("t1", "l1", "han", "kaggle", "kaggle-1", "qwen", 60)
        adapter = OpenCodeTaskAdapter(OpenCodeClient(fake.base_url), timeout_seconds=2, poll_interval_seconds=0.2)
        beats: list[dict] = []

        async def go():
            sid = await adapter.prepare_session(task, route, str(tmp_path / "wt"), None, "k-prep")
            await adapter.run(task, route, str(tmp_path / "wt"), "", sid, "k-run", beats.append)

        with pytest.raises(TimeoutError):
            asyncio.run(go())
        replies = [r for r in fake.requests if "permission" in r[1] or "reply" in r[1]]
        states = {b.get("state") for b in beats}
        print(f"\nE1 heartbeat states={sorted(s for s in states if s)}; permission replies sent={len(replies)}")
        assert "APPROVAL_REQUIRED" in states and replies == []
    finally:
        fake.close()


# E2 ---------------------------------------------------------------------------
def test_E2_successful_tasks_never_consume_gpu_quota(tmp_path):
    """OpenCodeTaskAdapter.run always reports usage_gpu_seconds=0, and complete() charges exactly that."""
    store = _store(tmp_path, quota=3600)
    for i in range(10):
        store.enqueue(_task(f"t{i}"))
        r = store.reserve_for_task(f"t{i}", f"k{i}", now=1000.0 + i, lease_seconds=600, max_observation_age_seconds=10**6)
        store.complete(r.lease.lease_id, WorkerResult(f"t{i}", r.lease.lease_id, "kaggle-1", "h" * 40,
                                                      "diff", "ok", usage_gpu_seconds=0), now=1001.0 + i)
    remaining = store.get_owner_policy("han").quota_remaining_seconds
    print(f"\nE2 after 10 tasks x 900s estimated: quota_remaining_seconds={remaining} (started 3600)")
    assert remaining == 3600


# E3 ---------------------------------------------------------------------------
def test_E3_required_capabilities_cannot_influence_routing():
    fields = {f.name for f in dataclasses.fields(RoutingRequest)}
    profile_fields = {f.name for f in dataclasses.fields(WorkerProfile)}
    print(f"\nE3 RoutingRequest fields={sorted(fields)}; WorkerProfile fields include capabilities? "
          f"{'capabilities' in profile_fields}")
    assert "capabilities" not in fields and "capabilities" not in profile_fields


# E4 ---------------------------------------------------------------------------
def test_E4_workflow_task_is_never_routable_because_it_is_not_enqueued(tmp_path):
    """`kite workflow start` builds CodingTaskInput only; reserve_for_task needs a QUEUED SQLite row."""
    store = _store(tmp_path)
    result = store.reserve_for_task("example-1", "example-1:lease:0", 1000.0, 600, 10**6)
    cli = Path("poc/kaggle_worker/cli.py").read_text()
    start_block = cli[cli.index('if action == "start"'):cli.index("handle = client.get_workflow_handle")]
    print(f"\nE4 reserve_for_task(not enqueued) -> {result}; 'enqueue' in workflow start path: {'enqueue' in start_block}")
    assert result is None and "enqueue" not in start_block


# E5 ---------------------------------------------------------------------------
def test_E5_git_push_deny_rule_is_bypassable_and_shell_runs_on_control_plane():
    src = Path("poc/kaggle_worker/opencode.py").read_text()
    assert '"resource": "git push *", "effect": "deny"' in src
    variants = ["git push origin main", "git -C . push origin main", "git -c x=y push",
                "sh -c 'git push'", "python -c \"import os;os.system('git push')\"",
                "cat /var/lib/kite/kite/secrets/kaggle_token"]
    denied = {v: fnmatch.fnmatch(v, "git push *") for v in variants}
    print("\nE5 matched by deny rule 'git push *' (glob semantics):")
    for v, d in denied.items():
        print(f"   {'DENY ' if d else 'ask  '} {v}")
    assert sum(denied.values()) == 1
    assert "docker" not in src and "sandbox" not in src.lower()


# E6 ---------------------------------------------------------------------------
def test_E6_waiting_for_worker_grows_workflow_history_without_bound():
    src = Path("poc/kaggle_worker/temporal_workflow.py").read_text()
    assert "await workflow.sleep(timedelta(seconds=15))" in src and "continue_as_new" not in src
    # Per wait iteration: ActivityTaskScheduled/Started/Completed + TimerStarted/Fired + 2x WorkflowTask
    # Scheduled/Started/Completed ~= 11 events (conservative lower bound ~5).
    for per_iter in (5, 11):
        iters = 51_200 // per_iter
        print(f"\nE6 ~{per_iter} events/iteration -> hard history limit (51,200) after {iters} waits "
              f"= {iters * 15 / 3600:.1f} h of waiting for a worker")
