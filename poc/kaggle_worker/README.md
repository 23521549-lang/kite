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

Use a fresh Ubuntu 22.04/24.04 EC2 host. Attach/mount persistent EBS storage at `/var/lib/kite` (or set `KITE_DATA_DIR` to its mount path) with at least 20 GiB free. Configure Git access to the private repository before cloning. The bootstrap installs missing system prerequisites, Docker Engine/Compose from Docker's official apt repository, uv with Python 3.12, the project dependencies from `requirements.txt`, OpenCode, and the pinned Harness² checkout. No AWS resources or permissions are changed by this repository.

```sh
git clone https://github.com/<OWNER>/<PRIVATE_REPOSITORY>.git kite-coding-agent
cd kite-coding-agent
KITE_DATA_DIR=/var/lib/kite bash poc/kaggle_worker/bootstrap.sh
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml setup
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml doctor
```

The first command is used on a clean Ubuntu image because `make` itself may not be installed yet. After bootstrap installs it, `make bootstrap` is the convenient repeatable entry point; it runs the same script. The only interactive follow-up is `kite setup` when entering a provider secret.

The bootstrap rejects unsupported OS versions, low free disk space, and conflicting Harness² state. It creates an isolated `.venv`, checks out Harness² at a fixed Git commit, creates an empty-provider config and EBS-backed scheduler/checkpoint/evaluation/log directories without overwriting existing config, writes a fresh PostgreSQL password to `deploy/.env` with mode `0600`, and starts only PostgreSQL, its one-shot Temporal schema initializer, and Temporal. The optional Temporal UI is not started. Rerunning the bootstrap preserves the config, secret file and database volume. It never provisions AWS, changes IAM/firewall settings, runs Kaggle or pushes to GitHub.

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
## Task-type dispatcher

Each task carries a `task_type` (`coding`, `analysis`, `review`, `vision`, `docs`, …) and optional required capabilities. The dispatcher keeps only eligible workers (idle capacity, owner policy, fresh heartbeat, quota, every required capability), then scores each (worker, model) pair:

```text
score = quality(task_type, model) - swap_penalty_per_second * load_seconds(worker, model)
```

`load_seconds` is 0 when the worker already has the model loaded, `load_seconds_cached` when the model is on the worker's disk, and `load_seconds_cold` otherwise. Ties go to the less-loaded worker. A pinned `--model` skips scoring. Configure it in `config.yaml`:

```yaml
providers:
  - id: kaggle-han
    adapter: opencode          # until the Kaggle pull-worker lands (phase 2)
    enabled: true
    owner: han
    model: qwen3.6
    models: [devstral]
    capabilities: [coding, vision]
    quota: {gpu_seconds: 108000, concurrency: 1}
routing:
  swap_penalty_per_second: 0.0005
  load_seconds_cached: 60
  load_seconds_cold: 600
  model_skill:               # replace with scores measured on your own tasks (Harness²)
    coding:   {qwen3.6: 0.75, devstral: 0.65}
    analysis: {qwen3.6: 0.70, devstral: 0.60}
```

Register configured providers in the scheduler store (safe to rerun; consumed quota and heartbeat state are kept), then queue work with a type:

```sh
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml provider sync
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml workflow start --task-id a-1 --goal "Analyse the parser" \
  --repo "$PWD" --repo-head "$HEAD" --branch agent/a-1 --task-type analysis
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml workflow wake a-1   # re-check for a worker now
```

`workflow start` now queues the task in the scheduler store before starting the workflow. A waiting workflow re-checks for a worker with backoff (15 s doubling to 5 min) or immediately on `workflow wake`, and continues as new every 200 checks so its Temporal history stays bounded. A completed task is charged at least its lease's wall time, because Kaggle bills session time.

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

## Kaggle pull workers (gateway)

Kaggle cannot accept inbound connections, so workers pull work. `kite gateway serve` exposes an OpenAI-compatible API on loopback (`http://127.0.0.1:8787/v1`, used by OpenCode) and a worker API (`/worker/*`). Caddy publishes only `/worker/*` over HTTPS; every worker authenticates with its own token.

