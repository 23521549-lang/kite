# Multi-Owner Worker Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a provider-neutral AWS-side scheduler that routes isolated coding tasks to explicitly authorized workers, tracks per-owner concurrency/quota, and returns patches to a review gate.

**Architecture:** Keep the existing `WorkerProvider` lifecycle interface and durable checkpoint model. Add a persistent SQLite queue, owner-scoped worker registry, leases, quota reservations, and a patch review bundle. This plan does not implement a Kaggle API adapter or connect real collaborator accounts; those remain disabled until Kaggle policy and a supported AWS-to-Kaggle multi-turn path are verified.

**Tech Stack:** Python 3.12+, standard-library `sqlite3`, `dataclasses`, `threading`, `pytest`; no paid service and no new runtime dependency.

**Spec:** `docs/2026-11-16-qwen-kaggle-coding-agent-design.md`

## Global Constraints

- MVP remains one user, one Kaggle account, and one GitHub repository.
- Never rotate among accounts controlled by one person or share another person's credentials.
- A collaborator-owned worker remains disabled unless Kaggle confirms the proposed shared-project use is allowed.
- Do not implement an unattended Kaggle adapter until its supported multi-turn request/response/cancel path is verified.
- AWS EBS-backed repo and checkpoint are the canonical project state.
- A task carries a pinned Git HEAD and isolated branch/worktree; workers do not concurrently edit one checkout.
- Worker output is a patch/review bundle; the scheduler never pushes or merges.
- Do not start a GPU session when there is no eligible queued task.
- Missing owner consent, policy approval, quota data, or provider health must fail closed for GPU dispatch.
- All queue, registry, lease, and quota tests run locally against temporary SQLite files and fake providers only.

---

## Scope and deliverables

This plan builds a safe provider-neutral scheduler prototype and its local tests. It does not change the Kaggle feasibility decision from `CONDITIONAL`, does not create accounts, does not consume Kaggle quota, and does not make notebooks or AWS call an undocumented Kaggle endpoint. If Kaggle does not approve collaborator compute use or no supported worker connection path is found, the provider-neutral scheduler still supports one authorized worker and queued sequential tasks.

## File map

- `poc/kaggle_worker/protocol.py` — shared immutable task, worker-profile, lease, and result types; retain the existing `WorkerProvider` interface.
- `poc/kaggle_worker/store.py` — SQLite schema and transaction-safe persistence for worker profiles, queued tasks, leases, and quota reservations.
- `poc/kaggle_worker/scheduler.py` — eligibility checks, worker selection, lease transitions, retry policy, and quota reservation/settlement.
- `poc/kaggle_worker/integration.py` — validate returned patch metadata and build a review bundle; do not mutate the canonical branch or push.
- `tests/poc/kaggle_worker/test_store.py` — persistence, transaction, and restart tests using a temporary SQLite database.
- `tests/poc/kaggle_worker/test_scheduler.py` — routing, consent/policy/quota/concurrency gates, lease, retry, and idempotency tests.
- `tests/poc/kaggle_worker/test_integration.py` — stale HEAD, malformed result, and review-only behavior tests.
- `poc/kaggle_worker/README.md` — local fake-worker usage and explicit real-provider gates.

## Interfaces

The first implementation task defines these immutable types in `protocol.py`:

```python
class TaskState(str, Enum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

@dataclass(frozen=True)
class WorkerProfile:
    worker_id: str
    owner_id: str
    provider_id: str
    roles: tuple[str, ...]
    model_ids: tuple[str, ...]
    enabled: bool
    provider_ready: bool
    status_observed_at: float
    max_concurrency: int

@dataclass(frozen=True)
class OwnerPolicy:
    owner_id: str
    owner_authorized: bool
    policy_approved: bool
    max_concurrent_sessions: int
    quota_remaining_seconds: int | None
    quota_observed_at: float

@dataclass(frozen=True)
class ScheduledTask:
    task_id: str
    role: str
    model_id: str | None
    repo_head: str
    branch: str
    payload_json: str
    estimated_gpu_seconds: int
    priority: int
    created_at: float

@dataclass(frozen=True)
class TaskLease:
    lease_id: str
    task_id: str
    worker_id: str
    owner_id: str
    expires_at: float

@dataclass(frozen=True)
class WorkerResult:
    task_id: str
    lease_id: str
    worker_id: str
    base_repo_head: str
    patch: str
    summary: str
    usage_gpu_seconds: int
```

