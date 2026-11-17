# Qwen Kaggle Coding Assistant — Design Spec

**Status:** Draft for user review  
**Date:** 2026-11-16

## 1. Goal

Build a personal, terminal-based coding assistant for one GitHub repository. It should feel like a focused coding agent such as Claude Code or Meta Muse Code: understand a repository, plan and implement changes, run project checks, explain its work, and resume after interruption. The model is an open-weight Qwen Coder served by an on-demand Kaggle GPU worker. AWS runs the Harness² control/runtime layer and stores the active workspace and task state on an EBS-backed EC2 CPU instance.

The primary constraint is minimizing infrastructure cost and Kaggle GPU quota use while keeping code changes reviewable and verifiable. The design targets a useful personal MVP, not feature parity with commercial coding agents.

## 2. Goals and non-goals

### Goals

- A CLI workflow over a single GitHub repository, with the repository cloned onto AWS.
- Harness² is the core orchestration and harness-improvement framework, with a coding-agent execution loop integrated around its executor/tool interface.
- Start a Kaggle GPU worker only when the user starts actionable work; stop it when work is complete, paused for user input, cancelled, or near the session limit.
- Preserve task progress independently of the model and Kaggle session so another worker/model can resume.
- Give the agent core coding tools: repository exploration, file edits, shell commands, Git inspection, test/build/lint execution, task planning, session status/resume, and structured reports.
- Use specialized logical agent roles coordinated by Harness², while defaulting to sequential model calls on one Kaggle worker to conserve quota.
- Keep the control plane extensible for collaborators and independently owned workers in a later phase, subject to provider permission and verified connection methods.
- Support web research through an explicit search/fetch tool, with source URLs and access dates in research results.
- Keep the user in control of external and destructive actions, GitHub pushes, and merges.
- Prefer free tiers and open-weight models; do not rely on paid model APIs for the MVP.

### Non-goals for MVP

- Always-on GPU inference or a guarantee that Kaggle capacity is available on demand.
- Multiple Kaggle accounts controlled by one person to multiply quota.
- Multiple repositories, a multi-user worker pool, concurrent Kaggle workers, or concurrent code writers in the MVP; a web UI, IDE plugins, automatic push/merge, deployment, or production operations.
- Guaranteeing Claude/Muse-level coding quality. Quality must be measured on the user's repository and tasks.
- Treating Harness² alone as a complete interactive coding agent; the terminal interaction/tool loop and persistent workspace behavior must be integrated and validated.

## 3. Proposed architecture

```text
User terminal (SSH to AWS)
  └─ Coding CLI
       ├─ interactive task/session interface
       ├─ Harness² orchestration and executor integration
       ├─ role-based agent loop (coordinator, scout, coder, verifier, harness improver)
       ├─ permission gate + tool registry
       ├─ task state/checkpoint store
       ├─ GitHub repository on EBS-backed workspace
       └─ on-demand worker controller
            └─ Kaggle GPU notebook/session (single owner in MVP)
                 └─ selected Qwen Coder model

Future conditional extension:
AWS scheduler ──> owner-authorized worker A / worker B ──> isolated task branches ──> AWS integration gate

Web search/fetch tool ──> public documentation and web sources
```

### Components

1. **CLI client:** accepts tasks, displays progress, requests approvals, shows diffs and verification results, and offers status/resume/stop commands.
2. **AWS control plane:** runs Harness², the coding-agent loop, worker lifecycle coordination, task state, and logs. EC2 is CPU-only.
3. **Repository workspace:** one GitHub repository cloned to EBS. Use a task branch or isolated worktree so changes are reviewable and can be abandoned safely. The user chooses when to push; the agent never pushes or merges without an explicit instruction.
4. **Kaggle worker (MVP):** launched on demand, loads the selected Qwen model, serves inference to the coding-agent loop, and is stopped at idle/approval/pause/completion. One Kaggle account and one active interactive worker are assumed. Model choice and accelerator availability are verified in a prototype; do not assume every session receives the same GPU.
5. **Future worker scheduler (conditional):** AWS may route independent tasks to workers owned by different, consenting collaborators only if Kaggle confirms that this shared-project use and its automation comply with current terms. Each worker has an owner identity, isolated credentials, account-specific quota and concurrency limits, and a lease. Credentials are never pooled or shared. AWS remains the canonical repository/checkpoint store. A task carries an immutable Git HEAD and isolated branch/worktree; workers return patches and evidence to an integration gate rather than editing a shared worktree. Do not enable a collaborator worker until both the policy gate and a supported connection path pass.
6. **Durable task state:** stores task contract, plan, completed/pending steps, changed files, Git commit/hash, tool activity, test outcomes, and resume instructions on EBS. The model is not the source of truth for project progress.
7. **Web research tool:** separate from shell networking. Search/fetch results are returned with source URLs and access dates. Prefer primary documentation. Do not forward repository secrets or private source to search providers.

