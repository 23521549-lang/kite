"""Regression tests for the Phase 1 fixes (E2, E3, E4, E6) and the task-type dispatcher."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from poc.kaggle_worker import cli
from poc.kaggle_worker.config import model_policy_from_config, sync_config_to_store, validate_config
from poc.kaggle_worker.protocol import OwnerPolicy, ScheduledTask, WorkerProfile, WorkerResult
from poc.kaggle_worker.routing import ModelPolicy, RoutingRequest, WorkerLoad, select_worker
from poc.kaggle_worker.store import SQLiteSchedulerStore

NOW = 1000.0
HEAD = "a" * 40


def owner(owner_id: str = "han", quota: int = 3600) -> OwnerPolicy:
    return OwnerPolicy(owner_id, True, True, 4, quota, NOW)


def worker(worker_id: str, *, models=("qwen",), caps=(), loaded=None, cached=(), owner_id="han",
           concurrency=1) -> WorkerProfile:
    return WorkerProfile(worker_id, owner_id, "kaggle", ("coder",), tuple(models), True, True, NOW,
                         concurrency, capabilities=tuple(caps), loaded_model=loaded,
                         cached_models=tuple(cached))


def task(task_id: str, *, task_type="coding", caps=(), estimate=900) -> ScheduledTask:
    return ScheduledTask(task_id, "coder", None, HEAD, f"agent/{task_id}", json.dumps({"goal": "g"}),
                         estimate, 0, NOW, task_type=task_type, required_capabilities=tuple(caps))


def idle() -> WorkerLoad:
    return WorkerLoad(0, 0, 0)


# E2 -------------------------------------------------------------------------------------------
def test_completed_task_charges_at_least_the_lease_wall_time(tmp_path):
    with SQLiteSchedulerStore(tmp_path / "s.db") as store:
        store.register_owner_policy(owner(quota=3600))
        store.register_worker(worker("w1"))
        store.enqueue(task("t1"))
        route = store.reserve_for_task("t1", "k1", NOW, 3600, 10**6)
        result = WorkerResult("t1", route.lease.lease_id, "w1", HEAD, "diff", "ok", usage_gpu_seconds=0)
        store.complete(route.lease.lease_id, result, NOW + 900)
        assert store.get_owner_policy("han").quota_remaining_seconds == 2700


def test_reported_usage_wins_when_larger_than_wall_time(tmp_path):
    with SQLiteSchedulerStore(tmp_path / "s.db") as store:
        store.register_owner_policy(owner(quota=3600))
        store.register_worker(worker("w1"))
        store.enqueue(task("t1"))
        route = store.reserve_for_task("t1", "k1", NOW, 3600, 10**6)
        result = WorkerResult("t1", route.lease.lease_id, "w1", HEAD, "diff", "ok", usage_gpu_seconds=1200)
        store.complete(route.lease.lease_id, result, NOW + 60)
        assert store.get_owner_policy("han").quota_remaining_seconds == 2400


# E3 -------------------------------------------------------------------------------------------
def test_required_capabilities_filter_workers():
    workers = [worker("plain", caps=("coding",)), worker("vision", caps=("coding", "vision"))]
    owners = {"han": owner()}
    loads = {"plain": idle(), "vision": idle()}
    decision = select_worker(RoutingRequest("coder", None, 60, required_capabilities=("vision",)),
                             workers, owners, loads, now=NOW)
    assert decision.worker_id == "vision"
    assert select_worker(RoutingRequest("coder", None, 60, required_capabilities=("audio",)),
                         workers, owners, loads, now=NOW) is None


def test_store_persists_capabilities_and_routes_on_them(tmp_path):
    with SQLiteSchedulerStore(tmp_path / "s.db") as store:
        store.register_owner_policy(owner())
        store.register_worker(worker("plain", caps=("coding",)))
        store.register_worker(worker("vision", caps=("coding", "vision")))
        store.enqueue(task("ui", task_type="vision", caps=("vision",)))
        assert store.get_task("ui")["required_capabilities"] == ["vision"]
        route = store.reserve_for_task("ui", "k-ui", NOW, 600, 10**6)
        assert route.lease.worker_id == "vision"


# Dispatcher: quality minus model-switch cost --------------------------------------------------
POLICY = ModelPolicy(
    skill={("analysis", "qwen"): 0.60, ("analysis", "devstral"): 0.70,
           ("coding", "qwen"): 0.75, ("coding", "devstral"): 0.65},
    swap_penalty_per_second=0.0005, load_seconds_cached=60, load_seconds_cold=600,
)


def test_picks_best_model_for_task_type_when_switch_is_cheap():
    # devstral is cached on w1: 0.70 - 0.0005*60 = 0.67 beats qwen already loaded at 0.60.
    w1 = worker("w1", models=("qwen", "devstral"), loaded="qwen", cached=("devstral",))
    d = select_worker(RoutingRequest("coder", None, 60, task_type="analysis"), [w1], {"han": owner()},
                      {"w1": idle()}, now=NOW, model_policy=POLICY)
    assert (d.worker_id, d.model_id, d.requires_model_load) == ("w1", "devstral", True)


def test_keeps_loaded_model_when_switch_is_too_expensive():
    # devstral would need a cold download: 0.70 - 0.0005*600 = 0.40 < 0.60 for the loaded qwen.
    w1 = worker("w1", models=("qwen", "devstral"), loaded="qwen")
    d = select_worker(RoutingRequest("coder", None, 60, task_type="analysis"), [w1], {"han": owner()},
                      {"w1": idle()}, now=NOW, model_policy=POLICY)
    assert (d.model_id, d.requires_model_load) == ("qwen", False)


def test_prefers_idle_worker_that_already_has_the_right_model():
    w1 = worker("w1", models=("qwen", "devstral"), loaded="qwen")
    w2 = worker("w2", models=("qwen", "devstral"), loaded="devstral")
    d = select_worker(RoutingRequest("coder", None, 60, task_type="analysis"), [w1, w2],
                      {"han": owner()}, {"w1": idle(), "w2": idle()}, now=NOW, model_policy=POLICY)
    assert (d.worker_id, d.model_id) == ("w2", "devstral")
    assert "measured" in d.reason


def test_busy_worker_is_skipped_even_if_its_model_is_best():
    w1 = worker("w1", models=("devstral",), loaded="devstral")
    w2 = worker("w2", models=("qwen",), loaded="qwen")
    loads = {"w1": WorkerLoad(1, 1, 0), "w2": idle()}
    d = select_worker(RoutingRequest("coder", None, 60, task_type="analysis"), [w1, w2],
                      {"han": owner()}, loads, now=NOW, model_policy=POLICY)
    assert d.worker_id == "w2"


def test_pinned_model_overrides_scoring():
    w1 = worker("w1", models=("qwen", "devstral"), loaded="qwen")
    d = select_worker(RoutingRequest("coder", "devstral", 60, task_type="coding"), [w1],
                      {"han": owner()}, {"w1": idle()}, now=NOW, model_policy=POLICY)
    assert d.model_id == "devstral"


def test_store_uses_model_policy_and_heartbeat_state(tmp_path):
    with SQLiteSchedulerStore(tmp_path / "s.db", model_policy=POLICY) as store:
        store.register_owner_policy(owner())
        store.register_worker(worker("w1", models=("qwen", "devstral")))
        store.update_worker_status("w1", provider_ready=True, observed_at=NOW,
                                   loaded_model="qwen", cached_models=("devstral",))
        assert store.get_worker("w1").loaded_model == "qwen"
        store.enqueue(task("a1", task_type="analysis"))
        route = store.reserve_for_task("a1", "k-a1", NOW, 600, 10**6)
        assert route.model_id == "devstral"


# Config -> store sync ---------------------------------------------------------------------------
def config(quota=7200):
    return validate_config({
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "q"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": [{
            "id": "opencode-local", "adapter": "opencode", "enabled": True, "model": "qwen",
            "models": ["devstral"], "owner": "han", "capabilities": ["coding", "vision"],
            "quota": {"gpu_seconds": quota, "concurrency": 1},
        }],
        "routing": {"swap_penalty_per_second": 0.0005,
                    "model_skill": {"analysis": {"qwen": 0.6, "devstral": 0.7}}},
    })


def test_sync_registers_workers_and_preserves_quota_and_heartbeat(tmp_path):
    cfg = config()
    with SQLiteSchedulerStore(tmp_path / "s.db") as store:
        first = sync_config_to_store(cfg, store, now=NOW)
        assert first[0]["new"] is True
        w = store.get_worker("opencode-local")
        assert w.model_ids == ("qwen", "devstral") and w.capabilities == ("coding", "vision")
        assert w.provider_ready is False
        store.update_worker_status("opencode-local", provider_ready=True, observed_at=NOW + 5,
                                   loaded_model="qwen")
        store.register_owner_policy(OwnerPolicy("han", True, True, 1, 100, NOW))  # quota consumed
        sync_config_to_store(cfg, store, now=NOW + 10)
        assert store.get_owner_policy("han").quota_remaining_seconds == 100
        again = store.get_worker("opencode-local")
        assert again.provider_ready is True and again.loaded_model == "qwen"
    policy = model_policy_from_config(cfg)
    assert policy.quality("analysis", "devstral") == (0.7, True)


# Schema migration: databases created before this change keep working ---------------------------
def test_pre_dispatcher_database_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE owners (owner_id TEXT PRIMARY KEY, owner_authorized INTEGER NOT NULL,
            policy_approved INTEGER NOT NULL, max_concurrent_sessions INTEGER NOT NULL,
            quota_remaining_seconds INTEGER, quota_observed_at REAL NOT NULL);
        CREATE TABLE workers (worker_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(owner_id),
            provider_id TEXT NOT NULL, roles_json TEXT NOT NULL, model_ids_json TEXT NOT NULL,
            enabled INTEGER NOT NULL, provider_ready INTEGER NOT NULL, status_observed_at REAL NOT NULL,
            max_concurrency INTEGER NOT NULL);
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, role TEXT NOT NULL, model_id TEXT,
            repo_head TEXT NOT NULL, branch TEXT NOT NULL, payload_json TEXT NOT NULL,
            estimated_gpu_seconds INTEGER NOT NULL, priority INTEGER NOT NULL, created_at REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'QUEUED', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
        INSERT INTO owners VALUES ('han', 1, 1, 1, 3600, 1000.0);
        INSERT INTO workers VALUES ('w1', 'han', 'kaggle', '["coder"]', '["qwen"]', 1, 1, 1000.0, 1);
        INSERT INTO tasks (task_id, role, model_id, repo_head, branch, payload_json,
            estimated_gpu_seconds, priority, created_at)
            VALUES ('old', 'coder', NULL, 'h', 'agent/old', '{}', 60, 0, 1000.0);
    """)
    con.close()
    with SQLiteSchedulerStore(db) as store:
        assert store.get_worker("w1").capabilities == ()
        assert store.get_task("old")["task_type"] == "coding"
        assert store.reserve_for_task("old", "k-old", NOW, 600, 10**6).lease.worker_id == "w1"


