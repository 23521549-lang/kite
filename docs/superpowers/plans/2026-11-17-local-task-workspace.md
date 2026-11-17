# Local Task Workspace Implementation Plan

> **For agentic workers:** Use this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Prepare each queued task in an isolated local Git worktree and save a durable checkpoint before any model is connected.

**Architecture:** The workspace module validates a clean source repository and the task's pinned HEAD, creates a sibling Git worktree on the task branch, and atomically saves the existing versioned checkpoint. The CLI exposes this as `task prepare`; repeated preparation resumes the matching workspace/checkpoint and rejects stale or conflicting state.

**Tech Stack:** Python standard library, Git CLI, existing SQLite scheduler store and checkpoint format.

**Spec:** `docs/2026-11-16-qwen-kaggle-coding-agent-design.md`

## Global Constraints

- AWS/EBS repo and checkpoint remain canonical project state.
- Every task carries a pinned Git HEAD and isolated branch/worktree.
- Workers return patches for a review gate; scheduler never pushes or merges.
- No model or Kaggle worker starts without an eligible task and provider integration remains disabled.
- Tests use temporary local Git repositories only; do not touch user repositories or Kaggle notebooks.

---

### Task 1: Prepare isolated worktree and initial checkpoint

**Files:**
- Create: `poc/kaggle_worker/workspace.py`
- Modify: `poc/kaggle_worker/cli.py`
- Test: `tests/poc/kaggle_worker/test_workspace.py`
- Modify: `tests/poc/kaggle_worker/test_cli.py`

- [ ] Add tests for clean repo preparation, pinned HEAD rejection, dirty repo rejection, safe task IDs, idempotent resume, and checkpoint content.
- [ ] Run the tests and confirm missing workspace API failures.
- [ ] Implement validation, sibling worktree creation, initial checkpoint save, and rollback of only a just-created worktree if checkpoint save fails.
- [ ] Add `task prepare <task-id> --repo <path>` which reads the task from SQLite and reports the workspace/checkpoint paths.
- [ ] Run focused workspace/CLI tests and the complete worker suite.
- [ ] Document task preparation and recovery behavior in the worker README.
