from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sys
import threading

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from poc.kaggle_worker import protocol
from poc.kaggle_worker.store import LeaseError, SQLiteSchedulerStore
try:
    from poc.kaggle_worker.scheduler import WorkerScheduler
except ModuleNotFoundError:
    WorkerScheduler = None


def valid_worker(**overrides):
    values = dict(
        worker_id="wk-a", owner_id="owner-a", provider_id="fake",
        roles=("coder", "reviewer"), model_ids=("model-a", "model-b"),
        enabled=True, provider_ready=True, status_observed_at=100.0,
        max_concurrency=1,
    )
    values.update(overrides)
    return protocol.WorkerProfile(**values)


def valid_owner(**overrides):
    values = dict(
        owner_id="owner-a", owner_authorized=True, policy_approved=True,
        max_concurrent_sessions=1, quota_remaining_seconds=1800,
        quota_observed_at=100.0,
    )
    values.update(overrides)
    return protocol.OwnerPolicy(**values)


def valid_task(**overrides):
    values = dict(
        task_id="task-a", role="coder", model_id="model-a",
        repo_head="abc123", branch="agent/task-a",
        payload_json='{"goal":"fix bug"}', estimated_gpu_seconds=90,
        priority=10, created_at=100.0,
    )
    values.update(overrides)
    return protocol.ScheduledTask(**values)


def make_scheduler(path, owner=None, workers=None, tasks=None, max_age=300):
    store = SQLiteSchedulerStore(path)
    owner = owner or valid_owner()
    workers = workers or [valid_worker()]
    tasks = tasks or [valid_task()]
    store.register_owner_policy(owner)
    for worker in workers:
        store.register_worker(worker)
    for task in tasks:
        store.enqueue(task)
    if WorkerScheduler is None:
        store.close()
        pytest.skip("worker scheduler has not been implemented yet")
    return store, WorkerScheduler(store, max_observation_age_seconds=max_age)


def test_scheduler_types_and_states_are_available():
    expected = {"WorkerProfile", "OwnerPolicy", "ScheduledTask", "TaskLease", "WorkerResult", "TaskState"}
    assert expected <= set(vars(protocol))
    assert WorkerScheduler is not None


def test_role_and_model_mismatch_stay_queued(tmp_path):
    cases = [
        (valid_worker(roles=("reviewer",)), valid_task()),
        (valid_worker(model_ids=("model-b",)), valid_task()),
    ]
    for index, (worker, task) in enumerate(cases):
        store, scheduler = make_scheduler(tmp_path / f"mismatch-{index}.db", workers=[worker], tasks=[task])
        try:
            assert scheduler.dispatch_next(worker.worker_id, now=110.0, lease_seconds=30) is None
            assert store.get_task_state(task.task_id) == "QUEUED"
        finally:
            store.close()


@pytest.mark.parametrize(
    "owner_changes,worker_changes,now",
    [
        ({}, {"enabled": False}, 110.0),
        ({}, {"provider_ready": False}, 110.0),
        ({}, {"status_observed_at": 0.0}, 1000.0),
        ({"owner_authorized": False}, {}, 110.0),
        ({"policy_approved": False}, {}, 110.0),
        ({"quota_remaining_seconds": None}, {}, 110.0),
        ({"quota_remaining_seconds": 10}, {}, 110.0),
        ({"quota_observed_at": 0.0}, {}, 1000.0),
    ],
)
def test_ineligible_worker_or_owner_never_receives_task(tmp_path, owner_changes, worker_changes, now):
    owner = valid_owner(**owner_changes)
    worker = valid_worker(**worker_changes)
    store, scheduler = make_scheduler(tmp_path / f"gate-{now}-{len(owner_changes)}-{len(worker_changes)}.db", owner, [worker])
    try:
        assert scheduler.dispatch_next(worker.worker_id, now=now, lease_seconds=30) is None
        assert store.get_task_state("task-a") == "QUEUED"
    finally:
        store.close()