# E4 -------------------------------------------------------------------------------------------
def test_workflow_start_enqueues_task_before_starting(tmp_path, capsys, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "q"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": [],
    }))
    started = {}

    class FakeHandle:
        id = "t-start"
        result_run_id = "run-1"

    class FakeClient:
        async def start_workflow(self, _run, task_input, *, id, task_queue):
            started.update(task_input=task_input, id=id, task_queue=task_queue)
            return FakeHandle()

    async def fake_connect(_endpoint):
        return FakeClient()

    import temporalio.client
    monkeypatch.setattr(temporalio.client.Client, "connect", staticmethod(fake_connect))
    code = cli.main(["--config", str(cfg_path), "workflow", "start", "--task-id", "t-start",
                     "--goal", "Analyse the parser", "--repo", str(tmp_path), "--repo-head", HEAD,
                     "--branch", "agent/t-start", "--task-type", "analysis", "--capability", "coding"])
    assert code == 0, capsys.readouterr().err
    assert started["task_input"].task_type == "analysis"
    with SQLiteSchedulerStore(tmp_path / "scheduler.sqlite3") as store:
        queued = store.get_task("t-start")
    assert queued["state"] == "QUEUED"
    assert queued["task_type"] == "analysis" and queued["required_capabilities"] == ["coding"]