`TaskState` is the only scheduler state enum. `StoreError` is raised for invalid persistence operations; `LeaseError` is raised for missing, expired, or mismatched lease identities. `StaleWorkerResult` is raised by patch-bundle validation. An owner policy can be marked `policy_approved=True` only after the external policy gate is recorded by the project owner; the scheduler must not infer approval from Kaggle notebook sharing settings.

The provider-neutral public API is:

```python
class SchedulerStore(Protocol):
    def register_owner_policy(self, policy: OwnerPolicy) -> None: ...
    def register_worker(self, profile: WorkerProfile) -> None: ...
    def enqueue(self, task: ScheduledTask) -> None: ...
    def get_worker(self, worker_id: str) -> WorkerProfile | None: ...
    def get_owner_policy(self, owner_id: str) -> OwnerPolicy | None: ...
    def claim_next(
        self, worker_id: str, now: float, lease_seconds: int,
        max_observation_age_seconds: int,
    ) -> TaskLease | None: ...
    def heartbeat(self, lease_id: str, now: float, lease_seconds: int) -> TaskLease: ...
    def complete(self, lease_id: str, result: WorkerResult, now: float) -> None: ...
    def fail(self, lease_id: str, error: str, retryable: bool, now: float) -> None: ...
    def cancel(self, task_id: str, now: float) -> None: ...
    def get_task_state(self, task_id: str) -> str: ...

class WorkerScheduler:
    def __init__(self, store: SchedulerStore, *, max_observation_age_seconds: int = 300) -> None: ...
    def dispatch_next(self, worker_id: str, now: float, lease_seconds: int) -> TaskLease | None: ...
    def heartbeat(self, lease_id: str, now: float, lease_seconds: int) -> TaskLease: ...
    def complete(self, lease_id: str, result: WorkerResult, now: float) -> None: ...
    def fail(self, lease_id: str, error: str, retryable: bool, now: float) -> None: ...
    def cancel(self, task_id: str, now: float) -> None: ...
```

`dispatch_next` delegates to the store's atomic `claim_next`. The store may claim only a queued task whose role/model match the worker, where the worker is enabled, its provider readiness observation is fresh, the owner is authorized and policy-approved, the worker and owner are below their separate concurrency caps, and the owner's quota observation is fresh with unreserved remaining quota at least `estimated_gpu_seconds`. It reserves the estimate in the same SQLite transaction that creates the lease. `complete` settles the reservation against measured usage exactly once. Expired leases become retryable queue entries after their reservation is released. Repeated completion/failure/cancel calls are idempotent for the same task/lease and never create a second patch acceptance.

---

## Tasks

### Task 1: Add owner-scoped scheduling types and fail-closed eligibility tests

**Files:**
- Modify: `poc/kaggle_worker/protocol.py`
- Create: `tests/poc/kaggle_worker/test_scheduler.py`

**Produces:** `WorkerProfile`, `OwnerPolicy`, `ScheduledTask`, `TaskLease`, `WorkerResult`, and scheduler-state enums. Preserve `WorkerProvider`, `ModelRequest`, `ModelResponse`, and `CheckpointRef` compatibility.

- [x] **Step 1: Write tests for profile and task validation.** Cover empty IDs, zero concurrency caps, negative quota, invalid GPU estimate, and malformed/non-object JSON payload. Assert invalid data raises `ValueError`.
- [x] **Step 2: Run the new focused tests and confirm import/behavior failures.** Run `python -m pytest tests/poc/kaggle_worker/test_scheduler.py -q`; the new type imports or validation assertions must fail before implementation.
- [x] **Step 3: Add frozen dataclasses and minimal `__post_init__` validation.** Ensure IDs and roles are non-empty, concurrency is positive, quota is `None` or non-negative, and task payload parses as a JSON object.
- [x] **Step 4: Run protocol and existing worker tests.** Run `python -m pytest tests/poc/kaggle_worker/test_scheduler.py tests/poc/kaggle_worker/test_controller.py tests/poc/kaggle_worker/test_checkpoint.py -q`; all focused tests must pass.

