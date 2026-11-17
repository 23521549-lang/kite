# Provider-Extensible Kite Coding System — Design Spec

**Status:** Approved architecture  
**Date:** 2026-11-17

## 1. Goal

Prepare Kite to run on a newly created AWS EC2 instance with a one-command bootstrap, then operate an OpenCode coding runtime, use Harness² to evaluate and improve the harness, and schedule work across interchangeable model workers. Use Temporal for durable workflow execution, while Kite retains provider/model eligibility and worker-selection policy. Kaggle is the first planned provider, but the runtime and scheduler must support adding another provider through a documented adapter/configuration path without redesigning coding sessions or task state.

The owner will configure one valid Kaggle account and prepare its notebook(s) in advance. Multiple Kaggle accounts must not be used to multiply GPU quota. Kaggle remains disabled until an interactive, supported request/response path is verified.

## 2. Goals and non-goals

### Goals

- On a fresh supported EC2 image, clone the private GitHub repository and run one bootstrap command to install/configure Kite, OpenCode, Harness², and required services.
- Keep setup secrets out of source control, command output, logs, task prompts, and checkpoints. The setup flow records credentials locally with restrictive permissions and validates their presence without printing them.
- Use OpenCode as the coding runtime: it owns the repository-oriented inspect/edit/tool loop and sends model requests through a provider boundary.
- Use OpenCode's documented provider/model support and server or CLI interfaces where they meet requirements; do not build a separate model SDK for endpoints OpenCode already supports.
- Use Harness² for harness refinement and evaluation. Candidate harness changes and measured outcomes are versioned and auditable; Harness² does not replace OpenCode's coding loop or Kite's scheduler.
- Use Temporal for durable task workflows, task delivery, retries, timers, pause/resume, and recovery after process restart.
- Keep Kite as the domain control plane: provider/worker registry, capability and model selection, quota/concurrency policy, route decisions, checkpoints and workspaces, audit evidence, and Git review gate. Temporal does not select the provider/model for Kite.
- Define a provider-neutral worker protocol so future providers can be added without changing task/checkpoint formats or OpenCode's coding interface.
- Support multiple worker instances where a provider and its terms permit them. Keep account identity, quota, concurrency, and authorization scoped to each owner/provider account. Fail closed when required capability, policy, health, or quota data is missing or stale.
- Select an eligible worker using task requirements (model/capabilities), availability, provider health, concurrency, and quota. Persist the selection and lease so a replacement worker can resume from the canonical checkpoint.
- Keep Kaggle configuration optional and disabled by default. The initial Kaggle profile represents one valid Kaggle account; multiple models may be offered by its prepared notebook, but all usage shares that account's limits.
- Return code changes as local diffs/review bundles. Require user approval before applying external changes, pushing, merging, or deploying. Never push automatically.

### Non-goals

- Creating, operating, or rotating multiple Kaggle accounts to increase free compute quota.
- Assuming a Kaggle notebook batch run is an interactive inference server. No real Kaggle dispatch until supported multi-turn invocation, result retrieval, cancellation, and credential handling are verified.
- Supporting a specific future provider before its adapter, authentication method, terms, and lifecycle are implemented and validated.
- Automatically installing credentials, creating Kaggle accounts, accepting provider terms, or provisioning a paid fallback.
- Making Harness² the coding executor or making OpenCode responsible for durable queueing, quota policy, or task recovery.
- Reimplementing durable workflow history, retry scheduling, and pause/resume on top of a custom SQLite task queue when Temporal provides those generic primitives.
- Automatic GitHub push, merge, pull request publication, or AWS deployment.

## 3. Architecture

```text
User over SSH
  └─ Kite CLI / task interface
       ├─ task contract, provider/model selector, approval flow, audit log
       ├─ Temporal workflow service + PostgreSQL persistence
       │    └─ durable task lifecycle, timers, retries, pause/resume
       ├─ OpenCode coding runtime
       │    └─ native model providers/custom endpoints from selected Kite route
       ├─ Harness² evaluation/refinement adapter
       ├─ Kite policy store
       │    ├─ provider registry and capability/model catalog
       │    ├─ health/quota/concurrency observations and route decisions
       │    ├─ EBS-backed task checkpoints and isolated Git worktrees
       │    └─ patch validation and human Git review gate
       └─ worker adapters
            ├─ Kaggle (disabled until feasibility gate passes)
            └─ future provider adapters
```

### Component responsibilities