# E6 -------------------------------------------------------------------------------------------
def test_waiting_for_a_worker_cannot_exhaust_workflow_history():
    from poc.kaggle_worker import temporal_workflow as tw

    source = Path(tw.__file__).read_text(encoding="utf-8")
    assert "workflow.continue_as_new(" in source
    assert "await workflow.sleep(timedelta(seconds=15))" not in source
    # Each recheck adds at most ~11 events (activity + timer + workflow tasks): one run stays
    # under Temporal's 10,240-event warning and far under the 51,200-event hard limit.
    assert tw.MAX_ROUTE_CHECKS_PER_RUN * 11 < 10_240


def test_worker_heartbeat_keeps_the_owner_quota_fresh_for_routing(tmp_path):
    with SQLiteSchedulerStore(tmp_path / "s.db") as store:
        store.register_owner_policy(OwnerPolicy("han", True, True, 4, 3600, NOW - 10_000))
        store.register_worker(worker("w1"))
        store.enqueue(task("t1"))
        assert store.reserve_for_task("t1", "k0", NOW, 600, 300) is None   # quota seen 10 000 s ago
        store.update_worker_status("w1", provider_ready=True, observed_at=NOW)
        assert store.get_owner_policy("han").quota_observed_at == NOW
        assert store.get_owner_policy("han").quota_remaining_seconds == 3600   # the ledger is untouched
        assert store.reserve_for_task("t1", "k1", NOW, 600, 300).lease.worker_id == "w1"


def test_chat_only_model_is_never_routed_even_when_loaded_or_pinned():
    from dataclasses import replace
    policy = replace(POLICY, skill={**POLICY.skill, ("analysis", "r1"): 0.99}, excluded=frozenset({"r1"}))
    w1 = worker("w1", models=("r1", "qwen"), loaded="r1")
    d = select_worker(RoutingRequest("coder", None, 60, task_type="analysis"), [w1], {"han": owner()},
                      {"w1": idle()}, now=NOW, model_policy=policy)
    assert d.model_id == "qwen"
    assert select_worker(RoutingRequest("coder", "r1", 60, task_type="analysis"), [w1], {"han": owner()},
                         {"w1": idle()}, now=NOW, model_policy=policy) is None
