# Local Worker Queue CLI Implementation Plan

**Goal:** Provide a small local CLI to create, inspect, and cancel scheduled tasks and to validate worker results for human review.

**Scope:** SQLite-backed task queue and review bundle only. The CLI does not start a model provider, contact Kaggle, apply patches, run repository commands, or push changes.

**Tech stack:** Python standard library (`argparse`, `json`, `sqlite3`) and the existing scheduler prototype.

## Tasks

### Task 1: Persisted task inspection API

- [x] Add `get_task` and `list_tasks` to the scheduler store interface and SQLite implementation.
- [x] Return state, attempt count, task payload, pinned HEAD, branch, quota estimate, and last error.
- [x] Keep SQLite queries parameterized and return copied plain-data records.

### Task 2: Queue and review CLI

- [x] Add `python -m poc.kaggle_worker.cli task add|list|show|cancel`.
- [x] Require goal and pinned repository HEAD; generate an isolated `agent/<task-id>` branch by default.
- [x] Add `review result` to validate an external `WorkerResult` JSON against expected identities and print a `ReviewBundle`.
- [x] Report input/store/validation errors to stderr with nonzero exit status.
- [x] Document commands and hard limits in the worker README.

### Task 3: Local verification

- [x] Add tests for add/list/show/cancel persistence, required pinned HEAD, valid review output, stale HEAD rejection, and no patch application.
- [x] Run the complete `tests/poc/kaggle_worker` suite using temporary SQLite/checkpoint files only.

## Follow-up gates

- Real Kaggle provider remains disabled until the policy and supported multi-turn execution gates in `2026-11-16-multi-owner-worker-scheduler.md` pass.
- Queue creation is intentionally separate from worker registration/dispatch until a local interactive coding loop is designed and approved.