def test_scheduler_picks_priority_then_fifo_and_reserves_estimate(tmp_path):
    owner = valid_owner(max_concurrent_sessions=2, quota_remaining_seconds=150)
    worker = valid_worker(max_concurrency=2)
    tasks = [
        valid_task(task_id="low", priority=1, created_at=100, estimated_gpu_seconds=60),
        valid_task(task_id="high-b", priority=5, created_at=101, estimated_gpu_seconds=60),
        valid_task(task_id="high-a", priority=5, created_at=100, estimated_gpu_seconds=60),
    ]
    store, scheduler = make_scheduler(tmp_path / "priority.db", owner, [worker], tasks)
    try:
        lease = scheduler.dispatch_next("wk-a", now=110, lease_seconds=30)
        assert lease.task_id == "high-a"
        assert scheduler.dispatch_next("wk-a", now=111, lease_seconds=30).task_id == "high-b"
        assert scheduler.dispatch_next("wk-a", now=112, lease_seconds=30) is None
    finally:
        store.close()


def test_worker_and_owner_concurrency_caps_are_enforced(tmp_path):
    owner = valid_owner(max_concurrent_sessions=1)
    workers = [valid_worker(worker_id="wk-a"), valid_worker(worker_id="wk-b")]
    tasks = [valid_task(task_id="task-a"), valid_task(task_id="task-b", created_at=101)]
    store, scheduler = make_scheduler(tmp_path / "caps.db", owner, workers, tasks)
    try:
        assert scheduler.dispatch_next("wk-a", now=110, lease_seconds=30) is not None
        assert scheduler.dispatch_next("wk-a", now=111, lease_seconds=30) is None
        assert scheduler.dispatch_next("wk-b", now=112, lease_seconds=30) is None
    finally:
        store.close()


def test_heartbeat_completion_and_idempotent_quota_settlement(tmp_path):
    store, scheduler = make_scheduler(
        tmp_path / "complete.db", valid_owner(max_concurrent_sessions=2, quota_remaining_seconds=300),
        [valid_worker(max_concurrency=2)], [valid_task(estimated_gpu_seconds=100)],
    )
    try:
        lease = scheduler.dispatch_next("wk-a", now=110, lease_seconds=20)
        extended = scheduler.heartbeat(lease.lease_id, now=120, lease_seconds=50)
        assert extended.expires_at == 170
        result = protocol.WorkerResult(
            task_id=lease.task_id, lease_id=lease.lease_id, worker_id=lease.worker_id,
            base_repo_head="abc123", patch="diff --git a/x b/x", summary="fixed", usage_gpu_seconds=30,
        )
        scheduler.complete(lease.lease_id, result, now=130)
        scheduler.complete(lease.lease_id, result, now=131)
        assert store.get_task_state(lease.task_id) == "COMPLETED"
        assert store.get_owner_policy("owner-a").quota_remaining_seconds == 270
        with pytest.raises(LeaseError, match="result"):
            scheduler.complete(lease.lease_id, replace(result, summary="different"), now=132)
    finally:
        store.close()


def test_retryable_failure_charges_reservation_and_requeues_task(tmp_path):
    store, scheduler = make_scheduler(
        tmp_path / "retry.db", valid_owner(quota_remaining_seconds=200),
        tasks=[valid_task(estimated_gpu_seconds=60)],
    )
    try:
        lease = scheduler.dispatch_next("wk-a", now=110, lease_seconds=30)
        scheduler.fail(lease.lease_id, "worker crashed", retryable=True, now=120)
        assert store.get_task_state("task-a") == "QUEUED"
        assert store.get_owner_policy("owner-a").quota_remaining_seconds == 140
        resumed = scheduler.dispatch_next("wk-a", now=121, lease_seconds=30)
        assert resumed.task_id == "task-a"
    finally:
        store.close()


