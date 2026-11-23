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

Operator config is schema-versioned YAML or JSON. `kite provider validate` validates references without resolving secret values; `kite provider refresh` reports providers as unknown until a live adapter is registered, never starts provider workloads, and never enables one. `kite setup --secret-name <NAME>` captures one local value without echoing it and writes a `file:` reference under the config directory with mode `0600`. `kite doctor` checks that Kite really works on this machine (systemd services, gateway and connected workers or their autostart state, Temporal reachable, Kite's ports not listening on public addresses, Docker with the sandbox image and internal network, secret files present and 0600, free disk, graphify, OpenCode, the Kaggle CLI when autostart is on) and prints the command to run for each problem; `--json` for scripts, exit code 1 when something fails. Temporal workflow commands are `kite workflow start|status|pause|resume|cancel`; start takes the pinned task inputs and the other actions accept a workflow ID. Set `--config <path>` before the command to select a config file.

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

### Autostart (Kite runs the notebook)

With `launch` on a pull worker, nobody has to open the notebook. Kite starts it only for you in the chat: when the chat opens (`kite mcp serve` starts), when a chat message or a task started from the chat finds no connected worker for its model, or on `kite worker start`. A bench run, a task started from the command line or any other caller never starts a notebook (the GPU quota is spent only when you are there): a bench run says how to start the worker and waits for it. To start, the gateway pulls the owner's notebook metadata from Kaggle (attached datasets and settings stay as set in the Kaggle UI), puts this repository's `kaggle_model_worker.ipynb` in its place with `KITE_LAUNCH` filled in, and pushes it as a new version that runs on GPU T4 x2 ("Save & Run All"). The run loads the requested model first, skips the sample task, and ends after `idle_minutes` without a job so the weekly GPU quota is not spent idle. While a start has not connected, the gateway asks Kaggle every minute what the run is doing: queued (Kaggle has not given it a GPU yet, which Kite cannot speed up), running (loading the model), or stopped, which is reported at once with Kaggle's failure message. The push carries no session-timeout flag, like "Save & Run All" in the UI. Opening that notebook by hand later runs it as a manual run (`KITE_LAUNCH` is ignored in interactive sessions and once the run's start window has passed). A worker that is heartbeating (also while it swaps models) is never started twice; right after a gateway restart Kite waits one heartbeat timeout for running workers to reconnect.

```sh
kite worker autostart kaggle-han --kernel <kaggle user>/<notebook slug>   # asks for the Kaggle API token (hidden), 0600 file
sudo systemctl restart kite-gateway
kite worker start kaggle-han      # or just open the chat / start a task
kite worker status                # one line per worker; while starting: Kaggle queued/running and for how long (--json: full report)
kite worker stop kaggle-han       # finish the current job, then end the Kaggle session
```

The Kaggle CLI (`.[kaggle]` extra) gets `KAGGLE_API_TOKEN` as the path of the secret file, in its own environment and a temporary config folder. Worker token: `launch.token: per-launch` (the default) writes a fresh token into the pushed (private) notebook; the gateway keeps only its SHA-256 and accepts it until that run's time is up (`max_hours` + 30 min) or the next start. Kaggle does not give Secrets to a version pushed through the API (the Secret read fails with HTTP 400), so `kaggle-secret` works only for runs saved on kaggle.com; `kite doctor` warns about it. `/kite/*` answers loopback callers only and is not published by Caddy.

Kaggle's queue: a version pushed through the API can wait in Kaggle's queue for hours while "Save Version" pressed on kaggle.com starts at once (22 Nov 2026: version 11 pushed by Kite waited 200+ minutes, version 12 saved by hand at the same moment ran at once; same environment, same GPU). So Kite treats your click as the quick way: after 3 minutes in the queue it sends an ntfy notification with the notebook's link (`kite notify setup`), `kite worker status` and the chat say the same, and a chat message that no worker can answer gets that status line at once instead of hanging. Saving a version by hand replaces Kite's queued one (Kaggle cancels it). If Kite's run starts anyway after a session you started is connected, the gateway tells it to stop (one Kaggle session per worker: `X-Kite-Session`). A run saved by hand ("Save Version", a batch run) also ends after `MANUAL_IDLE_MINUTES` (30) without a job; a session opened in the editor runs until you stop it. A run that stops before connecting is reported with the error line of its Kaggle log; after two such runs in a row Kite stops pushing until `kite worker start <id> --force`. Every push, start, connect and failure goes to the journal (`launches`), and so does a run you saved by hand once it connects; `kite journal summary` shows how long Kaggle queued the runs and how many connected after a push (`connected`) or after your click (`connected_by_hand`).

