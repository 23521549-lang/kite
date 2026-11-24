"""Local queue and patch-review CLI for the worker-scheduler prototype."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import re
import secrets as token_secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable

from .config import (ConfigError, LIVE_ADAPTERS, gateway_models, gateway_workers, load_config,
                     model_contexts, model_policy_from_config, redact_config, refresh_configured_providers,
                     resolve_secret, sandbox_settings_from_config, sync_config_to_store, validate_config)
from .integration import StaleWorkerResult, build_review_bundle
from .journal import Journal, show_task, summarize, task_usage
from .protocol import ScheduledTask, WorkerResult
from .sandbox import SandboxError
from .scheduler import WorkerScheduler
from .store import SQLiteSchedulerStore, StoreError
from .workspace import (
    WorkspaceError,
    checkpoint_path_for_task,
    inspect_task_workspace,
    prepare_task_workspace,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kite", description="Kite provider-neutral coding control plane")
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite state file (default: scheduler.sqlite3 beside --config)")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("KITE_CONFIG") or ".kite/config.yaml"),
                        help="Kite YAML/JSON configuration (default: $KITE_CONFIG, else .kite/config.yaml)")
    commands = parser.add_subparsers(dest="command", required=True)
    task = commands.add_parser("task", help="manage queued tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    add = task_commands.add_parser("add", help="queue a task without starting a model worker")
    add.add_argument("--task-id", help="stable task ID (generated when omitted)")
    add.add_argument("--goal", required=True)
    add.add_argument("--repo-head", required=True, help="pinned Git HEAD for review safety")
    add.add_argument("--branch", help="isolated task branch (default: agent/<task-id>)")
    add.add_argument("--role", default="coder")
    add.add_argument("--task-type", default="coding",
                     help="kind of work (coding, analysis, review, vision, docs) used to pick a model")
    add.add_argument("--capability", action="append", default=[],
                     help="capability a worker must have; repeat for several")
    add.add_argument("--model")
    add.add_argument("--estimate-gpu-seconds", type=int, default=900)
    add.add_argument("--priority", type=int, default=0)

    task_commands.add_parser("list", help="list persisted tasks")
    show = task_commands.add_parser("show", help="show a task and its checkpoint coordinates")
    show.add_argument("task_id")
    cancel = task_commands.add_parser("cancel", help="cancel a queued or leased task")
    cancel.add_argument("task_id")
    prepare = task_commands.add_parser("prepare", help="create an isolated worktree and checkpoint")
    prepare.add_argument("task_id")
    prepare.add_argument("--repo", type=Path, required=True, help="clean local Git repository at the pinned HEAD")
    inspect = task_commands.add_parser("inspect", help="read repo orientation files and checkpoint context")
    inspect.add_argument("task_id")

    review = commands.add_parser("review", help="validate a worker result for human review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    result = review_sub.add_parser("result", help="build a review bundle from result JSON")
    result.add_argument("result_json", type=Path)
    result.add_argument("--expected-head", required=True)
    result.add_argument("--expected-task", required=True)
    result.add_argument("--expected-lease", required=True)
    result.add_argument("--expected-worker", required=True)
    doctor = commands.add_parser("doctor", help="check that Kite works on this machine: services, gateway, "
                                                "workers, Temporal, Docker, ports, secrets, disk")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    cleanup = commands.add_parser("cleanup", help="free disk: remove the clones, libraries and code maps of tasks "
                                                  "and bench runs unchanged for --days (results and logs stay)")
    cleanup.add_argument("--days", type=float, default=7, help="only folders unchanged this long (default 7)")
    cleanup.add_argument("--deps-cache", action="store_true",
                         help="also remove cached libraries no kept task uses (they are reinstalled when needed)")
    cleanup.add_argument("--yes", action="store_true", help="remove (default: only list what would go)")
    notify_parser = commands.add_parser("notify", help="push notifications (ntfy) when a task finishes")
    notify_commands = notify_parser.add_subparsers(dest="notify_command", required=True)
    notify_setup = notify_commands.add_parser(
        "setup", help="pick a random ntfy topic, keep it in a 0600 secret file and turn notifications on")
    notify_setup.add_argument("--server", default="https://ntfy.sh", help="ntfy server (default https://ntfy.sh)")
    notify_setup.add_argument("--topic", default=None, help="topic name (default: a random one; it is the only key)")
    notify_setup.add_argument("--off", action="store_true", help="turn notifications off")
    notify_commands.add_parser("test", help="send a test notification")
    memory_parser = commands.add_parser("memory", help="lessons Kite keeps per repository (from your bad notes)")
    memory_commands = memory_parser.add_subparsers(dest="memory_command", required=True)
    memory_show = memory_commands.add_parser("show", help="the lessons for one repository and their file")
    memory_show.add_argument("repo", help="path of the repository")
    memory_forget = memory_commands.add_parser("forget", help="remove one lesson (its number in `memory show`)")
    memory_forget.add_argument("repo", help="path of the repository")
    memory_forget.add_argument("number", type=int, help="1 = the oldest lesson")
    update = commands.add_parser("update", help="update Kite on this machine: git pull, run the tests, restart the "
                                                "services (the old commit comes back if the tests fail)")
    update.add_argument("--yes", action="store_true", help="do it (default: only show what is new)")
    update.add_argument("--no-restart", action="store_true", help="pull and test, but restart nothing")
    commands.add_parser("busy", help="what an update or a restart would cut off: running tasks and a bench run "
                                     "(JSON; kite-stack asks before it updates)")
    stack = commands.add_parser("stack", help="Kite in the Docker Compose deployment (deploy/stack): run inside the "
                                              "Kite image, or on the old machine to move it")
    stack_commands = stack.add_subparsers(dest="stack_command", required=True)
    stack_init = stack_commands.add_parser("init", help="write the config of a new machine (never overwrites one)")
    stack_init.add_argument("--public-url", default=None, help="https:// address Kaggle notebooks reach /worker/* at")
    stack_init.add_argument("--sandbox-gateway", default=None,
                            help="the host's address on the sandbox network (kite-stack creates the network)")
    stack_health = stack_commands.add_parser("health", help="health check of one service (the containers' HEALTHCHECK)")
    stack_health.add_argument("service", choices=["gateway", "worker", "web", "opencode"])
    stack_commands.add_parser("opencode", help="start of the chat's container: rewrite the chat's files, then run "
                                               "opencode serve --service")
    stack_commands.add_parser("status", help="the running release, the newest one, the last update (kite-stack)")
    stack_request = stack_commands.add_parser("request", help="ask kite-stack on the host to update, check or restart")
    stack_request.add_argument("action", choices=["update", "check", "restart"])
    stack_request.add_argument("services", nargs="*", help="for restart: gateway, worker, web, opencode")
    stack_request.add_argument("--force", action="store_true", help="update even while a task or a bench runs")
    stack_export = stack_commands.add_parser(
        "export", help="on the old machine: one file with the config, secrets, journal, memory, task records, the "
                       "library cache, the chat folder and the repositories Kite works on (for kite-stack import)")
    stack_export.add_argument("--out", type=Path, default=Path.home() / "kite-export.tgz")
    stack_export.add_argument("--repo", action="append", type=Path, default=[],
                              help="repository to take along (default: the ones in the web's Repo tab)")
    stack_export.add_argument("--no-cache", action="store_true", help="leave the library cache (deps-cache) behind")
    stack_export.add_argument("--no-chat-history", action="store_true", help="leave OpenCode's chat sessions behind")
    stack_import = stack_commands.add_parser("import", help="on the new machine (kite-stack import runs this): put an "
                                                            "export in place")
    stack_import.add_argument("archive", type=Path)
    stack_import.add_argument("--public-url", default=os.environ.get("KITE_PUBLIC_URL") or None)
    stack_import.add_argument("--sandbox-gateway", default=None, help="this machine's address on the sandbox network")
    stack_import.add_argument("--force", action="store_true", help="overwrite the tasks this machine already has")
    model = commands.add_parser("model", help="models the workers serve")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_add = model_commands.add_parser(
        "add", help="let pull workers serve one more model (edits the config, keeps a backup); the model must "
                    "be in the notebook ROSTER and its dataset attached on Kaggle")
    model_add.add_argument("model_id", help="the notebook ROSTER name, e.g. r1-distill")
    model_add.add_argument("--provider", action="append", required=True,
                           help="pull worker that serves it (repeat for more; each needs the dataset attached)")
    model_add.add_argument("--context", type=int, help="context window in tokens (the ROSTER ctx), e.g. 32768")
    model_add.add_argument("--chat-only", action="store_true",
                           help="only for chatting (routing.chat_only): Kite never gives it a sandbox task")
    setup = commands.add_parser("setup", help="prepare local secret reference files without printing values")
    setup.add_argument("--secret-name", help="optional secret to capture without echo, e.g. KAGGLE_TOKEN")
    provider = commands.add_parser("provider", help="validate and manage configured providers")
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    provider_commands.add_parser("validate", help="validate configuration without contacting providers")
    provider_commands.add_parser("refresh", help="refresh configured adapters (read-only; never enables providers)")
    enable = provider_commands.add_parser("enable", help="enable a configured provider adapter")
    enable.add_argument("provider_id")
    provider_commands.add_parser(
        "sync", help="register configured providers as owners/workers in the scheduler store")
    workflow = commands.add_parser("workflow", help="operate Temporal coding workflows")
    workflow_commands = workflow.add_subparsers(dest="workflow_command", required=True)
    start = workflow_commands.add_parser("start", help="start a coding workflow")
    start.add_argument("--task-id", required=True)
    start.add_argument("--goal", required=True)
    start.add_argument("--repo", type=Path, required=True)
    start.add_argument("--repo-head", help="commit to work from (default: the repository's current HEAD)")
    start.add_argument("--branch", help="review branch name (default: agent/<task-id>)")
    start.add_argument("--capability", action="append", default=[])
    start.add_argument("--role", default="coder")
    start.add_argument("--task-type", default="coding")
    start.add_argument("--model")
    start.add_argument("--estimate-gpu-seconds", type=int, default=900)
    start.add_argument("--priority", type=int, default=0)
    for action in ("status", "pause", "resume", "cancel", "wake"):
        sub = workflow_commands.add_parser(action)
        sub.add_argument("workflow_id")
        if action == "resume":
            sub.add_argument("--instruction", default="")
    workflow_commands.add_parser("worker", help="run the Temporal worker that executes tasks in the sandbox")
    workflow_result = workflow_commands.add_parser("result", help="wait for a workflow and print its review bundle")
    workflow_result.add_argument("workflow_id")
    gateway = commands.add_parser("gateway", help="model gateway that pull workers (Kaggle) serve")
    gateway_commands = gateway.add_subparsers(dest="gateway_command", required=True)
    gateway_commands.add_parser("serve", help="run the gateway on loopback; publish /worker/* through Caddy")
    oc = gateway_commands.add_parser("opencode-config", help="print the opencode.json block for the gateway")
    oc.add_argument("--context", type=int, default=None,
                    help="one context limit for every model (default: gateway.model_context, else 32768)")
    worker = commands.add_parser("worker", help="manage pull workers")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    token = worker_commands.add_parser("token", help="create a worker token (printed once)")
    token.add_argument("worker_id")
    token.add_argument("--force", action="store_true", help="replace an existing token (rotation)")
    rotate = worker_commands.add_parser("rotate-token", help="rotate a pull worker's long-lived token: "
                                                     "replace the stored token, print the new one once, "
                                                     "and say how to update the worker's Kaggle Secret")
    rotate.add_argument("worker_id")
    add = worker_commands.add_parser("add", help="add a pull worker (e.g. another Kaggle account) to the config, "
                                                 "copying an existing worker's models and settings")
    add.add_argument("worker_id", help="new worker id, e.g. kaggle-an (lowercase, digits, '-')")
    add.add_argument("--owner", required=True, help="who owns the account (its GPU quota is counted per owner)")
    add.add_argument("--like", default=None, help="existing worker to copy (default: the first pull worker)")
    autostart = worker_commands.add_parser(
        "autostart", help="let Kite run this worker's Kaggle notebook when work arrives (asks for the "
                          "Kaggle API token without echo; edits the config, keeps a backup)")
    autostart.add_argument("worker_id")
    autostart.add_argument("--kernel", required=True,
                           help="<kaggle user>/<notebook slug> from the notebook URL kaggle.com/code/...")
    autostart.add_argument("--idle-minutes", type=int, default=30,
                           help="a notebook Kite started ends after this long without a job (default 30)")
    autostart.add_argument("--token", choices=["per-launch", "kaggle-secret"], default="per-launch",
                           help="worker token: a short-lived token Kite writes into the notebook it pushes "
                                "(default), or the notebook's Kaggle Secret (Kaggle does not give Secrets "
                                "to a run pushed through its API)")
    autostart.add_argument("--model", default=None, help="model to load first when nothing asks for one")
    autostart.add_argument("--via", choices=["api", "browser"], default=None,
                           help="api: push through the Kaggle API (may queue for hours); browser: open the "
                                "notebook in Kite's browser and press Save Version there (default: keep, else api)")
    autostart.add_argument("--notebook", choices=["stub", "full"], default=None,
                           help="what a push sends: stub (one cell that downloads the worker code from Kite; "
                                "default) or full (this repository's whole notebook)")
    autostart.add_argument("--off", action="store_true", help="remove autostart from this worker")
    start = worker_commands.add_parser("start", help="start the worker's Kaggle notebook now (autostart)")
    start.add_argument("worker_id")
    start.add_argument("--model", default=None, help="model to load first")
    start.add_argument("--force", action="store_true",
                       help="push a new run even if one seems to be starting or failed recently")
    stop = worker_commands.add_parser("stop", help="ask a worker to finish its job and end its Kaggle session")
    stop.add_argument("worker_id")
    restart = worker_commands.add_parser(
        "restart", help="end the worker's Kaggle session and start it again with this repository's notebook "
                        "(after an update, or when doctor says the notebook is old)")
    restart.add_argument("worker_id")
    restart.add_argument("--model", default=None, help="model to load first")
    restart.add_argument("--wait-minutes", type=float, default=20, help="how long to wait for the session to end")
    add_api = worker_commands.add_parser(
        "add-api", help="add a worker Kite calls itself: an OpenAI-compatible API (OpenAI, OpenRouter, Groq, "
                        "DeepSeek, Gemini, vLLM, Ollama...) or Anthropic; asks for the API key without echo")
    add_api.add_argument("worker_id", help="new worker id, e.g. openrouter (lowercase, digits, '-')")
    add_api.add_argument("--kind", choices=["openai", "anthropic"], required=True)
    add_api.add_argument("--base-url", default=None,
                         help="the API's base URL, e.g. https://openrouter.ai/api/v1 (Anthropic: its default)")
    add_api.add_argument("--model", action="append", required=True, metavar="NAME[=PROVIDER_MODEL]",
                         help="a model this worker serves, under the name Kite uses; '=...' gives the provider's "
                              "own name (repeat for more models)")
    add_api.add_argument("--price-in", type=float, default=0.0, help="USD per million prompt tokens (0: free)")
    add_api.add_argument("--price-out", type=float, default=0.0, help="USD per million completion tokens")
    add_api.add_argument("--daily-cap", type=float, default=None, help="USD per day; then the worker stops")
    add_api.add_argument("--monthly-cap", type=float, default=None, help="USD per month; then the worker stops")
    add_api.add_argument("--concurrency", type=int, default=2, help="requests at once (default 2)")
    add_api.add_argument("--no-key", action="store_true", help="the API needs no key (a server on this machine)")
    add_pod = worker_commands.add_parser(
        "add-runpod", help="add a rented GPU machine (an existing RunPod pod that serves an OpenAI-compatible "
                           "API, e.g. vLLM): Kite starts it when work arrives and stops it when idle; asks for "
                           "the RunPod API key and the model server's key without echo")
    add_pod.add_argument("worker_id", help="new worker id, e.g. runpod-a100 (lowercase, digits, '-')")
    add_pod.add_argument("--pod-id", required=True, help="the pod's id on RunPod's Pods page")
    add_pod.add_argument("--port", type=int, default=8000, help="the model server's port, exposed as HTTP (default 8000)")
    add_pod.add_argument("--model", action="append", required=True, metavar="NAME[=SERVED_NAME]",
                         help="a model the pod serves, under the name Kite uses; '=...' gives the name the server "
                              "knows (vLLM: --served-model-name); repeat for more")
    add_pod.add_argument("--price-hour", type=float, default=None,
                         help="USD per hour while the pod runs (default: the price RunPod reports)")
    add_pod.add_argument("--idle-minutes", type=int, default=30, help="stop the pod after this long without a job")
    add_pod.add_argument("--daily-cap", type=float, default=None, help="USD per day; then the pod is stopped")
    add_pod.add_argument("--monthly-cap", type=float, default=None, help="USD per month; then the pod is stopped")
    add_pod.add_argument("--concurrency", type=int, default=4, help="requests at once (default 4)")
    add_pod.add_argument("--base-url", default=None,
                         help="the server's base URL (default https://<pod>-<port>.proxy.runpod.net/v1)")
    add_pod.add_argument("--no-key", action="store_true", help="the model server needs no API key")
    stub = worker_commands.add_parser(
        "stub", help="print the bootstrap cell for the worker's Kaggle notebook: paste it in place of the old cells "
                     "once; every run then downloads the newest worker code from Kite")
    stub.add_argument("worker_id")
    browser = worker_commands.add_parser(
        "browser", help="Kite's own browser for kaggle.com: open the worker's notebook there, or switch "
                        "'Kite tự bấm' (Kite presses Save Version itself; off by default)")
    browser.add_argument("worker_id")
    browser.add_argument("--auto-click", choices=["on", "off"], default=None,
                         help="Kite presses Save Version -> Save & Run All -> Save itself when the worker must "
                              "start. Kaggle's terms forbid automated access outside its API: your decision")
    browser.add_argument("--close", action="store_true", help="close the browser now")
    worker_commands.add_parser("spend", help="what each API worker cost today and this month, against its caps")
    worker_status = worker_commands.add_parser("status", help="connected workers and autostart state "
                                                              "(from the gateway)")
    worker_status.add_argument("--json", action="store_true", help="the gateway's full report")
    sandbox = commands.add_parser("sandbox", help="run the coding agent in an isolated Docker sandbox")
    sandbox_commands = sandbox.add_subparsers(dest="sandbox_command", required=True)
    sandbox_commands.add_parser("build", help="create the internal network and build the sandbox image")
    sandbox_commands.add_parser("doctor", help="check sandbox isolation (no Internet, gateway reachable, no secrets)")
    sandbox_run = sandbox_commands.add_parser("run", help="run one task in a sandbox and produce a verified patch")
    sandbox_run.add_argument("--repo", type=Path, required=True, help="Git repository (committed state is used)")
    sandbox_run.add_argument("--goal", required=True, help="what the agent should do")
    sandbox_run.add_argument("--model", help="gateway model (default: first model of the first pull worker)")
    sandbox_run.add_argument("--task-id", help="lowercase id (default: generated)")
    sandbox_run.add_argument("--test-cmd", help="override sandbox.test_command; pass '' to skip tests")
    sandbox_run.add_argument("--setup-cmd", help="override sandbox.setup_command (runs with network "
                                                 "before the agent, e.g. 'pip install -r requirements.txt'); '' to skip")
    mcp = commands.add_parser("mcp", help="MCP server that lets a chat agent drive Kite")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser("serve", help="serve Kite tools over stdio (started by OpenCode)")
    mcp_serve.add_argument("--allow-root", action="append", type=Path, default=[],
                           help="folder whose Git repositories tasks may use; repeat (default: home)")
    mcp_serve.add_argument("--lead-model", default=None,
                           help="model the chat uses: started on a worker as soon as the chat opens")
    mcp_serve.add_argument("--chat-dir", type=Path, default=None,
                           help="the chat folder (its AGENTS.md gets what the person asks the chat to remember)")
    chat = commands.add_parser("chat", help="Vietnamese chat with the Kite lead agent (OpenCode)")
    chat_commands = chat.add_subparsers(dest="chat_command", required=True)
    chat_setup = chat_commands.add_parser("setup", help="create a chat folder with opencode.json and AGENTS.md")
    chat_setup.add_argument("--dir", type=Path, default=Path.home() / "kite-chat")
    chat_setup.add_argument("--model", help="lead model (default: qwen3.6-fast when configured)")
    chat_setup.add_argument("--allow-root", action="append", type=Path, default=[],
                            help="folder whose Git repositories tasks may use; repeat (default: home)")
    chat_setup.add_argument("--force", action="store_true", help="overwrite opencode.json and AGENTS.md")
    chat_setup.add_argument("--no-reload", action="store_true",
                            help="only write the files (do not stop a running OpenCode: used by kite update)")
    chat_setup.add_argument("--clear-approvals", action="store_true",
                            help="remove shell commands OpenCode was told to 'Always allow' (database is backed up)")
    chat_commands.add_parser("reload", help="stop the OpenCode background server and old Kite MCP servers "
                                            "so the next `opencode` loads the current Kite tools")
    bench = commands.add_parser("bench", help="measure the Kaggle models on Kite's jobs (lead agent, code, speed)")
    bench_commands = bench.add_subparsers(dest="bench_command", required=True)
    bench_run = bench_commands.add_parser("run", help="run the benchmark through the gateway (worker must be up)")
    bench_run.add_argument("--models", default="", help="comma-separated; default: every base model")
    bench_run.add_argument("--suites", default="speed,lead,code", help="comma-separated: speed, lead, code")
    bench_run.add_argument("--only", default="", help="comma-separated scenario/task ids, e.g. L04,L16,C1-bugfix")
    bench_run.add_argument("--out", type=Path, help="output folder (default: <state>/bench/<time>)")
    bench_run.add_argument("--repo-source", type=Path,
                           help="local clone of github.com/23521549-lang/aiops-traffic-shaper: the code suite "
                                "then runs the real-repository tasks (R1-R4) instead of C1-C8")
    bench_run.add_argument("--label", default="",
                           help="name of this run in `kite bench history`, e.g. v6 (default: its folder name)")
    bench_run.add_argument("--polyglot", type=Path,
                           help="local clone of github.com/Aider-AI/polyglot-benchmark: the code suite then "
                                "runs its Python exercises (P-<name>) the way Aider's leaderboard does")
    bench_run.add_argument("--polyglot-count", type=int, default=0,
                           help="only the first N polyglot exercises (by name; default: all 34)")
    bench_run.add_argument("--no-harness", action="store_true",
                           help="code suite without fix rounds, self-review and repo context (baseline)")
    bench_run.add_argument("--no-code-map", action="store_true",
                           help="harness on, but without the graphify code map (to measure what it adds)")
    bench_run.add_argument("--no-fast-path", action="store_true",
                           help="harness on, but without the one-call fast path (to measure what it adds)")
    bench_run.add_argument("--worker", default="",
                           help="send every request to this worker (e.g. kaggle-han): comparable timings, and "
                                "the other worker stays free for real tasks")
    bench_history = bench_commands.add_parser(
        "history", help="every bench run so far as one table (markdown): what each change did")
    bench_history.add_argument("--root", type=Path, help="folder of bench runs (default: <state>/bench and the "
                                                         "home folder)")
    bench_history.add_argument("--json", action="store_true", help="the rows as JSON instead")
    bench_apply = bench_commands.add_parser("apply", help="turn bench results into routing scores (dry run "
                                                          "unless --write)")
    bench_apply.add_argument("results", nargs="+", type=Path, help="results.json files (lead, code...)")
    bench_apply.add_argument("--write", action="store_true", help="write routing.model_skill / chat_only "
                                                                  "into the config (a backup is kept)")
    web = commands.add_parser("web", help="Kite's web page: one owner account with 2FA, an API for every tab")
    web_commands = web.add_subparsers(dest="web_command", required=True)
    web_admin = web_commands.add_parser(
        "admin", help="create the owner account (password + 2FA app), or with --reset give it a new password "
                      "and 2FA (for a lost phone; it signs every browser out)")
    web_admin.add_argument("--username", default=None, help="account name (default: the existing one, else 'admin')")
    web_admin.add_argument("--reset", action="store_true", help="new password and 2FA for the existing account")
    web_admin.add_argument("--web-db", default=os.environ.get("KITE_WEB_DB") or None,
                           help="database URL (default: $KITE_WEB_DB, else SQLite in the state folder)")
    web_serve = web_commands.add_parser("serve", help="run the web back end on loopback (Caddy publishes it over HTTPS)")
    web_serve.add_argument("--host", default="127.0.0.1")
    web_serve.add_argument("--port", type=int, default=8790)
    web_serve.add_argument("--web-db", default=os.environ.get("KITE_WEB_DB") or None,
                           help="database URL (default: $KITE_WEB_DB, else SQLite in the state folder; Postgres: "
                                "postgresql+psycopg://user:pass@host/db)")
    web_serve.add_argument("--allow-root", action="append", type=Path, default=[],
                           help="folders whose Git repositories tasks may use (default: the home folder)")
    web_serve.add_argument("--insecure-cookies", action="store_true",
                           help="cookies without Secure, for a plain-http test on this machine only")
    web_build = web_commands.add_parser("build", help="build the page (web/ → web/dist) with the ui extra's Node.js; "
                                                      "kite update does this when the page changed")
    web_build.add_argument("--force", action="store_true", help="build even when the sources did not change")
    journal = commands.add_parser("journal", help="what Kite did: model requests, task runs, lead-agent "
                                                  "actions, your notes (to trace and improve)")
    journal_commands = journal.add_subparsers(dest="journal_command", required=True)
    summary = journal_commands.add_parser("summary", help="success rates, time, tokens, failure reasons")
    summary.add_argument("--days", type=float, default=14, help="look back this many days (0 = everything)")
    show = journal_commands.add_parser("show", help="everything recorded about one task")
    show.add_argument("task_id")
    note = journal_commands.add_parser("note", help="record your own verdict on a task")
    note.add_argument("task_id")
    verdict = note.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--good", action="store_true", help="the result was right and useful")
    verdict.add_argument("--bad", action="store_true", help="the result was wrong or not usable")
    note.add_argument("text", nargs="?", default="", help="why (what was wrong, what you had to fix)")
    return parser


def _load_operator_config(args: argparse.Namespace):
    return load_config(args.config)


def _worker_add(args: argparse.Namespace) -> int:
    """Copy an existing pull provider into a new one (own id, owner and token file); backup + validate."""
    import copy

    import yaml

    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    providers = raw.get("providers") or []
    pulls = [p for p in providers if p.get("adapter") == "pull"]
    if any(args.worker_id in (p.get("id"), p.get("worker_id")) for p in providers):
        raise ConfigError(f"worker {args.worker_id} already exists")
    template = next((p for p in pulls if args.like in (None, p.get("id"), p.get("worker_id"))), None)
    if template is None:
        raise ConfigError("no pull worker to copy" if args.like is None else f"no pull worker {args.like}")
    new = copy.deepcopy(template)
    new.pop("worker_id", None)
    token_path = args.config.parent / "secrets" / f"worker_token_{args.worker_id}"
    new.update(id=args.worker_id, owner=args.owner, enabled=True,
               secrets={**(template.get("secrets") or {}), "worker_token": f"file:{token_path}"})
    providers.append(new)
    raw["providers"] = providers
    validate_config(raw)
    backup = args.config.with_name(f"{args.config.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(args.config, backup)
    text = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n" if args.config.suffix == ".json"
            else yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    args.config.write_text(text, encoding="utf-8")
    _emit({"added": args.worker_id, "owner": args.owner, "copied_from": template.get("id"),
           "models": new.get("models") or [new.get("model")], "backup": str(backup),
           "next": [f"kite worker token {args.worker_id}",
                    "kite provider sync",
                    "sudo systemctl restart kite-gateway kite-workflow-worker",
                    "Kaggle account mới: chia sẻ 7 dataset, Secrets KITE_WORKER_TOKEN = token vừa in, chạy ô Worker"]})
    return 0


def _worker_add_api(args: argparse.Namespace) -> int:
    """Add a relay provider (relay.py) and keep its API key in a 0600 file; backup + validate."""
    import yaml

    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    providers = raw.get("providers") or []
    if any(args.worker_id in (p.get("id"), p.get("worker_id")) for p in providers):
        raise ConfigError(f"worker {args.worker_id} already exists")
    names, upstream = [], {}
    for item in args.model:
        name, _, target = item.partition("=")
        names.append(name.strip())
        if target.strip():
            upstream[name.strip()] = target.strip()
    relay: dict = {"kind": args.kind, "price": {"input": args.price_in, "output": args.price_out}}
    if args.base_url:
        relay["base_url"] = args.base_url
    if upstream:
        relay["upstream"] = upstream
    caps = {k: v for k, v in (("daily_usd", args.daily_cap), ("monthly_usd", args.monthly_cap)) if v is not None}
    if caps:
        relay["caps"] = caps
    provider = {"id": args.worker_id, "adapter": "relay", "enabled": True, "owner": "self", "model": names[0],
                "models": names[1:], "capabilities": ["coding"],
                "quota": {"gpu_seconds": 0, "concurrency": args.concurrency}, "secrets": {}, "relay": relay}
    key_path = args.config.parent / "secrets" / f"api_key_{args.worker_id}"
    report: dict[str, object] = {"added": args.worker_id, "kind": args.kind, "models": names}
    if not args.no_key:
        provider["secrets"]["api_key"] = f"file:{key_path}"
    raw["providers"] = [*providers, provider]
    validate_config(raw)                      # before asking for the key: a typo costs nothing
    if not args.no_key:
        if key_path.exists():
            report["key"] = f"dùng lại {key_path}"
        else:
            value = getpass.getpass(f"API key cho {args.worker_id} (không hiện ra màn hình): ").strip()
            if not value:
                raise ConfigError("key is empty")
            key_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(key_path.parent, 0o700)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value + "\n")
            report["key"] = f"đã lưu {key_path} (0600)"
    report["backup"] = str(_save_config(args.config, raw))
    if (args.price_in or args.price_out) and not caps:
        report["warning"] = "worker trả tiền chưa có hạn mức chi: thêm --daily-cap / --monthly-cap khi tạo lại"
    report["next"] = ["sudo systemctl restart kite-gateway", "kite worker status", "kite worker spend"]
    _emit(report)
    return 0


def _worker_add_runpod(args: argparse.Namespace) -> int:
    """Add a relay provider with ``relay.pod`` (pods.py); keys in 0600 files; backup + validate."""
    import yaml

    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    providers = raw.get("providers") or []
    if any(args.worker_id in (p.get("id"), p.get("worker_id")) for p in providers):
        raise ConfigError(f"worker {args.worker_id} already exists")
    names, upstream = [], {}
    for item in args.model:
        name, _, target = item.partition("=")
        names.append(name.strip())
        if target.strip():
            upstream[name.strip()] = target.strip()
    pod: dict = {"provider": "runpod", "id": args.pod_id, "port": args.port, "idle_minutes": args.idle_minutes}
    if args.price_hour is not None:
        pod["price_hour"] = args.price_hour
    relay: dict = {"kind": "openai", "pod": pod, "timeout_seconds": 1800}
    if args.base_url:
        relay["base_url"] = args.base_url
    if upstream:
        relay["upstream"] = upstream
    caps = {k: v for k, v in (("daily_usd", args.daily_cap), ("monthly_usd", args.monthly_cap)) if v is not None}
    if caps:
        relay["caps"] = caps
    secrets_dir = args.config.parent / "secrets"
    runpod_key = secrets_dir / f"runpod_api_key_{args.worker_id}"
    server_key = secrets_dir / f"api_key_{args.worker_id}"
    provider = {"id": args.worker_id, "adapter": "relay", "enabled": True, "owner": "self", "model": names[0],
                "models": names[1:], "capabilities": ["coding"],
                "quota": {"gpu_seconds": 0, "concurrency": args.concurrency},
                "secrets": {"runpod_api_key": f"file:{runpod_key}"}, "relay": relay}
    if not args.no_key:
        provider["secrets"]["api_key"] = f"file:{server_key}"
    raw["providers"] = [*providers, provider]
    validate_config(raw)                      # before asking for keys: a typo costs nothing
    report: dict[str, object] = {"added": args.worker_id, "pod": args.pod_id, "models": names}
    asks = [(runpod_key, "RunPod API key (console.runpod.io -> Settings -> API Keys, quyền pods:write)", True)]
    if not args.no_key:
        asks.append((server_key, "API key của model server trên pod (vLLM --api-key)", False))
    for path, label, required in asks:
        if path.exists():
            report[path.name] = "dùng lại file có sẵn"
            continue
        value = getpass.getpass(f"{label} (không hiện ra màn hình): ").strip()
        if not value:
            raise ConfigError(f"{path.name}: key is empty")
        secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(secrets_dir, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
        report[path.name] = f"đã lưu {path} (0600)"
    report["backup"] = str(_save_config(args.config, raw))
    if not caps:
        report["warning"] = "máy thuê chưa có hạn mức chi: thêm --daily-cap / --monthly-cap khi tạo lại"
    report["next"] = ["sudo systemctl restart kite-gateway", f"kite worker start {args.worker_id}",
                      "kite worker status", "kite worker spend"]
    _emit(report)
    return 0


def _worker_stub(args: argparse.Namespace) -> int:
    from .stub import stub_source

    config = _load_operator_config(args)
    provider = next((p for p in config.providers if p.effective_worker_id == args.worker_id), None)
    if provider is None or provider.adapter != "pull":
        raise ConfigError(f"no pull worker {args.worker_id} in the config")
    if not config.gateway.public_url:
        raise ConfigError("gateway.public_url is not set: the notebook needs it to download the worker code")
    kernel = provider.launch.kernel if provider.launch else "<user>/<notebook>"
    print(f"""# Notebook khởi động cho {args.worker_id}. Làm một lần trên Kaggle:
