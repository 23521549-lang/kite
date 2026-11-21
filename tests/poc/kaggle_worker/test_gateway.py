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


async def sse_reply(request: web.Request, completion: dict, *, delay: float = 0.0) -> web.StreamResponse:
    """A fake llama-server answer: the worker always asks for a stream."""
    message = completion["choices"][0]["message"]
    chunks = [{"id": completion["id"], "model": completion["model"], "choices": [{"index": 0, "delta": {
        "role": "assistant", **({"content": message["content"]} if message.get("content") else {}),
        **({"tool_calls": [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]}
           if message.get("tool_calls") else {})}, "finish_reason": None}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": completion["choices"][0]["finish_reason"]}],
               "usage": completion.get("usage")}]
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    for chunk in chunks:
        await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        if delay:
            await asyncio.sleep(delay)
    await response.write(b"data: [DONE]\n\n")
    return response


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

        async def fake_llama(request: web.Request) -> web.StreamResponse:
            llama_bodies.append(await request.json())
            return await sse_reply(request, COMPLETION)

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
                try:
                    answer = await client.post("/v1/chat/completions", json={
                        "model": "qwen3.6", "messages": [{"role": "user", "content": "chào"}]})
                    assert answer.status == 200
                    assert (await answer.json())["choices"][0]["message"]["content"] == "xin chào"
                finally:
                    worker.stop()
                    await running
        finally:
            await llama.close()
        return llama_bodies, loaded["model"]

    bodies, model = asyncio.run(scenario())
    assert model == "qwen3.6"
    # The client asked for one piece; the worker still streams from llama-server (so a cancel stops it).
    assert bodies[0]["stream"] is True and bodies[0]["model"] == "qwen3.6"


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


# ------------------------------------------------------------------ real streaming
def _delta(content=None, *, reasoning=None, call=None, finish=None, usage=None, timings=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if call is not None:
        delta["tool_calls"] = [call]
    chunk = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "created": 1, "model": "/kaggle/qwen.gguf",
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        chunk["usage"] = usage
    if timings:
        chunk["timings"] = timings
    return chunk


STREAM = [_delta(reasoning="Nghĩ "), _delta(reasoning="đã."), _delta("xin "), _delta("chào"),
          _delta(finish="stop", usage={"prompt_tokens": 5, "completion_tokens": 4},
                 timings={"predicted_per_second": 41.0})]


def _data(text):
    return [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]


def test_streamed_chunks_reach_the_client_before_the_answer_is_finished():
    async def scenario():
        gateway = make_gateway(keepalive_seconds=0.05)
        async with TestClient(TestServer(create_app(gateway))) as client:
            headers = {"Authorization": f"Bearer {TOKEN_A}"}
            await client.post("/worker/heartbeat", json={"ready": True, "loaded_model": "qwen3.6"}, headers=headers)
            response_task = asyncio.create_task(client.post("/v1/chat/completions", json={
                "model": "qwen3.6-fast", "stream": True, "messages": []}))
            job = await (await client.get("/worker/jobs?wait=5", headers=headers)).json()
            assert job["stream"] is True and job["body"]["stream"] is False
            url = f"/worker/jobs/{job['job_id']}/chunks"
            assert (await client.post(url, json={"start": 0, "chunks": STREAM[:3]}, headers=headers)).status == 200
            response = await response_task                    # headers are out: read what has come so far
            first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 5)
            assert json.loads(first[len(b"data: "):])["choices"][0]["delta"]["reasoning_content"] == "Nghĩ "
            # A batch sent again (its reply was lost) is not passed on twice; a gap is refused.
            assert (await client.post(url, json={"start": 2, "chunks": STREAM[2:4]}, headers=headers)).status == 200
            assert (await client.post(url, json={"start": 9, "chunks": STREAM[4:]}, headers=headers)).status == 409
            done = await client.post(f"/worker/jobs/{job['job_id']}/result", headers=headers, json={
                "status": "ok", "response": COMPLETION, "start": 4, "chunks": STREAM[4:]})
            assert done.status == 200
            return first + await response.content.read()
    data = _data(asyncio.run(scenario()).decode())
    chunks = [json.loads(d) for d in data[:-1]]
    assert data[-1] == "[DONE]" and len(chunks) == 5                  # no replay of the whole answer
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "xin chào"
    assert {c["model"] for c in chunks} == {"qwen3.6-fast"}               # not the GGUF path
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_a_streamed_answer_is_not_retried_on_another_worker_and_a_gone_client_stops_the_worker():
    now = [1000.0]

    async def scenario():
        gateway = make_gateway(clock=lambda: now[0])
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
        job = await gateway.submit({"model": "qwen3.6", "messages": []}, stream=True)
        await gateway.next_job("w1", 0)
        assert gateway.add_chunks("w1", job.job_id, 0, STREAM[:2])
        assert not gateway.add_chunks("w2", job.job_id, 2, STREAM[2:3])       # not w2's job
        now[0] += 200                                                       # w1 dies mid-answer
        gateway.reap()
        with pytest.raises(GatewayError, match="middle of the answer"):
            await job.future
        other = await gateway.submit({"model": "qwen3.6", "messages": []}, stream=True)
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6"})
        await gateway.next_job("w1", 0)
        gateway.cancel(other)                                               # the person closed the chat
        assert not gateway.add_chunks("w1", other.job_id, 0, STREAM[:1])
    asyncio.run(scenario())


def test_pull_worker_streams_from_llama_server_and_journals_the_assembled_answer():
    records = []

    async def scenario():
        gateway = make_gateway(journal=records.append, keepalive_seconds=0.05)
        bodies = []

        async def fake_llama(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            bodies.append(body)
            if not body.get("stream"):
                return web.json_response(COMPLETION)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for chunk in STREAM:
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
                await asyncio.sleep(0.05)
            await response.write(b"data: [DONE]\n\n")
            return response

        llama_app = web.Application()
        llama_app.router.add_post("/v1/chat/completions", fake_llama)
        llama = TestServer(llama_app)
        await llama.start_server()
        try:
            async with TestClient(TestServer(create_app(gateway))) as client:
                worker = PullWorker(str(client.make_url("/")), TOKEN_A, ensure_model=lambda model: None,
                                    loaded_model=lambda: "qwen3.6", llama_url=str(llama.make_url("/")),
                                    poll_wait_seconds=1, heartbeat_seconds=0.2, stream_flush_seconds=0.02,
                                    log=lambda _msg: None)
                running = asyncio.create_task(asyncio.to_thread(worker.run))
                try:
                    streamed = await (await client.post("/v1/chat/completions", json={
                        "model": "qwen3.6", "stream": True, "messages": []})).text()
                    plain = await (await client.post("/v1/chat/completions", json={
                        "model": "qwen3.6", "messages": []})).json()
                finally:
                    worker.stop()
                    await running
        finally:
            await llama.close()
        return bodies, streamed, plain

    bodies, streamed, plain = asyncio.run(scenario())
    assert bodies[0]["stream"] is True and bodies[0]["stream_options"] == {"include_usage": True}
    assert bodies[1]["stream"] is True and plain["choices"][0]["message"]["content"] == "xin chào"
    data = _data(streamed)
    assert data[-1] == "[DONE]" and len(data) == len(STREAM) + 1
    assert records[0]["outcome"] == "ok" and records[0]["completion_tokens"] == 4
    assert records[0]["gen_tps"] == 41.0 and records[0]["reasoning_chars"] == len("Nghĩ đã.")


def test_assemble_stream_rebuilds_tool_calls_split_over_chunks():
    from poc.kaggle_worker.pull_worker import assemble_stream

    answer = assemble_stream([
        _delta(call={"index": 0, "id": "c1", "type": "function", "function": {"name": "read", "arguments": ""}}),
        _delta(call={"index": 0, "function": {"arguments": "{\"path\": "}}),
        _delta(call={"index": 1, "function": {"name": "grep", "arguments": "{}"}}),
        _delta(call={"index": 0, "function": {"arguments": "\"a.py\"}"}}),
        _delta(finish="tool_calls")])
    message = answer["choices"][0]["message"]
    assert answer["choices"][0]["finish_reason"] == "tool_calls" and message["content"] is None
    assert [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in message["tool_calls"]] == [
        ("c1", "read", "{\"path\": \"a.py\"}"), ("call_1", "grep", "{}")]


def test_model_add_puts_a_chat_only_model_on_the_named_workers(tmp_path, capsys):
    from poc.kaggle_worker.config import load_config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_base_config(tmp_path)))
    args = ["--config", str(config_path), "model", "add", "r1-distill", "--provider", "kaggle-han",
            "--context", "32768", "--chat-only"]
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["added_to"] == ["kaggle-han"] and list(tmp_path.glob("config.json.bak-*"))
    assert cli.main(args) == 0 and json.loads(capsys.readouterr().out)["added_to"] == []      # idempotent
    raw = json.loads(config_path.read_text())
    assert raw["providers"][0]["models"] == ["devstral", "r1-distill"]
    assert raw["gateway"]["model_context"] == {"r1-distill": 32768} and raw["routing"]["chat_only"] == ["r1-distill"]
    config = load_config(config_path)
    assert config.routing.chat_only == ("r1-distill",)
    assert cli.main(["--config", str(config_path), "model", "add", "x", "--provider", "kaggle-zz"]) == 1
    assert cli.main(["--config", str(config_path), "model", "add", "x", "--provider", "kaggle-han",
                     "--context", "100"]) == 1                              # validated before writing
    assert "x" not in json.loads(config_path.read_text())["providers"][0]["models"]