One task at a time per worker: the second request slot (`--parallel 2 --kv-unified`) is for the person chatting; another task's requests wait. The chat keeps 48K of a 128K window and a task's agent 80K, so both fit the shared KV cache; a request that still runs out of room next to another one is run again alone. Requests waiting for a worker that is being started do not expire after `job_timeout_seconds` (up to 4 hours).

Streaming: when a client asks for `stream: true` (OpenCode always does), the worker reads the answer from llama-server as it is generated and posts its chunks to `POST /worker/jobs/<id>/chunks` about twice a second; the gateway passes them on as SSE, so the chat shows the model's thinking and answer as they come instead of all at once at the end. If the person closes the chat, the gateway refuses the next batch and the worker stops generating. A streamed answer is never retried on another worker (the client already has part of it): a worker that dies mid-answer ends the request with an error. A notebook without streaming (imported before this change) still works: its whole answer is replayed as before. Re-import `kaggle_model_worker.ipynb` to get streaming.

### API workers (Kite calls the provider)

A worker can also be an API Kite calls itself (`adapter: relay`, see `relay.py`): any OpenAI-compatible `/chat/completions` (OpenAI, OpenRouter, Groq, DeepSeek, Gemini's compatible endpoint, vLLM, Ollama, llama-server) or Anthropic's Messages API. It takes jobs from the same queue as the Kaggle workers, so routing, the journal and per-task stickiness work the same.

```sh
kite worker add-api openrouter --kind openai --base-url https://openrouter.ai/api/v1 \
  --model gpt=openai/gpt-5-mini --price-in 0.25 --price-out 2 --daily-cap 1 --monthly-cap 10
# asks for the API key without echo, stores it in secrets/api_key_openrouter (0600)
sudo systemctl restart kite-gateway
kite worker spend
```

`--model NAME=PROVIDER_MODEL` gives the name Kite uses and the provider's own name. Prices are USD per million tokens; every answer's token usage is priced into the journal (`spend`) and shown by `kite worker spend`, `kite journal summary` and `kite doctor`. At its daily or monthly cap (UTC) a worker stops taking jobs, the others take them, and ntfy tells you once. Arguments only llama-server knows (`top_k`, `min_p`, `chat_template_kwargs`...) are not sent to an OpenAI-style API. A key is required for Anthropic and for any API outside this machine; plain `http` is accepted only for this machine and private networks.

Routing between workers: a free worker gets a request first (a paid one waits 5 s for it); a task keeps the worker that took its first request until that worker is gone, stopped or at its cap; the model name `kite-auto` goes to any free worker, which answers with its own model.

### Bootstrap notebook (stub)

`kite worker stub kaggle-han` prints one cell. Paste it in place of the notebook's old cells once (keep its inputs, GPU T4 x2, Internet and the `KITE_WORKER_TOKEN` Secret) and press Save Version. Each run then downloads the worker code from the gateway (`GET /worker/code`, published through Caddy with the rest of `/worker/*`) and runs it, so a notebook never runs old code after `kite update`. The gateway also says which model to load first (the oldest request waiting for that worker) and how many idle minutes end the session. Autostart pushes this stub too (`launch.notebook: stub`, the default; `full` pushes the whole notebook as before).

### Kite's browser for kaggle.com

Kaggle starts a run at once when Save Version is pressed on kaggle.com, while a run pushed through its API may wait in a queue for hours. Kite therefore has its own Chromium on the gateway machine (`browser.py`), streamed to `http://127.0.0.1:8787/kite/browser` (loopback only: `ssh -L 8787:127.0.0.1:8787 ubuntu@<machine>`, later the web's Worker tab).

```sh
~/kite/.venv/bin/pip install -e ~/kite[browser]
sudo ~/kite/.venv/bin/playwright install-deps chromium && ~/kite/.venv/bin/playwright install chromium
kite worker autostart kaggle-han --kernel <user>/<slug> --via browser
kite worker browser kaggle-han                 # opens the notebook editor in Kite's browser
kite worker browser kaggle-han --auto-click on # optional: Kite presses Save Version itself
```

Sign in to Kaggle there once with email and password (Google's sign-in may refuse a browser on a server); the session is saved encrypted (AES-GCM, key in `secrets/browser_key`, 0600). Only kaggle.com pages open in it. With `launch.via: browser`, a request that needs the worker opens the notebook there and tells you (chat note and ntfy) to press Save Version → Save & Run All → Save; Kite waits up to an hour for Kaggle to show the new run. "Kite tự bấm" (`--auto-click on`, or the switch on the page) is off by default: Kaggle's terms forbid automated access outside its API, so switching it on is your decision. If Kaggle changes its page and the click fails, you are asked to press it yourself. Chromium closes after 10 minutes without a viewer.

### Rented GPU machines (RunPod)

Create the pod once in RunPod's console: a GPU, an image that serves an OpenAI-compatible API on `0.0.0.0` (for example `vllm serve Qwen/Qwen3-32B --port 8000 --api-key <key> --served-model-name Qwen/Qwen3-32B`), port 8000 exposed as HTTP, and a volume for the model files. Then:

```sh
kite worker add-runpod runpod-a100 --pod-id <pod id> --model qwen3-32b=Qwen/Qwen3-32B \
  --daily-cap 5 --monthly-cap 40          # asks for the RunPod API key (pods:write) and the server's key
sudo systemctl restart kite-gateway
kite worker start runpod-a100             # or just chat with model qwen3-32b
kite worker stop runpod-a100
```

Kite starts the pod through RunPod's REST API v2 when a request for its models arrives from the chat (and no free Kaggle worker could take it), waits until the model server answers, and stops the pod after `--idle-minutes` (30) without a job, at the daily or monthly cap, or when it is not ready within 15 minutes. The hours it runs times `--price-hour` (default: the price RunPod reports) go to the spend ledger like an API's tokens. Answers are streamed from the pod, because RunPod's proxy closes a request that sends nothing for 100 seconds. Kite never creates or deletes pods.

### Adding a model, chat-only models

A model the workers should serve must be in the notebook's `ROSTER`, with its dataset attached to each worker's notebook. Then:

```sh
kite model add r1-distill --provider kaggle-han --context 32768 --chat-only   # edits the config, keeps a backup
kite provider sync && sudo systemctl restart kite-gateway kite-workflow-worker
kite chat setup --force && kite chat reload
```

`--chat-only` puts it in `routing.chat_only`: Kite never gives it a sandbox task (a task naming it is refused), and the chat gets a second primary agent, `suy-luan`, that runs on it with no tools at all; Tab switches between it and the lead agent. This is for a reasoning model that is no agent, such as DeepSeek-R1-Distill-Qwen-32B (`r1-distill` in the roster, dataset `kite-gguf-r1-distill` from `unsloth/DeepSeek-R1-Distill-Qwen-32B-GGUF`, `Q4_K_M`): good at thinking a question through, not at calling tools.

### Disk

Every task, sandbox run and bench run keeps a clone of the repository, its installed libraries and the code map next to its results and logs. `kite cleanup` lists those big folders for runs unchanged for `--days` (default 7), including bench folders written with `--out` into the home folder, and removes them with `--yes`; `result.json`, `result.patch`, the logs, `trace.jsonl` and bench `results.json`/`README.md` stay. A task cleaned up this way can no longer be continued with `send_instruction`. `--deps-cache` also removes cached libraries that no kept task links to (they are installed again when needed). Do not use `docker system prune`: it also removes the sandbox network while no container uses it.

### Notifications, repository memory, cost per task

- `kite notify setup` picks a random ntfy topic (the only key to it), keeps it in a 0600 secret file and turns on one push message per finished or failed task: task id, outcome, minutes, GPU minutes; never code or the goal. Subscribe to the printed topic in the ntfy app, restart `kite-workflow-worker`, then `kite notify test`. `--off` turns it off.
- A `bad` note on a task (chat `note_task`, or `kite journal note TASK --bad "why"`) becomes a line in `<state>/memory/<repo>.md`; later tasks on the same repository get the newest lines in their goal. `kite memory show <repo>` prints them; edit or delete lines in that file freely.
- Each task's `result.json`, summary and journal line carry its cost: model calls, GPU seconds on the workers (Kaggle's quota), tokens, the agent's turns, tool calls and re-reads of something it had already read, and the patch size. `kite bench` reports them per task (patch size next to the real fix's) and `kite bench history` per run.

### Everything from the chat

The full-test day (step 2) needed a terminal for half of it. The lead agent now has a tool for each of those jobs, and asks before any that changes the machine (`confirm=true`):

- Task IDs: an empty `task_id` means the newest task, and any unique part of an ID works (its last 4 characters, `short_id`); a wrong ID gets the recent IDs back instead of a bare "not found".
- `cancel_task` (and `kite workflow cancel`) first writes `<task>/cancel_requested`: every container of the task is killed, its model requests are dropped at the gateway (`POST /kite/cancel`, loopback only) and the run stops at its next step, so the worker is free within seconds. Pause still only stops the agent.
- `wait_task` answers when a task ends ("xong thì báo"); `task_status` says what a queued task waits for and what the agent did last (`vừa sửa services/agent/cli.py (12 thao tác)`); `task_log` tells the whole story.
- `start_task` refuses a repository with uncommitted changes to tracked files (the task would run on the old version without saying so) and says how to go on. `continue_from=<task>` starts a follow-up from that task's result (its patch goes into the clone first): "chưa tốt, sửa X" no longer means apply, commit and start over. `apply_result` of the follow-up swaps out the previous patch if it is applied.
- A repository without `.kite.yml` gets the config Kite proposes saved in `<state>/memory/<repo>.kite.yml` (never in the repository); `repo_config` shows it and saves a corrected one. A committed `.kite.yml` always wins.
- `remember` keeps what the person asks the chat to keep in mind: for every chat (`<state>/memory/_nguoi-dung.md`, written into the chat's AGENTS.md) or as a lesson for one repository. `memory_show` and `memory_forget` (and `kite memory forget <repo> <n>`) manage them.
- `doctor`, `cleanup`, `worker_restart`, `update_kite`, `bench_start`, `bench_history`: the same as the CLI. `doctor` also shows the GPU hours left this week (`kaggle quota`). Only one measurement run at a time (`<state>/bench.lock`); an attempt that failed because of the machines (no worker, a worker that stopped, a KV cache shared with another request) is run again once after a worker is back, and the result says so (`infra_rerun`). `kite update` pulls, runs the tests (the old commit comes back if they fail), restarts the services and rewrites the chat files; `kite worker restart <worker>` ends the Kaggle session and starts it again with this repository's notebook.
- The lead agent has no Code Mode (`"execute": "deny"`), so OpenCode lists Kite's tools directly: through Code Mode the model wrote invalid JavaScript to call them.

The worker reports `WORKER_VERSION` (a hash of the notebook's code cells): `kite doctor` and `kite worker status` say when a notebook started by hand runs older code. Models with `"parallel": 2` in the roster (qwen3.6) start llama-server with `--parallel 2 --kv-unified` (one 128K KV cache shared by two requests; back to 1 if this llama-server refuses the flag) and the worker takes two jobs at once on the loaded model, so a chat answer does not wait behind a task's long request. Every request is streamed from llama-server, so a cancelled task or a gone client stops generating at once.

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