#  1. Mở https://www.kaggle.com/code/{kernel}/edit
#  2. Xóa hết các ô cũ (giữ nguyên Input/dataset, GPU T4 x2, Internet, Secret KITE_WORKER_TOKEN).
#  3. Dán toàn bộ phần dưới vào một ô code, rồi Save Version -> Save & Run All -> Save.
# Từ đó mỗi lần chạy tải code worker mới nhất từ Kite; không phải dán lại sau mỗi lần cập nhật.
""")
    print(stub_source(config.gateway.public_url))
    return 0


def _worker_browser(args: argparse.Namespace) -> int:
    from .browser import set_auto_click

    config = _load_operator_config(args)
    provider = next((p for p in config.providers if p.effective_worker_id == args.worker_id), None)
    if provider is None or provider.adapter != "pull" or provider.launch is None:
        raise ConfigError(f"{args.worker_id} has no notebook: kite worker autostart {args.worker_id} --kernel <user>/<slug>")
    port = config.gateway.port
    page = f"http://127.0.0.1:{port}/kite/browser"
    if args.auto_click is not None:
        set_auto_click(args.config.parent, args.worker_id, args.auto_click == "on")
        _emit({"worker": args.worker_id, "auto_click": args.auto_click == "on",
               "note": ("Kite sẽ tự bấm Save Version khi cần bật worker này (điều khoản Kaggle cấm thao tác tự "
                        "động ngoài API: bạn chịu rủi ro). Tắt: --auto-click off"
                        if args.auto_click == "on" else "Kite chỉ mở notebook; bạn tự bấm Save Version"),
               "via": provider.launch.via})
        return 0
    if args.close:
        _emit(_gateway_call(config, "POST", "/kite/browser/open", {"close": True}, timeout=60))
        return 0
    report = _gateway_call(config, "POST", "/kite/browser/open", {"worker": args.worker_id}, timeout=180)
    _emit({"opened": report.get("url"), "title": report.get("title"), "error": report.get("error") or None,
           "see_it": [f"trên máy của bạn: ssh -L {port}:127.0.0.1:{port} <user>@<máy Kite>",
                      f"rồi mở {page} trong trình duyệt"],
           "then": "đăng nhập Kaggle (email + mật khẩu) nếu được hỏi, bấm Save Version -> Save & Run All -> Save"})
    return 0


def _worker_spend(args: argparse.Namespace) -> int:
    from .relay import SpendLedger
    from .spend_ledger import SpendStore

    config = _load_operator_config(args)
    ledger = SpendLedger(Journal(args.config.parent / "journal").read("spend"),
                         store=SpendStore(args.config.parent / "spend.sqlite3"))
    rows = []
    for p in config.providers:
        if p.adapter != "relay" or p.relay is None:
            continue
        day, month = ledger.spent(p.effective_worker_id)
        row = {"worker": p.effective_worker_id, "enabled": p.enabled, "kind": p.relay.kind,
               "today_usd": round(day, 4), "daily_cap": p.relay.daily_cap,
               "month_usd": round(month, 4), "monthly_cap": p.relay.monthly_cap}
        if p.relay.pod is not None:
            row.update(kind=p.relay.pod.provider, pod=p.relay.pod.pod_id,
                       price_hour=p.relay.pod.price_hour if p.relay.pod.price_hour is not None else "giá RunPod báo")
        else:
            row["price_per_mtok"] = {"input": p.relay.price_in, "output": p.relay.price_out}
        rows.append(row)
    _emit({"workers": rows, "note": "giờ UTC; API: token mỗi câu trả lời nhân giá bạn nhập; máy thuê: số giờ "
                                    "máy bật nhân giá theo giờ"})
    return 0


def _save_config(path: Path, raw: dict) -> Path:
    """Validate, keep a timestamped backup, write; returns the backup path."""
    import yaml

    validate_config(raw)
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    text = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n" if path.suffix == ".json"
            else yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    path.write_text(text, encoding="utf-8")
    return backup


def _model_add(args: argparse.Namespace) -> int:
    """Add a model to pull workers' ``models`` (and its context window, and chat_only if asked)."""
    import yaml

    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    providers = raw.get("providers") or []
    changed = []
    for wanted in dict.fromkeys(args.provider):
        provider = next((p for p in providers if wanted in (p.get("id"), p.get("worker_id"))), None)
        if provider is None or provider.get("adapter") != "pull":
            raise ConfigError(f"no pull worker {wanted} in the config")
        models = list(provider.get("models") or [])
        if args.model_id != provider.get("model") and args.model_id not in models:
            provider["models"] = models + [args.model_id]
            changed.append(provider.get("id"))
    gateway = raw.setdefault("gateway", {})
    if args.context is not None:
        gateway.setdefault("model_context", {})[args.model_id] = args.context
    if args.chat_only:
        routing = raw.setdefault("routing", {})
        routing["chat_only"] = list(dict.fromkeys([*(routing.get("chat_only") or []), args.model_id]))
    backup = _save_config(args.config, raw)
    _emit({"model": args.model_id, "added_to": changed, "context": args.context, "chat_only": args.chat_only,
           "backup": str(backup),
           "next": ["kite provider sync", "sudo systemctl restart kite-gateway kite-workflow-worker",
                    "kite chat setup --force && kite chat reload",
                    f"Kaggle: gắn dataset của {args.model_id} (Add Input) vào notebook của "
                    + ", ".join(args.provider)]})
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    from . import cleanup

    state = args.config.parent
    items = cleanup.plan(state, days=args.days, deps_cache=args.deps_cache, extra_roots=[Path.home()])
    by_kind: dict[str, list] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)
    total = sum(item.size for item in items)
    print(f"{state}: {len(items)} thư mục không đổi từ {args.days:g} ngày trở lên, tổng {cleanup.human(total)}"
          + ("" if args.yes else " (chưa xoá: thêm --yes để xoá)"))
    for kind, group in by_kind.items():
        print(f"  {kind}: {len(group)} thư mục, {cleanup.human(sum(i.size for i in group))}")
    for item in items[:10]:
        print(f"    {cleanup.human(item.size):>10}  {item.path}  ({item.age_days:.0f} ngày)")
    if args.yes and items:
        freed, errors = cleanup.remove(items)
        print(f"Đã xoá {cleanup.human(freed)}." + (f" Lỗi {len(errors)}:" if errors else ""))
        for error in errors[:10]:
            print("  " + error)
        return 1 if errors else 0
    return 0


