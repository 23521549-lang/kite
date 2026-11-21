"""Autostart: the gateway starts a worker's Kaggle notebook when work arrives; the worker ends itself when idle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from poc.kaggle_worker import cli
from poc.kaggle_worker.config import ConfigError, validate_config
from poc.kaggle_worker.gateway import ModelGateway, WorkerAuth, create_app
from poc.kaggle_worker.launcher import KaggleLauncher, NOTEBOOK, inject_launch, parse_status
from poc.kaggle_worker.pull_worker import PullWorker

TOKEN_A = "a" * 32
TOKEN_B = "b" * 32


def raw_config(tmp_path: Path, *, launch: dict | None = None, second: bool = False) -> dict:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "kaggle_api_token_kaggle-han").write_text("KGAT_secret\n")
    provider = {
        "id": "kaggle-han", "adapter": "pull", "enabled": True, "owner": "han",
        "model": "qwen3.6", "models": ["gemma4-26b"], "capabilities": ["coding"],
        "quota": {"gpu_seconds": 108000, "concurrency": 1},
        "secrets": {"worker_token": f"file:{secrets / 'worker_token_kaggle-han'}",
                    "kaggle_api_token": f"file:{secrets / 'kaggle_api_token_kaggle-han'}"},
    }
    if launch is not None:
        provider["launch"] = launch
    providers = [provider]
    if second:
        providers.append({**provider, "id": "kaggle-an", "owner": "an", "launch": None,
                          "secrets": {"worker_token": f"file:{secrets / 'worker_token_kaggle-an'}"}})
        providers[1].pop("launch")
    return {
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "q"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": providers,
        "gateway": {"public_url": "https://1-2-3-4.sslip.io:8443"},
    }


class FakeKaggle:
    """Answers the Kaggle CLI calls the launcher makes and records them."""

    def __init__(self, status: str = "COMPLETE", push_ok: bool = True):
        self.status = status
        self.push_ok = push_ok
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.pushed: dict | None = None
        self.metadata: dict | None = None

    def __call__(self, command, env, timeout):
        args = command[1:]
        self.calls.append(args)
        self.envs.append(env)
        if args[:2] == ["kernels", "status"]:
            return 0, f'{args[2]} has status "KernelWorkerStatus.{self.status}"\n'
        if args[:2] == ["kernels", "pull"]:
            folder = Path(args[args.index("-p") + 1])
            (folder / "kite-worker.ipynb").write_text("{}")
            (folder / "kernel-metadata.json").write_text(json.dumps({
                "id": args[2], "id_no": 7, "title": "Kite worker", "code_file": "kite-worker.ipynb",
                "language": "python", "kernel_type": "notebook", "is_private": True, "enable_gpu": False,
                "enable_tpu": False, "enable_internet": True,
                "dataset_sources": ["han/kite-gguf-qwen3-6", "han/kite-llama-bin"], "machine_shape": None}))
            return 0, "Source code and metadata downloaded\n"
        if args[:2] == ["kernels", "push"]:
            folder = Path(args[args.index("-p") + 1])
            self.metadata = json.loads((folder / "kernel-metadata.json").read_text())
            self.pushed = json.loads((folder / self.metadata["code_file"]).read_text())
            if not self.push_ok:
                return 1, "Kernel push error: Notebook not found\n"
            return 0, "Kernel version 12 successfully pushed.  Please check progress at https://www.kaggle.com/x\n"
        return 2, "unexpected"


def make(tmp_path, *, launch=None, fake=None, second=False, **kwargs):
    config = validate_config(raw_config(tmp_path, launch=launch or {"kernel": "hanle/kite-worker"}, second=second))
    workers = [WorkerAuth("kaggle-han", TOKEN_A, ("qwen3.6", "gemma4-26b"))]
    if second:
        workers.append(WorkerAuth("kaggle-an", TOKEN_B, ("qwen3.6", "gemma4-26b")))
    clock = {"now": 10_000.0}
    gateway = ModelGateway(workers, clock=lambda: clock["now"])
    clock["now"] += 1000                                  # past the restart grace
    fake = fake or FakeKaggle()

    async def no_sleep(_seconds):
        return None

    launcher = KaggleLauncher(config, gateway, state_dir=tmp_path, runner=fake, clock=lambda: clock["now"],
                              command="kaggle", sleep=no_sleep, **kwargs)
    gateway.on_demand = launcher.on_demand
    return config, gateway, launcher, fake, clock


def code(nb: dict) -> str:
    return "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")


def launch_values(nb: dict) -> dict:
    import ast

    for line in code(nb).splitlines():
        if line.startswith("KITE_LAUNCH = {") and line != "KITE_LAUNCH = {}":
            return ast.literal_eval(line.split("=", 1)[1].split("   #")[0].strip())
    raise AssertionError("KITE_LAUNCH not filled in")


def test_a_request_with_no_connected_worker_pushes_the_notebook_with_the_requested_model(tmp_path):
    async def scenario():
        _config, gateway, launcher, fake, _clock = make(tmp_path)
        job = await gateway.submit({"model": "qwen3.6", "messages": []}, tag="chat")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if launcher.states["kaggle-han"].state != "launching":
                break
        gateway.cancel(job)
        return launcher, fake

    launcher, fake = asyncio.run(scenario())
    assert [c[:2] for c in fake.calls] == [["kernels", "status"], ["kernels", "pull"], ["kernels", "push"]]
    push = fake.calls[-1]
    # Like "Save & Run All" in the UI: GPU T4 x2, no session-timeout flag.
    assert push[push.index("--accelerator") + 1] == "NvidiaTeslaT4" and "-t" not in push
    # The datasets attached in the Kaggle UI are kept; GPU and Internet are switched on.
    assert fake.metadata["dataset_sources"] == ["han/kite-gguf-qwen3-6", "han/kite-llama-bin"]
    assert fake.metadata["enable_gpu"] is True and fake.metadata["enable_internet"] is True
    assert "enable_tpu" not in fake.metadata
    values = launch_values(fake.pushed)
    assert values["model"] == "qwen3.6" and values["idle_minutes"] == 30 and values["worker_id"] == "kaggle-han"
    assert values["gateway_url"] == "https://1-2-3-4.sslip.io:8443" and "token" not in values
    assert values["expires"] == 11_000 + 6 * 3600
    state = launcher.status()["kaggle-han"]
    assert state["state"] == "starting" and state["version"] == "12" and state["reason"] == "request"
    # The Kaggle CLI gets the token as a path to the 0600 secret file, in its environment only.
    env = fake.envs[0]
    assert env["KAGGLE_API_TOKEN"].endswith("kaggle_api_token_kaggle-han") and "KAGGLE_KEY" not in env
    assert env["KAGGLE_CONFIG_DIR"] and not Path(env["KAGGLE_CONFIG_DIR"]).exists()   # temporary, removed


def test_no_push_while_a_worker_is_connected_even_if_it_is_loading_a_model(tmp_path):
    async def scenario():
        _config, gateway, launcher, fake, _clock = make(tmp_path)
        await gateway.heartbeat("kaggle-han", {"ready": False, "loaded_model": None})   # swapping models
        job = await gateway.submit({"model": "gemma4-26b", "messages": []})
        result = await launcher.wake(model="gemma4-26b")
        gateway.cancel(job)
        return result, fake

    result, fake = asyncio.run(scenario())
    assert result["state"] == "alive" and fake.calls == []


def test_a_run_already_queued_on_kaggle_is_waited_for_not_pushed_again(tmp_path):
    async def scenario():
        _config, _gateway, launcher, fake, _clock = make(tmp_path, fake=FakeKaggle(status="RUNNING"))
        first = await launcher.wake(model="qwen3.6", reason="chat")
        second = await launcher.wake(model="qwen3.6", reason="request")
        return first, second, fake

    first, second, fake = asyncio.run(scenario())
    assert first["state"] == second["state"] == "starting"
    assert [c[:2] for c in fake.calls] == [["kernels", "status"]]


def test_a_failed_push_is_reported_and_not_retried_at_once(tmp_path):
    async def scenario():
        _config, _gateway, launcher, fake, clock = make(tmp_path, fake=FakeKaggle(push_ok=False))
        first = await launcher.wake(model="qwen3.6")
        again = await launcher.wake(model="qwen3.6")
        calls = len(fake.calls)
        clock["now"] += 601
        fake.push_ok = True
        later = await launcher.wake(model="qwen3.6")
        return first, again, calls, later

    first, again, calls, later = asyncio.run(scenario())
    assert first["state"] == "error" and "Notebook not found" in first["detail"]
    assert again["state"] == "error" and calls == 3         # status, pull, push: nothing more within 10 minutes
    assert later["state"] == "starting"


def test_while_starting_kite_follows_the_kaggle_run_and_reports_a_stop_at_once(tmp_path):
    async def scenario():
        fake = FakeKaggle()
        _config, gateway, launcher, _fake, clock = make(tmp_path, fake=fake)
        await launcher.wake(model="qwen3.6")
        seen = []
        fake.status = "QUEUED"
        clock["now"] += 60
        await launcher.check_starting()
        clock["now"] += 14 * 60
        await launcher.check_starting()
        seen.append(launcher.status()["kaggle-han"])
        fake.status = "RUNNING"
        clock["now"] += 60
        await launcher.check_starting()
        seen.append(launcher.status()["kaggle-han"])
        fake.status = "ERROR"
        clock["now"] += 60                      # long before the old 25-minute wait
        await launcher.check_starting()
        seen.append(launcher.status()["kaggle-han"])
        return seen

    queued, running, stopped = asyncio.run(scenario())
    assert queued["state"] == "starting" and queued["kaggle"] == "queued" and queued["kaggle_seconds"] == 15 * 60
    assert "xếp hàng GPU cho version 12 (15 phút)" in queued["detail"] and "Run All" in queued["detail"]
    assert running["kaggle"] == "running" and running["kaggle_seconds"] == 0 and "đang nạp model" in running["detail"]
    assert stopped["state"] == "error" and stopped["kaggle"] == "error" and "per-launch" in stopped["detail"]


def test_a_run_cancelled_on_kaggle_is_reported_without_the_secret_hint(tmp_path):
    async def scenario():
        fake = FakeKaggle()
        _config, _gateway, launcher, _fake, _clock = make(tmp_path, fake=fake)
        await launcher.wake(model="qwen3.6")
        fake.status = "CANCEL_ACKNOWLEDGED"
        await launcher.check_starting()
        return launcher.status()["kaggle-han"]

    state = asyncio.run(scenario())
    assert state["state"] == "error" and "cancel_acknowledged" in state["detail"] and "per-launch" not in state["detail"]


def test_worker_status_reads_like_a_sentence(tmp_path, capsys, monkeypatch):
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    report = {"queued_jobs": 1, "workers": [
        {"worker_id": "kaggle-han", "alive": False, "loaded_model": None, "busy": False,
         "launch": {"state": "starting", "seconds": 900, "kaggle": "queued", "kaggle_seconds": 840,
                    "detail": "Kaggle đang xếp hàng GPU cho version 5 (14 phút)", "version": "5"}},
        {"worker_id": "kaggle-an", "alive": True, "loaded_model": "qwen3.6", "busy": True}]}
    monkeypatch.setattr(cli, "_gateway_call", lambda *a, **k: report)
    assert cli.main(["--config", str(path), "worker", "status"]) == 0
    text = capsys.readouterr().out
    assert "kaggle-han: chưa kết nối" in text and "tự bật: starting (15 phút), Kaggle: queued (14 phút)" in text
    assert "xếp hàng GPU cho version 5" in text and "kaggle-an: sống, model qwen3.6, đang làm việc" in text
    assert "1 request đang chờ" in text
    assert cli.main(["--config", str(path), "worker", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_per_launch_token_is_accepted_until_the_run_ends_and_survives_a_gateway_restart(tmp_path):
    async def scenario():
        config, gateway, launcher, fake, clock = make(
            tmp_path, launch={"kernel": "hanle/kite-worker", "token": "per-launch", "idle_minutes": 20})
        await launcher.wake(worker="kaggle-han", reason="manual")
        token = launch_values(fake.pushed)["token"]
        accepted = gateway.authenticate(f"Bearer {token}")
        saved = json.loads((tmp_path / "secrets" / "launch_token_kaggle-han.json").read_text())
        # A restarted gateway loads the digest (never the token) and still accepts the running notebook.
        fresh = ModelGateway([WorkerAuth("kaggle-han", TOKEN_A, ("qwen3.6", "gemma4-26b"))], clock=lambda: clock["now"])
        KaggleLauncher(config, fresh, state_dir=tmp_path, runner=fake, clock=lambda: clock["now"]).load_tokens()
        after_restart = fresh.authenticate(f"Bearer {token}")
        clock["now"] += 13 * 3600
        expired = fresh.authenticate(f"Bearer {token}")
        return token, accepted, saved, after_restart, expired, launch_values(fake.pushed)

    token, accepted, saved, after_restart, expired, values = asyncio.run(scenario())
    assert accepted == after_restart == "kaggle-han" and expired is None
    assert saved["digest"] == hashlib.sha256(token.encode()).hexdigest() and token not in json.dumps(saved)
    assert (tmp_path / "secrets" / "launch_token_kaggle-han.json").stat().st_mode & 0o777 == 0o600
    assert values["idle_minutes"] == 20


def test_restart_grace_waits_for_running_workers_to_reconnect(tmp_path):
    slept = []

    async def scenario():
        config = validate_config(raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"}))
        clock = {"now": 5000.0}
        gateway = ModelGateway([WorkerAuth("kaggle-han", TOKEN_A, ("qwen3.6", "gemma4-26b"))],
                               clock=lambda: clock["now"])

        async def sleep(seconds):
            slept.append(seconds)
            clock["now"] += seconds
            await gateway.heartbeat("kaggle-han", {"ready": True})   # the old worker came back meanwhile

        fake = FakeKaggle()
        launcher = KaggleLauncher(config, gateway, state_dir=tmp_path, runner=fake, clock=lambda: clock["now"],
                                  sleep=sleep)
        clock["now"] += 30
        return await launcher.wake(model="qwen3.6"), fake

    result, fake = asyncio.run(scenario())
    assert slept == [60.0] and result["state"] == "alive" and fake.calls == []


def test_only_workers_with_launch_are_started(tmp_path):
    async def scenario():
        _config, gateway, launcher, fake, _clock = make(tmp_path, second=True)
        await gateway.heartbeat("kaggle-an", {"ready": True})
        with pytest.raises(ValueError):
            await launcher.wake(worker="kaggle-an")
        return sorted(launcher.states), await launcher.wake(model="qwen3.6")

    states, result = asyncio.run(scenario())
    # kaggle-an has no launch: it is never started, but it is connected and serves the model.
    assert states == ["kaggle-han"] and result["state"] == "starting"


def test_wake_and_stop_routes_are_local_only_and_stop_reaches_the_worker(tmp_path):
    async def scenario():
        _config, gateway, launcher, fake, _clock = make(tmp_path)
        async with TestClient(TestServer(create_app(gateway, launcher=launcher))) as client:
            woke = await (await client.post("/kite/wake", json={"model": "qwen3.6", "reason": "chat"})).json()
            headers = {"Authorization": f"Bearer {TOKEN_A}"}
            await client.post("/worker/heartbeat", json={"ready": True}, headers=headers)
            status = await (await client.get("/status")).json()
            stopped = await client.post("/kite/stop", json={"worker": "kaggle-han"})
            beat = await (await client.post("/worker/heartbeat", json={"ready": True}, headers=headers)).json()
            job = await gateway.submit({"model": "qwen3.6", "messages": []})
            picked = await gateway.next_job("kaggle-han", 0)
            gateway.cancel(job)
            await client.post("/worker/heartbeat", json={"ready": False, "draining": True}, headers=headers)
            return woke, status, stopped.status, beat, picked, gateway.stop_requested

    woke, status, stopped, beat, picked, pending = asyncio.run(scenario())
    assert woke["state"] == "starting" and woke["reason"] == "chat"
    assert status["workers"][0]["launch"]["state"] == "alive"
    assert stopped == 200 and beat["stop"] is True and picked is None and pending == set()


def test_routes_refuse_callers_that_are_not_on_loopback(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    _config, gateway, launcher, _fake, _clock = make(tmp_path)
    app = create_app(gateway, launcher=launcher)
    handler = next(r.handler for r in app.router.routes() if r.resource.canonical == "/kite/wake")
    request = make_mocked_request("POST", "/kite/wake", app=app,
                                  transport=type("T", (), {"get_extra_info": lambda self, k, d=None:
                                                           ("172.19.0.5", 1) if k == "peername" else d})())
    from aiohttp import web

    with pytest.raises(web.HTTPForbidden):
        asyncio.run(handler(request))


def test_pull_worker_stops_when_idle_and_when_the_gateway_asks():
    calls = []

    class Gate(PullWorker):
        def __init__(self, *, stop_reply=False, **kwargs):
            super().__init__("https://gw", TOKEN_A, ensure_model=lambda m: None, loaded_model=lambda: "qwen3.6",
                             poll_wait_seconds=0, heartbeat_seconds=60, log=calls.append, **kwargs)
            self.stop_reply = stop_reply

        def _call(self, method, url, body=None, *, timeout=70, auth=True):
            if url.endswith("/worker/heartbeat"):
                return 200, {"worker_id": "w", "stop": self.stop_reply}
            return 204, None

    idle = Gate(stop_when_idle_seconds=0.05)
    idle.last_job_at -= 1
    idle.run()
    assert idle.draining and any("tự tắt" in line for line in calls)

    asked = Gate(stop_reply=True)
    asked.run()
    assert asked.draining and any("kite worker stop" in line for line in calls)


def test_notebook_worker_cell_is_pull_worker_py_and_has_the_launch_line():
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    sources = ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]
    assert Path("poc/kaggle_worker/pull_worker.py").read_text(encoding="utf-8") in sources
    filled = inject_launch(nb, {"worker_id": "kaggle-han", "model": "qwen3.6", "idle_minutes": 30})
    assert launch_values(filled)["model"] == "qwen3.6"
    setup_cell = next(s for s in sources if s.count("KITE_LAUNCH = {}") == 2)
    assert 'KAGGLE_KERNEL_RUN_TYPE") == "Interactive"' in setup_cell and 'get("expires"' in setup_cell
    worker_cell = next(s for s in sources if "WORKER_TOKEN = KITE_LAUNCH.get(\"token\")" in s)
    assert "stop_when_idle_seconds=IDLE_MINUTES * 60" in worker_cell
    with pytest.raises(ValueError):
        inject_launch({"cells": []}, {})


def test_parse_status():
    assert parse_status('han/kite has status "KernelWorkerStatus.RUNNING"') == "running"
    assert parse_status('han/kite has status "complete"') == "complete"
    assert parse_status("403 Forbidden") is None


def test_launch_config_is_validated(tmp_path):
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker", "token": "per-launch", "model": "gemma4-26b"})
    launch = validate_config(raw).providers[0].launch
    assert (launch.kernel, launch.token, launch.model, launch.idle_minutes) == \
        ("hanle/kite-worker", "per-launch", "gemma4-26b", 30)
    for bad in ({"kernel": "no-slash"}, {"kernel": "a/b", "token": "plain"}, {"kernel": "a/b", "idle_minutes": 1},
                {"kernel": "a/b", "model": "llama"}, {"kernel": "a/b", "max_hours": 13}, {"kernel": "a/b", "x": 1}):
        with pytest.raises(ConfigError):
            validate_config(raw_config(tmp_path, launch=bad))
    raw = raw_config(tmp_path, launch={"kernel": "a/b"})
    del raw["providers"][0]["secrets"]["kaggle_api_token"]
    with pytest.raises(ConfigError, match="kaggle_api_token"):
        validate_config(raw)


def test_worker_autostart_command_stores_the_token_and_edits_the_config(tmp_path, monkeypatch, capsys):
    raw = raw_config(tmp_path)
    del raw["providers"][0]["secrets"]["kaggle_api_token"]
    (tmp_path / "secrets" / "kaggle_api_token_kaggle-han").unlink()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "KGAT_new")
    assert cli.main(["--config", str(path), "worker", "autostart", "kaggle-han", "--kernel", "hanle/kite-worker",
                     "--idle-minutes", "45"]) == 0
    report = json.loads(capsys.readouterr().out)
    token = tmp_path / "secrets" / "kaggle_api_token_kaggle-han"
    assert token.read_text() == "KGAT_new\n" and token.stat().st_mode & 0o777 == 0o600
    saved = json.loads(path.read_text())["providers"][0]
    assert saved["launch"] == {"kernel": "hanle/kite-worker", "idle_minutes": 45, "token": "kaggle-secret"}
    assert saved["secrets"]["kaggle_api_token"] == f"file:{token.resolve()}"
    assert "KGAT_new" not in json.dumps(report) and report["next"][1] == "kite worker start kaggle-han"
    assert cli.main(["--config", str(path), "worker", "autostart", "kaggle-han", "--kernel", "x/y", "--off"]) == 0
    assert "launch" not in json.loads(path.read_text())["providers"][0]


def test_chat_setup_tells_the_mcp_server_which_model_to_start(tmp_path, capsys):
    raw = raw_config(tmp_path)
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("t" * 40 + "\n")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    chat = tmp_path / "chat"
    assert cli.main(["--config", str(path), "chat", "setup", "--dir", str(chat), "--allow-root", str(tmp_path)]) == 0
    capsys.readouterr()
    command = json.loads((chat / "opencode.json").read_text())["mcp"]["kite"]["command"]
    assert command[command.index("--lead-model") + 1] == "qwen3.6"
