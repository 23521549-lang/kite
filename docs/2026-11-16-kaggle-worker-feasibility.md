# Kaggle On-Demand Worker Feasibility Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove whether one Kaggle GPU session can be started on demand, serve Qwen inference to the AWS coding loop, stop when idle, and resume work from a durable checkpoint without consuming quota while idle.

**Architecture:** Run a narrow feasibility prototype before building the full CLI. Keep the AWS-side worker lifecycle behind a small provider interface, first test it with a fake worker, then test Kaggle through documented interfaces. Treat a notebook batch job as insufficient evidence for an interactive coding assistant unless it can support the required multi-turn tool loop and clean stop/resume behavior.

**Tech Stack:** Python 3.12 on Linux, Kaggle CLI/API (use only a documented supported session interface), pytest for local protocol/controller checks, Qwen Coder model selected for the accelerator actually allocated. Do not introduce a paid inference service.

**Spec:** `docs/superpowers/specs/2026-11-16-qwen-kaggle-coding-agent-design.md`

## Global Constraints

- Support one user, one Kaggle account, and one GitHub repository for the MVP.
- No Kaggle session runs when there is no active inference-requiring task.
- Stop the worker on completion, cancellation, user-input wait, approval wait, or idle timeout.
- Save task progress independently of model context before intentional stop.
- Never store credentials in source files, notebooks, logs, or model prompts.
- Do not push/merge repository changes without an explicit user instruction.
- Do not enable a paid fallback; report quota and free-tier uncertainty honestly.
- The Kaggle integration must use a documented supported API and comply with provider terms.

---

## Plan scope

This is the first implementation plan only: the critical Kaggle worker feasibility gate. The spec includes additional independent product areas (CLI, Harness² coding loop, repo tools, web search, and quality workflow). Create separate implementation plans for those after this gate determines whether the required interactive Kaggle worker path is viable. Do not implement the full assistant before this gate passes.

## Proposed file structure

- `poc/kaggle_worker/README.md` — exact manual setup, launch, status, stop, and cleanup steps for the throwaway prototype.
- `poc/kaggle_worker/kernel-metadata.json` — Kaggle notebook/script metadata; no credentials.
- `poc/kaggle_worker/worker.py` — minimal Qwen load/inference probe and task-loop experiment.
- `poc/kaggle_worker/protocol.py` — provider-neutral worker lifecycle types used by the probe.
- `poc/kaggle_worker/controller.py` — on-demand lifecycle orchestration, idle stop, and state transitions.
- `tests/poc/kaggle_worker/test_controller.py` — fake-provider lifecycle and failure/recovery tests; never contacts Kaggle.
- `tests/poc/kaggle_worker/test_checkpoint.py` — checkpoint serialization and recovery tests; never contacts Kaggle.
- `docs/architecture/kaggle-worker-feasibility.md` — measured result, capability matrix, quota observations, and go/no-go decision.

Credentials are supplied at runtime through the Kaggle credential mechanism on the AWS host and Kaggle Secrets where required. The probe must redact credential-bearing environment values and command output.

## Tasks

### Task 1: Verify the supported Kaggle lifecycle and inference interfaces

**Files:**
- Create: `docs/architecture/kaggle-worker-feasibility.md`
- Read: `docs/superpowers/specs/2026-11-16-qwen-kaggle-coding-agent-design.md`

**Interfaces:**
- Produces: a capability matrix for create/start, readiness/status, multi-turn inference, stop/cancel, output retrieval, session limit, accelerator selection, and quota reporting.

- [ ] **Step 1: Inspect official Kaggle CLI and API documentation.** Record documented commands/endpoints for creating a notebook run or interactive session, polling status, sending work to a running session, stopping/cancelling, and retrieving artifacts. Distinguish documented functions from community workarounds.
- [ ] **Step 2: Inspect the Kaggle account's available accelerators and quota display.** Record the actual offered GPU type and available weekly GPU time before launching anything; do not assume a T4 allocation.
- [ ] **Step 3: Decide the probe path from documented capabilities.** Use an interactive session API only if it supports the full required request/response loop. If the only supported path is a batch notebook run, mark interactive inference as unproven and document whether batch execution is an acceptable degraded mode; do not build a public tunnel or use keep-alive workarounds.
- [ ] **Step 4: Write the capability matrix and go/no-go criteria.** The interactive path passes only if it can start on demand, accept at least two separate inference requests in one session, return each response to AWS, stop/cancel, and leave no GPU worker running.