def _worker_autostart(args: argparse.Namespace) -> int:
    """Add (or remove) ``launch`` for a pull worker and store its Kaggle API token as a 0600 file."""
    import yaml

    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    providers = raw.get("providers") or []
    provider = next((p for p in providers if args.worker_id in (p.get("id"), p.get("worker_id"))), None)
    if provider is None or provider.get("adapter") != "pull":
        raise ConfigError(f"no pull worker {args.worker_id} in the config")
    report: dict[str, object] = {"worker": args.worker_id}
    if args.off:
        provider.pop("launch", None)
        report["autostart"] = False
    else:
        secrets_dir = args.config.parent / "secrets"
        token_path = secrets_dir / f"kaggle_api_token_{args.worker_id}"
        reference = (provider.get("secrets") or {}).get("kaggle_api_token")
        if reference is None and not token_path.exists():
            print("Kaggle API token của tài khoản sở hữu notebook (kaggle.com/settings/api -> "
                  "Generate New Token). Nội dung không hiện ra màn hình.", file=sys.stderr)
            value = getpass.getpass("Kaggle API token: ").strip()
            if not value:
                raise ConfigError("token is empty")
            secrets_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(secrets_dir, 0o700)
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value + "\n")
            report["token_file"] = str(token_path)
        if reference is None:
            provider.setdefault("secrets", {})["kaggle_api_token"] = f"file:{token_path.resolve()}"
        old = provider.get("launch") or {}
        launch = {"kernel": args.kernel, "idle_minutes": args.idle_minutes, "token": args.token}
        if args.model:
            launch["model"] = args.model
        for key, value in (("via", args.via or old.get("via")), ("notebook", args.notebook or old.get("notebook"))):
            if value:
                launch[key] = value
        provider["launch"] = launch
        report.update(autostart=True, launch=launch)
        if args.token == "kaggle-secret":
            report["warning"] = ("launch.token: kaggle-secret: token worker trong Kaggle Secret không tự "
                                 "hết hạn; nếu nghi lộ, xoay vòng bằng `kite worker rotate-token` rồi "
                                 "cập nhật Kaggle Secret")
    validate_config(raw)
    backup = args.config.with_name(f"{args.config.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(args.config, backup)
    text = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n" if args.config.suffix == ".json"
            else yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    args.config.write_text(text, encoding="utf-8")
    report["backup"] = str(backup)
    report["next"] = ["sudo systemctl restart kite-gateway", f"kite worker start {args.worker_id}",
                      "kite worker status"] if not args.off else ["sudo systemctl restart kite-gateway"]
    _emit(report)
    return 0


def _gateway_call(config, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    """Call the gateway's local-only routes (/status, /kite/*) on loopback."""
    import urllib.error
    import urllib.request

    url = f"http://{config.gateway.host}:{config.gateway.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ConfigError(f"gateway HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise ConfigError(f"không gọi được gateway {url}: {exc} (sudo systemctl status kite-gateway)") from exc


def _worker_remote(args: argparse.Namespace) -> int:
    config = _load_operator_config(args)
    if args.worker_command == "status":
        report = _gateway_call(config, "GET", "/status")
        if not args.json:
            print(_worker_status_text(report))
            return 0
    elif args.worker_command == "start":
        report = _gateway_call(config, "POST", "/kite/wake",
                               {"worker": args.worker_id, "model": args.model, "reason": "manual",
                                "force": args.force}, timeout=600)
    else:
        report = _gateway_call(config, "POST", "/kite/stop", {"worker": args.worker_id})
    _emit(report)
    return 0


def _worker_restart(args: argparse.Namespace) -> int:
    """Stop the worker's Kaggle session (it finishes its current request), wait until it is gone, then
    start it again through autostart, which pushes this repository's notebook."""
    config = _load_operator_config(args)
    _gateway_call(config, "POST", "/kite/stop", {"worker": args.worker_id})
    deadline = time.monotonic() + args.wait_minutes * 60
    print(f"Đã yêu cầu {args.worker_id} dừng; chờ phiên Kaggle kết thúc...", flush=True)
    while True:
        report = _gateway_call(config, "GET", "/status")
        worker = next((w for w in report.get("workers") or [] if w.get("worker_id") == args.worker_id), None)
        if worker is None:
            raise ConfigError(f"không có worker {args.worker_id}")
        if not worker.get("alive"):
            break
        if time.monotonic() > deadline:
            raise ConfigError(f"{args.worker_id} vẫn chạy sau {args.wait_minutes:g} phút (đang làm một request dài?)")
        time.sleep(10)
    print("Worker đã dừng; bật lại với notebook mới...", flush=True)
    _emit(_gateway_call(config, "POST", "/kite/wake", {"worker": args.worker_id, "model": args.model,
                                                      "reason": "restart", "force": True}, timeout=600))
    return 0


def _chat_setup_again(config_path: Path) -> dict:
    """After an update: rewrite the chat's files with the NEW code (a new process), keeping its roots
    and lead model, without stopping the OpenCode that may be running this very update."""
    chat_dir = Path.home() / "kite-chat"
    try:
        command = json.loads((chat_dir / "opencode.json").read_text(encoding="utf-8"))["mcp"]["kite"]["command"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"chat": "chưa có ~/kite-chat (bỏ qua)"}
    extra = []
    for i, value in enumerate(command[:-1]):
        if value == "--lead-model":
            extra += ["--model", command[i + 1]]
        elif value == "--allow-root":
            extra += ["--allow-root", command[i + 1]]
    done = subprocess.run([*_kite_executable(), "--config", str(config_path), "chat", "setup", "--force",
                           "--no-reload", "--dir", str(chat_dir), *extra], capture_output=True, text=True)
    return {"chat": "đã viết lại opencode.json và AGENTS.md" if done.returncode == 0
            else "chat setup lỗi: " + (done.stderr or done.stdout).strip()[-300:]}


def _update_command(args: argparse.Namespace) -> int:
    from . import stack, update

    if stack.in_compose():
        # The image cannot update itself: kite-stack on the host pulls the new image and checks it.
        if not args.yes:
            report = stack.update_plan()
            report["next"] = "kite update --yes (kite-stack tải image mới, kiểm tra rồi chạy)" if report["behind"] \
                else "Kite đã là bản mới nhất (kite-stack kiểm tra mỗi 10 phút)"
        else:
            report = stack.request("update")
            report["next"] = "kite stack status (vài phút sau)"
        _emit(report)
        return 0
    if not args.yes:
        report = update.plan()
        if not report.get("error"):
            report["next"] = ("kite update --yes" if report["behind"] else "Kite đã là bản mới nhất")
        _emit(report)
        return 0
    report = update.run(restart=not args.no_restart, after=lambda: _chat_setup_again(args.config.resolve()))
    if report.get("updated"):
        report["next"] = ["thoát opencode, chạy `kite chat reload`, rồi mở lại để có tool mới"]
        if report.get("notebook_changed"):
            report["next"].append("notebook worker đổi: kite worker restart <worker> (hoặc nhờ chat)")
    _emit(report)
    return 0 if report.get("updated") or not report.get("error") else 1


def _worker_status_text(report: dict) -> str:
    minutes = lambda seconds: f"{round((seconds or 0) / 60)} phút"   # noqa: E731
    lines = []
    for worker in report.get("workers") or []:
        if worker.get("alive"):
            from .pull_worker import WORKER_VERSION

            state = f"sống, model {worker.get('loaded_model') or 'chưa nạp'}"
            slots = worker.get("slots") or 1
            state += (f", đang làm {worker.get('running') or 1}/{slots} việc" if worker.get("busy")
                      else f", rảnh ({slots} việc cùng lúc)" if slots > 1 else ", rảnh")
            if worker.get("version") != WORKER_VERSION and not str(worker.get("version")).startswith("relay-"):
                state += f"; notebook cũ (chạy: kite worker restart {worker.get('worker_id')})"
        else:
            state = "đang dừng (draining)" if worker.get("draining") else "chưa kết nối"
        lines.append(f"{worker.get('worker_id')}: {state}")
        launch = worker.get("launch")
        if launch:
            line = f"  tự bật: {launch.get('state')}"
            if launch.get("seconds") is not None and launch.get("state") != "idle":
                line += f" ({minutes(launch.get('seconds'))})"
            if launch.get("kaggle") and launch.get("state") != "alive":   # stale once the worker is up
                line += f", Kaggle: {launch['kaggle']}" + (f" ({minutes(launch['kaggle_seconds'])})"
                                                          if launch.get("kaggle_seconds") is not None else "")
            if launch.get("failures"):
                line += f", lỗi liền {launch['failures']} lần"
            lines.append(line)
            if launch.get("detail"):
                lines.append(f"  {launch['detail']}")
    lines.append(f"{report.get('queued_jobs', 0)} request đang chờ worker")
    return "\n".join(lines)


def _journal_command(args: argparse.Namespace) -> int:
    state_dir = args.config.parent
    journal = Journal(state_dir / "journal")
    if args.journal_command == "summary":
        since = time.time() - args.days * 86400 if args.days > 0 else None
        _emit(summarize(journal, since=since))
    elif args.journal_command == "show":
        from .backend import task_slug

        _emit(show_task(journal, args.task_id, state_dir / "tasks" / task_slug(args.task_id) / "trace.jsonl"))
    else:
        from .memory import remember_note

        record = {"task_id": args.task_id, "verdict": "good" if args.good else "bad", "text": args.text}
        journal.write("notes", record)
        _emit({"saved": record, "file": str(journal.path("notes")),
               "remembered_in": remember_note(state_dir, args.task_id, record["verdict"], args.text)})
    return 0


def _web_store(args: argparse.Namespace):
    from .web.db import Store, connect, sqlite_url

    store = Store(connect(args.web_db or sqlite_url(args.config.parent / "web.sqlite3")))
    store.migrate()
    return store


def _web_command(args: argparse.Namespace) -> int:
    from .web import security as sec

    if args.web_command == "build":
        from . import webui

        report = webui.build(force=args.force)
        if "ui_error" not in report:
            report["next"] = "tải lại trang web (kite-web đọc web/dist ở mỗi lần tải, không cần khởi động lại)"
        _emit(report)
        return 1 if "ui_error" in report else 0
    store = _web_store(args)
    sealer = sec.Sealer(args.config.parent / "secrets" / "web_key")
    if args.web_command == "admin":
        return _web_admin(args, store, sealer)
    return _web_serve(args, store, sealer)


def _web_admin(args: argparse.Namespace, store, sealer) -> int:
    from .web import security as sec

    existing = store.user_by_name(args.username) if args.username else None
    if existing is None and store.user_count():
        from sqlalchemy import select

        from .web.db import users

        with store.engine.connect() as conn:
            name = conn.execute(select(users.c.username)).scalar()
        existing = store.user_by_name(name)
    if existing is not None and not args.reset:
        raise ConfigError(f"đã có tài khoản {existing.username}; quên mật khẩu hoặc mất điện thoại: "
                          "kite web admin --reset")
    username = existing.username if existing else (args.username or "admin")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", username):
        raise ConfigError("tên đăng nhập 3-64 ký tự: chữ, số, '.', '_', '-'")
    password = getpass.getpass(f"Mật khẩu mới cho {username} (ít nhất {sec.MIN_PASSWORD} ký tự): ")
    problem = sec.password_problem(password)
    if problem:
        raise ConfigError(problem)
    if getpass.getpass("Nhập lại mật khẩu: ") != password:
        raise ConfigError("hai lần nhập không khớp")
    secret = sec.new_totp_secret()
    print("Thêm Kite vào app xác thực (Google Authenticator, Aegis, 1Password...):", file=sys.stderr)
    print(f"  khóa: {secret}", file=sys.stderr)
    print(f"  hoặc liên kết: {sec.otpauth_uri(secret, username)}", file=sys.stderr)
    code = input("Mã 6 số app đang hiện: ").strip()
    counter = sec.verify_totp(secret, code, now=time.time())
    if counter is None:
        raise ConfigError("mã chưa đúng (giờ trên điện thoại có lệch không?); chạy lại lệnh")
    hashed = sec.hash_password(password)
    if existing is None:
        store.create_user(username, hashed, sealer.seal(secret))
        user = store.user_by_name(username)
        store.update_user(user.id, totp_last=counter)
        store.audit(username, "local", "admin_created", username, True)
    else:
        store.update_user(existing.id, password_hash=hashed, totp_secret=sealer.seal(secret), totp_pending=None,
                          totp_last=counter, failed=0, locked_until=0.0, password_changed_at=time.time())
        store.drop_sessions(existing.id)
        store.audit(username, "local", "admin_reset", username, True)
    _emit({"account": username, "2fa": "đã bật", "reset": existing is not None,
           "next": ["sudo cp deploy/kite-web.service /etc/systemd/system/ && sudo systemctl daemon-reload",
                    "sudo systemctl enable --now kite-web"]})
    return 0


def _web_serve(args: argparse.Namespace, store, sealer) -> int:
    import aiohttp
    import uvicorn

    from .mcp_server import KiteOps
    from .web.app import GatewayDown, WebContext, create_app

    if not store.user_count():
        # Serve anyway (a new Compose machine is healthy before its owner exists): signing in says what to do.
        print("kite web: chưa có tài khoản; tạo bằng `kite web admin` (máy Compose: sudo kite-stack admin)",
              file=sys.stderr, flush=True)
    config = _load_operator_config(args)
    config_path = args.config.resolve()
    chat_dir = Path.home() / "kite-chat"

    def ops_factory(current):
        return KiteOps(current, db_path=_db_path(args), state_dir=args.config.parent,
                       allowed_roots=args.allow_root or [Path.home()],
                       chat_dir=chat_dir if chat_dir.is_dir() else None,
                       kite_command=[*_kite_executable(), "--config", str(config_path)], config_path=config_path)

    base = f"http://{config.gateway.host}:{config.gateway.port}"

    async def gateway(method: str, path: str, body: dict | None) -> dict:
        timeout = aiohttp.ClientTimeout(total=600 if path in ("/kite/wake", "/kite/browser/open") else 30)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, base + path, json=body, timeout=timeout) as response:
                    data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            raise GatewayDown(f"{type(exc).__name__}") from exc
        if response.status >= 400:
            raise ValueError((data or {}).get("error") or f"gateway HTTP {response.status}")
        return data

    async def restart(services: list[str]) -> dict:
        from . import stack

        if stack.in_compose():          # kite-stack on the host restarts the containers
            queued = stack.request("restart", services=services)
            return {"ok": True, "services": services, "queued": True, "request": queued["request"], "error": None}
        done = await asyncio.to_thread(subprocess.run, ["sudo", "-n", "systemctl", "restart", *services],
                                       capture_output=True, text=True, timeout=120)
        return {"ok": done.returncode == 0, "services": services, "error": done.stderr.strip()[-300:] or None}

    cache: dict = {"at": 0.0, "value": None}

    async def quota() -> dict:
        if time.time() - cache["at"] < 600:
            return cache["value"]

        def read() -> dict:
            from .doctor import default_probes, gpu_quota
            from .launcher import kaggle_command

            out = {}
            for p in load_config(config_path).providers:
                token = p.secrets.get("kaggle_api_token", "")
                if p.adapter == "pull" and p.enabled and token.startswith("file:"):
                    status, detail, _fix = gpu_quota(default_probes(), kaggle_command(), token[5:])
                    out[p.effective_worker_id] = {"status": status, "detail": detail}
            return out

        cache.update(at=time.time(), value=await asyncio.to_thread(read))
        return cache["value"]

    password_file = args.config.parent / "secrets" / "opencode_server_password"
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    fallback_url = f"http://{config.opencode_host}:{config.opencode_port}"

    def opencode_locator() -> tuple[str, str | None]:
        from .web.app import find_opencode

        fallback = password_file.read_text(encoding="utf-8").strip() if password_file.is_file() else None
        return find_opencode(Path.home(), fallback_url, fallback)

    # dist is looked up on each request: `kite web build` (or kite update) swaps a new one in.
    ctx = WebContext(config_path, store, sealer, ops_factory=ops_factory, gateway=gateway,
                     secure_cookies=not args.insecure_cookies, opencode_locator=opencode_locator,
                     chat_dir=chat_dir, gateway_ws=f"ws://{config.gateway.host}:{config.gateway.port}",
                     restart=restart, dist=dist, quota=quota)
    _emit({"web": f"http://{args.host}:{args.port}", "db": str(store.engine.url.render_as_string(hide_password=True)),
           "page": str(dist) if (dist / "index.html").is_file() else "chưa build giao diện: kite web build"})
    sys.stdout.flush()
    uvicorn.Server(uvicorn.Config(create_app(ctx), host=args.host, port=args.port, proxy_headers=False,
                                  server_header=False, date_header=False, log_level="warning",
                                  ws_max_size=1 << 20)).run()
    return 0


def _db_path(args: argparse.Namespace) -> Path:
    return args.db if args.db is not None else args.config.parent / "scheduler.sqlite3"


def _scheduled_task(args: argparse.Namespace, task_id: str) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        role=args.role,
        model_id=args.model,
        repo_head=args.repo_head,
        branch=args.branch or f"agent/{task_id}",
        payload_json=json.dumps({"goal": args.goal}, ensure_ascii=False, sort_keys=True),
        estimated_gpu_seconds=args.estimate_gpu_seconds,
        priority=args.priority,
        created_at=time.time(),
        task_type=args.task_type,
        required_capabilities=tuple(args.capability),
    )