def test_a_worker_with_two_slots_runs_a_chat_while_a_task_request_generates():
    # Bench day, step 2: the person waited 1 min 47 s for the lead's answer behind a task's request.
    async def scenario():
        gateway = make_gateway(keepalive_seconds=0.05)
        order = []
        release = asyncio.Event()

        async def fake_llama(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            name = body["messages"][0]["content"]
            order.append(f"start {name}")
            if name == "task":
                await release.wait()
            reply = await sse_reply(request, COMPLETION)
            order.append(f"end {name}")
            if name == "chat":
                release.set()
            return reply

        llama_app = web.Application()
        llama_app.router.add_post("/v1/chat/completions", fake_llama)
        llama = TestServer(llama_app)
        await llama.start_server()
        try:
            async with TestClient(TestServer(create_app(gateway))) as client:
                worker = PullWorker(str(client.make_url("/")), TOKEN_A, ensure_model=lambda model: None,
                                    loaded_model=lambda: "qwen3.6", llama_url=str(llama.make_url("/")),
                                    poll_wait_seconds=1, heartbeat_seconds=0.2, stream_flush_seconds=0.02,
                                    slots=lambda: 2, log=lambda _msg: None)
                running = asyncio.create_task(asyncio.to_thread(worker.run))
                try:
                    task = asyncio.create_task(client.post("/v1/chat/completions", headers={"X-Kite-Task": "t1"},
                                                           json={"model": "qwen3.6", "messages": [
                                                               {"role": "user", "content": "task"}]}))
                    while "start task" not in order:
                        await asyncio.sleep(0.02)
                    chat = await client.post("/v1/chat/completions", headers={"X-Kite-Task": "chat"}, json={
                        "model": "qwen3.6", "messages": [{"role": "user", "content": "chat"}]})
                    assert chat.status == 200 and (await task).status == 200
                    status = (await (await client.get("/status")).json())["workers"][0]
                finally:
                    worker.stop()
                    await running
        finally:
            await llama.close()
        return order, status

    order, status = asyncio.run(scenario())
    assert order.index("end chat") < order.index("end task")      # the chat did not wait for the task
    assert status["slots"] == 2 and status["version"]


def test_a_busy_worker_only_takes_more_requests_for_the_model_it_has_loaded():
    async def scenario():
        gateway = make_gateway()
        await gateway.heartbeat("w1", {"ready": True, "loaded_model": "qwen3.6", "slots": 2})
        first = await gateway.submit({"model": "qwen3.6", "messages": []})
        other = await gateway.submit({"model": "devstral", "messages": []})
        assert (await gateway.next_job("w1", 0)).job_id == first.job_id
        assert await gateway.next_job("w1", 0) is None          # devstral would swap the model under it
        same = await gateway.submit({"model": "qwen3.6", "messages": []})
        assert (await gateway.next_job("w1", 0)).job_id == same.job_id
        assert await gateway.next_job("w1", 0) is None          # both slots taken
        gateway.complete("w1", first.job_id, {"status": "ok", "response": COMPLETION})
        gateway.complete("w1", same.job_id, {"status": "ok", "response": COMPLETION})
        assert (await gateway.next_job("w1", 0)).job_id == other.job_id   # idle: now it may switch
        return gateway.status()["workers"][0]

    worker = asyncio.run(scenario())
    assert worker["busy"] and worker["running"] == 1 and worker["slots"] == 2


def test_cancelling_a_task_drops_its_queued_and_running_requests_and_stops_the_worker_generating():
    async def scenario():
        gateway = make_gateway()
        async with TestClient(TestServer(create_app(gateway))) as client:
            headers = {"Authorization": f"Bearer {TOKEN_A}"}
            await client.post("/worker/heartbeat", json={"ready": True, "loaded_model": "qwen3.6"}, headers=headers)
            running = asyncio.create_task(client.post("/v1/chat/completions", headers={"X-Kite-Task": "t9"},
                                                      json={"model": "qwen3.6", "messages": []}))
            job = await (await client.get("/worker/jobs?wait=5", headers=headers)).json()
            helper = asyncio.create_task(client.post("/v1/chat/completions", headers={"X-Kite-Task": "t9-b2"},
                                                     json={"model": "qwen3.6", "messages": []}))
            other = asyncio.create_task(client.post("/v1/chat/completions", headers={"X-Kite-Task": "t10"},
                                                    json={"model": "qwen3.6", "messages": []}))
            await asyncio.sleep(0.1)
            # A non-streamed job still takes chunks while it is wanted (the worker always streams).
            alive = await client.post(f"/worker/jobs/{job['job_id']}/chunks", headers=headers,
                                      json={"start": 0, "chunks": [{"choices": []}]})
            refused = await client.post("/kite/cancel", json={"tag": ""})
            cancelled = await (await client.post("/kite/cancel", json={"tag": "t9"})).json()
            gone = await client.post(f"/worker/jobs/{job['job_id']}/chunks", headers=headers,
                                     json={"start": 1, "chunks": [{"choices": []}]})
            answers = [await running, await helper]
            left = [gateway.jobs[j].tag for j in gateway._queue]
            other.cancel()
            return alive.status, refused.status, cancelled, gone.status, [a.status for a in answers], left

    alive, refused, cancelled, gone, answers, left = asyncio.run(scenario())
    assert alive == 200 and refused == 400 and cancelled == {"tag": "t9", "dropped": 2}
    assert gone == 409 and all(code >= 500 for code in answers) and left == ["t10"]
