# Temporal, OpenCode, and Harness² Integration Plan

> **For agentic workers:** Use this plan task-by-task. Steps use checkbox syntax for tracking. Execute inline unless the owner selects a different execution mode.

**Goal:** Turn the existing Kite scheduler prototype into an EC2-ready coding system using Temporal for durable workflows, OpenCode for coding/model execution, Harness² for harness evaluation, and Kite for provider/model selection, quota policy, checkpoints, and Git review.

**Architecture:** Temporal runs a durable `CodingTaskWorkflow` and its Kite activities. Kite keeps its worker registry and eligibility/routing policy, then records the provider/model selection and resource reservation before an OpenCode session starts in an isolated Git worktree. Harness² runs through its supported CLI/executor for bounded harness evaluations. Docker Compose runs Temporal and PostgreSQL on EC2; Kaggle remains disabled until its interactive worker path is verified.

**Tech Stack:** Python 3.12, Temporal Python SDK, PostgreSQL, Docker Compose, OpenCode CLI/server API, Harness² supported CLI, existing SQLite worker registry/checkpoint/worktree modules, pytest, fake providers and fake OpenCode HTTP server.

**Spec:** `docs/superpowers/specs/2026-11-17-provider-extensible-kite-design.md`

## Global Constraints

- Kite, not Temporal, selects the provider, worker, and model using capability, health, quota, concurrency, and authorization policy.
- Temporal owns durable workflow history, activity retries, timers, pause/resume, and recovery; all provider side effects use idempotency keys.
- OpenCode's documented provider/model support is reused; do not implement a second model SDK.
- Harness²'s supported setup/evaluation/reporting commands are reused; harness promotion requires an explicit human decision.
- One valid Kaggle account only; Kaggle provider remains disabled and no Kaggle session is started during implementation or local verification.
- All coding tasks run in isolated Git worktrees pinned to a repository HEAD; changes are returned as review bundles and are never pushed automatically.
- Persistent repository, service, policy, checkpoint, and evaluation data paths must reside on EBS in EC2 deployment.
- The workspace currently has no `.git` directory or remote. Work proceeds in the existing writable workspace; do not create a remote, push, or claim a commit.
- Verification commands in this plan must use fake providers, local Temporal/PostgreSQL services, and temporary repositories/databases unless a later user instruction explicitly enables a live integration.

---

## File map

- `pyproject.toml` — Python package metadata, supported Python version, pinned runtime dependencies, and developer/test extras.
- `deploy/compose.yaml` — version-pinned PostgreSQL and Temporal services with persistent named volumes and health checks.
- `poc/kaggle_worker/protocol.py` — retain provider-neutral worker types; add workflow/task identity fields only when required by the Temporal boundary.
- `poc/kaggle_worker/temporal_workflow.py` — deterministic task workflow, approval/pause signals, retry routing, and workflow state.
- `poc/kaggle_worker/temporal_activities.py` — non-deterministic Kite operations: route selection/reservation, workspace preparation, OpenCode session execution, checkpoint writes, result validation, and release.
- `poc/kaggle_worker/temporal_worker.py` — Temporal client/worker startup and activity dependency construction.
- `poc/kaggle_worker/routing.py` — pure task-requirement-to-worker/model matching and stable route explanation, reusing current worker profile/policy fields.
- `poc/kaggle_worker/store.py` — retain provider profiles, owner policies, quota observations/reservations, and resource leases; task queue retry state is owned by Temporal workflows.
- `poc/kaggle_worker/opencode.py` — task-scoped OpenCode server/session client with selected provider/model configuration, event streaming, abort, and session identity persistence.
- `poc/kaggle_worker/harness2.py` — versioned invocation and output collection for supported Harness² CLI commands; no copied benchmark engine.
- `poc/kaggle_worker/config.py` — validated provider/runtime configuration and secret references; no credential values in emitted JSON/logs.
- `poc/kaggle_worker/bootstrap.sh` — Ubuntu LTS bootstrap, pinned tools, state directory initialization, Compose startup, and local health checks; safe to rerun.
- `poc/kaggle_worker/cli.py` — preserve current task/review commands and add provider validate/refresh/enable, doctor, workflow start/status/pause/resume commands.
- `tests/poc/kaggle_worker/test_routing.py` — deterministic matching, quota, capability, freshness, and concurrency tests.
- `tests/poc/kaggle_worker/test_temporal_workflow.py` — Temporal workflow tests with fake activities for success, retry, pause/approval, cancellation, and restart.
- `tests/poc/kaggle_worker/test_temporal_activities.py` — activity idempotency, checkpoint-before-stop, and lease cleanup tests.
- `tests/poc/kaggle_worker/test_opencode.py` — fake HTTP OpenCode server tests for session creation, prompt streaming, model selection, resume, abort, and failure handling.
- `tests/poc/kaggle_worker/test_harness2.py` — fake subprocess tests for invocation, version/evidence recording, bounded budget, and explicit promotion.
- `tests/poc/kaggle_worker/test_config.py` — validation, secret redaction, disabled-by-default providers, and safe file permissions.
- `tests/poc/kaggle_worker/test_bootstrap.py` — shell/Compose static and dry-run checks using fake command runners; never provisions AWS.
- `tests/poc/kaggle_worker/README.md` and `poc/kaggle_worker/README.md` — local development, bootstrap, configuration, worker routing, recovery, and disabled-provider behavior.

