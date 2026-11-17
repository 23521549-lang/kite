from dataclasses import replace
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from poc.kaggle_worker.protocol import OwnerPolicy, ScheduledTask, TaskState, WorkerProfile
try:
    from poc.kaggle_worker.store import LeaseError, SQLiteSchedulerStore, StoreError
except ModuleNotFoundError:
    LeaseError = SQLiteSchedulerStore = StoreError = None


def sample_worker(**changes):
    data = dict(
        worker_id="worker-a", owner_id="owner-a", provider_id="fake",
        roles=("coder",), model_ids=("model-a",), enabled=True,
        provider_ready=True, status_observed_at=100.0, max_concurrency=1,
    )
    data.update(changes)
    return WorkerProfile(**data)


def sample_owner(**changes):
    data = dict(
        owner_id="owner-a", owner_authorized=True, policy_approved=True,
        max_concurrent_sessions=1, quota_remaining_seconds=900,
        quota_observed_at=100.0,
    )
    data.update(changes)
    return OwnerPolicy(**data)


def sample_task(**changes):
    data = dict(
        task_id="task-a", role="coder", model_id="model-a",
        repo_head="abc123", branch="agent/task-a", payload_json='{"goal":"fix"}',
        estimated_gpu_seconds=60, priority=1, created_at=100.0,
    )
    data.update(changes)
    return ScheduledTask(**data)


def test_scheduler_store_module_exists():
    assert SQLiteSchedulerStore is not None


@pytest.fixture
def store(tmp_path):
    if SQLiteSchedulerStore is None:
        pytest.skip("scheduler store has not been implemented yet")
    instance = SQLiteSchedulerStore(tmp_path / "scheduler.sqlite3")
    yield instance
    instance.close()


def test_owner_worker_and_queued_task_survive_store_restart(tmp_path):
    if SQLiteSchedulerStore is None:
        pytest.skip("scheduler store has not been implemented yet")
    db = tmp_path / "scheduler.sqlite3"
    owner, worker, task = sample_owner(), sample_worker(), sample_task()
    first = SQLiteSchedulerStore(db)
    first.register_owner_policy(owner)
    first.register_worker(worker)
    first.enqueue(task)
    assert first.get_owner_policy("owner-a") == owner
    assert first.get_worker("worker-a") == worker
    assert first.get_task_state("task-a") == TaskState.QUEUED.value
    first.close()

    reopened = SQLiteSchedulerStore(db)
    try:
        assert reopened.get_owner_policy("owner-a") == owner
        assert reopened.get_worker("worker-a") == worker
        assert reopened.get_task_state("task-a") == TaskState.QUEUED.value
    finally:
        reopened.close()


def test_enqueue_is_idempotent_for_identical_task_and_rejects_conflicting_id(store):
    task = sample_task()
    store.enqueue(task)
    store.enqueue(task)
    with pytest.raises(StoreError, match="task-a"):
        store.enqueue(replace(task, branch="agent/other"))
    assert store.get_task_state("task-a") == TaskState.QUEUED.value


def test_worker_id_cannot_be_reassigned_to_a_different_owner(store):
    store.register_owner_policy(sample_owner())
    store.register_worker(sample_worker())
    store.register_owner_policy(sample_owner(owner_id="owner-b"))
    with pytest.raises(StoreError, match="owner"):
        store.register_worker(sample_worker(owner_id="owner-b"))
    assert store.get_worker("worker-a").owner_id == "owner-a"


def test_worker_registration_requires_existing_owner_and_rolls_back(store):
    with pytest.raises(StoreError):
        store.register_worker(sample_worker())
    assert store.get_worker("worker-a") is None


def test_unknown_lease_heartbeat_raises_lease_error(store):
    with pytest.raises(LeaseError, match="lease"):
        store.heartbeat("missing-lease", now=110.0, lease_seconds=30)


def test_temporal_route_reservation_reuses_same_key_and_lease(store):
    store.register_owner_policy(sample_owner())
    store.register_worker(sample_worker())
    store.enqueue(sample_task())

    first = store.reserve_for_task(
        "task-a", "task-a:run-a:activity-4:reserve", now=110.0,
        lease_seconds=120, max_observation_age_seconds=300,
    )
    retry = store.reserve_for_task(
        "task-a", "task-a:run-a:activity-4:reserve", now=111.0,
        lease_seconds=120, max_observation_age_seconds=300,
    )

    assert first is not None
    assert retry == first
    assert first.lease.task_id == "task-a"
    assert first.lease.worker_id == "worker-a"
    assert first.provider_id == "fake"
    assert first.model_id == "model-a"
    assert store.get_task_state("task-a") == TaskState.LEASED.value
    assert store._connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 1