1. **Bootstrap and host setup:** A versioned script clones or updates the selected repository checkout, installs pinned dependencies, creates state/config directories on persistent EBS, configures a Kite CLI/service, and prints the next setup command. It must be safe to rerun and must not enable providers by default. The bootstrap does not create cloud resources, alter AWS IAM, configure a firewall, or publish changes.
2. **Kite CLI/control plane:** Accepts tasks, displays status and approvals, owns provider configuration and worker/model selection policy, coordinates Temporal workflows, validates returned results, and exposes setup/doctor/provider commands. It owns domain progress and route decisions; Temporal owns durable workflow history.
3. **Temporal service:** Stores workflow event history in PostgreSQL and schedules Kite activities through Temporal task queues. It handles timers, retries, pause/resume signals, and workflow recovery. Temporal activities use idempotency keys before any provider lifecycle side effect. It never chooses a model or provider itself.
4. **OpenCode runtime adapter:** Starts or connects to a task-scoped OpenCode session in the task worktree, configures the selected provider/model using documented OpenCode configuration/API, streams or collects session events, and reports checkpoints/results to Kite. OpenCode handles coding turns; Kite controls task lifecycle and provider selection. Prefer native provider support and implement only a narrow translation adapter for a provider not supported directly.
5. **Harness² adapter:** Uses Harness²'s supported setup, executor, refinement, and reporting commands for bounded evaluations against recorded task evidence and fixed inputs. Each candidate is tied to a harness version and reports score/evidence. Promotion is explicit and reversible; the default refinement budget is sequential and bounded. Kite does not reimplement Harness² evaluation loops.
6. **Provider adapter contract:** Each adapter declares stable `provider_id`, authentication/config schema, supported models and capabilities, health/status observation, quota/concurrency observation where available, start/invoke/cancel/stop lifecycle, and error classification. It must redact credentials and never receive task payloads it was not assigned. Provider adapters do not edit the canonical repository or push.
7. **Kite worker selector:** Selects a worker matching task role/model/capabilities and current policy, status, concurrency, and quota observations. It records the route and returns a lease/idempotency identity to the Temporal workflow. Provider network calls occur in idempotent Temporal activities outside database transactions. No eligible worker means the workflow waits with a clear reason, not bypassing checks.
8. **Workspace/checkpoint/review:** AWS EBS is canonical for the repository, Kite policy store, OpenCode data, logs, and versioned task checkpoints. Each writer works in an isolated Git worktree pinned to a base HEAD. A worker returns a result/patch; Kite validates identity and base HEAD and presents a review bundle. Push and merge remain user actions.

## 4. Provider discovery and routing

- Provider integrations are explicit plugins/adapters listed in local configuration. Adding credentials alone must not execute arbitrary code or automatically trust an unknown provider package.
- `kite provider validate` validates config and secret references locally. `kite provider refresh` asks configured adapters to refresh model/capability and health/quota observations. A worker becomes schedulable only after validation succeeds and an operator enables it.
- Scheduler matching uses task requirements and adapter-advertised capability metadata, not provider-specific logic in OpenCode or Harness².
- Profiles include provider/account owner identity, worker ID, model IDs, capabilities, enabled state, maximum concurrency, status freshness, policy authorization, and quota observation. Multiple workers belonging to one owner share the owner's configured quota/concurrency policy; worker count cannot multiply an owner's quota.
- A task's route decision (provider, worker, model, observed status/quota, and reason) is written to the audit log and checkpoint metadata.
- Temporal owns retry timers and durable workflow state. On a retryable provider failure, Kite re-evaluates worker eligibility after a checkpoint, reconciles the prior lease using an idempotency key, and verifies the replacement supports the checkpoint's model/task requirements. It must never run two writers against one worktree.
- New provider adapters are opt-in and remain disabled until a local/fake-provider contract suite passes and the operator explicitly enables them.

## 5. Kaggle adapter constraints

- Configure one valid Kaggle account only. Do not store duplicate account credentials or implement round-robin/account rotation.
- A prepared notebook may expose more than one model route. The adapter reports model IDs, readiness, lifecycle, and quota for that single account. Unless verified otherwise, concurrency defaults to one active inference session and quota is shared across all model routes.
- Kaggle is disabled by default. Validation may inspect local configuration and use a fake adapter; it must not submit/start a notebook run, consume accelerator time, or contact Kaggle unless the operator explicitly enables a separately approved live diagnostic.
- Before activation, document the supported connection method and verify multi-turn invoke, result retrieval, cancellation, stop, authentication ownership, restart/recovery, and task checkpoint/resume. If only batch execution is supported, label it as a separate batch provider mode and do not advertise interactive OpenCode coding.