## Interfaces

The Temporal workflow input is immutable serializable data:

```python
@dataclass(frozen=True)
class CodingTaskInput:
    task_id: str
    goal: str
    repo_path: str
    repo_head: str
    branch: str
    required_capabilities: tuple[str, ...]
    preferred_model_id: str | None
    estimated_gpu_seconds: int
```

The workflow owns the lifecycle and calls activities in this order:

```text
validate_task -> prepare_workspace -> reserve_route -> run_opencode
     -> checkpoint_and_stop -> build_review_bundle -> wait_for_approval_or_finish
```

`reserve_route(task, idempotency_key) -> RouteLease` is a Kite activity. It selects exactly one eligible worker/model, reserves that owner's quota/concurrency, and records a route reason. A retry with the same idempotency key returns the existing reservation. `run_opencode(task, route_lease, session_id?) -> CodingResult` starts or resumes the session in the pinned worktree and heartbeats durable context to Temporal/Kite. The workflow must never run two OpenCode writers for one worktree. `release_route(route_lease, outcome, actual_usage)` settles quota exactly once.

The OpenCode client uses the official server API on loopback (or `opencode run` only for non-resumable one-shot commands). It creates a session in the worktree, selects the route's `providerID`/`modelID`, submits task instructions, consumes events, and supports abort. OpenCode's native provider support handles API-compatible model endpoints; Kite writes only the per-session route/config required for the selected model.

Approval signals are `approve`, `reject`, and `resume_with_instruction`. The workflow checkpoints and stops the provider session before awaiting an approval signal. A resume reconstructs context from the Kite checkpoint, OpenCode session identity/transcript, worktree, and Temporal history rather than hidden model memory.

## Tasks

### Task 1: Add package metadata and local Temporal/PostgreSQL deployment

**Files:**
- Create: `pyproject.toml`
- Create: `deploy/compose.yaml`
- Create: `deploy/README.md`
- Test: `tests/poc/kaggle_worker/test_deployment_config.py`

- [x] Define a Python 3.12 package and declare the Temporal SDK as a runtime dependency; put pytest in a development extra and preserve current namespace package imports.
- [x] Pin exact stable Temporal server and PostgreSQL image tags from official release documentation in `deploy/compose.yaml`; expose PostgreSQL only on the Compose network, expose Temporal only on loopback, use health checks, and mount named volumes under a configurable EBS-backed data root.
- [x] Add a deployment configuration test that parses the Compose YAML and asserts pinned tags, loopback-only host ports, health checks, and persistent mounts.
- [x] Run `python -m pytest tests/poc/kaggle_worker/test_deployment_config.py -q`; expect the test to fail before the deployment files exist and pass after implementation.
- [x] Document `docker compose -f deploy/compose.yaml up -d`, service health inspection, log viewing, and non-destructive stop (`down` without `-v`).

### Task 2: Extract Kite's provider/model selection policy from task queue execution

**Files:**
- Create: `poc/kaggle_worker/routing.py`
- Modify: `poc/kaggle_worker/protocol.py`
- Modify: `poc/kaggle_worker/store.py`
- Modify: `poc/kaggle_worker/scheduler.py`
- Test: `tests/poc/kaggle_worker/test_routing.py`
- Modify: `tests/poc/kaggle_worker/test_scheduler.py`

- [x] Add tests for exact model match, capability match, stale/missing health, disabled worker, unauthorized owner, unapproved policy, stale/exhausted quota, worker and owner concurrency caps, and deterministic tie-breaking.
- [x] Add `select_worker(request: RoutingRequest, workers: Sequence[WorkerProfile], owners: Mapping[str, OwnerPolicy], loads: Mapping[str, WorkerLoad], *, now: float, max_observation_age_seconds: int = 300) -> RouteDecision | None`; `RouteDecision` includes worker ID, provider ID, model ID, and human-readable reason.
- [x] Move worker eligibility/model matching from the SQLite claim operation into the pure selector without changing fail-closed behavior; keep SQLite transactions around quota/concurrency reservation.
- [x] Keep task queue state APIs for backwards compatibility during migration; the Temporal workflow will become the only execution queue in CLI/runtime paths.
- [x] Run `python -m pytest tests/poc/kaggle_worker/test_routing.py tests/poc/kaggle_worker/test_scheduler.py tests/poc/kaggle_worker/test_store.py -q`; all routing checks use temporary SQLite only.

