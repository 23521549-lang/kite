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

from .config import (ConfigError, LIVE_ADAPTERS, gateway_models, gateway_workers, load_config,
                     model_contexts, model_policy_from_config, redact_config, refresh_configured_providers,
                     sandbox_settings_from_config, sync_config_to_store, validate_config)
from .integration import StaleWorkerResult, build_review_bundle
from .journal import Journal, show_task, summarize
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
    parser.add_argument("--config", type=Path, default=Path(".kite/config.yaml"), help="Kite YAML/JSON configuration")
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
    autostart.add_argument("--token", choices=["kaggle-secret", "per-launch"], default="kaggle-secret",
                           help="worker token: the notebook's Kaggle Secret (default) or a short-lived "
                                "token Kite writes into the notebook it pushes")
    autostart.add_argument("--model", default=None, help="model to load first when nothing asks for one")
    autostart.add_argument("--off", action="store_true", help="remove autostart from this worker")
    start = worker_commands.add_parser("start", help="start the worker's Kaggle notebook now (autostart)")
    start.add_argument("worker_id")
    start.add_argument("--model", default=None, help="model to load first")
    start.add_argument("--force", action="store_true",
                       help="push a new run even if one seems to be starting or failed recently")
    stop = worker_commands.add_parser("stop", help="ask a worker to finish its job and end its Kaggle session")
    stop.add_argument("worker_id")
    worker_commands.add_parser("status", help="connected workers and autostart state (from the gateway)")
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
    chat = commands.add_parser("chat", help="Vietnamese chat with the Kite lead agent (OpenCode)")
    chat_commands = chat.add_subparsers(dest="chat_command", required=True)
    chat_setup = chat_commands.add_parser("setup", help="create a chat folder with opencode.json and AGENTS.md")
    chat_setup.add_argument("--dir", type=Path, default=Path.home() / "kite-chat")
    chat_setup.add_argument("--model", help="lead model (default: qwen3.6-fast when configured)")
    chat_setup.add_argument("--allow-root", action="append", type=Path, default=[],
                            help="folder whose Git repositories tasks may use; repeat (default: home)")
    chat_setup.add_argument("--force", action="store_true", help="overwrite opencode.json and AGENTS.md")
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
    bench_run.add_argument("--no-harness", action="store_true",
                           help="code suite without fix rounds, self-review and repo context (baseline)")
    bench_run.add_argument("--no-code-map", action="store_true",
                           help="harness on, but without the graphify code map (to measure what it adds)")
    bench_run.add_argument("--no-fast-path", action="store_true",
                           help="harness on, but without the one-call fast path (to measure what it adds)")
    bench_run.add_argument("--worker", default="",
                           help="send every request to this worker (e.g. kaggle-han): comparable timings, and "
                                "the other worker stays free for real tasks")
    bench_apply = bench_commands.add_parser("apply", help="turn bench results into routing scores (dry run "
                                                          "unless --write)")
    bench_apply.add_argument("results", nargs="+", type=Path, help="results.json files (lead, code...)")
    bench_apply.add_argument("--write", action="store_true", help="write routing.model_skill / chat_only "
                                                                  "into the config (a backup is kept)")
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
        launch = {"kernel": args.kernel, "idle_minutes": args.idle_minutes, "token": args.token}
        if args.model:
            launch["model"] = args.model
        provider["launch"] = launch
        report.update(autostart=True, launch=launch)
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
    elif args.worker_command == "start":
        report = _gateway_call(config, "POST", "/kite/wake",
                               {"worker": args.worker_id, "model": args.model, "reason": "manual",
                                "force": args.force}, timeout=600)
    else:
        report = _gateway_call(config, "POST", "/kite/stop", {"worker": args.worker_id})
    _emit(report)
    return 0


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
        record = {"task_id": args.task_id, "verdict": "good" if args.good else "bad", "text": args.text}
        journal.write("notes", record)
        _emit({"saved": record, "file": str(journal.path("notes"))})
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
        raise ConfigError("no enabled provider uses adapter 'pull'")
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
    launcher = None
    if any(p.launch is not None for p in config.providers if p.adapter == "pull" and p.enabled):
        from .launcher import KaggleLauncher

        launcher = KaggleLauncher(config, gateway, state_dir=args.config.parent)
        launcher.load_tokens()
        gateway.on_demand = launcher.on_demand
        watcher = asyncio.get_running_loop().create_task(launcher.watch())   # noqa: F841 (kept alive)
    runner = web.AppRunner(create_app(gateway, launcher=launcher))
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
           "autostart": sorted(launcher.states) if launcher else []})
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
        aliases={name: spec["model"] for name, spec in config.gateway.aliases.items()})
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
                  allowed_roots=args.allow_root or [Path.home()])
    # The chat just opened: start a worker for the lead model now, while the person types.
    ops.wake_soon(model=getattr(args, "lead_model", None), reason="chat")
    build_server(ops).run(transport="stdio")
    return 0


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
    kite = Path(sys.argv[0]).resolve() if Path(sys.argv[0]).exists() else Path(shutil.which("kite") or "kite")
    command = [str(kite), "--config", str(args.config.resolve()), "mcp", "serve", "--lead-model", lead]
    for root in roots:
        command += ["--allow-root", str(root)]
    target.mkdir(parents=True, exist_ok=True)
    for name, content in chat_files(config, lead_model=lead, kite_command=command, allowed_roots=roots).items():
        (target / name).write_text(content, encoding="utf-8")
    if not (target / ".git").exists():
        # OpenCode only reads a project's opencode.json inside a Git repository.
        subprocess.run(["git", "init", "-q", str(target)], check=True)
    # A running OpenCode background server would keep the old config and the old Kite tools.
    stopped = stop_stale_chat_processes(args.config)
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
    pool = bench.REPO_TASKS if source is not None else bench.CODE_TASKS
    code_tasks = [t for t in pool if not only or t.id in only]
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

        def run_code(task, model, out_dir):
            slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
            if isinstance(task, bench.RepoTask):
                repo = bench.make_repo_fixture(task, source, out_dir / "repos" / f"{slug}-{stamp}")
            else:
                repo = bench.make_fixture(task, out_dir / "repos" / f"{slug}-{stamp}")
            task_id = f"bench-{slug}-{task.id.lower()}-{stamp[-6:]}"
            started = time.monotonic()
            run = sbx.run_task(settings, repo=repo, goal=task.goal, model=model, models=served,
                               state_dir=out_dir / "runs", task_id=task_id, with_context=not args.no_harness,
                               log=lambda line: print(line, file=sys.stderr), worker_pin=args.worker or None,
                               deps_cache=args.config.parent / "deps-cache" if settings.deps_cache else None)
            result = run.to_dict()
            result["wall_seconds"] = round(time.monotonic() - started, 1)
            files = bench.hidden_files(task, source) if run.agent_status == "ok" else {}
            if files:
                # Hidden tests: written only now, run like Kite's own tests (no network, same deps).
                workspace = Path(run.workspace)
                for rel, content in files.items():
                    (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
                    (workspace / rel).write_text(content, encoding="utf-8")
                name = sbx.container_name("hidden", task_id)
                result["hidden_exit_code"], output = sbx._run_step(
                    settings, sbx.default_runner, name=name, timeout=max(settings.test_timeout_seconds, 1200),
                    args=sbx.docker_run_args(settings, name=name, workspace=workspace, network="none",
                                             deps=workspace.parent / "deps")
                    + ["sh", "-c", bench.hidden_command(task)])
                result["hidden_output_tail"] = sbx._tail(output)
            return result

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
    log(f"bench -> {out} | models {models} | suites {suites} | {len(scenarios)} kịch bản, {len(code_tasks)} task code"
        f" | harness {harness}")
    results = bench.run_bench(models=models, suites=suites, post=post, out=out, system=system, tools=tools,
                              run_code=run_code, scenarios=scenarios, code_tasks=code_tasks, log=log,
                              meta={"harness": harness})
    _emit({"out": str(out), "report": str(out / "README.md"),
           "summary": {m: bench.summarize(entry) for m, entry in results["models"].items()}})
    return 0


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
    if args.command == "journal":
        return _journal_command(args)
    if args.command == "bench":
        return _bench_command(args)
    if args.command == "worker" and args.worker_command == "add":
        return _worker_add(args)
    if args.command == "worker" and args.worker_command == "autostart":
        return _worker_autostart(args)
    if args.command == "worker" and args.worker_command in ("start", "stop", "status"):
        return _worker_remote(args)
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

    if args.command in {"doctor", "setup", "provider", "workflow", "gateway", "worker", "sandbox",
                        "mcp", "chat", "bench", "journal"}:
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