async def _serve_gateway(args: argparse.Namespace, config) -> None:
    from aiohttp import web

    from .gateway import ModelAlias, ModelGateway, create_app

    workers = gateway_workers(config)
    if not workers:
        # A new machine has no worker yet: serve anyway, so the web can add one (then restart the gateway).
        print("kite gateway: chưa có worker nào (pull hoặc relay); thêm trên web rồi khởi động lại gateway",
              file=sys.stderr, flush=True)
    store = SQLiteSchedulerStore(_db_path(args), model_policy=model_policy_from_config(config))
    temporal: dict[str, object] = {"client": None}

    async def on_status(worker, became_ready: bool) -> None:
        try:
            store.update_worker_status(
                worker.worker_id, provider_ready=worker.ready and not worker.draining,
                observed_at=time.time(), loaded_model=worker.loaded_model,
                cached_models=tuple(worker.cached_models))
        except StoreError:
            pass   # worker not in the scheduler store yet: run `kite provider sync`
        if not became_ready:
            return
        # A worker just came online: wake workflows that are waiting for one.
        try:
            from temporalio.client import Client
            from .temporal_workflow import CodingTaskWorkflow

            if temporal["client"] is None:
                temporal["client"] = await Client.connect(config.temporal_endpoint)
            for task in store.list_tasks():
                if task["state"] == "QUEUED":
                    try:
                        await temporal["client"].get_workflow_handle(str(task["task_id"])).signal(
                            CodingTaskWorkflow.worker_available)
                    except Exception:
                        pass   # task queued without a workflow, or the workflow already finished
        except Exception as exc:
            print(f"kite gateway: could not wake waiting workflows: {exc}", file=sys.stderr)

    gateway = ModelGateway(
        workers,
        aliases=[ModelAlias(name, spec["model"], spec["params"])
                 for name, spec in config.gateway.aliases.items()],
        job_timeout_seconds=config.gateway.job_timeout_seconds,
        heartbeat_timeout_seconds=config.gateway.heartbeat_timeout_seconds,
        on_worker_status=on_status,
        journal=lambda record: Journal(args.config.parent / "journal").write("gateway", record),
    )
    state_dir = args.config.parent
    journal = Journal(state_dir / "journal")
    notifier = _notifier(config)
    kernels = {p.effective_worker_id: p.launch.kernel for p in config.providers
               if p.adapter == "pull" and p.enabled and p.launch is not None}
    browser = None
    if kernels:
        # Kite's own browser for kaggle.com (browser.py): Chromium starts only when it is used.
        from .browser import KiteBrowser

        browser = KiteBrowser(state_dir, kernels=kernels)
        browser_watch = asyncio.get_running_loop().create_task(browser.watch())   # noqa: F841 (kept alive)
    kaggle = None
    if kernels:
        from .launcher import KaggleLauncher

        kaggle = KaggleLauncher(
            config, gateway, state_dir=state_dir, notify=notifier, browser=browser,
            journal=lambda record: journal.write("launches", record))
        kaggle.load_tokens()
    # Workers Kite calls itself (relay.py): paid APIs, servers by URL, rented machines (pods.py). They take
    # jobs from the same queue.
    from .pods import PodController, RunPodClient
    from .relay import RelayWorker, SpendLedger, relay_specs

    specs = relay_specs(config)
    gateway.paid = {spec.worker_id for spec in specs if spec.paid}
    from .spend_ledger import SpendStore

    # Settled spend is durable (SQLite in the state dir); the journal's old records import once.
    ledger = SpendLedger(journal.read("spend"), store=SpendStore(state_dir / "spend.sqlite3"))
    from .starters import Starters, Switches

    switches = Switches(state_dir / "switches.json")
    relays = {spec.worker_id: RelayWorker(spec, gateway, ledger, notify=notifier, journal=journal.write,
                                          # an API worker switched off (web, Worker tab) takes no job
                                          available=None if spec.pod else (lambda wid=spec.worker_id: switches.on(wid)))
              for spec in specs}
    pods = []
    for p in config.providers:
        if p.adapter == "relay" and p.enabled and p.relay is not None and p.relay.pod is not None:
            relay = relays[p.effective_worker_id]
            pods.append(PodController(relay.spec, RunPodClient(resolve_secret(p.secrets["runpod_api_key"])),
                                      relay, ledger, notify=notifier, journal=journal.write))
    relay_tasks = [asyncio.get_running_loop().create_task(relay.run())   # noqa: F841 (kept alive)
                   for relay in relays.values()]
    launcher = None
    if kaggle is not None or pods:
        launcher = Starters(kaggle, pods, switches)
        gateway.on_demand = launcher.on_demand
        gateway.starting = launcher.starting
        gateway.wait_note = launcher.wait_note
        watcher = asyncio.get_running_loop().create_task(launcher.watch())   # noqa: F841 (kept alive)

    def worker_code(worker_id: str, headers) -> dict:
        """GET /worker/code: the bootstrap notebook (stub.py) asks for the worker code and this run's values."""
        from .stub import launch_for, worker_bundle

        spec = next((p.launch for p in config.providers if p.effective_worker_id == worker_id and p.launch), None)
        launch = launch_for(worker_id, now=time.time(), idle_minutes=spec.idle_minutes if spec else 30,
                            model=gateway.first_queued_model(worker_id) or (spec.model if spec else None),
                            gateway_url=config.gateway.public_url)
        journal.write("launches", {"event": "code", "worker": worker_id, "model": launch.get("model"),
                                   "run_type": str(headers.get("X-Kite-Run-Type", ""))[:20]})
        return worker_bundle(launch)

    sandbox_digest = None
    if config.sandbox is not None:
        # One sandbox client token per gateway boot: the plaintext goes to a 0600 file the
        # sandbox side reads; only its digest stays in this process (gateway.py).
        from .gateway import issue_sandbox_client_token

        token_file = Path(os.environ.get("KITE_SANDBOX_TOKEN_FILE")
                          or state_dir / "secrets" / "sandbox_client_token")
        sandbox_digest = issue_sandbox_client_token(token_file)
    runner = web.AppRunner(create_app(gateway, launcher=launcher, worker_code=worker_code, browser=browser,
                                      switches=switches, relays=relays,
                                      sandbox_host=config.sandbox.gateway_host if config.sandbox else None,
                                      sandbox_token_digest=sandbox_digest))
    await runner.setup()
    hosts = [config.gateway.host]
    if config.sandbox is not None:
        hosts.append(config.sandbox.gateway_host)   # reachable only from the internal sandbox network
    for host in hosts:
        try:
            await web.TCPSite(runner, host, config.gateway.port).start()
        except OSError as exc:
            await runner.cleanup()
            store.close()
            raise ConfigError(
                f"cannot listen on {host}:{config.gateway.port} ({exc}); for the sandbox address run "
                "`kite sandbox build` first (it creates the Docker network)") from exc
    _emit({"listening": [f"http://{h}:{config.gateway.port}" for h in hosts],
           "public_url": config.gateway.public_url,
           "workers": [w.worker_id for w in workers], "models": gateway.served_models(),
           "api_workers": [spec.worker_id for spec in specs if spec.pod is None],
           "pods": [pod.worker_id for pod in pods],
           "autostart": launcher.states if launcher else [],
           "browser": f"http://127.0.0.1:{config.gateway.port}/kite/browser" if browser else None})
    sys.stdout.flush()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        store.close()