### Task 2: Persist workers, tasks, leases, and quota reservations in SQLite

**Files:**
- Create: `poc/kaggle_worker/store.py`
- Create: `tests/poc/kaggle_worker/test_store.py`

**Produces:** `SQLiteSchedulerStore(path: Path)` implementing `SchedulerStore`, with `register_owner_policy(policy: OwnerPolicy)`, `get_owner_policy(owner_id)`, idempotent schema initialization, and explicit close/context-manager support. Registration upserts observations but rejects changing a worker ID's owner. Store exactly one quota/policy row per owner so multiple workers for one owner cannot multiply the recorded budget.

- [x] **Step 1: Write failing tests for schema creation and round-trip.** Register an owner policy and one worker profile, enqueue one task, close the store, reopen it, and assert the records and queued state survive.
- [x] **Step 2: Write failing tests for duplicate IDs and invalid transitions.** Enqueueing the same task ID with different task content must raise `StoreError`; unknown lease operations must raise `LeaseError`.
- [x] **Step 3: Write failing tests for transaction safety.** Force a quota reservation conflict and verify neither a partial lease nor a partially claimed task remains in SQLite.
- [x] **Step 4: Implement schema and store methods using parameterized SQL.** Use `BEGIN IMMEDIATE` for claim/reservation/settlement, foreign keys for task/lease relationships, UTC-independent numeric timestamps, and JSON serialization for tuple fields. Implement `claim_next` as one transaction that rechecks task, worker, owner, active-lease, timestamp-age, and quota-reservation conditions before changing state.
- [x] **Step 5: Run store tests against `tmp_path` databases.** Run `python -m pytest tests/poc/kaggle_worker/test_store.py -q`; no test may access the network or a real Kaggle account.

### Task 3: Implement routing, owner limits, leases, retry, and quota accounting

**Files:**
- Create: `poc/kaggle_worker/scheduler.py`
- Modify: `poc/kaggle_worker/store.py`
- Modify: `tests/poc/kaggle_worker/test_scheduler.py`

**Consumes:** `WorkerProfile`, `ScheduledTask`, `TaskLease`, and `SchedulerStore` from Tasks 1–2.

**Produces:** `WorkerScheduler` with the API defined in the Interfaces section.

- [x] **Step 1: Add failing eligibility tests.** Verify dispatch returns `None` for disabled, unauthorized, policy-unapproved, stale provider status, provider-not-ready, role-mismatch, model-mismatch, unknown quota, stale quota, exhausted quota, worker-cap, and owner-cap cases.
- [x] **Step 2: Add failing deterministic selection tests.** Enqueue high- and low-priority tasks; select the highest priority eligible task, then FIFO by `created_at` and `task_id` for ties.
- [x] **Step 3: Add failing lease lifecycle tests.** Verify claim reserves estimated GPU seconds, heartbeat extends expiry, complete settles actual usage once, non-retryable failure ends the task, retryable failure requeues it, and expired leases recover the task exactly once.
- [x] **Step 4: Implement the scheduler with a single transaction per state change.** Keep provider calls outside SQLite transactions. `dispatch_next` calls `store.claim_next(worker_id, now, lease_seconds, max_observation_age_seconds)`; the store rechecks all eligibility constraints atomically. Return no lease when either owner quota or worker status observation is missing or older than the configured maximum age.
- [x] **Step 5: Run scheduler and store tests.** Run `python -m pytest tests/poc/kaggle_worker/test_scheduler.py tests/poc/kaggle_worker/test_store.py -q`; all cases must use fake clocks and temporary storage.

### Task 4: Make patch integration review-only and reject stale work

**Files:**
- Create: `poc/kaggle_worker/integration.py`
- Create: `tests/poc/kaggle_worker/test_integration.py`

