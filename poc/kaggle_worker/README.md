# Kaggle worker scheduler prototype

This directory contains Kite's provider-neutral control plane for scheduling isolated coding tasks to model workers. Local verification uses fake workers and temporary SQLite databases only.

## Local behavior

- The SQLite store is the durable queue and stores one quota/policy record per worker owner.
- A worker can claim a task only when its owner is authorized, the policy gate is approved, status/quota observations are fresh, and role/model, concurrency, and estimated quota checks pass.
- Claims create leases and reserve quota atomically. Heartbeats extend leases; failures and expired leases can requeue tasks; cancellation and result settlement are idempotent.
- Task records pin a repository HEAD and branch. Checkpoints preserve task ID, branch, HEAD, completed/pending steps, and last tool result so a replacement worker can resume without the old provider session ID.
- Returned patches are validated into a `ReviewBundle`. The integration layer does not inspect or mutate a Git checkout, apply a patch, run commands, or push. The user remains the approval gate for repository changes and pushes.

## Local CLI

Run commands from the project root. The default state file is `scheduler.sqlite3` beside the selected config (`.kite/config.yaml` by default); pass `--db <path>` before the command to select another SQLite file. On EC2, point `--config` at `/var/lib/kite/kite/config.yaml` to keep scheduler state on EBS. The installed `kite` command is equivalent to `python -m poc.kaggle_worker.cli`.

```powershell
python -m poc.kaggle_worker.cli task add --goal "Fix parser edge case" --repo-head <git-head> --estimate-gpu-seconds 900
python -m poc.kaggle_worker.cli task list
python -m poc.kaggle_worker.cli task show <task-id>
python -m poc.kaggle_worker.cli task prepare <task-id> --repo <clean-local-repo>
python -m poc.kaggle_worker.cli task cancel <task-id>
```

Operator config is schema-versioned YAML or JSON. `kite provider validate` validates references without resolving secret values; `kite provider refresh` reports providers as unknown until a live adapter is registered, never starts provider workloads, and never enables one. `kite setup --secret-name <NAME>` captures one local value without echoing it and writes a `file:` reference under the config directory with mode `0600`. `kite doctor` reports local setup checks. Temporal workflow commands are `kite workflow start|status|pause|resume|cancel`; start takes the pinned task inputs and the other actions accept a workflow ID. Set `--config <path>` before the command to select a config file.

The `kaggle` adapter is a disabled config stub. Validation and refresh do not invoke Kaggle. `kite provider enable` intentionally fails until an actual adapter implementation and its policy/interaction review are installed; entering a token alone does not make Kaggle execution available.

`task add` only queues a pinned task. `task prepare` checks that the source repo is clean and at the pinned HEAD, creates a sibling Git worktree on the task branch, and writes an initial checkpoint beside the SQLite state. Repeating it resumes the matching workspace even if the original checkout later advances. It does not register or start a model provider. To validate a result file and print a review bundle:

```powershell
python -m poc.kaggle_worker.cli review result worker-result.json --expected-head <git-head> --expected-task <task-id> --expected-lease <lease-id> --expected-worker <worker-id>
```

The review command rejects mismatched task/lease/worker/HEAD metadata. Its JSON output contains the patch and summary for human inspection; it does not apply the patch or push it.

## Local verification

Run from the project root:

```powershell
python -m pytest tests/poc/kaggle_worker -q -p no:cacheprovider
```

Tests use fake clocks, fake providers, and temporary local SQLite/checkpoint files. They do not contact Kaggle or consume GPU quota.

## Ubuntu EC2 bootstrap

Prepare a fresh Ubuntu 22.04/24.04 EC2 host with Python 3.12 or 3.13, Git, Docker Engine and the Compose v2 plugin already installed. Attach/mount the persistent EBS filesystem at `/var/lib/kite`; allow at least 20 GiB free. Configure the host's Git credential helper with a read-only credential for the private repository before cloning. No AWS resources or permissions are changed by this repository.