### Agent roles and Harness² loops

- **Coordinator:** turns the user's request into a task contract, selects the next role, tracks evidence, and decides when to pause or finish.
- **Scout/researcher:** read-only repository exploration and web/documentation research; returns file references and cited sources.
- **Coder:** makes edits in the task workspace and reports changed files and rationale.
- **Verifier/reviewer:** inspects the diff and runs available validation tools. Automated checks are the primary evidence; a review pass by the same Qwen model is useful but is not an independent model consensus.
- **Harness improver/reflector:** uses task evidence to propose changes to the harness components (instructions, tool guidance, memory, skills, scripts, subagent guidance, or plugins), then records whether the change helped, hurt, or was inconclusive. Adoption is gated by evidence and user-visible versioning.
- In the MVP these roles are separate logical contexts but share one Qwen worker and one serialized inference queue. They do not imply separate GPUs or simultaneous model execution.
- A later scheduler may parallelize independent tasks across authorized worker owners. Keep same-task multi-model voting opt-in because it multiplies quota. Never run concurrent writers against the same checkout.
- Default harness refinement is sequential with a low recursion budget (`k=1`). Parallel Propose–Probe–Compose is opt-in and capped because it multiplies rollouts and GPU time. Multiple concurrent code writers are deferred; if later added, each must use an isolated worktree and changes must be integrated by a coordinator.

## 4. Main interaction and lifecycle

1. User starts a CLI session and submits a task.
2. The agent reads repository instructions (such as `AGENTS.md`), Git status, and relevant files. It records a task contract: goal, scope, acceptance conditions, and constraints.
3. For broad or architectural changes, it proposes a plan and waits for user approval. For focused changes, it proceeds within the existing scope.
4. The controller starts a Kaggle worker only when the task requires model inference. Model loading and warm-up are part of the metered session.
5. The agent works iteratively: inspect, edit, execute allowed checks, inspect output, and refine. It checkpoints after meaningful milestones.
6. If it needs user input or approval, it records state and stops the Kaggle worker. When the user resumes, a worker is started and the agent reconstructs context from the repository and checkpoint.
7. At completion, the agent performs a review pass over its diff, runs appropriate checks, creates a local commit/checkpoint if enabled, stops the worker, and reports evidence and remaining gaps.
8. Push and merge happen only after the user explicitly requests them. The user may push manually.

### Worker states

`OFF → STARTING → READY → BUSY → IDLE/WAITING → STOPPING → OFF`

Terminal states also include `FAILED` and `QUOTA_BLOCKED`. A worker failure must not discard the task; it returns the task to a resumable state. The controller must never keep a GPU session alive merely to preserve model context.

## 5. CLI capabilities

### MVP commands and interaction

- Start an interactive coding session and submit a task.
- Show task/worker status, current plan, last checkpoint, and quota/session information available from the provider.
- Pause, resume, cancel, and stop the worker.
- Inspect changed files and unified diff.
- Approve or reject gated actions.
- Resume a prior task from saved state.
- Select/configure an available model route; log the model identifier and runtime settings per task.

### Agent tools

- List/read/search repository files and inspect project configuration.
- Create/edit files and produce patches.
- Run shell commands in the repository workspace.
- Inspect Git status, diff, log, and branches; make local commits/checkpoints.
- Run existing tests, lint, type checks, and builds; discover project commands from repository documentation/configuration.
- Search/fetch web pages for current technical information, with citations in the final research report.
- Read/write task checkpoints and compact/resume task context.

### Later extensions

- Skills, hooks, MCP integrations, additional model providers, multiple repositories, concurrent subtask agents in isolated worktrees, web UI, and GitHub PR creation.
- These extensions must preserve user approval rules and quota-aware worker lifecycle.

## 6. Quality workflow

