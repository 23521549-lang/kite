from poc.kaggle_worker.protocol import OwnerPolicy, WorkerProfile
from poc.kaggle_worker.routing import RoutingRequest, WorkerLoad, select_worker


def worker(**changes):
    values = {
        "worker_id": "worker-a",
        "owner_id": "owner-a",
        "provider_id": "fake",
        "roles": ("coder", "reviewer"),
        "model_ids": ("model-a", "model-b"),
        "enabled": True,
        "provider_ready": True,
        "status_observed_at": 100.0,
        "max_concurrency": 2,
    }
    values.update(changes)
    return WorkerProfile(**values)


def owner(**changes):
    values = {
        "owner_id": "owner-a",
        "owner_authorized": True,
        "policy_approved": True,
        "max_concurrent_sessions": 3,
        "quota_remaining_seconds": 600,
        "quota_observed_at": 100.0,
    }
    values.update(changes)
    return OwnerPolicy(**values)


def load(**changes):
    values = {
        "active_sessions": 0,
        "owner_active_sessions": 0,
        "owner_reserved_gpu_seconds": 0,
    }
    values.update(changes)
    return WorkerLoad(**values)


def test_selects_requested_model_from_an_eligible_worker():
    decision = select_worker(
        RoutingRequest(role="coder", model_id="model-b", estimated_gpu_seconds=90),
        [worker(model_ids=("model-a", "model-b"))],
        {"owner-a": owner()},
        {"worker-a": load()},
        now=110.0,
    )

    assert decision is not None
    assert (decision.provider_id, decision.worker_id, decision.model_id) == (
        "fake", "worker-a", "model-b"
    )
    assert "model-b" in decision.reason


def test_selects_least_loaded_compatible_worker_and_its_preferred_model():
    workers = [
        worker(worker_id="worker-b", owner_id="owner-b", model_ids=("model-b",)),
        worker(worker_id="worker-a", owner_id="owner-a", model_ids=("model-a", "model-b")),
    ]
    decision = select_worker(
        RoutingRequest(role="coder", model_id=None, estimated_gpu_seconds=90),
        workers,
        {"owner-a": owner(), "owner-b": owner(owner_id="owner-b")},
        {"worker-a": load(active_sessions=1), "worker-b": load()},
        now=110.0,
    )

    assert decision is not None
    assert (decision.worker_id, decision.model_id) == ("worker-b", "model-b")


def test_rejects_candidates_with_stale_policy_or_insufficient_capacity():
    request = RoutingRequest(role="coder", model_id="model-a", estimated_gpu_seconds=90)
    cases = [
        (worker(enabled=False), owner(), load(), 110.0),
        (worker(provider_ready=False), owner(), load(), 110.0),
        (worker(status_observed_at=0), owner(quota_observed_at=1000), load(), 1000.0),
        (worker(roles=("reviewer",)), owner(), load(), 110.0),
        (worker(model_ids=("model-b",)), owner(), load(), 110.0),
        (worker(), owner(owner_authorized=False), load(), 110.0),
        (worker(), owner(policy_approved=False), load(), 110.0),
        (worker(), owner(quota_remaining_seconds=None), load(), 110.0),
        (worker(status_observed_at=1000), owner(quota_observed_at=0), load(), 1000.0),
        (worker(), owner(quota_remaining_seconds=100), load(owner_reserved_gpu_seconds=20), 110.0),
        (worker(), owner(), load(active_sessions=2), 110.0),
        (worker(), owner(max_concurrent_sessions=1), load(owner_active_sessions=1), 110.0),
    ]

    for candidate, policy, usage, now in cases:
        assert select_worker(
            request,
            [candidate],
            {policy.owner_id: policy},
            {candidate.worker_id: usage},
            now=now,
        ) is None


def test_tie_breaks_by_worker_id_for_equal_load():
    workers = [worker(worker_id="worker-b"), worker(worker_id="worker-a")]
    policies = {"owner-a": owner()}
    loads = {"worker-a": load(), "worker-b": load()}

    decision = select_worker(
        RoutingRequest(role="coder", model_id="model-a", estimated_gpu_seconds=90),
        workers,
        policies,
        loads,
        now=110.0,
    )

    assert decision is not None
    assert decision.worker_id == "worker-a"