async def _ensure_temporal_namespace(client, *, retention_days: int = 7, settle_seconds: float = 15) -> bool:
    """Create the client's namespace when the server does not have it yet; return True if created.

    The bootstrap creates Temporal's schema but not the ``default`` namespace, and a worker cannot
    start without it.
    """
    from google.protobuf.duration_pb2 import Duration
    from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest

    try:
        await client.workflow_service.register_namespace(RegisterNamespaceRequest(
            namespace=client.namespace,
            workflow_execution_retention_period=Duration(seconds=retention_days * 24 * 3600)))
    except Exception as error:
        if "already" in str(error).lower():
            return False
        raise
    await asyncio.sleep(settle_seconds)   # the server caches namespaces; let it see the new one
    return True


def _notifier(config):
    """The configured push notifier (notify.ntfy), or one that does nothing."""
    from .notify import Notifier

    if not config.notify.ntfy:
        return Notifier(None)
    try:
        return Notifier(resolve_secret(config.notify.ntfy))
    except (ConfigError, OSError) as exc:
        print(f"kite notify: {exc}; notifications are off", file=sys.stderr)
        return Notifier(None)


def _notify_command(args: argparse.Namespace) -> int:
    import yaml

    from . import notify

    if args.notify_command == "test":
        config = _load_operator_config(args)
        if not config.notify.ntfy:
            raise ConfigError("notifications are not set up: kite notify setup")
        url = resolve_secret(config.notify.ntfy)
        notify.send(url, "Kite: thử thông báo", "Nếu bạn đọc được dòng này, Kite sẽ báo khi mỗi task xong.",
                    tags=("bell",))
        _emit({"sent": True, "topic": notify.split_url(url)[1]})
        return 0
    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    secret = args.config.parent / "secrets" / "ntfy_url"
    if args.off:
        raw.pop("notify", None)
        backup = _save_config(args.config, raw)
        _emit({"notify": "off", "backup": str(backup), "next": "sudo systemctl restart kite-workflow-worker"})
        return 0
    url = f"{args.server.rstrip('/')}/{args.topic or notify.new_topic()}"
    notify.split_url(url)                          # validate before writing anything
    secret.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(secret.parent, 0o700)
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(url + "\n")
    raw["notify"] = {"ntfy": f"file:{secret}"}
    backup = _save_config(args.config, raw)
    server, topic = notify.split_url(url)
    _emit({"notify": "ntfy", "server": server, "topic": topic, "secret_file": str(secret), "backup": str(backup),
           "next": [f"App ntfy trên điện thoại: Subscribe to topic -> {topic}"
                    + ("" if server == notify.DEFAULT_SERVER else f" (server {server})"),
                    "sudo systemctl restart kite-workflow-worker", "kite notify test"]})
    return 0