**Verification:** Review the recorded claims against linked official docs and the account UI. The task is complete only when each capability is labeled `documented and observed`, `documented but not observed`, or `unsupported/unverified`.

### Task 2: Define and test the provider-neutral lifecycle controller

**Files:**
- Create: `poc/kaggle_worker/protocol.py`
- Create: `poc/kaggle_worker/controller.py`
- Create: `tests/poc/kaggle_worker/test_controller.py`

**Interfaces:**
- `WorkerProvider.start(task_id: str) -> str` returns a provider session ID.
- `WorkerProvider.status(session_id: str) -> WorkerStatus` returns `STARTING`, `READY`, `BUSY`, `STOPPED`, or `FAILED`.
- `WorkerProvider.invoke(session_id: str, request: ModelRequest) -> ModelResponse` performs one model request.
- `WorkerProvider.stop(session_id: str) -> None` stops the session and is safe to retry.
- `WorkerController.run_request(task_id: str, request: ModelRequest) -> ModelResponse` starts only when needed, waits for readiness with a bounded timeout, invokes, and records state.
- `WorkerController.pause(task_id: str, reason: str) -> CheckpointRef` checkpoints before stop and leaves the task resumable.

- [ ] **Step 1: Add failing fake-provider tests for idle behavior.** Assert controller construction and status inspection do not call `start`; submitting the first inference request starts exactly one worker.
- [x] **Step 2: Add failing tests for session reuse and stop behavior.** Assert two requests in one active task use one session; `pause`, completion, cancellation, and idle timeout call `stop` exactly once or safely retry it.
- [x] **Step 3: Add failing tests for startup timeout and worker failure.** Assert a timed-out/failed worker returns a resumable error and does not lose the task ID or checkpoint reference.
- [x] **Step 4: Implement the minimal state machine and fake provider.** Keep provider I/O out of controller state logic so later Kaggle API changes do not alter CLI behavior.
- [x] **Step 5: Run only the focused local tests.** Run `python -m pytest tests/poc/kaggle_worker/test_controller.py -q`; expected result: all fake-provider lifecycle tests pass without network access or Kaggle credentials.

### Task 3: Test checkpoint persistence and recovery across a worker replacement

**Files:**
- Create: `poc/kaggle_worker/checkpoint.py`
- Create: `tests/poc/kaggle_worker/test_checkpoint.py`
- Modify: `poc/kaggle_worker/controller.py`

**Interfaces:**
- `TaskCheckpoint` records task ID, task contract, plan, completed and pending steps, repo path, branch, Git HEAD, changed-file list, last tool result, model/provider ID, and timestamp.
- `save_checkpoint(checkpoint: TaskCheckpoint, path: Path) -> None` writes atomically to local EBS-backed workspace storage.
- `load_checkpoint(path: Path) -> TaskCheckpoint` validates schema and returns a typed checkpoint or a clear recovery error.
- `WorkerController.pause(...)` must persist the checkpoint before calling provider stop.

- [x] **Step 1: Add failing tests for round-trip and atomic writes.** Verify a checkpoint can be loaded after process restart and a partial write cannot replace the last valid checkpoint.
- [x] **Step 2: Add failing tests for invalid/stale checkpoint state.** Reject malformed schema and report repository HEAD mismatch without overwriting repository files.
- [x] **Step 3: Implement the checkpoint model and atomic persistence.** Use a versioned JSON schema and write-then-rename in the same filesystem.
- [x] **Step 4: Add a worker replacement test.** Stop fake worker A, instantiate a fresh controller, load the saved checkpoint, start worker B, and verify the resumed request includes task state but no dependency on A's in-memory context.
- [x] **Step 5: Run the focused checkpoint/controller tests.** Run `python -m pytest tests/poc/kaggle_worker/test_checkpoint.py tests/poc/kaggle_worker/test_controller.py -q`; expected result: all local tests pass.