## 6. Bootstrap and configuration flow

1. The owner creates an EC2 instance and persistent EBS volume using their existing AWS process.
2. The owner clones the private GitHub repository using a preconfigured GitHub SSH key or other supplied credential mechanism and runs one documented bootstrap command.
3. Bootstrap checks supported OS/Python/Git/Docker Compose versions, installs pinned Kite/OpenCode/Harness² packages, starts Temporal and PostgreSQL through Docker Compose, initializes a non-secret config template and persistent EBS-backed state directories, and runs local health checks. It does not start Kaggle or push to GitHub.
4. The owner runs `kite setup` (or equivalent) to enter the allowed repository path, OpenCode runtime path/settings, Harness² checkout/config, and provider secret references. Secrets are entered through a no-echo prompt or an external secret store, not as command-line arguments.
5. The owner runs `kite provider validate` and `kite doctor`. Kaggle validation is local/offline by default; provider remains disabled until its feasibility gate passes.
6. The owner explicitly enables a validated provider and starts a task. Kite selects a compatible available worker, checkpoints before pause/stop, and stops the worker when work completes or waits for user approval.

Use Ubuntu LTS on EC2 as the first supported host. Use official OpenCode CLI/server interfaces, Harness²'s supported CLI/setup, Temporal's Python SDK, and Docker Compose for Temporal/PostgreSQL services. Pin exact package and image versions in the install manifest; report upgrades rather than silently changing a running system.

## 7. Persistence and security

- Keep source checkout, Kite provider policy state, Temporal PostgreSQL data, OpenCode session data, checkpoints, task logs, and Harness² evaluation artifacts on persistent EBS. Confirm paths are configurable and avoid EC2 instance-store paths.
- Keep provider secrets outside Git and task state. Use file permissions of `0600` for secret files, or an explicitly configured system secret store. Redact secrets from subprocess output, tracebacks, environment dumps, and audit events.
- Bootstrap and provider validation are idempotent. A restart reloads config and Temporal workflows without starting any model worker automatically. Docker Compose restarts only the local Temporal/PostgreSQL control services.
- Task input, repository contents, model output, notebooks, and adapter metadata are untrusted data and cannot grant themselves permission to run commands, access credentials, or push.
- The scheduler fails closed for missing/expired health, policy, owner, quota, or capability observations when those are required by the adapter's declared policy.
- Use least-privilege GitHub read access for clone. Write access is not needed for the MVP because Kite does not push.

## 8. Failure behavior and approvals

- Bootstrap reports the failed stage, leaves existing state intact, and can be rerun. It must not delete a repository, state database, EBS data, or user configuration as an implicit rollback.
- Provider startup/invocation/cancellation errors are recorded with secrets redacted. The task remains resumable from its last durable checkpoint.
- Stale base HEAD, mismatched task/lease/worker/provider identity, or malformed patch is rejected from the review bundle.
- No automatic approval is inferred from a test passing. User approval is required before push, merge, deployment, or any future external write.
- A worker waiting on user input or approval is stopped after a checkpoint so no GPU session remains idle.

## 9. Validation and acceptance criteria

- A disposable supported EC2 Linux instance can run the documented bootstrap command twice safely and reach a healthy, provider-disabled Kite installation.
- Bootstrap tests use local fixtures/fake command runners; they do not clone to a real user repository, alter AWS resources, contact Kaggle, or push.
- A fake provider can be registered, validated, health-refreshed, selected by Kite policy using model/capability, invoked through a Temporal activity, failed over after checkpoint, and stopped through the provider-neutral contract.
- Scheduler tests prove that stale or missing required observations, disabled adapters, mismatched model/capabilities, exhausted owner quota, and concurrency caps prevent dispatch.
- Two fake providers can advertise different models/capabilities and Kite chooses only an eligible worker with a recorded routing explanation.
- One Kaggle profile can advertise multiple model routes under one account identity and one shared quota record; tests reject registering a second Kaggle account owned by the same user.
- Temporal resumes a task after a forced Kite worker/process restart without duplicating provider side effects. OpenCode can run a fake-provider coding task in an isolated worktree and reconstruct context from durable checkpoint/session data rather than hidden model context.
- Harness² evaluation records the harness version, inputs, score/evidence, candidate change, and promotion decision; budget is bounded and promotion is reversible.
- Review bundle generation does not apply patches or mutate the source checkout. Push is absent from the normal runtime path.
- No real Kaggle session or GitHub push occurs during local test/acceptance runs.

## 10. Delivery phases

