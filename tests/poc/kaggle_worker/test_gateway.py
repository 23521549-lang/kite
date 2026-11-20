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
                      "timings": {"prompt_per_second": 1200.0, "predicted_per_second": 48.5, "cache_n": 512,
                                  "draft_n": 64, "draft_n_accepted": 51}}
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
    assert (ok["draft_n"], ok["draft_accepted"], ok["reasoning_chars"]) == (64, 51, 0)
    assert ok["tool_args_chars"] > 0 and isinstance(ok["content_chars"], int)
    assert "bí mật" not in json.dumps(ok, ensure_ascii=False)           # prompts are never stored
    assert failed["tag"] is None and failed["outcome"] == "error" and "OOM" in failed["error"]


def test_a_task_pinned_to_a_worker_is_served_by_that_worker_even_if_another_has_the_model():
    async def scenario():
        gateway = make_gateway()
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
        await gateway.heartbeat("w2", {"ready": True, "loaded_model": "devstral"})
        pinned = await gateway.submit({"model": "qwen3.6", "messages": []}, pin="w2")
        assert await gateway.next_job("w1", 0) is None          # w1 has the model loaded but is not the route
        assert (await gateway.next_job("w2", 0)).job_id == pinned.job_id   # w2 switches models for its task
        free = await gateway.submit({"model": "qwen3.6", "messages": []}, pin="nobody")
        assert free.pinned is None and (await gateway.next_job("w1", 0)).job_id == free.job_id
    asyncio.run(scenario())


def test_a_job_pinned_to_a_dead_worker_is_taken_by_another_so_the_task_does_not_hang():
    now = [1000.0]

    async def scenario():
        gateway = make_gateway(clock=lambda: now[0], heartbeat_timeout_seconds=90)
        await gateway.heartbeat("w2", {"ready": True, "loaded_model": "qwen3.6"})
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
        job = await gateway.submit({"model": "qwen3.6", "messages": []}, pin="w2")
        assert await gateway.next_job("w1", 0) is None
        now[0] += 100                                            # w2 goes silent
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
        assert (await gateway.next_job("w1", 0)).job_id == job.job_id
    asyncio.run(scenario())


def test_worker_add_copies_a_worker_for_another_kaggle_account(tmp_path, capsys):
    from poc.kaggle_worker.config import load_config, sync_config_to_store
    from poc.kaggle_worker.store import SQLiteSchedulerStore

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_base_config(tmp_path)))
    assert cli.main(["--config", str(config_path), "worker", "add", "kaggle-an", "--owner", "an"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["copied_from"] == "kaggle-han" and added["next"][0] == "kite worker token kaggle-an"
    assert list(tmp_path.glob("config.json.bak-*"))
    assert cli.main(["--config", str(config_path), "worker", "add", "kaggle-an", "--owner", "an"]) == 1
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "worker", "token", "kaggle-an"]) == 0
    capsys.readouterr()
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("h" * 40 + "\n")
    config = load_config(config_path)
    workers = {w.worker_id: w for w in gateway_workers(config)}
    assert set(workers) == {"kaggle-han", "kaggle-an"} and workers["kaggle-an"].model_ids == ("qwen3.6", "devstral")
    assert workers["kaggle-an"].token != workers["kaggle-han"].token
    # One owner with two accounts may run two tasks at once (limit = sum, not max).
    raw = json.loads(config_path.read_text())
    raw["providers"][1]["owner"] = raw["providers"][0].get("owner", "self")
    with SQLiteSchedulerStore(tmp_path / "s.sqlite3") as store:
        sync_config_to_store(validate_config(raw), store, now=1000.0)
        assert store.get_owner_policy(raw["providers"][0].get("owner", "self")).max_concurrent_sessions == 2


def test_chat_requests_go_before_queued_task_requests():
    async def scenario():
        gateway = make_gateway()
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
        task_job = await gateway.submit({"model": "qwen3.6", "messages": []}, tag="chat-20261120-aaaa")
        chat_job = await gateway.submit({"model": "qwen3.6", "messages": []}, tag="chat")
        assert (await gateway.next_job("w1", 0)).job_id == chat_job.job_id
        assert task_job.job_id in gateway._queue                  # the task's request waits its turn
    asyncio.run(scenario())


def test_a_conversation_waits_briefly_for_the_worker_that_has_it_cached():
    now = [1000.0]

    async def scenario():
        gateway = make_gateway(clock=lambda: now[0], affinity_seconds=20)
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
        await gateway.heartbeat("w2", {"ready": True, "loaded_model": "qwen3.6"})
        body = {"model": "qwen3.6", "messages": []}
        first = await gateway.submit(body, tag="chat")
        assert (await gateway.next_job("w1", 0)).job_id == first.job_id
        gateway.complete("w1", first.job_id, {"status": "ok", "response": COMPLETION})
        second = await gateway.submit(body, tag="chat")
        assert await gateway.next_job("w2", 0) is None            # w1 has this conversation cached
        assert (await gateway.next_job("w1", 0)).job_id == second.job_id
        third = await gateway.submit(body, tag="chat")              # w1 is busy with the second one...
        assert await gateway.next_job("w2", 0) is None
        now[0] += 21                                                # ...for too long: w2 takes it
        assert (await gateway.next_job("w2", 0)).job_id == third.job_id
        assert gateway.affinity["chat"] == "w2"
        gateway.complete("w1", second.job_id, {"status": "ok", "response": COMPLETION})
        gateway.complete("w2", third.job_id, {"status": "ok", "response": COMPLETION})
        untagged = await gateway.submit(body)                       # no tag: whoever asks first
        assert (await gateway.next_job("w2", 0)).job_id == untagged.job_id
        await gateway.heartbeat("w2", {"ready": True, "loaded_model": "devstral"})
        gateway.complete("w2", untagged.job_id, {"status": "ok", "response": COMPLETION})
        fourth = await gateway.submit(body, tag="chat")             # w2 switched models: its cache is gone
        assert (await gateway.next_job("w1", 0)).job_id == fourth.job_id
    asyncio.run(scenario())