### Task 4: Build the Kaggle Qwen inference probe using the verified interface

**Files:**
- Create: `poc/kaggle_worker/kernel-metadata.json`
- Create: `poc/kaggle_worker/worker.py`
- Create: `poc/kaggle_worker/README.md`
- Modify: `poc/kaggle_worker/protocol.py`
- Create or modify: `poc/kaggle_worker/kaggle_provider.py` only if Task 1 verified an official request/response mechanism.

**Interfaces:**
- `KaggleWorkerProvider.start(task_id: str) -> str`
- `KaggleWorkerProvider.status(session_id: str) -> WorkerStatus`
- `KaggleWorkerProvider.invoke(session_id: str, request: ModelRequest) -> ModelResponse`
- `KaggleWorkerProvider.stop(session_id: str) -> None`
- The provider must use bounded polling, redacted logs, idempotent stop, and explicit terminal states.

- [ ] **Step 1: Create a minimal notebook/script that reports accelerator identity and performs one short Qwen inference.** Keep the prompt generic and contain no project source or secrets.
- [ ] **Step 2: Add a two-request session probe.** Send two independent prompts through the verified supported path and record startup/load latency, response latency, accelerator type, observed quota change, and output retrieval behavior.
- [ ] **Step 3: Add the AWS-side provider adapter.** Implement only the interface proven in Task 1; do not emulate an interactive API with an unsupported public tunnel.
- [ ] **Step 4: Exercise start, ready, invoke, stop, and post-stop status.** Start only after confirming quota availability and explicitly stop/cancel at the end.
- [ ] **Step 5: Record the result without secrets.** Update `docs/architecture/kaggle-worker-feasibility.md` with exact observed behavior and limitations.

### Task 5: Run the go/no-go gate and hand off the next plan

**Files:**
- Modify: `docs/architecture/kaggle-worker-feasibility.md`
- Modify: `docs/superpowers/specs/2026-11-16-qwen-kaggle-coding-agent-design.md` only if the user approves a design correction after reviewing the evidence.

**Interfaces:**
- Produces: a decision of `GO: interactive worker proven`, `CONDITIONAL: batch-only/degraded`, or `NO-GO: no supported connection path`.

- [ ] **Step 1: Verify the idle invariant.** Confirm from Kaggle session status that no GPU worker remains after stop and that no worker was started while the controller was idle.
- [ ] **Step 2: Verify interruption recovery.** Interrupt an active toy task, load its checkpoint in a fresh process, and resume it on a replacement fake or Kaggle session.
- [ ] **Step 3: Compare the observed quota cost with the starting quota.** Include model load time and cleanup time; do not extrapolate a guaranteed weekly allowance from one run.
- [ ] **Step 4: Make the go/no-go decision.** If a supported interactive path fails, stop this implementation path and present the evidence and alternatives before building the full CLI.
- [ ] **Step 5: Draft separate follow-on plans only after a `GO` or user-approved `CONDITIONAL`.** The next plans cover (a) Harness² coding loop and role orchestration, (b) repo tools/permissions/quality workflow, and (c) CLI, web research, persistence, and AWS deployment integration.

## Self-review against the spec

- Covers the critical on-demand Kaggle worker, quota tracking, checkpoint/resume, model fit probe, and Harness² integration risk before full implementation.
- Does not claim that Kaggle batch notebook execution proves an interactive model endpoint.
- Preserves user control of GitHub pushes and avoids credentials in code/logs.
- Leaves broader CLI features for separate plans because they are independent workstreams and depend on the worker feasibility result.
- The Kaggle API integration task is gated on Task 1 evidence; no unsupported endpoint or network tunnel is assumed.

