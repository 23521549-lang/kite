"""P2: rented GPU machines (RunPod pods) that Kite starts on demand and stops when idle; streaming relays."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from poc.kaggle_worker import cli
from poc.kaggle_worker.config import ConfigError, validate_config
from poc.kaggle_worker.gateway import ModelGateway, WorkerAuth, create_app
from poc.kaggle_worker.journal import spend_summary
from poc.kaggle_worker.pods import PodController, PodError, RunPodClient
from poc.kaggle_worker.relay import RelaySpec, RelayWorker, SpendLedger, relay_specs
from poc.kaggle_worker.starters import Starters

from tests.poc.kaggle_worker.test_launcher import make, raw_config

TOKEN = "p" * 32
DAY = 1795400000.0        # 2026-11-23 UTC


def pod_raw(tmp_path, **pod):
    raw = raw_config(tmp_path)
    secrets = tmp_path / "secrets"
    (secrets / "runpod_api_key_runpod").write_text("rp-secret\n")
    (secrets / "api_key_runpod").write_text("vllm-secret\n")
    raw["providers"].append({
        "id": "runpod", "adapter": "relay", "enabled": True, "owner": "self", "model": "qwen3-32b",
        "capabilities": ["coding"], "quota": {"gpu_seconds": 0, "concurrency": 4},
        "secrets": {"runpod_api_key": f"file:{secrets / 'runpod_api_key_runpod'}",
                    "api_key": f"file:{secrets / 'api_key_runpod'}"},
        "relay": {"kind": "openai", "pod": {"id": "abc123xyz", **pod}, "caps": {"daily_usd": 5}}})
    return raw


def test_a_pod_is_an_openai_relay_that_streams_through_runpods_proxy(tmp_path):
    config = validate_config(pod_raw(tmp_path, price_hour=0.79))
    relay = config.providers[1].relay
    assert relay.base_url == "https://abc123xyz-8000.proxy.runpod.net/v1" and relay.stream
    assert (relay.pod.pod_id, relay.pod.port, relay.pod.price_hour, relay.pod.idle_minutes) == ("abc123xyz", 8000, 0.79, 30)
    spec = relay_specs(config)[0]
    assert spec.paid and spec.stream and spec.pod.pod_id == "abc123xyz"
    for bad in ({"id": "x"}, {"port": 0}, {"price_hour": -1}, {"idle_minutes": 1}, {"provider": "vast"},
                {"start_timeout_seconds": 5}, {"gpu": "A100"}):
        raw = pod_raw(tmp_path, **bad)
        if "id" in bad:
            raw["providers"][1]["relay"]["pod"]["id"] = bad["id"]
        with pytest.raises(ConfigError):
            validate_config(raw)
    raw = pod_raw(tmp_path)
    del raw["providers"][1]["secrets"]["runpod_api_key"]
    with pytest.raises(ConfigError, match="runpod_api_key"):
        validate_config(raw)
    raw = pod_raw(tmp_path)
    raw["providers"][1]["relay"]["kind"] = "anthropic"
    raw["providers"][1]["secrets"]["api_key"] = raw["providers"][1]["secrets"]["runpod_api_key"]
    with pytest.raises(ConfigError, match="kind openai"):
        validate_config(raw)


def test_a_pod_relay_without_a_server_api_key_is_rejected(tmp_path):
    raw = pod_raw(tmp_path)
    del raw["providers"][1]["secrets"]["api_key"]
    with pytest.raises(ConfigError, match="--api-key"):
        validate_config(raw)


# ---------------------------------------------------------------- the controller, with a fake RunPod
class FakeRunPod:
    def __init__(self, statuses=("STARTING", "RUNNING"), cost=0.69, start_error=None):
        self.statuses, self.cost, self.start_error = list(statuses), cost, start_error
        self.actions: list[str] = []
        self.status = "EXITED"

    async def get(self, pod_id):
        if self.statuses and self.actions and self.actions[-1] == "start":
            self.status = self.statuses.pop(0)
        return {"id": pod_id, "status": self.status, "cost": self.cost}

    async def action(self, pod_id, action):
        self.actions.append(action)
        if action == "start" and self.start_error:
            raise self.start_error
        self.status = "EXITED" if action == "stop" else self.status
        return {"id": pod_id, "status": self.status, "cost": self.cost}


def controller(*, pod=None, fake=None, healthy=lambda n: n >= 2, caps=None, price=None):
    from poc.kaggle_worker.config import PodConfig

    clock = {"now": DAY}
    pod = pod or PodConfig("abc123xyz", price_hour=price, idle_minutes=30, start_timeout=900)
    spec = RelaySpec("runpod", "openai", "https://abc123xyz-8000.proxy.runpod.net/v1", "vllm-key", ("qwen3-32b",),
                     slots=2, stream=True, pod=pod, **(caps or {}))
    gateway = ModelGateway([WorkerAuth("runpod", TOKEN, ("qwen3-32b",))], clock=lambda: clock["now"])
    ledger = SpendLedger(clock=lambda: clock["now"])
    records, told = [], []
    relay = RelayWorker(spec, gateway, ledger, clock=lambda: clock["now"])
    fake = fake or FakeRunPod()
    checks = {"n": 0}

    async def health():
        checks["n"] += 1
        return healthy(checks["n"])

    async def sleep(seconds):
        clock["now"] += seconds

    ctl = PodController(spec, fake, relay, ledger, clock=lambda: clock["now"], sleep=sleep, health=health,
                        journal=lambda name, record: records.append((name, record)),
                        notify=lambda title, message, **kw: told.append((title, message)))
    return ctl, gateway, relay, ledger, fake, clock, records, told


async def settle(ctl):
    for _ in range(100):
        if ctl.s.state != "starting":
            return
        await asyncio.sleep(0)


def test_a_request_starts_the_pod_and_its_worker_joins_once_the_server_answers():
    async def scenario():
        ctl, gateway, relay, ledger, fake, clock, records, told = controller()
        before = gateway.present("runpod")
        report = await ctl.wake(reason="request")
        await settle(ctl)
        return report, before, ctl.status(), gateway.present("runpod"), fake, ctl.price, records

    report, before, status, present, fake, price, records = asyncio.run(scenario())
    assert not before and report["state"] == "starting"
    assert fake.actions == ["start"] and status["state"] == "up" and present
    assert price == 0.69                                               # RunPod's own price when none is set
    assert [r["event"] for name, r in records if name == "launches"] == ["push", "alive"]


def test_an_idle_pod_is_stopped_and_its_hours_are_billed():
    async def scenario():
        ctl, gateway, relay, ledger, fake, clock, records, told = controller(price=1.2)
        await ctl.wake()
        await settle(ctl)
        clock["now"] += 20 * 60
        await ctl.tick()
        mid = ctl.s.state
        relay.last_active = clock["now"] - 31 * 60                       # nothing for 31 minutes
        await ctl.tick()
        return ctl.status(), mid, fake.actions, gateway.present("runpod"), ledger.spent("runpod", clock["now"]), records

    status, mid, actions, present, spent, records = asyncio.run(scenario())
    assert mid == "up" and status["state"] == "off" and actions == ["start", "stop"] and not present
    hours = sum(r["hours"] for name, r in records if name == "spend")
    usd = sum(r["usd"] for name, r in records if name == "spend")
    assert spent[0] == pytest.approx(usd, abs=1e-5) and hours == pytest.approx(20 / 60, abs=0.01)
    assert usd == pytest.approx(hours * 1.2, abs=1e-3)
    assert spend_summary([r for name, r in records if name == "spend"])["runpod"]["requests"] == 0
    stop = [r for name, r in records if name == "launches"][-1]
    assert stop["event"] == "stopped" and stop["reason"] == "idle"


def test_a_busy_pod_is_not_stopped_while_it_answers():
    async def scenario():
        ctl, gateway, relay, *_rest = controller(price=1.0)
        await ctl.wake()
        await settle(ctl)
        relay.active, relay.last_active = 1, 0.0
        await ctl.tick()
        return ctl.s.state

    assert asyncio.run(scenario()) == "up"


def test_at_the_cap_the_pod_is_stopped_and_you_are_told():
    async def scenario():
        ctl, gateway, relay, ledger, fake, clock, records, told = controller(price=2.0, caps={"daily_cap": 1.0})
        await ctl.wake()
        await settle(ctl)
        clock["now"] += 31 * 60                                          # 1.03 USD at 2 USD/hour
        relay.last_active = clock["now"]
        await ctl.tick()
        again = await ctl.wake()
        return ctl.status(), told, fake.actions, again

    status, told, actions, again = asyncio.run(scenario())
    assert actions == ["start", "stop"] and "hạn mức" in told[0][0]
    assert again["state"] == "off" and "không bật" in again["detail"]     # no start past the cap


def test_a_pod_that_never_gets_ready_is_stopped_so_it_does_not_bill_all_day():
    async def scenario():
        ctl, gateway, relay, ledger, fake, clock, records, told = controller(healthy=lambda n: False, price=1.0)
        await ctl.wake()
        for _ in range(500):
            if ctl.s.state != "starting":
                break
            await asyncio.sleep(0)
        return ctl.status(), fake.actions, told

    status, actions, told = asyncio.run(scenario())
    assert status["state"] == "error" and "không sẵn sàng" in status["detail"]
    assert actions == ["start", "stop"] and told


def test_runpod_saying_no_is_an_error_and_already_running_is_fine():
    async def scenario(error):
        ctl, *_rest, fake, clock, records, told = controller(fake=FakeRunPod(start_error=error))
        await ctl.wake()
        await settle(ctl)
        return ctl.status()

    refused = asyncio.run(scenario(PodError("RunPod HTTP 400: no GPU available", status=400)))
    assert refused["state"] == "error" and "no GPU" in refused["detail"]
    running = asyncio.run(scenario(PodError("RunPod HTTP 409: already running", status=409)))
    assert running["state"] == "up"


def test_kite_takes_over_a_pod_that_was_running_when_it_restarted():
    async def scenario():
        fake = FakeRunPod(statuses=())
        fake.status = "RUNNING"
        ctl, gateway, *_rest = controller(fake=fake, healthy=lambda n: True)
        await ctl.adopt()
        await settle(ctl)
        return ctl.status(), fake.actions, ctl.s.billed_from > 0

    status, actions, billed = asyncio.run(scenario())
    assert status["state"] == "up" and actions == [] and billed


def test_runpod_client_calls_the_v2_api_with_the_key():
    async def scenario():
        seen = []

        async def handler(request):
            seen.append((request.method, request.path, request.headers.get("Authorization"),
                         await request.json() if request.can_read_body else None))
            if request.path.endswith("/action"):
                return web.json_response({"error": "pod is already running"}, status=409)
            return web.json_response({"id": "abc123xyz", "status": "EXITED", "cost": 0.44})

        app = web.Application()
        app.router.add_route("*", "/v2/pods/{pod}", handler)
        app.router.add_route("*", "/v2/pods/{pod}/action", handler)
        server = TestServer(app)
        await server.start_server()
        client = RunPodClient("rp-secret", base_url=str(server.make_url("/v2")))
        pod = await client.get("abc123xyz")
        try:
            await client.action("abc123xyz", "start")
            conflict = None
        except PodError as exc:
            conflict = exc.status
        await server.close()
        return seen, pod, conflict

    seen, pod, conflict = asyncio.run(scenario())
    assert seen[0][:3] == ("GET", "/v2/pods/abc123xyz", "Bearer rp-secret")
    assert seen[1] == ("POST", "/v2/pods/abc123xyz/action", "Bearer rp-secret", {"action": "start"})
    assert pod["cost"] == 0.44 and conflict == 409


# ---------------------------------------------------------------- one set of hooks for Kaggle and pods
def test_free_kaggle_first_then_the_pod_and_kite_stops_only_pods(tmp_path):
    async def scenario():
        _config, gateway, kaggle, fake, _clock = make(tmp_path)
        ctl, *_rest = controller()
        starters = Starters(kaggle, [ctl])
        woken = []
        ctl.on_demand = lambda: woken.append("pod")
        kaggle.on_demand = lambda model: woken.append(("kaggle", model))
        starters.on_demand("qwen3.6")                                    # Kaggle serves it: free first
        starters.on_demand("qwen3-32b")                                  # only the pod serves it
        kaggle.states["kaggle-han"].failures = 5
        stopped = await starters.stop("runpod"), await starters.stop("kaggle-han")
        manual = await starters.wake(worker="runpod")
        await settle(ctl)
        return woken, stopped, manual, sorted(starters.status())

    woken, stopped, manual, workers = asyncio.run(scenario())
    assert woken == [("kaggle", "qwen3.6"), "pod"]
    assert stopped[0]["stopped"] and stopped[1] is None                 # Kaggle: the gateway asks the worker
    assert manual["worker"] == "runpod" and manual["state"] == "starting"
    assert workers == ["kaggle-han", "runpod"]


def test_a_chat_message_for_the_pods_model_starts_it_and_kite_stop_turns_it_off():
    async def scenario():
        ctl, gateway, relay, ledger, fake, clock, records, told = controller(price=1.0)
        starters = Starters(None, [ctl])
        gateway.on_demand, gateway.starting, gateway.wait_note = (starters.on_demand, starters.starting,
                                                                  starters.wait_note)
        async with TestClient(TestServer(create_app(gateway, launcher=starters))) as client:
            reply = await client.post("/v1/chat/completions", headers={"X-Kite-Task": "chat"},
                                      json={"model": "qwen3-32b", "messages": [{"role": "user", "content": "chào"}]})
            note = (await reply.json())["choices"][0]["message"]["content"]
            await settle(ctl)
            up = ctl.s.state
            stop = await client.post("/kite/stop", json={"worker": "runpod"})
            stopped = await stop.json()
        return note, up, stopped, fake.actions

    note, up, stopped, actions = asyncio.run(scenario())
    assert "RunPod" in note and up == "up"
    assert stopped["stopped"] and stopped["state"] == "off" and actions == ["start", "stop"]


# ---------------------------------------------------------------- streaming relay (the pod's model server)
def sse_server(seen):
    async def handler(request):
        seen.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for piece in ("xin ", "chào"):
            chunk = {"id": "c1", "object": "chat.completion.chunk", "model": "Qwen/Qwen3-32B",
                     "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        end = {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        usage = {"id": "c1", "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}}
        await response.write(f"data: {json.dumps(end)}\n\ndata: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    return app


def test_a_streaming_relay_passes_the_answer_on_as_it_comes():
    async def scenario():
        seen = []
        upstream = TestServer(sse_server(seen))
        await upstream.start_server()
        spec = RelaySpec("vllm", "openai", str(upstream.make_url("/v1")), None, ("qwen3-32b",),
                         upstream={"qwen3-32b": "Qwen/Qwen3-32B"}, stream=True)
        gateway = ModelGateway([WorkerAuth("vllm", TOKEN, ("qwen3-32b",))])
        records = []
        relay = RelayWorker(spec, gateway, SpendLedger(), journal=lambda name, record: records.append(record))
        task = asyncio.create_task(relay.run(beat_every=0.05))
        async with TestClient(TestServer(create_app(gateway))) as client:
            streamed = await client.post("/v1/chat/completions", json={
                "model": "qwen3-32b", "stream": True, "messages": [{"role": "user", "content": "chào"}]})
            text = await streamed.text()
            whole = await client.post("/v1/chat/completions", json={
                "model": "qwen3-32b", "messages": [{"role": "user", "content": "chào"}]})
            body = await whole.json()
        task.cancel()
        await upstream.close()
        return seen, text, body, records

    seen, text, body, records = asyncio.run(scenario())
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}
    assert seen[0]["model"] == "Qwen/Qwen3-32B"
    pieces = [json.loads(line[6:])["choices"][0]["delta"].get("content") for line in text.splitlines()
              if line.startswith("data: {") and json.loads(line[6:]).get("choices")]
    assert "xin " in pieces and "chào" in pieces and text.rstrip().endswith("data: [DONE]")
    assert body["choices"][0]["message"]["content"] == "xin chào" and body["usage"]["prompt_tokens"] == 12
    assert records[0]["prompt_tokens"] == 12 and records[0]["completion_tokens"] == 2


# ---------------------------------------------------------------- kite worker add-runpod
def test_add_runpod_keeps_both_keys_private_and_validates_first(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw_config(tmp_path)))
    keys = iter(["rp-key-value", "vllm-key-value"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(keys))
    code = cli.main(["--config", str(path), "worker", "add-runpod", "runpod-a100", "--pod-id", "abc123xyz",
                     "--model", "qwen3-32b=Qwen/Qwen3-32B", "--price-hour", "1.19", "--daily-cap", "10"])
    assert code == 0
    raw = json.loads(path.read_text())
    provider = raw["providers"][-1]
    assert provider["relay"]["pod"] == {"provider": "runpod", "id": "abc123xyz", "port": 8000, "idle_minutes": 30,
                                        "price_hour": 1.19}
    assert provider["relay"]["upstream"] == {"qwen3-32b": "Qwen/Qwen3-32B"}
    for name, value in (("runpod_api_key_runpod-a100", "rp-key-value"), ("api_key_runpod-a100", "vllm-key-value")):
        key = tmp_path / "secrets" / name
        assert key.read_text().strip() == value and os.stat(key).st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert "rp-key-value" not in out and "vllm-key-value" not in out
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": pytest.fail("asked for a key"))
    assert cli.main(["--config", str(path), "worker", "add-runpod", "bad", "--pod-id", "x",
                     "--model", "m"]) != 0                               # invalid pod id: no key asked


def test_status_does_not_call_an_api_worker_an_old_notebook():
    report = {"workers": [{"worker_id": "openrouter", "alive": True, "loaded_model": "gpt", "slots": 2,
                           "version": "relay-openai"},
                          {"worker_id": "runpod", "alive": False, "launch": {"state": "starting", "seconds": 60,
                                                                              "detail": "RunPod: STARTING"}}],
              "queued_jobs": 1}
    text = cli._worker_status_text(report)
    assert "notebook cũ" not in text and "runpod: chưa kết nối" in text and "RunPod: STARTING" in text


def test_a_pod_that_is_off_does_not_look_connected_while_its_relay_runs():
    async def scenario():
        ctl, gateway, relay, *_rest = controller(price=1.0)
        task = asyncio.create_task(relay.run(beat_every=0.05))
        await asyncio.sleep(0.2)
        off = gateway.present("runpod"), gateway.serving("qwen3-32b")
        await ctl.wake()
        await settle(ctl)
        await asyncio.sleep(0.1)
        up = gateway.present("runpod")
        await ctl.stop()
        await asyncio.sleep(0.2)
        after = gateway.present("runpod")
        task.cancel()
        return off, up, after

    off, up, after = asyncio.run(scenario())
    assert off == (False, False) and up and not after


# ---------------------------------------------------------------- the owner's switches (web, Worker tab)
def test_switches_turn_an_api_worker_off_and_keep_kite_from_starting_a_worker(tmp_path):
    from poc.kaggle_worker.starters import Switches

    async def scenario():
        switches = Switches(tmp_path / "switches.json")
        spec = RelaySpec("groq", "openai", "https://api.groq.com/openai/v1", "k", ("llama",))
        gateway = ModelGateway([WorkerAuth("groq", TOKEN, ("llama",))])
        relay = RelayWorker(spec, gateway, SpendLedger(), available=lambda: switches.on("groq"))
        await relay.beat()
        before = gateway.present("groq")
        async with TestClient(TestServer(create_app(gateway, switches=switches, relays={"groq": relay}))) as client:
            off = await (await client.post("/kite/switch", json={"worker": "groq", "on": False})).json()
            after_off = gateway.present("groq")
            status = await (await client.get("/status")).json()
            on = await (await client.post("/kite/switch", json={"worker": "groq", "on": True})).json()
            bad = (await client.post("/kite/switch", json={"worker": "nobody", "on": True})).status
        return before, off, after_off, status, on, gateway.present("groq"), bad, Switches(tmp_path / "switches.json")

    before, off, after_off, status, on, after_on, bad, reloaded = asyncio.run(scenario())
    assert before and off == {"worker": "groq", "on": False, "auto": True} and not after_off
    assert status["workers"][0]["switch"] == {"on": False, "auto": True}
    assert on["on"] and after_on and bad == 400
    assert reloaded.on("groq") and reloaded.auto("groq")                   # saved to switches.json


def test_with_auto_off_kite_does_not_start_that_notebook_or_pod(tmp_path):
    from poc.kaggle_worker.starters import Switches

    async def scenario():
        _config, gateway, kaggle, fake, _clock = make(tmp_path)
        ctl, *_rest = controller()
        switches = Switches(None)
        starters = Starters(kaggle, [ctl], switches)
        woken = []
        ctl.on_demand = lambda: woken.append("pod")
        kaggle.on_demand = lambda model: woken.append(("kaggle", model))
        switches.set("kaggle-han", auto=False)
        switches.set("runpod", auto=False)
        starters.on_demand("qwen3.6")
        starters.on_demand("qwen3-32b")
        candidates = kaggle._candidates("qwen3.6", None)
        manual = kaggle._candidates(None, "kaggle-han")                      # kite worker start still works
        return woken, candidates, manual

    woken, candidates, manual = asyncio.run(scenario())
    assert woken == [] and candidates == [] and manual == ["kaggle-han"]


def test_a_gateway_restart_counts_the_pods_full_uptime(tmp_path):
    """The pod keeps billing while the gateway is down: adoption resumes from the saved cursor."""
    from poc.kaggle_worker.config import PodConfig
    from poc.kaggle_worker.spend_ledger import SpendStore

    async def scenario():
        store = SpendStore(tmp_path / "spend.sqlite3")
        clock = {"now": DAY}
        pod = PodConfig("abc123xyz", price_hour=1.0, idle_minutes=30, start_timeout=900)
        spec = RelaySpec("runpod", "openai", "https://abc123xyz-8000.proxy.runpod.net/v1", "vllm-key",
                         ("qwen3-32b",), slots=2, stream=True, pod=pod)
        fake = FakeRunPod(statuses=())
        fake.status = "RUNNING"

        async def health():
            return True

        async def sleep(seconds):
            clock["now"] += seconds

        def life():
            gateway = ModelGateway([WorkerAuth("runpod", TOKEN, ("qwen3-32b",))], clock=lambda: clock["now"])
            ledger = SpendLedger(clock=lambda: clock["now"], store=store)
            relay = RelayWorker(spec, gateway, ledger, clock=lambda: clock["now"])
            return PodController(spec, fake, relay, ledger, clock=lambda: clock["now"], sleep=sleep,
                                 health=health, store=store), ledger

        ctl1, ledger1 = life()
        await ctl1.adopt()
        await settle(ctl1)
        clock["now"] += 600
        ctl1._accrue()
        cursor = store.pod_cursor("runpod")
        # The gateway dies here — no stop, no final accrual. The pod bills on for 20 more minutes.
        clock["now"] += 1200
        ctl2, ledger2 = life()
        await ctl2.adopt()
        resumed_from = ctl2.s.billed_from
        ctl2._accrue()
        return cursor, resumed_from, ledger2.spent("runpod", clock["now"])[0], clock["now"]

    cursor, resumed_from, total, now = asyncio.run(scenario())
    assert cursor is not None and resumed_from == cursor < now     # resumed where life 1 stopped counting
    assert total == pytest.approx((now - DAY) / 3600 * 1.0, abs=1e-6)