def test_expired_lease_releases_task_and_charges_reservation_once(tmp_path):
    store, scheduler = make_scheduler(
        tmp_path / "expiry.db", valid_owner(quota_remaining_seconds=200),
        tasks=[valid_task(estimated_gpu_seconds=50)],
    )
    try:
        old = scheduler.dispatch_next("wk-a", now=110, lease_seconds=20)
        new = scheduler.dispatch_next("wk-a", now=131, lease_seconds=20)
        assert new.task_id == old.task_id
        assert new.lease_id != old.lease_id
        assert store.get_owner_policy("owner-a").quota_remaining_seconds == 150
        assert scheduler.dispatch_next("wk-a", now=132, lease_seconds=20) is None
    finally:
        store.close()


def test_two_store_instances_cannot_claim_the_same_task_twice(tmp_path):
    db = tmp_path / "atomic.db"
    owner = valid_owner(max_concurrent_sessions=2)
    worker_a, worker_b = valid_worker(worker_id="wk-a"), valid_worker(worker_id="wk-b")
    store_a = SQLiteSchedulerStore(db)
    store_b = SQLiteSchedulerStore(db)
    for store in (store_a, store_b):
        store.register_owner_policy(owner)
    store_a.register_worker(worker_a)
    store_a.register_worker(worker_b)
    store_a.enqueue(valid_task())
    if WorkerScheduler is None:
        pytest.skip("worker scheduler has not been implemented yet")
    sched_a, sched_b = WorkerScheduler(store_a), WorkerScheduler(store_b)
    barrier = threading.Barrier(2)
    def claim(scheduler, worker_id):
        barrier.wait(timeout=3)
        return scheduler.dispatch_next(worker_id, now=110, lease_seconds=30)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: claim(*args), [(sched_a, "wk-a"), (sched_b, "wk-b")]))
        assert sum(result is not None for result in results) == 1
        assert store_a.get_task_state("task-a") == "LEASED"
    finally:
        store_a.close()
        store_b.close()




def test_cancel_queued_task_is_idempotent(tmp_path):
    store, scheduler = make_scheduler(tmp_path / "cancel-queued.db")
    try:
        cancel = getattr(scheduler, "cancel", None)
        assert callable(cancel), "WorkerScheduler.cancel is missing"
        cancel("task-a", now=110)
        cancel("task-a", now=111)
        assert store.get_task_state("task-a") == "CANCELLED"
        assert scheduler.dispatch_next("wk-a", now=112, lease_seconds=30) is None
    finally:
        store.close()


def test_cancel_active_task_stops_lease_and_charges_reservation_once(tmp_path):
    store, scheduler = make_scheduler(
        tmp_path / "cancel-active.db", valid_owner(quota_remaining_seconds=200),
        tasks=[valid_task(estimated_gpu_seconds=60)],
    )
    try:
        lease = scheduler.dispatch_next("wk-a", now=110, lease_seconds=30)
        cancel = getattr(scheduler, "cancel", None)
        assert callable(cancel), "WorkerScheduler.cancel is missing"
        cancel("task-a", now=115)
        cancel("task-a", now=116)
        assert store.get_task_state("task-a") == "CANCELLED"
        assert store.get_owner_policy("owner-a").quota_remaining_seconds == 140
        with pytest.raises(LeaseError, match="active"):
            scheduler.heartbeat(lease.lease_id, now=117, lease_seconds=20)
    finally:
        store.close()