## Repo-level Qwen3.5 benchmark fixture prepared (2026-11-16)

A disposable multi-file Python task is prepared at `docs/benchmarks/repo-task-001/`. It asks Qwen3.5-9B to fix discount-aware free-shipping calculation, preserve Decimal semantics, reject invalid discounts, and avoid mutating input. The model prompt contains the README, implementation context, and visible tests; six hidden assertions remain in `grading/test_hidden.py`.

Local baseline: visible suite `3 passed`; hidden suite on the intentionally buggy starter `3 failed, 3 passed`, with failures for discount-adjusted free-shipping eligibility and out-of-range discounts. This confirms the fixture detects the intended behavior. The dedicated Qwen repository-task result is recorded below. Do not treat the prior small-function 9/9 result as repository-task evidence.

Benchmark record follows below, including runtime settings, exact patch outcome, checks, and worker shutdown.

## Repo-level Qwen3.5 result (2026-11-16)

The one-shot task used `Qwen/Qwen3.5-9B` on Kaggle T4 x2, with `enable_thinking=False` and deterministic generation (`do_sample=False`, 900-token cap). The cell reported a 28.1-second model load from the session cache and completed in 63.9 seconds, including loading. The session had to fetch the 19.3 GiB checkpoint once earlier in the same notebook session; Kaggle's session display reached 11 minutes. The session was explicitly stopped; the notebook showed `Draft Session off (run a cell to start)`.

The generated response failed the task gate in two ways:

- It is not a valid unified patch: `git apply --check` reports `corrupt patch at line 23` on a clean disposable fixture copy.
- Its intended change only adds discount range validation. It leaves `shipping = shipping_for(subtotal, shipping_method)` unchanged, so the reported implementation still grants free shipping based on the pre-discount subtotal. It misses the primary task requirement.

No model-patch tests were run because the exact response did not apply. The independent starter baseline remains 3/3 visible checks passing and 3/6 hidden checks passing; the three hidden failures detect the intended defect and invalid-discount behavior. Save the raw response in `docs/benchmarks/repo-task-001/grading/qwen3.5.patch` for audit. This is a failed repo-level coding-agent probe, not a clean comparison of model ability: the Kaggle notebook cell had editing/runtime errors and required reloading the model before generation. Fix the notebook prompt harness and rerun only if a second, carefully prepared attempt is warranted.


## Repo-level Qwen2.5-Coder-7B result (2026-11-16)

A dedicated cell ran `Qwen/Qwen2.5-Coder-7B-Instruct` on Kaggle T4 x2 in FP16, sharded across both GPUs. The Kaggle image exposed Python 3.13 and `bitsandbytes==0.50.2` imported, but Transformers reported its 4-bit backend unavailable, so this run used FP16. The model checkpoint download/reconstruction showed 15.2 GB; model load took 108.8 seconds and the first generation took 11.6 seconds. A focused retry took 29.42 seconds. Kaggle showed a 10-minute draft session before it was explicitly stopped; the final notebook state showed `Draft Session off (run a cell to start)`. The notebook's Settings menu showed `Turn off internet`, meaning Internet access was enabled for this download.

Both raw responses failed before test execution:

- First response: only attempted to add a duplicate `shop.shipping` import and omitted the implementation changes. `git apply --check` reported `corrupt patch at line 10`.
- Retry response: emitted malformed hunks and placed the validation and discounted-subtotal logic after the function's existing `return`, where it would be unreachable. `git apply --check` reported `corrupt patch at line 9`.

No patch tests were run because neither response passed the patch-application gate. Preserve the raw outputs in `docs/benchmarks/repo-task-001/grading/qwen2.5-coder-7b.patch` and `qwen2.5-coder-7b-retry.patch`. The benchmark measures the combined local inference setup and one-shot patch workflow; it does not establish that FP16 Qwen2.5-Coder-7B can deliver a reliable autonomous coding loop. Compared with the earlier Qwen3.5 run, this model also failed the repo task, and both failed primarily at producing a usable patch.
### Qwen2.5-Coder-7B full-file retry (2026-11-16)