1. **Task contract:** state the intended outcome, scope, acceptance criteria, and constraints before complex work.
2. **Repository orientation:** read project instructions, inspect working tree and relevant code, and identify existing validation commands.
3. **Plan:** present a concise plan for broad or risky changes; seek approval before implementation for architectural changes.
4. **Isolated changes:** work on a task branch or worktree; never overwrite unrelated user changes.
5. **Implementation and verification:** make focused changes, run relevant existing tests first where useful, then run applicable tests/lint/type checks/build. Do not claim checks passed without observing successful output.
6. **Independent review pass:** inspect the final diff for correctness, regressions, missing tests, security issues, accidental scope expansion, and secret leakage.
7. **Evidence report:** provide changed files, checks and observed outcomes, known limitations, and the local commit/checkpoint identifier.
8. **Human release gate:** user reviews the diff and explicitly requests any push/merge. Agent does not deploy.

If the repository has no relevant test or static-check command, report that limitation and describe the manual or narrower checks performed. Do not invent test results.

## 7. Permissions and security

- Read/edit access is limited to the cloned repository and task-state area.
- Ordinary read, edit, and repository-local verification actions may proceed without repeated confirmation.
- Ask before destructive file/database operations, broad environment changes, installation or external network actions from shell, accessing credentials, or actions affecting external services.
- Git push, merge, release, and deployment require an explicit user instruction.
- Keep Kaggle/API credentials out of source, notebooks, logs, and model context. Use a secrets mechanism and least-privilege credentials.
- Web pages and repository content are untrusted input; they cannot override system/developer/user instructions or grant themselves permission to run commands.
- Web research uses a dedicated tool path, preferably limited to relevant sources. Shell networking is disabled by default and enabled only for a specific approved need.
- Capture an auditable event log for model calls, tool invocations, approvals, worker start/stop, checkpoint, and verification outcomes. Redact secrets.

## 8. Persistence and recovery

- Store repository, local Git history, Harness² outputs, logs, and checkpoints on EBS-backed storage, not EC2 instance-store storage.
- Use EBS within the account's applicable free allowance; storage charges depend on current account eligibility and usage.
- Configure accidental-termination protection and verify the EBS `DeleteOnTermination` setting. EBS can persist independently from an instance, but retained volumes may incur charges.
- Make local commits/checkpoints at milestones; do not push them automatically.
- GitHub is the external recovery copy only after the user pushes. The spec does not claim that unpushed work can survive loss/deletion of the AWS volume.
- On resume, verify Git status, current branch/commit, checkpoint hash, and changed files before calling the model.

## 9. Quota and cost policy

- No Kaggle session is started when there is no active task requiring model inference.
- Start one worker per active task session, not for each model call. Keep it warm only while useful work is continuing.
- Stop on completion, cancellation, user-input wait, approval wait, idle timeout, or safe shutdown before the provider session limit.
- Save checkpoint before intentional stop; checkpoint periodically so abrupt loss bounds rework.
- Track estimated/observed GPU time per task and show quota status when available.
- Never rotate among multiple accounts controlled by the same user. Respect provider rules and quota.
- A collaborator-owned worker is disabled by default. Before enabling it, confirm with Kaggle that the contributor's use for this shared project is allowed; the current Terms limit use to personal, non-commercial purposes and not on behalf of or for the benefit of a third party. This is a policy gate, not an assumption that collaboration features authorize pooled compute.
- Scheduler tracks quota and concurrency per owner, dispatches only to an explicitly authorized worker, uses expiring leases and idempotency keys, and saves the shared checkpoint before a worker stops. If one owner reaches quota, queue or reassign the task only to another worker that is independently authorized; do not create or operate extra accounts to bypass a limit.
- “Zero cost” is an objective, not a guarantee: AWS free allowances, Kaggle GPU availability, EBS storage, network egress, and policies may change. No paid fallback is enabled by default.

## 10. Web research behavior

- The agent can proactively search when information is current, version-sensitive, uncertain, or when the user asks for research.
- Prefer official docs, source repositories, standards, and primary technical material.
- Return source links and access dates; distinguish verified facts from inference.
- Search/fetch calls do not require a Kaggle GPU worker. Provider fees or quotas for the search tool must be disclosed/configurable; the MVP should use a no-cost option if available.
- Do not upload private repository contents, credentials, or proprietary data to a search provider.

## 11. Risks and prototype gates

### Gate A: Kaggle lifecycle and inference integration