```sh
git clone https://github.com/<OWNER>/<PRIVATE_REPOSITORY>.git kite-coding-agent
cd kite-coding-agent
KITE_DATA_DIR=/var/lib/kite bash poc/kaggle_worker/bootstrap.sh
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml setup
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml doctor
```

The bootstrap rejects unsupported OS versions, missing prerequisites, low free disk space, and conflicting Harness² state. It installs the project in `.venv`, installs OpenCode through its official installer if absent, checks out Harness² at a fixed Git commit, creates an empty-provider config and EBS-backed scheduler/checkpoint/evaluation/log directories without overwriting existing config, writes a fresh PostgreSQL password to `deploy/.env` with mode `0600`, and starts only PostgreSQL, its one-shot Temporal schema initializer, and Temporal. The optional Temporal UI is not started. Rerunning the bootstrap preserves the config, secret file and database volume. It never provisions AWS, changes IAM/firewall settings, runs Kaggle or pushes to GitHub.

When a local credential is ready, capture it without terminal echo and keep the returned `file:` reference for the provider config:

```sh
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml setup --secret-name KAGGLE_TOKEN
```

This only stores the value locally with mode `0600`; it does not configure or enable Kaggle. Kaggle remains a validation-only schema stub until the supported notebook request/response, cancellation, quota and credentials path is implemented and reviewed. `provider validate` checks the schema and registered adapter metadata; `provider refresh` invokes only registered adapters named in config and reports unregistered adapters as unknown; `provider enable` requires a registered live implementation. New adapter IDs can be registered in code without changing the config/task schema. After an adapter package is installed and configured, use:

```sh
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml provider validate
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml provider refresh
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml provider enable <provider-id>
```

Kaggle activation requires, at minimum, a verified supported interactive notebook request/response path, result retrieval, cancellation, one-account quota accounting, and documented credential ownership. Each gate needs a local fake-backed integration test and a policy review before the hard disable can be removed. Adding a token alone does not pass this gate.

To submit a durable Temporal workflow, ensure the Temporal worker process for the configured task queue is running, then pass a pinned HEAD and isolated branch:

```sh
HEAD="$(git rev-parse HEAD)"
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml workflow start --task-id example-1 --goal "Inspect the project and propose a change" --repo "$PWD" --repo-head "$HEAD" --branch agent/example-1 --capability coding
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml workflow status example-1
```

The current repository contains the Temporal worker construction API and activity contracts; a configured production worker/provider backend is still required before coding workflows can execute. The bootstrap intentionally does not launch an unconfigured worker. The local task/review CLI remains available for queue and review-bundle operations.

Local queue and review commands:

```sh
HEAD="$(git rev-parse HEAD)"
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml task add --task-id review-1 --goal "Inspect the parser" --repo-head "$HEAD" --estimate-gpu-seconds 900
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml task prepare review-1 --repo "$PWD"
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml review result worker-result.json --expected-head "$HEAD" --expected-task review-1 --expected-lease <lease-id> --expected-worker <worker-id>
```

Stop the control plane without deleting PostgreSQL history or EBS state:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml down
```

To roll back, stop the services with the command above and restore the previous Kite checkout/config from your own backup. The PostgreSQL named bind path and `/var/lib/kite` are left in place; never use `docker compose down --volumes` for rollback.

## Not enabled

- No Kaggle API/session adapter is implemented.
- No real collaborator accounts, shared credentials, account rotation, or pooled quota are supported.
- `policy_approved` is an explicit external gate; notebook sharing does not imply compute approval.
- No worker starts when there is no eligible queued task.

Before implementing a real provider, the project must verify both Kaggle's policy for independently owned collaborator compute and an officially supported AWS-to-Kaggle multi-turn request, result retrieval, cancellation, and credential-ownership path. Keep real workers disabled until both gates pass. The AWS-side scheduler must never use undocumented endpoints or shared account credentials.