After the malformed-patch retries, a corrected prompt requested a complete `quote.py` file so patch formatting could be removed from the model task. On Kaggle T4 x2, FP16, `Qwen/Qwen2.5-Coder-7B-Instruct` loaded in 109.3 seconds and generated in 21.6 seconds (130.9 seconds total for load plus inference; one-time 15.2 GB model download completed beforehand). The output was preserved as `docs/benchmarks/repo-task-001/grading/qwen2.5-coder-7b-full-file.py`. The draft GPU session was stopped immediately after the completed cell; notebook status confirmed `Draft Session off (run a cell to start)`.

The response includes discount validation and a `discount` result key, but still calls `shipping_for(subtotal, shipping_method)` using the pre-discount subtotal. It also calculates `total` without final cent quantization. Consequently it misses the central discount-aware shipping requirement. The exact generated file was evaluated in an isolated temporary fixture copy against the visible and hidden repository-task tests: **7 passed, 2 failed**. The failures are (1) visible API compatibility, because the response adds a `discount` key and changes the established return shape, and (2) the core hidden case, because it bases free-shipping eligibility on the pre-discount subtotal. The other discount-boundary, tax, total, and immutability checks passed. No benchmark fixture or working source files were modified by the evaluation.


### Qwen3.5-9B full-file comparison (2026-11-16)

Using the same repository fixture and full-file output format as the final Qwen2.5-Coder attempt, `Qwen/Qwen3.5-9B` ran on Kaggle T4 x2 in FP16. This fresh session could not use the notebook's prior 4-bit `bitsandbytes` setup, so no external packages were installed; FP16 sharding was used instead. The model loaded in 141.5 seconds and generated in 31.3 seconds after a one-time 19.3 GB checkpoint download. Kaggle reported a 6-minute draft session including setup retries, and the session was explicitly stopped with the notebook confirming `Draft Session off (run a cell to start)`.

The exact response is saved as `docs/benchmarks/repo-task-001/grading/qwen3.5-9b-full-file.py`. On an isolated temporary copy of the fixture, it passed **9/9 checks** (three visible plus six hidden): preserved the return schema, rejected invalid discounts, based shipping on discounted subtotal, calculated tax and total correctly, and left inputs unchanged. The earlier Qwen3.5 unified-diff attempt still failed patch formatting and functional checks; this full-file run tests a different, patch-format-independent workflow and is the comparable result against Qwen2.5-Coder-7B's 7/9.

### Benchmark oracle correction and final comparable scores (2026-11-16)

During evaluation, the hidden shipping case's expected total was found to be incorrect: $55.00 subtotal - $10.00 discount + $5.00 shipping = $50.00 taxable amount; at 10% tax, the total is $55.00. The test expected $50.00, contradicting the fixture README and the visible tests' formula. `grading/test_hidden.py` now expects $55.00. Both full-file outputs were rerun against the same corrected fixture in separate temporary copies:

- Qwen3.5-9B FP16: **9/9 passed**.
- Qwen2.5-Coder-7B FP16: **7/9 passed**. It failed the unchanged return-shape assertion (extra `discount` key) and discount-aware free-shipping case (pre-discount threshold).

This correction does not change the Qwen2.5 score because both of its failures remain. No project source code was changed; only the benchmark's inconsistent hidden-test expected value was corrected. The full-file Qwen3.5 result is promising for this narrow repair task, but one small fixture is not enough to conclude broad repository-level autonomous coding quality.

## Repo task 002: checkpoint persistence and worker replacement (2026-11-16)

A second synthetic repository task tested the project's durable-context requirement. The task asks for strict validation of a versioned JSON checkpoint, same-directory atomic replacement, cleanup on failed writes, and rejection of a checkpoint whose repo HEAD is stale. Baseline implementation is a stub; the initial visible and hidden suite failed 8/8 before four additional edge checks were added.

