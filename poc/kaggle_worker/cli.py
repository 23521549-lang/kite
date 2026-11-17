"""Local queue and patch-review CLI for the worker-scheduler prototype."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import secrets as token_secrets
import sys
import time
import uuid

from .config import (ConfigError, LIVE_ADAPTERS, gateway_models, gateway_workers, load_config,
                     model_policy_from_config, redact_config, refresh_configured_providers,
                     sync_config_to_store)
from .integration import StaleWorkerResult, build_review_bundle
from .protocol import ScheduledTask, WorkerResult
from .scheduler import WorkerScheduler
from .store import SQLiteSchedulerStore, StoreError
from .workspace import (
    WorkspaceError,
    checkpoint_path_for_task,
    inspect_task_workspace,
    prepare_task_workspace,
)


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
    commands.add_parser("doctor", help="check local configuration and installation prerequisites")
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
    start.add_argument("--repo-head", required=True)
    start.add_argument("--branch", required=True)
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
    gateway = commands.add_parser("gateway", help="model gateway that pull workers (Kaggle) serve")
    gateway_commands = gateway.add_subparsers(dest="gateway_command", required=True)
    gateway_commands.add_parser("serve", help="run the gateway on loopback; publish /worker/* through Caddy")
    oc = gateway_commands.add_parser("opencode-config", help="print the opencode.json block for the gateway")
    oc.add_argument("--context", type=int, default=32768, help="context limit to declare per model")
    worker = commands.add_parser("worker", help="manage pull workers")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    token = worker_commands.add_parser("token", help="create a worker token (printed once)")
    token.add_argument("worker_id")
    token.add_argument("--force", action="store_true", help="replace an existing token (rotation)")
    return parser


def _load_operator_config(args: argparse.Namespace):
    return load_config(args.config)


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
    )
    runner = web.AppRunner(create_app(gateway))
    await runner.setup()
    site = web.TCPSite(runner, config.gateway.host, config.gateway.port)
    await site.start()
    _emit({"gateway": f"http://{config.gateway.host}:{config.gateway.port}",
           "public_url": config.gateway.public_url,
           "workers": [w.worker_id for w in workers], "models": gateway.served_models()})
    sys.stdout.flush()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        store.close()


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
        config = _load_operator_config(args)
        checks = {
            "config": "ok",
            "temporal_endpoint": config.temporal_endpoint,
            "task_queue": config.temporal_task_queue,
            "opencode_executable": config.opencode_executable,
            "harness2_checkout_exists": Path(config.harness2_checkout).exists(),
            "providers_enabled": [p.id for p in config.providers if p.enabled],
        }
        _emit({"status": "ok" if checks["harness2_checkout_exists"] else "needs_setup", "checks": checks})
        return 0
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
            _emit(opencode_provider_config(url, gateway_models(config), context=args.context))
            return 0
        if args.gateway_command == "serve":
            try:
                asyncio.run(_serve_gateway(args, config))
            except KeyboardInterrupt:
                pass
            return 0
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
        from temporalio.client import Client
        from .temporal_workflow import CodingTaskInput, CodingTaskWorkflow

        async def operate():
            client = await Client.connect(config.temporal_endpoint)
            action = args.workflow_command
            if action == "start":
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
        _emit(asyncio.run(operate()))
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

    if args.command in {"doctor", "setup", "provider", "workflow", "gateway", "worker"}:
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
            StaleWorkerResult, WorkspaceError) as error:
        print(f"kite: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())