def _memory_command(args: argparse.Namespace) -> int:
    from . import memory

    path = memory.memory_path(args.config.parent, args.repo)
    if args.memory_command == "forget":
        removed = memory.forget(path, args.number)
        if removed is None:
            raise ConfigError(f"không có lưu ý số {args.number} trong {path}")
        _emit({"file": str(path), "removed": removed})
        return 0
    _emit({"repo": str(Path(args.repo).expanduser().resolve()), "file": str(path),
           "lessons": memory.lessons(args.config.parent, args.repo, limit=1000, chars=10**9),
           "used_in_tasks": memory.lessons(args.config.parent, args.repo)})
    return 0


def _run_workflow_worker(args: argparse.Namespace, config) -> int:
    """Serve the Temporal task queue with the sandbox backend (see deploy/kite-workflow-worker.service)."""
    from .backend import SandboxActivityBackend
    from .temporal_activities import TemporalKiteActivities
    from .temporal_state import TemporalActivityJournal

    settings = sandbox_settings_from_config(config)
    state_dir = args.config.parent
    models = gateway_models(config)
    store = SQLiteSchedulerStore(_db_path(args), model_policy=model_policy_from_config(config))
    journal = TemporalActivityJournal(state_dir / "activities.sqlite3")
    backend = SandboxActivityBackend(
        store, settings, state_dir=state_dir, models=models,
        aliases={name: spec["model"] for name, spec in config.gateway.aliases.items()},
        notifier=_notifier(config))
    activities = TemporalKiteActivities(backend, journal)
    methods = [activities.validate_task, activities.prepare_workspace, activities.reserve_route,
               activities.prepare_opencode_session, activities.run_opencode,
               activities.interrupt_opencode, activities.checkpoint_and_stop,
               activities.build_review_bundle, activities.release_route]
    _emit({"temporal": config.temporal_endpoint, "task_queue": config.temporal_task_queue,
           "state_dir": str(state_dir), "sandbox_image": settings.image, "models": models})
    sys.stdout.flush()
    async def serve() -> None:
        from temporalio.client import Client
        from temporalio.worker import Worker
        from .temporal_workflow import CodingTaskWorkflow

        client = await Client.connect(config.temporal_endpoint)
        if await _ensure_temporal_namespace(client):
            print(f"kite: created Temporal namespace {client.namespace}", file=sys.stderr)
        await Worker(client, task_queue=config.temporal_task_queue,
                     workflows=[CodingTaskWorkflow], activities=methods).run()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    finally:
        journal.close()
        store.close()
    return 0


def _mcp_command(args: argparse.Namespace) -> int:
    """Serve Kite's chat tools over stdio. stdout carries the MCP protocol: never print to it."""
    from .mcp_server import KiteOps, build_server

    config = _load_operator_config(args)
    ops = KiteOps(config, db_path=_db_path(args), state_dir=args.config.parent,
                  allowed_roots=args.allow_root or [Path.home()], chat_dir=args.chat_dir,
                  kite_command=[*_kite_executable(), "--config", str(args.config.resolve())],
                  config_path=args.config.resolve())
    # The chat just opened: start a worker for the lead model now, while the person types.
    ops.wake_soon(model=getattr(args, "lead_model", None), reason="chat")
    build_server(ops).run(transport="stdio")
    return 0


def _kite_executable() -> list[str]:
    """How to run this Kite again (the chat's MCP server, detached jobs started from the chat)."""
    kite = Path(sys.argv[0]).resolve() if Path(sys.argv[0]).exists() else Path(shutil.which("kite") or "kite")
    return [str(kite)]


def _busy_command(args: argparse.Namespace) -> int:
    """Running tasks and a bench run: what kite-stack must not cut off with an update."""
    from . import bench

    tasks = []
    if args.db.exists():
        with SQLiteSchedulerStore(args.db) as store:
            tasks = [str(t["task_id"]) for t in store.list_tasks() if t["state"] == "LEASED"]
    running = bench.running_bench(args.config.parent)
    chat = _chat_sessions_busy(Path.home())
    _emit({"busy": bool(tasks or running or chat), "tasks": tasks, "chat": chat,
           "bench": {"label": running.get("label"), "started": running.get("started")} if running else None})
    return 0


def _chat_sessions_busy(home: Path) -> int:
    """Chat sessions answering right now (OpenCode's background server); 0 when it cannot be asked."""
    import base64
    import urllib.parse
    import urllib.request

    try:
        from .web.app import _session_busy, find_opencode

        url, password = find_opencode(home, "", None)
        if not url:
            return 0
        query = urllib.parse.urlencode({"directory": str(home / "kite-chat"), "limit": "30"})
        request = urllib.request.Request(f"{url}/api/session?{query}")
        if password:
            request.add_header("Authorization", "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode())
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode())
    except Exception:               # no server, no web extra, an old server: nothing to wait for
        return 0
    items = data.get("data") if isinstance(data, dict) else None
    return sum(1 for item in items if _session_busy(item)) if isinstance(items, list) else 0


def _stack_command(args: argparse.Namespace) -> int:
    from . import stack

    config_path = args.config.resolve()
    if args.stack_command == "init":
        _emit(stack.init_config(config_path, public=args.public_url, sandbox_gateway=args.sandbox_gateway))
        return 0
    if args.stack_command == "health":
        ok, detail = stack.health(args.service, config_path)
        print(detail)
        return 0 if ok else 1
    if args.stack_command == "status":
        _emit({"version": stack.version(), **stack.status()})
        return 0
    if args.stack_command == "request":
        fields = {"services": args.services} if args.action == "restart" else {"force": args.force}
        _emit(stack.request(args.action, **fields))
        return 0
    if args.stack_command == "export":
        report = stack.export_state(config_path.parent, args.out, repos=args.repo, with_cache=not args.no_cache,
                                    chat_history=not args.no_chat_history)
        report["next"] = (f"chép {args.out} sang máy mới (scp), rồi ở máy mới: sudo kite-stack import "
                          f"~/{Path(args.out).name}")
        _emit(report)
        return 0
    if args.stack_command == "import":
        report = stack.import_state(args.archive, config_path.parent, home=Path.home(),
                                    web_db=os.environ.get("KITE_WEB_DB") or None, public=args.public_url,
                                    sandbox_gateway=args.sandbox_gateway, force=args.force)
        _emit(report)
        return 0
    if args.stack_command == "opencode":
        def refresh(path: Path) -> dict:
            return _chat_setup_again(path)

        def setup(path: Path) -> dict:
            config = load_config(path)
            if not gateway_models(config):
                return {"chat": "chưa có worker nào phục vụ model: thêm worker trên web, chat sẽ được tạo khi "
                                "khởi động lại"}
            # --force: AGENTS.md is written from Kite's state (what the chat remembers lives there too).
            done = subprocess.run([*_kite_executable(), "--config", str(path), "chat", "setup", "--no-reload",
                                   "--force"], capture_output=True, text=True)
            return {"chat": "đã tạo ~/kite-chat" if done.returncode == 0
                    else "chat setup lỗi: " + (done.stderr or done.stdout).strip()[-300:]}

        def serve() -> None:
            sys.stdout.flush()
            os.execvp("opencode", ["opencode", "serve", "--service"])

        stack.opencode_entry(config_path, refresh, setup, serve)
        return 0
    return -1


def _chat_command(args: argparse.Namespace) -> int:
    from .mcp_server import (chat_files, clear_shell_approvals, persisted_shell_approvals,
                             stop_stale_chat_processes)

    if args.chat_command == "reload":
        stopped = stop_stale_chat_processes(args.config)
        _emit({"stopped": stopped, "next": "mở lại opencode (session mới: /new) để nạp tool mới"})
        return 0
    config = _load_operator_config(args)
    target = args.dir.expanduser().resolve()
    existing = [name for name in ("opencode.json", "AGENTS.md") if (target / name).exists()]
    if existing and not args.force:
        raise ConfigError(f"{target} already has {', '.join(existing)}; pass --force to overwrite")
    models = gateway_models(config)
    if not models:
        raise ConfigError("no enabled pull worker serves a model")
    lead = args.model or ("qwen3.6-fast" if "qwen3.6-fast" in models else models[0])
    if lead not in models:
        raise ConfigError(f"model {lead} is not served by the gateway ({', '.join(models)})")
    roots = [Path(root).expanduser().resolve() for root in (args.allow_root or [Path.home()])]
    command = [*_kite_executable(), "--config", str(args.config.resolve()), "mcp", "serve", "--lead-model", lead,
               "--chat-dir", str(target)]
    for root in roots:
        command += ["--allow-root", str(root)]
    target.mkdir(parents=True, exist_ok=True)
    for name, content in chat_files(config, lead_model=lead, kite_command=command, allowed_roots=roots,
                                    state_dir=args.config.parent).items():
        (target / name).write_text(content, encoding="utf-8")
    if not (target / ".git").exists():
        # OpenCode only reads a project's opencode.json inside a Git repository.
        subprocess.run(["git", "init", "-q", str(target)], check=True)
    # A running OpenCode background server would keep the old config and the old Kite tools.
    stopped = [] if args.no_reload else stop_stale_chat_processes(args.config)
    report = {"chat_dir": str(target), "lead_model": lead, "allowed_roots": [str(r) for r in roots],
              "stopped": stopped, "next": f"cd {target} && opencode"}
    # "Always allow" in OpenCode is saved for every project and outlives opencode.json: check it.
    if args.clear_approvals:
        report["approvals"] = clear_shell_approvals()
    elif approvals := persisted_shell_approvals():
        report["warning"] = ("OpenCode đang lưu quyền shell 'Always allow': "
                             + ", ".join(a["pattern"] for a in approvals)
                             + ". Xóa bằng: kite chat setup --force --clear-approvals")
    _emit(report)
    return 0