1. Open ports **80 and 443** in the EC2 security group (80 is used once by Let's Encrypt).
2. Add a provider with `adapter: pull` and a gateway block to `/var/lib/kite/kite/config.yaml`:

```yaml
providers:
  - id: kaggle-han
    adapter: pull
    enabled: true
    owner: han
    model: qwen3.6
    models: [devstral]
    capabilities: [coding, vision]
    quota: {gpu_seconds: 108000, concurrency: 1}
    secrets:
      worker_token: file:/var/lib/kite/kite/secrets/worker_token_kaggle-han
gateway:
  public_url: https://1-2-3-4.sslip.io      # EC2 public IP with dashes
  aliases:
    qwen3.6-fast: {model: qwen3.6, params: {chat_template_kwargs: {enable_thinking: false}}}
routing:
  swap_penalty_per_second: 0.0005
  load_seconds_cached: 100     # measured on Kaggle 2xT4
  load_seconds_cold: 200       # download (~90 s) + load
```

3. Create the worker token (printed once; store it as the Kaggle secret `KITE_WORKER_TOKEN`), sync the store, start HTTPS and the gateway:

```sh
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml worker token kaggle-han
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml provider sync
KITE_GATEWAY_DOMAIN=1-2-3-4.sslip.io sudo -E docker compose -f deploy/gateway.compose.yaml up -d
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml gateway serve
```

4. In the Kaggle notebook run everything up to "Worker mode"; it heartbeats and waits for jobs. Check from EC2 with `curl -s 127.0.0.1:8787/status`.
5. Point OpenCode at the gateway: `kite gateway opencode-config` prints the block for `opencode.json`.

`qwen3.6-fast` is the same model with thinking disabled, for chat and simple tool steps. `GET /status` lists each worker's models, whether it is alive, and what it has loaded; `kite sandbox run` checks it first and stops immediately when no live worker serves the model, instead of waiting for the job timeout.

Known limitation: the gateway waits for the worker's complete answer and replays it as one SSE stream (no token-by-token streaming yet). Clients therefore see the whole answer at once, and throughput figures they display (OpenCode's `tok/s`) are not real generation speed; on 2×T4 qwen3.6 generates about 54–58 tokens/s.

## Sandboxed runs

`kite sandbox run` executes one task end to end without Temporal, in three throw-away containers that share a per-task clone of the repository's committed state:

1. **setup** (optional, with network): runs only `sandbox.setup_command` / `--setup-cmd`, e.g. `pip install -r requirements.txt` or `npm ci`. Python packages are installed into a per-task `/deps` directory. Files this step creates in the repository are committed separately, so they never appear in the agent's patch. If setup fails, the agent is not started.
2. **agent** (no Internet): OpenCode (`opencode run`) on an internal Docker network whose only reachable service is the gateway. All capabilities dropped, no privilege gain, unprivileged user, CPU/memory/process limits, no secrets, no Git remote.
3. **test** (no network): Kite runs the test command itself.

```yaml
sandbox:
  gateway_host: 172.19.0.1        # host address on kite-sandbox-net (printed by `kite sandbox build`)
  memory: 1g
  cpus: 1
  test_command: python -m pytest -q
  setup_command: ""               # e.g. "pip install -r requirements.txt"; can be overridden per run
```

```sh
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml sandbox build    # network + image
sudo systemctl restart kite-gateway                                       # also listen on gateway_host
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml sandbox doctor   # isolation checks
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml sandbox run --repo ~/project \
  --setup-cmd "pip install -r requirements.txt" --goal "..."
```

Results are written to `/var/lib/kite/kite/sandbox/<task-id>/` (`result.json`, `result.patch`, `setup.log`, `agent.log`, `test.log`). `verified` is true only when setup (if any) succeeded, the agent finished and changed files, and Kite's own test run passed. Review the patch, then apply it with `git apply`. The setup step has network access while it installs, which carries the same supply-chain risk as installing those packages yourself; the agent never has network access.

## Temporal workflows in the sandbox

`kite workflow worker` serves the Temporal task queue with `SandboxActivityBackend`: every workflow step runs through the sandbox above, and leases, the task-type dispatcher and GPU quota go through the scheduler store.

- `prepare_workspace` clones the pinned commit into `/var/lib/kite/kite/tasks/<slug>/work` and runs the optional setup command (the only step with network). Re-running it reuses the clone.
- `run_opencode` runs `opencode run` in the offline agent container with the routed model, then Kite runs the test command itself in a container with no network; the patch, changed files and test result go into the review bundle summary.
- `workflow resume --instruction "..."` runs the agent again on the same clone; the patch is always computed against the original commit.
- `pause`, `cancel` and `reject` kill the task's containers.

The agent never talks to an OpenCode server on this path, so the unanswered permission prompt (E1) and the bypassable `git push` rule (E5) do not apply: the container has no network, no Git remote and no credentials.

```sh
sudo cp deploy/kite-workflow-worker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kite-workflow-worker
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml workflow start --task-id demo-1 \
  --repo ~/project --goal "..."            # --repo-head defaults to HEAD, --branch to agent/<id>
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml workflow status demo-1
./.venv/bin/kite --config /var/lib/kite/kite/config.yaml workflow result demo-1
```

## Not enabled

- No Kaggle API/session adapter is implemented.
- No real collaborator accounts, shared credentials, account rotation, or pooled quota are supported.
- `policy_approved` is an explicit external gate; notebook sharing does not imply compute approval.
- No worker starts when there is no eligible queued task.

Before implementing a real provider, the project must verify both Kaggle's policy for independently owned collaborator compute and an officially supported AWS-to-Kaggle multi-turn request, result retrieval, cancellation, and credential-ownership path. Keep real workers disabled until both gates pass. The AWS-side scheduler must never use undocumented endpoints or shared account credentials.