1. **Durable workflow foundation:** add Temporal Python workflows/activities, local Temporal/PostgreSQL Compose deployment, workflow tests, idempotent activity boundaries, and Kite route decision records. Retain existing scheduler selection policy while moving generic queue/timer/retry/recovery ownership to Temporal.
2. **Provider-neutral registry and selector:** formalize provider config/capability schema and health/quota refresh; preserve the current owner-aware matching rules and fake-provider contract.
3. **OpenCode runtime integration:** implement a task-scoped coding runtime adapter using OpenCode's official server/CLI API and native provider/model configuration; test with fake OpenCode HTTP and fake model providers before live services.
4. **Harness² integration:** invoke the supported Harness² setup/evaluation/reporting commands for bounded jobs; connect versioned evidence and explicit candidate promotion.
5. **EC2 bootstrap:** pinned, idempotent install/setup/doctor commands using Docker Compose for Temporal/PostgreSQL; validate on a disposable Ubuntu LTS EC2 instance without provisioning AWS resources automatically.
6. **Kaggle feasibility and adapter:** investigate documented lifecycle/interface and terms; only implement and enable a real adapter if an interactive supported path passes Gate A. Otherwise keep Kaggle disabled and allow a separate, clearly labeled batch experiment only if explicitly approved.
7. **Additional providers:** add one adapter at a time using the same fake-provider contract suite. Provider enablement requires explicit local configuration and operator approval.

## 11. Open constraints for implementation

- The workspace Git repository is initialized locally, but has no configured remote; no commit or push has been made.
- Upstream OpenCode/Harness² interfaces and Kaggle's current supported interaction methods must be verified from official sources when implementation reaches those integrations.
- No claim is made that Kaggle currently provides the interactive worker API needed by OpenCode.

## 12. Current implementation and verification state

- Tasks 1–8 in `docs/superpowers/plans/2026-11-17-temporal-opencode-harness-integration.md` are implemented and locally tested with fake services. Configuration, CLI, provider registration and the disabled Kaggle stub are present.
- Temporal workflows, activity journaling, single-writer protection, OpenCode adapter, Harness² CLI adapter, review bundle, and an E2E fake provider/OpenCode workflow are covered by the local test suite.
- The recovery test replaces `TemporalKiteActivities` and its journal after a simulated crash following checkpoint/quota side effects. A full OS-process restart has not been run against an EC2 deployment.
- `bootstrap.sh` validates Ubuntu LTS, prerequisites, state storage and free space, but has not been run on AWS. It provisions no AWS resources. Docker, OpenCode installation, Harness² checkout, and Temporal service startup were not run in this workspace.
- A production `KiteActivityBackend` and live worker adapter are not registered yet. The bootstrap does not launch a coding worker, and `workflow start` therefore needs a separately configured worker before it can execute coding.
- Kaggle remains a disabled config-only stub: no notebook is started, no Kaggle API is called, and no Kaggle model session is supported.
- OpenCode install uses the official current install channel. Harness² is checked out to a fixed commit. The Harness² benchmark datasets and model-backed evaluation are never installed or run by bootstrap.

## 13. Upstream references

- [Kaggle Terms of Use](https://www.kaggle.com/terms) — account requirements and service terms.
- [Kaggle Community Guidelines](https://www.kaggle.com/community-guidelines) — duplicate-account and resource-abuse enforcement.
- [Kaggle notebook documentation](https://www.kaggle.com/docs/notebooks) and [GPU usage documentation](https://www.kaggle.com/docs/efficient-gpu-usage) — notebook lifecycle and quota guidance.
- [OpenCode provider configuration](https://opencode.ai/docs/providers/) and [model configuration](https://opencode.ai/docs/models/) — supported provider and custom endpoint configuration.
- [Harness² repository](https://github.com/google-research/harness2), [setup guide](https://github.com/google-research/harness2/blob/main/docs/SETUP.md), and [experiment guide](https://github.com/google-research/harness2/blob/main/docs/EXPERIMENTS.md) — executor setup, evaluation, recursion modes, and result persistence.
- [Temporal Python SDK](https://docs.temporal.io/develop/python), [workflow tasks](https://docs.temporal.io/tasks), and [durable execution overview](https://docs.temporal.io/evaluate/understanding-temporal) — workflow/activity lifecycle and recovery model.
- [OpenCode CLI commands](https://opencode.ai/docs/cli/commands), [server API](https://opencode.ai/docs/server/), and [provider configuration](https://opencode.ai/docs/providers/) — task sessions and model routes.
