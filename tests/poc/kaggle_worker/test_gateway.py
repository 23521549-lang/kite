"""Gateway + pull-worker tests: queueing, auth, model affinity, failure handling, SSE, CLI."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from poc.kaggle_worker import cli
from poc.kaggle_worker.config import ConfigError, gateway_workers, validate_config
from poc.kaggle_worker.gateway import GatewayError, ModelAlias, ModelGateway, WorkerAuth, create_app
from poc.kaggle_worker.pull_worker import PullWorker

TOKEN_A = "a" * 32
TOKEN_B = "b" * 32
COMPLETION = {
    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "qwen3.6",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "xin chào"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
TOOL_COMPLETION = {
    "id": "chatcmpl-2", "object": "chat.completion", "created": 1, "model": "qwen3.6",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read", "arguments": "{\"path\": \"a.py\"}"}}]}}],
}


def make_gateway(**kwargs) -> ModelGateway:
    return ModelGateway(
        [WorkerAuth("w1", TOKEN_A, ("qwen3.6", "devstral")), WorkerAuth("w2", TOKEN_B, ("qwen3.6",))],
        aliases=[ModelAlias("qwen3.6-fast", "qwen3.6", {"chat_template_kwargs": {"enable_thinking": False}})],
        **kwargs,
    )


def test_worker_routes_require_a_valid_token():
    async def scenario():
        async with TestClient(TestServer(create_app(make_gateway()))) as client:
            assert (await client.post("/worker/heartbeat", json={})).status == 401
            bad = {"Authorization": "Bearer " + "x" * 32}
            assert (await client.get("/worker/jobs?wait=0", headers=bad)).status == 401
            good = {"Authorization": f"Bearer {TOKEN_A}"}
            assert (await client.post("/worker/heartbeat", json={"ready": True}, headers=good)).status == 200
    asyncio.run(scenario())


def test_chat_round_trip_applies_alias_and_forces_non_streaming():
    async def scenario():
        gateway = make_gateway()
        async with TestClient(TestServer(create_app(gateway))) as client:
            headers = {"Authorization": f"Bearer {TOKEN_A}"}
            await client.post("/worker/heartbeat", json={"ready": True, "loaded_model": "qwen3.6"}, headers=headers)
            chat = asyncio.create_task(client.post("/v1/chat/completions", json={
                "model": "qwen3.6-fast", "messages": [{"role": "user", "content": "chào"}]}))
            response = await client.get("/worker/jobs?wait=5", headers=headers)
            assert response.status == 200
            job = await response.json()
            assert job["model"] == "qwen3.6"
            assert job["body"]["chat_template_kwargs"] == {"enable_thinking": False}
            assert job["body"]["stream"] is False
            done = await client.post(f"/worker/jobs/{job['job_id']}/result",
                                     json={"status": "ok", "response": COMPLETION}, headers=headers)
            assert done.status == 200
            answer = await chat
            assert answer.status == 200
            assert (await answer.json())["choices"][0]["message"]["content"] == "xin chào"
            models = await (await client.get("/v1/models")).json()
            assert {m["id"] for m in models["data"]} == {"qwen3.6", "devstral", "qwen3.6-fast"}
    asyncio.run(scenario())


def test_streaming_request_gets_keepalives_and_valid_tool_call_chunks():
    async def scenario():
        gateway = make_gateway(keepalive_seconds=0.05)
        async with TestClient(TestServer(create_app(gateway))) as client:
            await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
            chat = asyncio.create_task(client.post("/v1/chat/completions", json={
                "model": "qwen3.6", "stream": True, "messages": []}))
            job = await gateway.next_job("w1", 5)
            await asyncio.sleep(0.2)
            assert gateway.complete("w1", job.job_id, {"status": "ok", "response": TOOL_COMPLETION})
            return await (await chat).text()
    text = asyncio.run(scenario())
    assert ": waiting for worker" in text
    data = [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]
    assert data[-1] == "[DONE]"
    call = json.loads(data[0])["choices"][0]["delta"]["tool_calls"][0]
    assert call["index"] == 0 and call["function"]["name"] == "read"
    assert json.loads(data[1])["choices"][0]["finish_reason"] == "tool_calls"


def test_job_goes_to_the_worker_that_already_has_the_model_loaded():
    async def scenario():
        gateway = make_gateway()
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "devstral"})
        await gateway.heartbeat("w2", {"ready": True, "loaded_model": "qwen3.6"})
        job = await gateway.submit({"model": "qwen3.6", "messages": []})
        assert await gateway.next_job("w1", 0) is None      # would have to switch models
        taken = await gateway.next_job("w2", 0)
        assert taken.job_id == job.job_id
    asyncio.run(scenario())


def test_job_of_a_silent_worker_is_retried_then_failed():
    now = [1000.0]

    async def scenario():
        gateway = make_gateway(clock=lambda: now[0], heartbeat_timeout_seconds=90, max_attempts=2)
        await gateway.heartbeat("w2", {"ready": True, "loaded_model": "qwen3.6"})
        job = await gateway.submit({"model": "qwen3.6", "messages": []})
        assert (await gateway.next_job("w2", 0)).job_id == job.job_id
        now[0] += 100                                        # w2 stops heartbeating
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
        retried = await gateway.next_job("w1", 0)
        assert retried.job_id == job.job_id and retried.attempts == 2
        now[0] += 100                                        # w1 also goes silent
        gateway.reap()
        with pytest.raises(GatewayError):
            await job.future
    asyncio.run(scenario())


def test_unknown_model_is_rejected_and_ready_transition_is_reported():
    seen = []

    async def scenario():
        gateway = make_gateway(on_worker_status=lambda w, became_ready: seen.append((w.worker_id, became_ready)))
        with pytest.raises(GatewayError):
            await gateway.submit({"model": "llama-9000", "messages": []})
        await gateway.heartbeat("w1", {"ready": True})
        await gateway.heartbeat("w1", {"ready": True})
    asyncio.run(scenario())
    assert seen == [("w1", True), ("w1", False)]


def test_pull_worker_serves_a_request_end_to_end_and_switches_model():
    async def scenario():
        gateway = make_gateway()
        llama_bodies = []

        async def fake_llama(request: web.Request) -> web.Response:
            llama_bodies.append(await request.json())
            return web.json_response(COMPLETION)

        llama_app = web.Application()
        llama_app.router.add_post("/v1/chat/completions", fake_llama)
        llama = TestServer(llama_app)
        await llama.start_server()
        loaded = {"model": "devstral"}
        try:
            async with TestClient(TestServer(create_app(gateway))) as client:
                worker = PullWorker(
                    str(client.make_url("/")), TOKEN_A,
                    ensure_model=lambda model: loaded.update(model=model),
                    loaded_model=lambda: loaded["model"],
                    llama_url=str(llama.make_url("/")),
                    poll_wait_seconds=1, heartbeat_seconds=0.2, log=lambda _msg: None)
                running = asyncio.create_task(asyncio.to_thread(worker.run))
                answer = await client.post("/v1/chat/completions", json={
                    "model": "qwen3.6", "messages": [{"role": "user", "content": "chào"}]})
                assert answer.status == 200
                assert (await answer.json())["choices"][0]["message"]["content"] == "xin chào"
                worker.stop()
                await running
        finally:
            await llama.close()
        return llama_bodies, loaded["model"]

    bodies, model = asyncio.run(scenario())
    assert model == "qwen3.6"
    assert bodies[0]["stream"] is False and bodies[0]["model"] == "qwen3.6"


def _base_config(tmp_path, **extra):
    return {
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "q"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": [{
            "id": "kaggle-han", "adapter": "pull", "enabled": True, "owner": "han",
            "model": "qwen3.6", "models": ["devstral"], "capabilities": ["coding"],
            "quota": {"gpu_seconds": 108000, "concurrency": 1},
            "secrets": {"worker_token": f"file:{tmp_path / 'secrets' / 'worker_token_kaggle-han'}"},
        }],
        "gateway": {"public_url": "https://1-2-3-4.sslip.io", "aliases": {
            "qwen3.6-fast": {"model": "qwen3.6", "params": {"chat_template_kwargs": {"enable_thinking": False}}}}},
        **extra,
    }


def test_pull_provider_config_resolves_worker_token(tmp_path):
    raw = _base_config(tmp_path)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("t" * 40 + "\n")
    config = validate_config(raw)
    [worker] = gateway_workers(config)
    assert (worker.worker_id, worker.token, worker.model_ids) == ("kaggle-han", "t" * 40, ("qwen3.6", "devstral"))
    raw["providers"][0]["secrets"] = {}
    with pytest.raises(ConfigError):
        validate_config(raw)
    raw = _base_config(tmp_path)
    raw["gateway"]["host"] = "0.0.0.0"
    with pytest.raises(ConfigError):
        validate_config(raw)


def test_worker_token_and_opencode_config_commands(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_base_config(tmp_path)))
    assert cli.main(["--config", str(config_path), "worker", "token", "kaggle-han"]) == 0
    created = json.loads(capsys.readouterr().out)
    token_path = tmp_path / "secrets" / "worker_token_kaggle-han"
    assert token_path.read_text().strip() == created["token"] and len(created["token"]) >= 32
    if os.name != "nt":
        assert token_path.stat().st_mode & 0o777 == 0o600
    assert cli.main(["--config", str(config_path), "worker", "token", "kaggle-han"]) == 1   # no silent overwrite
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "gateway", "opencode-config"]) == 0
    provider = json.loads(capsys.readouterr().out)["provider"]["kite"]
    assert provider["options"]["baseURL"] == "http://127.0.0.1:8787/v1"
    assert set(provider["models"]) == {"qwen3.6", "devstral", "qwen3.6-fast"}


def test_status_lists_models_and_answers_carry_the_requested_model_name():
    async def scenario():
        gateway = make_gateway()
        async with TestClient(TestServer(create_app(gateway))) as client:
            await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
            chat = asyncio.create_task(client.post("/v1/chat/completions", json={
                "model": "qwen3.6-fast", "messages": []}))
            job = await gateway.next_job("w1", 5)
            gateway.complete("w1", job.job_id, {"status": "ok", "response": {
                **COMPLETION, "model": "/kaggle/tmp/kite/models/x.gguf"}})
            answer = await (await chat).json()
            status = await (await client.get("/status")).json()
        return answer, status

    answer, status = asyncio.run(scenario())
    assert answer["model"] == "qwen3.6-fast"
    w1 = next(w for w in status["workers"] if w["worker_id"] == "w1")
    assert w1["models"] == ["qwen3.6", "devstral"]


def test_every_finished_request_is_journaled_with_its_task_tag_but_no_content():
    records = []

    async def scenario():
        gateway = make_gateway(journal=records.append)
        async with TestClient(TestServer(create_app(gateway))) as client:
            headers = {"Authorization": f"Bearer {TOKEN_A}"}
            await client.post("/worker/heartbeat", json={"ready": True, "loaded_model": "qwen3.6"}, headers=headers)
            chat = asyncio.create_task(client.post(
                "/v1/chat/completions", headers={"X-Kite-Task": "chat-20261119-aaaa"},
                json={"model": "qwen3.6-fast", "messages": [{"role": "user", "content": "bí mật"}], "max_tokens": 64}))
            job = await (await client.get("/worker/jobs?wait=5", headers=headers)).json()
            answer = {**TOOL_COMPLETION, "usage": {"prompt_tokens": 900, "completion_tokens": 40},
                      "timings": {"prompt_per_second": 1200.0, "predicted_per_second": 48.5, "cache_n": 512}}
            await client.post(f"/worker/jobs/{job['job_id']}/result", json={"status": "ok", "response": answer},
                              headers=headers)
            await chat
            bad = asyncio.create_task(client.post("/v1/chat/completions", headers={"X-Kite-Task": "bad tag!"},
                                                  json={"model": "devstral", "messages": []}))
            job = await (await client.get("/worker/jobs?wait=5", headers=headers)).json()
            await client.post(f"/worker/jobs/{job['job_id']}/result", json={"status": "error", "error": "OOM"},
                              headers=headers)
            await bad
    asyncio.run(scenario())
    ok, failed = records
    assert ok["tag"] == "chat-20261119-aaaa" and ok["requested"] == "qwen3.6-fast" and ok["model"] == "qwen3.6"
    assert (ok["worker"], ok["outcome"], ok["prompt_tokens"], ok["completion_tokens"]) == ("w1", "ok", 900, 40)
    assert (ok["prompt_tps"], ok["gen_tps"], ok["cached_tokens"]) == (1200.0, 48.5, 512)
    assert ok["tool_calls"] == ["read"] and ok["max_tokens"] == 64 and ok["queue_s"] >= 0
    assert "bí mật" not in json.dumps(ok, ensure_ascii=False)           # prompts are never stored
    assert failed["tag"] is None and failed["outcome"] == "error" and "OOM" in failed["error"]