def test_temporal_route_reservation_assigns_next_task_to_other_capacity(store):
    store.register_owner_policy(sample_owner())
    store.register_worker(sample_worker())
    store.register_owner_policy(sample_owner(owner_id="owner-b"))
    store.register_worker(sample_worker(
        worker_id="worker-b", owner_id="owner-b", provider_id="another",
        model_ids=("model-b",),
    ))
    store.enqueue(sample_task())
    store.enqueue(sample_task(
        task_id="task-b", model_id="model-b", branch="agent/task-b",
        created_at=101.0,
    ))

    first = store.reserve_for_task(
        "task-a", "key-a", now=110.0, lease_seconds=120,
        max_observation_age_seconds=300,
    )
    second = store.reserve_for_task(
        "task-b", "key-b", now=110.0, lease_seconds=120,
        max_observation_age_seconds=300,
    )

    assert first.lease.worker_id == "worker-a"
    assert second.lease.worker_id == "worker-b"
    assert second.model_id == "model-b"


def test_temporal_route_reservation_fails_closed_for_ineligible_workers(store):
    store.register_owner_policy(sample_owner(policy_approved=False))
    store.register_worker(sample_worker())
    store.enqueue(sample_task())

    decision = store.reserve_for_task(
        "task-a", "key-a", now=110.0, lease_seconds=120,
        max_observation_age_seconds=300,
    )

    assert decision is None
    assert store.get_task_state("task-a") == TaskState.QUEUED.value
    assert store._connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0


def test_temporal_route_reservation_is_readable_after_store_restart(tmp_path):
    path = tmp_path / "scheduler.sqlite3"
    first = SQLiteSchedulerStore(path)
    first.register_owner_policy(sample_owner())
    first.register_worker(sample_worker())
    first.enqueue(sample_task())
    expected = first.reserve_for_task(
        "task-a", "durable-route-key", now=110.0, lease_seconds=120,
        max_observation_age_seconds=300,
    )
    first.close()

    reopened = SQLiteSchedulerStore(path)
    try:
        recovered = reopened.get_route_reservation("durable-route-key", now=111.0)
        assert recovered == expected
        assert recovered.reserved_gpu_seconds == 60
    finally:
        reopened.close()


def test_temporal_route_key_cannot_be_reused_for_another_task(store):
    store.register_owner_policy(sample_owner())
    store.register_worker(sample_worker())
    store.enqueue(sample_task())
    store.enqueue(sample_task(task_id="task-b", branch="agent/task-b", created_at=101.0))
    store.reserve_for_task(
        "task-a", "unique-route-key", now=110.0, lease_seconds=120,
        max_observation_age_seconds=300,
    )

    with pytest.raises(LeaseError, match="different task"):
        store.reserve_for_task(
            "task-b", "unique-route-key", now=111.0, lease_seconds=120,
            max_observation_age_seconds=300,
        )
    assert store.get_task_state("task-b") == TaskState.QUEUED.value


def test_store_migrates_older_lease_schema_without_dropping_rows(tmp_path):
    import sqlite3

    db = tmp_path / "older.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute(
        """CREATE TABLE leases (
            lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL,
            reserved_gpu_seconds INTEGER NOT NULL, actual_gpu_seconds INTEGER,
            created_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("lease-old", "task-old", "worker-old", "owner-old", 10, "DONE", 5, 4, 1),
    )
    connection.commit()
    connection.close()

    migrated = SQLiteSchedulerStore(db)
    try:
        columns = {row["name"] for row in migrated._connection.execute("PRAGMA table_info(leases)")}
        row = migrated._connection.execute(
            "SELECT lease_id, actual_gpu_seconds FROM leases WHERE lease_id = ?", ("lease-old",)
        ).fetchone()
        assert {"result_json", "error", "retryable", "idempotency_key", "model_id"} <= columns
        assert row["lease_id"] == "lease-old"
        assert row["actual_gpu_seconds"] == 4
    finally:
        migrated.close()
