from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from poc.kaggle_worker.protocol import (
    CheckpointRef, ModelRequest, ModelResponse, OwnerPolicy, ScheduledTask,
    TaskLease, TaskState, WorkerProfile, WorkerResult, WorkerStatus,
)


def worker(**changes):
    fields = dict(worker_id="w1", owner_id="o1", provider_id="fake", roles=("coder",),
                  model_ids=("m1",), enabled=True, provider_ready=True,
                  status_observed_at=10.0, max_concurrency=1)
    fields.update(changes)
    return WorkerProfile(**fields)


def owner(**changes):
    fields = dict(owner_id="o1", owner_authorized=True, policy_approved=True,
                  max_concurrent_sessions=1, quota_remaining_seconds=100,
                  quota_observed_at=10.0)
    fields.update(changes)
    return OwnerPolicy(**fields)


def task(**changes):
    fields = dict(task_id="t1", role="coder", model_id="m1", repo_head="head",
                  branch="agent/t1", payload_json='{"goal":"change"}',
                  estimated_gpu_seconds=20, priority=1, created_at=10.0)
    fields.update(changes)
    return ScheduledTask(**fields)


def test_worker_profile_rejects_empty_identity_capabilities_and_limits():
    for changes, message in [({"worker_id": " "}, "worker_id"),
                             ({"roles": ()}, "roles"),
                             ({"model_ids": ("",)}, "model_ids"),
                             ({"max_concurrency": 0}, "max_concurrency")]:
        with pytest.raises(ValueError, match=message):
            worker(**changes)


def test_worker_profile_rejects_nonfinite_observation():
    with pytest.raises(ValueError, match="status_observed_at"):
        worker(status_observed_at=float("nan"))


def test_owner_policy_rejects_negative_quota_or_zero_session_cap():
    with pytest.raises(ValueError, match="quota_remaining_seconds"):
        owner(quota_remaining_seconds=-1)
    with pytest.raises(ValueError, match="max_concurrent_sessions"):
        owner(max_concurrent_sessions=0)
    assert owner(quota_remaining_seconds=None).quota_remaining_seconds is None


def test_scheduled_task_requires_object_json_and_positive_gpu_estimate():
    for payload in ("broken", "[]"):
        with pytest.raises(ValueError, match="payload_json"):
            task(payload_json=payload)
    with pytest.raises(ValueError, match="estimated_gpu_seconds"):
        task(estimated_gpu_seconds=0)


def test_lease_and_worker_result_validate_identity_and_usage():
    with pytest.raises(ValueError, match="lease_id"):
        TaskLease("", "t1", "w1", "o1", 20)
    with pytest.raises(ValueError, match="patch"):
        WorkerResult("t1", "l1", "w1", "head", " ", "summary", 1)
    with pytest.raises(ValueError, match="usage_gpu_seconds"):
        WorkerResult("t1", "l1", "w1", "head", "diff", "summary", -1)


def test_existing_provider_types_remain_compatible_and_task_states_stable():
    assert ModelRequest(prompt="hello").prompt == "hello"
    assert ModelResponse(text="world").text == "world"
    assert CheckpointRef(task_id="t1", path=Path("state.json")).task_id == "t1"
    assert WorkerStatus.READY.value == "READY"
    assert TaskState.QUEUED.value == "QUEUED"
    assert TaskState.CANCELLED.value == "CANCELLED"