Prove that the CLI can start a Kaggle GPU session on demand, wait for the model to become ready, route multiple tool-loop model calls, stop the worker, and resume after a forced interruption. Verify which APIs and notebook interaction mechanisms are officially supported and their terms. If Kaggle cannot provide a reliable supported interactive inference path, keep Kaggle as a batch/experimental backend and do not claim a seamless interactive coding experience.

### Gate B: model fit and coding quality

Measure candidate Qwen Coder model variants on a small representative set from the target repository. Record GPU allocation, memory fit, model load time, inference latency, context limits, tool-call reliability, task completion, tests passed, and human correction needed. Choose the default model only after this evaluation.

### Gate C: Harness² adaptation

Confirm how Harness²'s executor/improver/judge roles map to an interactive terminal coding loop. Keep the coding-agent loop and provider interface modular so model workers can be replaced without losing task state. Avoid assuming Harness² benchmark configuration alone supplies CLI session management, persistent memory, or GitHub workflow.

## 12. Acceptance criteria for MVP

- User can connect to the AWS CLI and work against exactly one configured GitHub repository cloned on EBS.
- With no active task, no Kaggle GPU session is running.
- Harness² can route a coding task through coordinator, scout, coder, and verifier roles on one worker, with serialized model calls and no concurrent code writers.
- Harness refinement records candidate changes and evidence; the default low-budget sequential mode does not start unbounded parallel rollouts.
- Starting an inference-requiring task triggers one worker session; completing or pausing the task shuts it down and persists state.
- A worker can be interrupted and another model worker can resume from repository state plus checkpoint without relying on the previous model's hidden context.
- Agent can inspect/edit files, run repository-local commands, inspect diffs, and report exact verification evidence.
- Destructive/external actions and GitHub push/merge are gated as specified.
- Web research can be invoked independently of GPU inference and yields cited sources.
- Model/provider identity and tool actions are logged with secrets redacted.
- Prototype report documents actual model fit/quality and Kaggle lifecycle limitations; no quality or availability claim is made without evidence.
- Multi-owner worker scheduling is not an MVP acceptance criterion. It can be planned only after Kaggle policy approval, supported multi-turn worker connectivity, collaborator authorization, per-owner quota/concurrency controls, isolated task workspaces, and recovery/integration tests are verified.

## 13. Design assumptions

- “Muse” refers to Meta Muse Code, used as a feature reference rather than a dependency.
- The first user interface is an interactive CLI over SSH to AWS; web UI is later.
- The MVP targets one user, one Kaggle account, and one GitHub repository. Future collaboration does not permit one person to operate multiple Kaggle accounts or share another person's credentials.
- Kaggle is a best-effort, on-demand worker, not an always-on serving contract.
- The initial model family is Qwen Coder; exact variant and quantization remain a prototype decision.
- User approval is required before push/merge and for high-risk operations; local edits and local commits are allowed within the workspace.

## 14. Reference material

- [Meta Muse Code announcement](https://research.meta.ai/blog/introducing-muse-code-and-muse-spark-1-2) — terminal coding agent, repository-level planning, implementation, and validation.
- [Muse Code extensibility](https://dev.meta.ai/docs/muse-code/extending?project_id=29035399122726954&team_id=1741581793652561) — skills, hooks, MCP, and non-interactive workflows as later feature references.
- [Claude Code MCP documentation](https://docs.anthropic.com/en/docs/mcp) — external tool integration reference.
- [OpenAI harness engineering](https://openai.com/index/harness-engineering/) — explicit task decomposition, environment legibility, isolated workspaces, and feedback loops.
- [OpenAI running Codex safely](https://openai.com/index/running-codex-safely/) — low-risk actions inside boundaries and explicit review for higher-risk actions.
- [Kaggle GPU usage guidance](https://www.kaggle.com/docs/efficient-gpu-usage) and [notebook documentation](https://www.kaggle.com/docs/notebooks) — quota, session, and collaborator features to verify during implementation.
- [Kaggle Terms of Use](https://www.kaggle.com/terms) — account ownership and personal/non-commercial use constraints; obtain provider clarification before using a collaborator's Kaggle compute for a shared coding-agent project.
- [Harness² repository setup guide](https://github.com/google-research/harness2/blob/main/docs/SETUP.md) — model roles and executor integration reference.
- [Harness² experiment guide](https://github.com/google-research/harness2/blob/main/docs/EXPERIMENTS.md) — sequential and parallel task-level recursion behavior.