### Task 3: Implement Temporal workflow and idempotent Kite activities

**Files:**
- Create: `poc/kaggle_worker/temporal_workflow.py`
- Create: `poc/kaggle_worker/temporal_activities.py`
- Create: `poc/kaggle_worker/temporal_worker.py`
- Test: `tests/poc/kaggle_worker/test_temporal_workflow.py`
- Test: `tests/poc/kaggle_worker/test_temporal_activities.py`

- [x] Add fake activity tests for task validation, workspace preparation, route reservation, OpenCode result, checkpoint, review bundle, and quota settlement; assert a successful workflow calls each stage in order exactly once.
- [x] Add tests for retryable provider failure, no eligible worker wait/recheck, user approval pause/resume, rejection, cancellation, and non-retryable failure; assert checkpoint precedes every provider stop/release.
- [x] Implement deterministic Temporal workflow control flow using only workflow time, workflow sleeps, and signals; call Kite I/O and Git/OpenCode actions only through activities.
- [x] Make activity side effects idempotent by `task_id + workflow_run_id + activity_name`; store OpenCode session ID and route lease before long model work; heartbeat checkpoint/session state so worker restart can resume without duplicate writers. Implemented request fingerprints, SQLite result caching and execution leases, fail-closed reconciliation before replay, transactional route reservation/recovery, session preparation before coding, periodic route plus Temporal heartbeats, and a durable SQLite single-writer lease keyed by task across workflow runs. OpenCode session-level reconciliation is handled by Task 4.
- [x] Add Temporal worker bootstrap that registers the workflow/activity implementations on a named task queue and uses the configured local Temporal endpoint; do not start model providers at worker boot.
- [x] Run `python -m pytest tests/poc/kaggle_worker/test_temporal_workflow.py tests/poc/kaggle_worker/test_temporal_activities.py -q`; use Temporal's local test environment and fake adapters only.

### Task 4: Connect OpenCode through its supported server/session API

**Files:**
- Create: `poc/kaggle_worker/opencode.py`
- Modify: `poc/kaggle_worker/temporal_activities.py`
- Test: `tests/poc/kaggle_worker/test_opencode.py`

- [x] Build fake HTTP server tests for OpenCode health, session creation/reuse, provider/model selection, idempotent prompt submission, durable event reads, abort, provider conflicts, and remote-endpoint safeguards.
- [x] Implement and wire an OpenCode client that binds to `127.0.0.1` by default, creates/reuses a session in the task worktree, selects `providerID`/`modelID` from Kite's route, records session IDs before prompts, and streams structured events plus resumable sequence cursors to Temporal heartbeats. The OpenCode task adapter derives a review patch from the isolated worktree and verifies the requested base HEAD.
- [x] Disable blanket auto-approval; new sessions receive task-scoped ask-by-default shell permissions with `git push *` denied. Permission events surface as bounded Temporal heartbeat details. Remote endpoints require explicit opt-in, HTTPS, and authentication.
- [x] Implement safe interruption end-to-end: pause/cancel/reject signals interrupt the active session; Temporal waits for the coding activity to return, checkpoints the worktree, confirms the session is inactive, and only then releases the route. Each stop action is journaled and recoverable.
- [x] Run `python -m pytest tests/poc/kaggle_worker/test_opencode.py tests/poc/kaggle_worker/test_temporal_activities.py tests/poc/kaggle_worker/test_temporal_workflow.py -q`; all OpenCode requests target the fake server.

### Task 5: Integrate Harness² through its supported CLI and record evaluations

**Files:**
- Create: `poc/kaggle_worker/harness2.py`
- Modify: `poc/kaggle_worker/config.py`
- Modify: `poc/kaggle_worker/temporal_activities.py`
- Test: `tests/poc/kaggle_worker/test_harness2.py`

- [x] Add fake subprocess tests for selected benchmark/domain, sequential `k=1` bounds, evidence path, output namespace, nonzero exit, malformed/missing reports, and secret-free records.
- [x] Implement preflight through `harness2-check` and the documented run/judge/report commands; dataset installation remains a separate explicit action and ordinary bootstrap never installs benchmark data.
- [x] Record Harness² Git commit/tree, task/evaluation inputs, redacted command arguments, score/evidence/artifact paths, candidate artifact hash, and promotion decision in an atomic durable JSON record.
- [x] Default to sequential `k=1`; previews use `--dry-run`; candidate output stays in an isolated evaluation namespace and promotion requires a named reviewer decision.
- [x] Run `python -m pytest tests/poc/kaggle_worker/test_harness2.py -q`; tests use fake subprocess commands and never invoke model credentials or download benchmark data.

### Task 6: Add validated config, provider commands, and operator CLI