def test_worker_replacement_resumes_from_shared_checkpoint_and_pinned_head(tmp_path):
    from poc.kaggle_worker.checkpoint import TaskCheckpoint, load_checkpoint, save_checkpoint

    owner_b = valid_owner(owner_id="owner-b")
    worker_a = valid_worker(worker_id="wk-a", owner_id="owner-a")
    worker_b = valid_worker(worker_id="wk-b", owner_id="owner-b")
    task = valid_task(repo_head="pinned-head", branch="agent/task-a")
    store = SQLiteSchedulerStore(tmp_path / "replacement.db")
    store.register_owner_policy(valid_owner())
    store.register_owner_policy(owner_b)
    store.register_worker(worker_a)
    store.register_worker(worker_b)
    store.enqueue(task)
    scheduler = WorkerScheduler(store)
    checkpoint_path = tmp_path / "shared-checkpoints" / "task-a.json"
    try:
        store.register_owner_policy(owner_b)
        lease_a = scheduler.dispatch_next("wk-a", now=110, lease_seconds=20)
        assert lease_a.task_id == task.task_id
        save_checkpoint(TaskCheckpoint(
            task_id=task.task_id, task_contract="fix bug", plan=["inspect", "fix"],
            completed_steps=["inspect"], pending_steps=["fix"], repo_path="/shared/repo",
            branch=task.branch, repo_head=task.repo_head, changed_files=["src/app.py"],
            last_tool_result="Root cause identified", model_id="model-a",
            provider_id="fake-a", updated_at="2026-11-17T00:00:00Z",
        ), checkpoint_path)
        scheduler.fail(lease_a.lease_id, "worker stopped", retryable=True, now=115)

        lease_b = scheduler.dispatch_next("wk-b", now=116, lease_seconds=20)
        resumed = load_checkpoint(checkpoint_path, expected_repo_head=task.repo_head)
        resume_context = (
            f"Task {resumed.task_id}; branch {resumed.branch}; HEAD {resumed.repo_head}; "
            f"completed {resumed.completed_steps}; pending {resumed.pending_steps}; "
            f"last result {resumed.last_tool_result}"
        )
        assert lease_b.task_id == task.task_id
        assert lease_b.worker_id == "wk-b"
        assert resumed.task_id == task.task_id
        assert resumed.branch == task.branch
        assert resumed.repo_head == task.repo_head
        assert "fix" in resume_context
        assert "wk-a" not in resume_context
        assert "lease" not in resume_context
        assert scheduler.dispatch_next("wk-a", now=117, lease_seconds=20) is None
    finally:
        store.close()


def test_simultaneous_workers_claim_isolated_task_branches_and_heads(tmp_path):
    owners = [valid_owner(owner_id="owner-a"), valid_owner(owner_id="owner-b")]
    workers = [
        valid_worker(worker_id="wk-a", owner_id="owner-a", roles=("coder-a",), model_ids=("model-a",)),
        valid_worker(worker_id="wk-b", owner_id="owner-b", roles=("coder-b",), model_ids=("model-b",)),
    ]
    tasks = [
        valid_task(task_id="task-a", role="coder-a", model_id="model-a", branch="agent/task-a", repo_head="head-a", created_at=100),
        valid_task(task_id="task-b", role="coder-b", model_id="model-b", branch="agent/task-b", repo_head="head-b", created_at=101),
    ]
    db = tmp_path / "isolated.db"
    store_a, store_b = SQLiteSchedulerStore(db), SQLiteSchedulerStore(db)
    for owner in owners:
        store_a.register_owner_policy(owner)
    for worker in workers:
        store_a.register_worker(worker)
    for task in tasks:
        store_a.enqueue(task)
    schedulers = [WorkerScheduler(store_a), WorkerScheduler(store_b)]
    barrier = threading.Barrier(2)

    def claim(args):
        scheduler, worker_id = args
        barrier.wait(timeout=3)
        return scheduler.dispatch_next(worker_id, now=110, lease_seconds=30)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            leases = list(pool.map(claim, zip(schedulers, ("wk-a", "wk-b"))))
        assert {lease.task_id for lease in leases} == {"task-a", "task-b"}
        assert len({lease.lease_id for lease in leases}) == 2
        scheduler_task_map = {task.task_id: task for task in tasks}
        assert {scheduler_task_map[lease.task_id].branch for lease in leases} == {"agent/task-a", "agent/task-b"}
        assert {scheduler_task_map[lease.task_id].repo_head for lease in leases} == {"head-a", "head-b"}
        assert store_a.get_task_state("task-a") == "LEASED"
        assert store_a.get_task_state("task-b") == "LEASED"
    finally:
        store_a.close()
        store_b.close()