**Produces:** `ReviewBundle` and `build_review_bundle(result: WorkerResult, *, expected_repo_head: str, expected_task_id: str, expected_lease_id: str, expected_worker_id: str) -> ReviewBundle`.

- [x] **Step 1: Write failing tests for valid and stale results.** A valid result produces a bundle with task/worker identity, base HEAD, patch, summary, and measured usage; a mismatched HEAD raises `StaleWorkerResult`.
- [x] **Step 2: Write failing tests for malformed patch metadata.** Reject empty task/worker/lease IDs, negative usage, empty patch, and results whose task, lease, worker, or base HEAD differs from the expected active lease and repository HEAD.
- [x] **Step 3: Write a test proving review-only behavior.** Building a bundle must not change files, the Git index, branch, remote, or push state.
- [x] **Step 4: Implement the pure validation function.** Do not invoke Git, run commands, apply the patch, or push from this module; AWS integration owns any later apply/test action.
- [x] **Step 5: Run integration tests.** Run `python -m pytest tests/poc/kaggle_worker/test_integration.py -q`; assert the filesystem snapshot is unchanged.

### Task 5: Verify worker replacement and multi-owner queue recovery end-to-end

**Files:**
- Modify: `tests/poc/kaggle_worker/test_scheduler.py`
- Modify: `tests/poc/kaggle_worker/test_checkpoint.py`
- Modify: `poc/kaggle_worker/README.md`

**Produces:** Local-only evidence that a worker owned by one contributor can fail and a separately authorized fake worker can resume the task from the shared AWS checkpoint and pinned Git HEAD.

- [x] **Step 1: Write a failing recovery test.** Worker A claims task T, checkpoint is saved, A fails, worker B claims the requeued task, and the request is reconstructed from checkpoint without any A session ID.
- [x] **Step 2: Write a failing isolation test.** Two simultaneous fake workers must receive different task branches/HEADs; a task already leased cannot be claimed by another worker.
- [x] **Step 3: Implement only missing recovery metadata and store transitions.** Use the existing `save_checkpoint`/`load_checkpoint`; include task ID, branch, HEAD, and last completed step in resume context.
- [x] **Step 4: Document the fake-worker lifecycle and policy gates.** State that real collaborator accounts, real Kaggle sessions, and shared quotas are not enabled or tested.
- [x] **Step 5: Run the full local worker test set.** Run `python -m pytest tests/poc/kaggle_worker -q`; expect all controller, checkpoint, scheduler, store, and integration tests to pass without network access.

### Task 6: Re-evaluate gates before any real Kaggle connector

**Files:**
- Modify: `docs/architecture/kaggle-worker-feasibility.md`
- Modify: `poc/kaggle_worker/README.md`

This task is a hard stop until both policy and technical gates are satisfied.

- [ ] **Step 1: Obtain explicit Kaggle guidance** on whether an independently owned collaborator account may run model inference for a shared private coding project orchestrated by AWS. Record the source and date; do not treat Notebook collaboration features as approval for pooled compute.
- [ ] **Step 2: Verify a supported AWS-to-Kaggle multi-turn path** with two prompts, output retrieval, cancellation, and secure credential ownership. Do not use undocumented endpoints, public tunnels, account rotation, or shared credentials.
- [ ] **Step 3: Measure per-owner quota and concurrency behavior** through official account surfaces or documented CLI; store only the minimum status needed by the scheduler.
- [ ] **Step 4: If both gates pass, create a separate Kaggle-adapter implementation plan** with isolated credential storage, owner opt-in, session lifecycle, model load/unload, redacted audit events, and recovery tests. If either gate fails, leave the real adapter disabled and keep the scheduler fake-provider-only.

## Focused verification commands

```powershell
python -m pytest tests/poc/kaggle_worker/test_scheduler.py tests/poc/kaggle_worker/test_store.py -q
python -m pytest tests/poc/kaggle_worker/test_integration.py -q
python -m pytest tests/poc/kaggle_worker -q
```

No Kaggle GPU test is part of the scheduler plan. Real provider integration is gated by Task 6.