def _bench_command(args: argparse.Namespace) -> int:
    from . import bench
    from . import sandbox as sbx
    from .mcp_server import KiteOps, build_server, chat_files

    if args.bench_command == "history":
        # Runs written with --out somewhere else (~/bench-repo-v5) count too: the home folder is read.
        roots = [args.root.expanduser()] if args.root else [args.config.parent / "bench", Path.home()]
        text, rows = bench.history(roots)
        if args.json:
            _emit({"roots": [str(r) for r in roots], "runs": rows})
        else:
            print(text if rows else f"Chưa có lần đo nào trong {', '.join(str(r) for r in roots)}.")
        return 0
    config = _load_operator_config(args)
    if args.bench_command == "apply":
        import yaml

        suggestion = bench.suggest(bench.merge_results(args.results), aliases=set(config.gateway.aliases))
        result = {"suggestion": suggestion, "written": False}
        if args.write:
            raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
            updated = bench.apply_suggestion(raw, suggestion)
            validate_config(updated)
            backup = args.config.with_name(f"{args.config.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(args.config, backup)
            args.config.write_text(yaml.safe_dump(updated, sort_keys=False, allow_unicode=True), encoding="utf-8")
            result.update(written=True, backup=str(backup),
                          next="kite provider sync; sudo systemctl restart kite-gateway kite-workflow-worker")
        _emit(result)
        return 0
    served = gateway_models(config)
    models = [m.strip() for m in args.models.split(",") if m.strip()] or \
        [m for m in served if m not in config.gateway.aliases]
    unknown = [m for m in models if m not in served]
    if unknown:
        raise ConfigError(f"model not served by the gateway: {', '.join(unknown)} (served: {', '.join(served)})")
    suites = [s.strip() for s in args.suites.split(",") if s.strip()]
    if not suites or set(suites) - {"speed", "lead", "code"}:
        raise ConfigError("--suites takes speed, lead, code")
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    scenarios = [s for s in bench.SCENARIOS if not only or s.id in only]
    source = args.repo_source.expanduser().resolve() if args.repo_source else None
    if source is not None and not (source / ".git").exists():
        raise ConfigError(f"--repo-source {source} is not a Git clone")
    polyglot = args.polyglot.expanduser().resolve() if args.polyglot else None
    if polyglot is not None and source is not None:
        raise ConfigError("--polyglot and --repo-source pick different code suites: use one")
    if args.polyglot_count < 0:
        raise ConfigError("--polyglot-count must be 0 (all) or more")
    if polyglot is not None:
        try:
            pool = bench.polyglot_tasks(polyglot)
        except FileNotFoundError as exc:
            raise ConfigError(str(exc)) from exc
    else:
        pool = bench.REPO_TASKS if source is not None else bench.CODE_TASKS
    code_tasks = [t for t in pool if not only or t.id in only]
    if polyglot is not None and args.polyglot_count:
        code_tasks = code_tasks[:args.polyglot_count]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = (args.out or args.config.parent / "bench" / stamp).expanduser().resolve()
    post = bench.gateway_post(f"http://{config.gateway.host}:{config.gateway.port}/v1/chat/completions",
                              timeout=config.gateway.job_timeout_seconds + 60, worker=args.worker or None)
    ops = KiteOps(config, db_path=_db_path(args), state_dir=args.config.parent, allowed_roots=[Path.home()])
    tools = bench.lead_tools(asyncio.run(build_server(ops).list_tools()))
    system = chat_files(config, lead_model=models[0], kite_command=["kite"], allowed_roots=[Path.home()])["AGENTS.md"]
    run_code = None
    if "code" in suites:
        settings = sandbox_settings_from_config(config)
        if args.no_harness:
            settings = dataclasses.replace(settings, fix_rounds=0, self_review=False, code_map=False,
                                           quick_tests=False, fast_path=False)
        else:
            if args.no_code_map:
                settings = dataclasses.replace(settings, code_map=False)
            if args.no_fast_path:
                settings = dataclasses.replace(settings, fast_path=False)

        status_url = f"http://{config.gateway.host}:{config.gateway.port}/status"
        bench_journal = Journal(args.config.parent / "journal")

        def kite_run(task, model, repo, goal, task_id, out_dir):
            """One attempt; run again once if it failed because of the machines (no worker, a worker that
            stopped, a KV cache shared with another request), after waiting for a worker."""
            run, result = kite_run_once(task, model, repo, goal, task_id, out_dir)
            if result.get("hidden_exit_code") == 0:
                return run, result
            errors = bench.infra_errors([r for r in bench_journal.read("gateway")
                                         if r.get("tag") == task_id or str(r.get("tag") or "").startswith(f"{task_id}-b")])
            if not errors:
                return run, result
            log(f"[{task_id}] lỗi hạ tầng, không tính lần này: {errors[0]}")
            base = config.gateway.aliases.get(model, {}).get("model", model)
            if not bench.wait_for_worker(status_url, base, log=log):
                result["infra_errors"] = errors[:3]
                return run, result
            run, again = kite_run_once(task, model, repo, goal, f"{task_id}-r", out_dir)
            again["infra_rerun"] = {"errors": errors[:3], "lost_seconds": result.get("wall_seconds")}
            return run, again

        def kite_run_once(task, model, repo, goal, task_id, out_dir):
            started = time.monotonic()
            run = sbx.run_task(settings, repo=repo, goal=goal, model=model, models=served,
                               state_dir=out_dir / "runs", task_id=task_id, with_context=not args.no_harness,
                               log=lambda line: print(line, file=sys.stderr), worker_pin=args.worker or None,
                               deps_cache=args.config.parent / "deps-cache" if settings.deps_cache else None)
            result = run.to_dict()
            result["wall_seconds"] = round(time.monotonic() - started, 1)
            # Model time and tokens of this run, from the gateway's journal on this machine.
            result.update(task_usage(Journal(args.config.parent / "journal"), task_id))
            if isinstance(task, bench.RepoTask):
                result["reference_lines"] = bench.reference_lines(task, source)
            # Hidden tests: written only now, run like Kite's own tests (no network, same deps). The
            # polyglot exercises are scored like Aider: their tests run whatever state Kite ended in.
            polyglot_task = isinstance(task, bench.PolyglotTask)
            ran = run.agent_status == "ok" or (polyglot_task and run.agent_status != "skipped")
            files = bench.hidden_files(task, source) if ran else {}
            if files:
                workspace = Path(run.workspace)
                for rel, content in files.items():
                    (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
                    (workspace / rel).write_text(content, encoding="utf-8")
                name = sbx.container_name("hidden", task_id)
                timeout = bench.POLYGLOT_TEST_TIMEOUT if polyglot_task else max(settings.test_timeout_seconds, 1200)
                result["hidden_exit_code"], output = sbx._run_step(
                    settings, sbx.default_runner, name=name, timeout=timeout,
                    args=sbx.docker_run_args(settings, name=name, workspace=workspace, network="none",
                                             deps=workspace.parent / "deps")
                    + ["sh", "-c", bench.hidden_command(task)])
                result["hidden_output_tail"] = sbx._tail(output)
            return run, result

        def run_code(task, model, out_dir):
            slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
            if isinstance(task, bench.PolyglotTask):
                repo = bench.make_polyglot_fixture(task, out_dir / "repos" / f"{slug}-{stamp}")
            elif isinstance(task, bench.RepoTask):
                repo = bench.make_repo_fixture(task, source, out_dir / "repos" / f"{slug}-{stamp}")
            else:
                repo = bench.make_fixture(task, out_dir / "repos" / f"{slug}-{stamp}")
            task_id = f"bench-{slug}-{task.id.lower()}-{stamp[-6:]}"
            if isinstance(task, bench.PolyglotTask):
                # Aider's protocol: a second try from where the first left the files, told the test errors.
                def attempt(goal, attempt_id):
                    run, result = kite_run(task, model, repo, goal, attempt_id, out_dir)
                    return Path(run.workspace), result

                return bench.run_polyglot(task, repo, task_id, attempt, log=lambda line: log(f"[{model}] {line}"))
            return kite_run(task, model, repo, task.goal, task_id, out_dir)[1]

    log = lambda line: print(line, file=sys.stderr, flush=True)
    harness_settings = sandbox_settings_from_config(config)
    map_on = harness_settings.code_map and not args.no_code_map
    if map_on and sbx.codemap.graphify_cli() is None:
        log("cảnh báo: không thấy lệnh graphify (pip install graphifyy) - chạy không có bản đồ code")
        map_on = False
    harness = "tắt (không vòng sửa, không rà soát, không ngữ cảnh repo)" if args.no_harness else \
        (f"bật ({harness_settings.fix_rounds} vòng sửa, rà soát {'bật' if harness_settings.self_review else 'tắt'}, "
         f"có ngữ cảnh repo, bản đồ code graphify {'bật' if map_on else 'tắt'}, đường nhanh "
         f"{'bật' if harness_settings.fast_path and not args.no_fast_path else 'tắt'})")
    try:
        lock = bench.acquire_lock(args.config.parent, label=args.label or out.name, out=out)
    except bench.BenchBusy as exc:
        raise ConfigError(str(exc)) from exc
    log(f"bench -> {out} | models {models} | suites {suites} | {len(scenarios)} kịch bản, {len(code_tasks)} task code"
        f" | harness {harness}")
    try:
        _bench_wait_for_workers(config, models, log)
        results = bench.run_bench(models=models, suites=suites, post=post, out=out, system=system, tools=tools,
                                  run_code=run_code, scenarios=scenarios, code_tasks=code_tasks, log=log,
                                  meta={"harness": harness, **({"label": args.label} if args.label else {})})
    finally:
        bench.release_lock(lock)
    _emit({"out": str(out), "report": str(out / "README.md"),
           "summary": {m: bench.summarize(entry) for m, entry in results["models"].items()}})
    return 0


def _bench_wait_for_workers(config, models: list[str], log: Callable[[str], None]) -> None:
    """A bench run never starts a Kaggle notebook (only the chat does, see gateway.starts_a_worker):
    with no worker up it says how to start one and waits, up to 4 hours."""
    from . import bench
    from .launcher import kaggle_url

    status_url = f"http://{config.gateway.host}:{config.gateway.port}/status"
    pages = [kaggle_url(p.launch.kernel) for p in config.providers
             if p.adapter == "pull" and p.enabled and p.launch is not None]
    for model in dict.fromkeys(config.gateway.aliases.get(m, {}).get("model", m) for m in models):
        note = (f"chưa có worker cho {model}: bench không tự bật Kaggle. Bật notebook "
                + (f"(mở {pages[0]} rồi bấm Save Version) " if pages else "")
                + "hoặc chạy `kite worker start <worker>`; bench chờ worker tối đa 4 giờ rồi chạy")
        if not bench.wait_for_worker(status_url, model, log=log, note=note):
            raise ConfigError(f"không có worker cho {model} sau 4 giờ: bench dừng")


def _sandbox_command(args: argparse.Namespace) -> int:
    from . import sandbox as sbx

    config = _load_operator_config(args)
    settings = sandbox_settings_from_config(config)
    if args.sandbox_command == "build":
        opencode = shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode")
        if not Path(opencode).is_file():
            raise ConfigError("opencode binary not found on the host; run the bootstrap first")
        network_gateway = sbx.ensure_network(settings)
        print("Đang build image sandbox (vài phút)...", file=sys.stderr)
        image = sbx.build_image(settings, dockerfile=REPO_ROOT / "deploy" / "sandbox" / "Dockerfile",
                                opencode_binary=Path(opencode).resolve(),
                                opencode_cache=Path.home() / ".cache" / "opencode",
                                log=lambda line: print(line, file=sys.stderr))
        _emit({"image": image, "network": settings.network, "network_gateway": network_gateway,
               "configured_gateway_host": config.sandbox.gateway_host,
               "gateway_host_matches": network_gateway == config.sandbox.gateway_host})
        return 0
    if args.sandbox_command == "doctor":
        checks = sbx.doctor(settings)
        _emit(checks)
        return 0 if checks and all(check["ok"] for check in checks.values()) else 1
    if args.sandbox_command == "run":
        models = gateway_models(config)
        base_models = [m for m in models if m not in config.gateway.aliases]
        if not base_models:
            raise ConfigError("no enabled pull worker serves a model")
        model = args.model or base_models[0]
        small = next((name for name, spec in config.gateway.aliases.items() if spec["model"] == model), None)
        worker = sbx.require_worker(settings, model)
        print(f"Worker {worker['worker_id']} sẵn sàng (model đang nạp: {worker.get('loaded_model')})",
              file=sys.stderr)
        result = sbx.run_task(
            settings, repo=args.repo, goal=args.goal, model=model, models=models,
            state_dir=args.config.parent / "sandbox", task_id=args.task_id, small_model=small,
            test_command=args.test_cmd, setup_command=args.setup_cmd,
            log=lambda message: print(message, file=sys.stderr))
        summary = result.to_dict()
        summary["apply_with"] = f"git -C {result.repo} apply {result.patch_path}"
        _emit(summary)
        return 0 if result.verified else 2
    return -1


def _operator_command(args: argparse.Namespace) -> int:
    if args.command == "setup":
        secrets_dir = args.config.parent / "secrets"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(secrets_dir, 0o700)
        if args.secret_name:
            if not args.secret_name.replace("_", "").isalnum() or not args.secret_name[0].isalpha():
                raise ConfigError("secret name must contain only letters, digits, and underscores")
            secret_path = secrets_dir / args.secret_name.lower()
            if secret_path.exists():
                raise ConfigError("secret file already exists; refusing to overwrite it")
            value = getpass.getpass(f"Enter value for {args.secret_name} (input hidden): ")
            if not value:
                raise ConfigError("secret value cannot be empty")
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value)
                stream.write("\n")
            _emit({"secret_reference": f"file:{secret_path.resolve()}", "mode": "0600", "value": "<redacted>"})
        else:
            _emit({"secrets_directory": str(secrets_dir.resolve()), "created": True})
        return 0
    if args.command == "cleanup":
        return _cleanup(args)
    if args.command == "notify":
        return _notify_command(args)
    if args.command == "memory":
        return _memory_command(args)
    if args.command == "update":
        return _update_command(args)
    if args.command == "busy":
        return _busy_command(args)
    if args.command == "stack":
        return _stack_command(args)
    if args.command == "model":
        return _model_add(args)
    if args.command == "doctor":
        from .doctor import render, run_checks, summary

        config = _load_operator_config(args)
        checks = run_checks(config, args.config)
        report = summary(checks)
        if args.json:
            _emit(report)
        else:
            print(render(checks, args.config))
        return 0 if report["status"] == "ok" else 1
    if args.command == "provider":
        config = _load_operator_config(args)
        if args.provider_command == "validate":
            for provider in config.providers:
                adapter = LIVE_ADAPTERS.get(provider.adapter)
                if adapter:
                    try:
                        adapter.validate(provider)
                    except Exception as error:
                        raise ConfigError(f"provider validation failed for {provider.id}") from error
            _emit({"valid": True, "config": redact_config(config), "provider_count": len(config.providers)})
            return 0
        if args.provider_command == "refresh":
            refreshed, unknown = refresh_configured_providers(config)
            _emit({"refreshed": refreshed, "unknown": unknown,
                   "enabled": [p.id for p in config.providers if p.enabled]})
            return 0
        if args.provider_command == "enable":
            provider = next((p for p in config.providers if p.id == args.provider_id), None)
            if provider is None:
                raise ConfigError("provider ID is not configured")
            if provider.adapter == "kaggle":
                raise ConfigError("Kaggle adapter is validation-only and cannot be enabled")
            if provider.adapter not in LIVE_ADAPTERS:
                raise ConfigError("provider adapter has no registered live implementation")
            try:
                LIVE_ADAPTERS[provider.adapter].validate(provider)
            except Exception as error:
                raise ConfigError(f"provider validation failed for {provider.id}") from error
            raw_text = args.config.read_text(encoding="utf-8")
            if args.config.suffix.lower() == ".json":
                raw_config = json.loads(raw_text)
            else:
                import yaml
                raw_config = yaml.safe_load(raw_text)
            for item in raw_config["providers"]:
                if item["id"] == provider.id:
                    item["enabled"] = True
                    break
            temp_path = args.config.with_suffix(args.config.suffix + ".tmp")
            if args.config.suffix.lower() == ".json":
                temp_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            else:
                import yaml
                temp_path.write_text(yaml.safe_dump(raw_config, sort_keys=False), encoding="utf-8")
            temp_path.replace(args.config)
            _emit({"provider_id": provider.id, "enabled": True})
            return 0
        if args.provider_command == "sync":
            with SQLiteSchedulerStore(_db_path(args)) as store:
                synced = sync_config_to_store(config, store, now=time.time())
            _emit({"synced": synced, "db": str(_db_path(args))})
            return 0
    if args.command == "gateway":
        config = _load_operator_config(args)
        if args.gateway_command == "opencode-config":
            from .gateway import opencode_provider_config

            url = f"http://{config.gateway.host}:{config.gateway.port}"
            models = gateway_models(config)
            _emit(opencode_provider_config(url, models, context=args.context or model_contexts(config, models)))
            return 0
        if args.gateway_command == "serve":
            try:
                asyncio.run(_serve_gateway(args, config))
            except KeyboardInterrupt:
                pass
            return 0
    if args.command == "sandbox":
        return _sandbox_command(args)
    if args.command == "mcp":
        return _mcp_command(args)
    if args.command == "chat":
        return _chat_command(args)
    if args.command == "web":
        return _web_command(args)
    if args.command == "journal":
        return _journal_command(args)
    if args.command == "bench":
        return _bench_command(args)
    if args.command == "worker" and args.worker_command == "add":
        return _worker_add(args)
    if args.command == "worker" and args.worker_command == "autostart":
        return _worker_autostart(args)
    if args.command == "worker" and args.worker_command == "add-api":
        return _worker_add_api(args)
    if args.command == "worker" and args.worker_command == "add-runpod":
        return _worker_add_runpod(args)
    if args.command == "worker" and args.worker_command == "stub":
        return _worker_stub(args)
    if args.command == "worker" and args.worker_command == "browser":
        return _worker_browser(args)
    if args.command == "worker" and args.worker_command == "spend":
        return _worker_spend(args)
    if args.command == "worker" and args.worker_command == "restart":
        return _worker_restart(args)
    if args.command == "worker" and args.worker_command in ("start", "stop", "status"):
        return _worker_remote(args)
    if args.command == "worker" and args.worker_command == "rotate-token":
        args.force = True    # rotation is the token command below, with replacement allowed
    if args.command == "worker":
        config = _load_operator_config(args)
        provider = next((p for p in config.providers if p.effective_worker_id == args.worker_id), None)
        if provider is None:
            raise ConfigError("worker ID is not configured in providers")
        reference = provider.secrets.get("worker_token")
        if reference and reference.startswith("file:"):
            token_path = Path(reference[5:])
        elif reference:
            raise ConfigError("secrets.worker_token must be a file: reference for kite to write it")
        else:
            token_path = args.config.parent / "secrets" / f"worker_token_{args.worker_id}"
        if token_path.exists() and not args.force:
            raise ConfigError("token file already exists; pass --force to rotate it")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(token_path.parent, 0o700)
        token = token_secrets.token_urlsafe(32)
        temp_path = token_path.with_name(token_path.name + ".tmp")
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
        os.replace(temp_path, token_path)
        _emit({"worker_id": args.worker_id, "secret_reference": f"file:{token_path.resolve()}",
               "token": token,
               "next": "Lưu token này vào Kaggle Secrets với tên KITE_WORKER_TOKEN. Token chỉ in một lần; "
                       "restart `kite gateway serve` để gateway nhận token mới."})
        return 0
    if args.command == "workflow":
        config = _load_operator_config(args)
        if args.workflow_command == "worker":
            return _run_workflow_worker(args, config)
        from temporalio.client import Client
        from .temporal_workflow import CodingTaskInput, CodingTaskWorkflow

        if args.workflow_command == "cancel":
            from .backend import request_cancel

            request_cancel(args.config.parent, args.workflow_id)

        async def operate():
            client = await Client.connect(config.temporal_endpoint)
            action = args.workflow_command
            if action == "result":
                from .backend import task_slug

                handle = client.get_workflow_handle_for(CodingTaskWorkflow.run, args.workflow_id)
                outcome = await handle.result()
                summary = asdict(outcome)
                summary["patch_path"] = str(args.config.parent / "tasks" / task_slug(outcome.task_id)
                                            / "result.patch")
                repo_path = None
                if outcome.checkpoint_path and Path(outcome.checkpoint_path).is_file():
                    repo_path = json.loads(Path(outcome.checkpoint_path).read_text(encoding="utf-8")).get("repo_path")
                summary["apply_with"] = (f"git -C {repo_path} apply {summary['patch_path']}" if repo_path
                                         else f"git apply {summary['patch_path']}")
                return summary
            if action == "start":
                if not args.repo_head:
                    head = subprocess.run(["git", "-C", str(args.repo), "rev-parse", "HEAD"],
                                          capture_output=True, text=True)
                    if head.returncode != 0:
                        raise ConfigError(f"{args.repo} is not a Git repository")
                    args.repo_head = head.stdout.strip()
                args.branch = args.branch or f"agent/{args.task_id}"
                # The workflow reserves a route from the scheduler store, so the task must be
                # queued there first. Enqueue is idempotent for identical task content.
                with SQLiteSchedulerStore(_db_path(args), model_policy=model_policy_from_config(config)) as store:
                    existing = None
                    try:
                        existing = store.get_task(args.task_id)
                    except StoreError:
                        store.enqueue(_scheduled_task(args, args.task_id))
                    if existing is not None and existing["repo_head"] != args.repo_head:
                        raise StoreError(f"task {args.task_id} already exists with a different repo HEAD")
                task_input = CodingTaskInput(args.task_id, args.goal, str(args.repo.resolve()), args.repo_head,
                                             args.branch, tuple(args.capability), args.model, args.estimate_gpu_seconds,
                                             task_type=args.task_type, role=args.role)
                handle = await client.start_workflow(CodingTaskWorkflow.run, task_input,
                                                     id=args.task_id, task_queue=config.temporal_task_queue)
                return {"workflow_id": handle.id, "run_id": handle.result_run_id, "queued": True}
            handle = client.get_workflow_handle(args.workflow_id)
            if action == "status":
                return {"workflow_id": args.workflow_id, "status": await handle.query(CodingTaskWorkflow.current_status)}
            signal = {"pause": CodingTaskWorkflow.pause, "resume": CodingTaskWorkflow.resume_with_instruction,
                      "cancel": CodingTaskWorkflow.cancel,
                      "wake": CodingTaskWorkflow.worker_available}[action]
            if action == "resume":
                await handle.signal(signal, args.instruction)
            else:
                await handle.signal(signal)
            return {"workflow_id": args.workflow_id, "signal": action, "sent": True}
        from temporalio.service import RPCError

        try:
            _emit(asyncio.run(operate()))
        except RPCError as error:
            raise ConfigError(f"Temporal: {error}") from error
        return 0
    return -1


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _run(args: argparse.Namespace) -> int:
    if args.db is None:
        args.db = _db_path(args)
    if args.command == "review":
        raw = json.loads(args.result_json.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("result JSON must contain an object")
        result = WorkerResult(**raw)
        bundle = build_review_bundle(
            result,
            expected_repo_head=args.expected_head,
            expected_task_id=args.expected_task,
            expected_lease_id=args.expected_lease,
            expected_worker_id=args.expected_worker,
        )
        _emit(asdict(bundle))
        return 0

    if args.command in {"doctor", "setup", "provider", "workflow", "gateway", "worker", "sandbox", "mcp", "chat",
                        "bench", "journal", "cleanup", "model", "notify", "memory", "update", "web", "busy", "stack"}:
        return _operator_command(args)

    with SQLiteSchedulerStore(args.db) as store:
        scheduler = WorkerScheduler(store)
        if args.task_command == "add":
            task_id = args.task_id or uuid.uuid4().hex
            task = _scheduled_task(args, task_id)
            store.enqueue(task)
            _emit(store.get_task(task.task_id))
        elif args.task_command == "list":
            _emit(store.list_tasks())
        elif args.task_command == "show":
            _emit(store.get_task(args.task_id))
        elif args.task_command == "cancel":
            scheduler.cancel(args.task_id, now=time.time())
            _emit(store.get_task(args.task_id))
        elif args.task_command == "prepare":
            task = store.get_task(args.task_id)
            scheduled_task = ScheduledTask(
                task_id=str(task["task_id"]), role=str(task["role"]),
                model_id=task["model_id"], repo_head=str(task["repo_head"]),
                branch=str(task["branch"]), payload_json=str(task["payload_json"]),
                estimated_gpu_seconds=int(task["estimated_gpu_seconds"]),
                priority=int(task["priority"]), created_at=float(task["created_at"]),
                task_type=str(task["task_type"]),
                required_capabilities=tuple(task["required_capabilities"]),
            )
            prepared = prepare_task_workspace(scheduled_task, args.repo, state_dir=args.db.parent)
            _emit({
                "task_id": prepared.task_id,
                "repo_path": str(prepared.repo_path),
                "workspace_path": str(prepared.workspace_path),
                "checkpoint_path": str(prepared.checkpoint_path),
                "branch": prepared.branch,
                "repo_head": prepared.repo_head,
                "provider_started": False,
            })
        elif args.task_command == "inspect":
            store.get_task(args.task_id)
            checkpoint_path = checkpoint_path_for_task(args.db.parent, args.task_id)
            _emit(asdict(inspect_task_workspace(checkpoint_path)))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        return _run(args)
    except SystemExit as error:
        return int(error.code)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, StoreError, ConfigError,
            StaleWorkerResult, WorkspaceError, SandboxError) as error:
        print(f"kite: {error}", file=sys.stderr)
        return 1


def console_main() -> None:
    """Entry point of the installed `kite` command.

    The Temporal SDK runs a Rust (tokio) runtime in background threads; when the interpreter shuts
    down normally those threads can touch freed Python state and the process segfaults *after* the
    command has finished and printed its result. Leaving with os._exit once output is flushed avoids
    that shutdown race. Tests call main() directly and are not affected.
    """
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    console_main()