**Files:**
- Create: `poc/kaggle_worker/config.py`
- Modify: `poc/kaggle_worker/cli.py`
- Test: `tests/poc/kaggle_worker/test_config.py`
- Modify: `tests/poc/kaggle_worker/test_cli.py`
- Modify: `poc/kaggle_worker/README.md`

- [x] Add config tests for malformed provider IDs/models/capabilities, unknown adapter, missing secret reference, disabled-by-default provider, and redaction of values matching secret fields.
- [x] Implement a schema-versioned YAML/JSON config containing Temporal endpoint/task queue, OpenCode executable/server binding, Harness² checkout/CLI path, provider adapter IDs, model/capability metadata, quotas/concurrency, and secret references; reject inline credential values in config output.
- [x] Add `kite doctor`, `kite provider validate`, `kite provider refresh`, `kite provider enable`, and Temporal workflow `start/status/pause/resume/cancel` commands; `refresh` may run only configured adapters and must report stale/unknown observations without enabling providers.
- [x] Keep Kaggle disabled by default and add only a config/schema stub that cannot start a Kaggle notebook or call Kaggle during validate/refresh.
- [x] Run `python -m pytest tests/poc/kaggle_worker/test_config.py tests/poc/kaggle_worker/test_cli.py -q`; use temporary config, SQLite, and fake adapters.

### Task 7: Add idempotent Ubuntu EC2 bootstrap with Docker Compose

**Files:**
- Create: `poc/kaggle_worker/bootstrap.sh`
- Create: `poc/kaggle_worker/config.example.yaml`
- Create: `tests/poc/kaggle_worker/test_bootstrap.py`
- Modify: `deploy/compose.yaml`
- Modify: `poc/kaggle_worker/README.md`

- [x] Write bootstrap dry-run tests using a fake command runner for OS/version rejection, missing Docker, disk/state path validation, rerun behavior, and provider-disabled initial state.
- [x] Implement `bootstrap.sh` for Ubuntu LTS that validates Git/Python/Docker Compose, installs pinned project dependencies and OpenCode using the official install channel, clones Harness² at a pinned revision, initializes config/state under the EBS path, and starts only Temporal/PostgreSQL Compose services.
- [x] Require the repository to be cloned with an existing read-only GitHub credential before bootstrap; never ask for a GitHub write token, start Kaggle, provision AWS resources, alter firewall/IAM, delete existing state, or push.
- [x] Add `kite setup` no-echo prompts for local secret references and paths; create secret files with mode `0600`, keep them out of Git, and print only redacted validation results.
- [x] Run shell syntax checks and `python -m pytest tests/poc/kaggle_worker/test_bootstrap.py tests/poc/kaggle_worker/test_deployment_config.py -q`; tests must use dry-run/fake runners and must not execute privileged install commands.

### Task 8: End-to-end fake-provider recovery and documentation gate

**Files:**
- Create: `tests/poc/kaggle_worker/test_end_to_end.py`
- Modify: `tests/poc/kaggle_worker/README.md`
- Modify: `docs/superpowers/specs/2026-11-17-provider-extensible-kite-design.md`

- [x] Build a temporary Git repository and fake provider/OpenCode server; submit a task, verify Kite selected the only compatible worker/model, confirm the Temporal activity produced a review bundle, and assert the source repo and remote were untouched.
- [x] Force an activity to fail after durable checkpoint/quota settlement, replace the activity host object and journal, and resume the same operation; assert one settlement and no patch auto-application or push. (A full OS-process restart on EC2 remains a deployment check.)
- [x] Add a two-provider routing scenario where only one provider matches model/capability and quota; record the selected route and prove a future adapter can register without changing task/checkpoint schemas.
- [x] Run `python -m pytest tests/poc/kaggle_worker -q -p no:cacheprovider`; all test services and data must be local/temporary, with Kaggle disabled.
- [x] Update the READMEs with exact clone/bootstrap/setup/doctor/task/review commands, rollback-safe service stop steps, provider add/validate/enable steps, and the Kaggle activation gate; record real limitations instead of claiming live Kaggle support.

## Self-review

- The plan preserves the requested ownership split: Kite routes workers/models; Temporal persists and advances workflows; OpenCode codes; Harness² evaluates/refines.
- Existing protocol, SQLite policy/scheduler, checkpoint, worktree, review integration, and CLI code are reused or migrated; the plan does not rebuild them from scratch.
- No acceptance step enables Kaggle, contacts Kaggle, writes to GitHub, or provisions AWS automatically.
- The Temporal route reservation and OpenCode session identity are persisted before side effects, and quota release is idempotent to cover activity retries.
- No unresolved placeholder tokens (`TBD`, `TODO`, `FIXME`) remain. Exact upstream version pins are selected from official release references in Task 1 and recorded in project files.