`Qwen/Qwen3.5-9B` ran on Kaggle T4 x2 in FP16. It loaded in 148.4 seconds and generated in 98.8 seconds; the checkpoint download was 19.3 GB. The exact output is preserved at `docs/benchmarks/repo-task-002/grading/qwen3.5-9b.py`. The GPU session was explicitly stopped and Kaggle showed `Draft Session off (run a cell to start)`.

The final cross-model suite ran 14 checks. On the local Windows fixture copy, Qwen3.5 passed **11/14** and Qwen2.5 passed **4/14** (with Windows rename compatibility affecting Qwen2.5). Qwen3.5 implemented versioned JSON, atomic replacement, cleanup after replace failure, and stale repo-head rejection, but missed string validation for list members on save/load and accepted JSON boolean `true` as schema version `1` because Python treats `bool` as an `int` subclass. These are meaningful checkpoint correctness gaps. The task-001 result (9/9 on the corrected fixture) should therefore be read as a narrow fix, not evidence that model-generated persistence code is production ready. No Kite source code was modified; this task fixture and output live under `docs/benchmarks/repo-task-002/`.

### Qwen2.5-Coder-7B comparison on repo task 002 (2026-11-16)

The same prompt and model-independent 14-check suite were used for `Qwen/Qwen2.5-Coder-7B-Instruct` on Kaggle T4 x2 in FP16. It loaded in 112.5 seconds and generated in 69.4 seconds after a 15.2 GB model download. The raw answer is saved as `docs/benchmarks/repo-task-002/grading/qwen2.5-coder-7b.py`. The GPU session was stopped and notebook status returned to `Draft Session off (run a cell to start)`.

On the local Windows fixture copy, Qwen2.5 scored **4/14**, compared with Qwen3.5's **11/14**. Qwen2.5 also misses the strict `CheckpointError` contract for invalid input, accepts extra fields that could persist ephemeral worker data, accepts `true` as schema version 1, and does not validate that the JSON envelope is a mapping. Its `NamedTemporaryFile` remains open during `Path.rename`; this fails on Windows and explains three visible-test failures plus some cleanup effects. Since the planned controller runs on AWS Linux, those rename failures are platform-specific and should not be used alone to compare model reasoning; the validation/schema misses remain independently observable. The Qwen3.5 output is the stronger starting point, but it still needs a deterministic validation layer and targeted tests before use in the checkpoint subsystem.


## Official Kaggle interface research (2026-11-16)

Created `docs/architecture/kaggle-worker-feasibility.md` with a capability matrix based on Kaggle's official MCP, Notebooks, API, Efficient GPU Usage, and Kaggle CLI documentation. The API documents interactive session creation/status/cancel and batch kernel push/status/log/output. Its published tool list does not document executing arbitrary code in an already running session. The separate Kaggle Jupyter Server connection to Colab/VS Code is documented as experimental and requires a compatible URL surfaced in the editor. Accordingly, the project gate is **CONDITIONAL**: provider-neutral controller/checkpoint work can proceed, but unattended AWS-to-Kaggle multi-turn inference remains unproven. Kaggle CLI documents `kaggle quota` for weekly GPU/TPU accelerator hours; this run did not inspect the account's remaining quota. The dedicated test notebook was confirmed off after runs, and other notebooks were not changed.


## Local provider prototype update (2026-11-16)

Implemented the fake-provider controller and versioned durable checkpoint path. Controller tests cover lazy start/reuse, serialized requests, timeout/failure cleanup, pause-before-stop, and idle stopping. Checkpoint tests cover typed round-trip, strict schema/version validation (including rejecting boolean schema version), invalid list items, stale Git HEAD rejection without touching files, atomic replacement failure preserving the last valid checkpoint, and recovery by stopping fake provider A, creating a fresh controller with provider B, and resuming from the saved task state without persisting a Kaggle session ID. Focused command pytest -q tests/poc/kaggle_worker/test_checkpoint.py tests/poc/kaggle_worker/test_controller.py passed **18 tests**. This validates local orchestration behavior only; no Kaggle provider adapter or AWS-to-Kaggle interactive invocation was tested.


