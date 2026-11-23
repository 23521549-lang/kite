"""Relay workers (P1 of the web + provider design): OpenAI-compatible and Anthropic providers that Kite
calls itself, the spend ledger and caps, free workers first, a task keeping its worker, kite-auto."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from poc.kaggle_worker.config import ConfigError, gateway_models, gateway_workers, validate_config
from poc.kaggle_worker.gateway import ANY_MODEL, ModelGateway, WorkerAuth, create_app
from poc.kaggle_worker.journal import Journal, summarize
from poc.kaggle_worker.relay import (RelaySpec, RelayWorker, SpendLedger, anthropic_body, anthropic_to_openai,
                                     openai_body, relay_specs)

from tests.poc.kaggle_worker.test_launcher import raw_config

TOKEN = "k" * 32
DAY = 1795400000.0        # 2026-11-23 UTC


def fake_provider(answers: list, seen: list):
    """An upstream API: records each request (path, headers, JSON) and replies with the next answer."""
    async def handler(request: web.Request) -> web.Response:
        seen.append((request.path, dict(request.headers), await request.json()))
        status, body = answers.pop(0)
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_post("/v1/messages", handler)
    return app


def openai_answer(text="xin chào", prompt=1000, completion=500):
    return {"id": "x", "object": "chat.completion", "model": "gpt-up",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}


def spec(base_url: str, **kwargs) -> RelaySpec:
    values = dict(worker_id="api", kind="openai", base_url=base_url, api_key="sk-secret-key", models=("gpt",),
                  upstream={"gpt": "gpt-up"}, price_in=2.0, price_out=8.0, daily_cap=None, monthly_cap=None)
    values.update(kwargs)
    return RelaySpec(**values)


def test_an_openai_compatible_worker_answers_counts_the_cost_and_keeps_the_key_out_of_the_journal(tmp_path):
    async def scenario():
        seen, records = [], []
        upstream = TestServer(fake_provider([(200, openai_answer())], seen))
        await upstream.start_server()
        gateway = ModelGateway([WorkerAuth("api", TOKEN, ("gpt",))])
        relay = RelayWorker(spec(str(upstream.make_url("/v1"))), gateway, SpendLedger(clock=lambda: DAY),
                            journal=lambda name, record: records.append((name, record)), clock=lambda: DAY)
        task = asyncio.create_task(relay.run(beat_every=0.05))
        async with TestClient(TestServer(create_app(gateway))) as client:
            response = await client.post("/v1/chat/completions", headers={"X-Kite-Task": "chat"}, json={
                "model": "gpt", "stream": True, "top_k": 20, "chat_template_kwargs": {},
                "messages": [{"role": "user", "content": "chào"}]})
            text = await response.text()
        task.cancel()
        await upstream.close()
        return seen, records, text, relay.ledger.spent("api", DAY)

    seen, records, text, spent = asyncio.run(scenario())
    path, headers, body = seen[0]
    assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer sk-secret-key"
    assert body["model"] == "gpt-up" and body["stream"] is False
    assert "top_k" not in body and "chat_template_kwargs" not in body       # OpenAI rejects unknown arguments
    assert "xin chào" in text and text.rstrip().endswith("data: [DONE]")  # the gateway replays it as a stream
    assert spent == pytest.approx((0.006, 0.006))                          # 1000*2/1e6 + 500*8/1e6
    spend = [r for name, r in records if name == "spend"]
    assert spend[0]["usd"] == pytest.approx(0.006) and spend[0]["upstream"] == "gpt-up"
    assert "sk-secret-key" not in json.dumps(records)


def test_anthropic_requests_and_answers_are_translated():
    body = {"model": "claude", "max_tokens": 1024, "temperature": 0.2, "stop": "END", "tool_choice": "required",
            "messages": [
                {"role": "system", "content": "Bạn là Kite."},
                {"role": "user", "content": [{"type": "text", "text": "đọc file"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"},
                {"role": "tool", "tool_call_id": "call_2", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "read", "description": "Đọc file",
                                                        "parameters": {"type": "object", "properties": {}}}}]}
    out = anthropic_body(spec("https://api.anthropic.com", kind="anthropic", upstream={"claude": "claude-up"},
                              models=("claude",)), body)
    assert out["model"] == "claude-up" and out["system"] == "Bạn là Kite." and out["max_tokens"] == 1024
    assert out["stop_sequences"] == ["END"] and out["tool_choice"] == {"type": "any"}
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]   # tool results merged
    assert out["messages"][0]["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}
    assert out["messages"][1]["content"] == [{"type": "tool_use", "id": "call_1", "name": "read",
                                              "input": {"path": "a.py"}}]
    assert [b["tool_use_id"] for b in out["messages"][2]["content"]] == ["call_1", "call_2"]
    assert out["tools"][0]["input_schema"] == {"type": "object", "properties": {}}

    answer = anthropic_to_openai({"id": "msg_1", "stop_reason": "tool_use", "content": [
        {"type": "thinking", "thinking": "..."}, {"type": "text", "text": "Đọc tiếp"},
        {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"path": "b.py"}}],
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 50}}, "claude")
    choice = answer["choices"][0]
    assert choice["finish_reason"] == "tool_calls" and choice["message"]["content"] == "Đọc tiếp"
    assert choice["message"]["tool_calls"][0]["function"] == {"name": "read", "arguments": '{"path": "b.py"}'}
    assert answer["usage"] == {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050}


def test_an_anthropic_worker_sends_its_key_and_version_header():
    async def scenario():
        seen = []
        reply = {"id": "m", "stop_reason": "end_turn", "content": [{"type": "text", "text": "OK"}],
                 "usage": {"input_tokens": 10, "output_tokens": 5}}
        upstream = TestServer(fake_provider([(200, reply)], seen))
        await upstream.start_server()
        gateway = ModelGateway([WorkerAuth("claude-api", TOKEN, ("claude",))])
        relay = RelayWorker(spec(str(upstream.make_url("")).rstrip("/"), worker_id="claude-api", kind="anthropic",
                                 models=("claude",), upstream={}), gateway, SpendLedger())
        task = asyncio.create_task(relay.run(beat_every=0.05))
        job = await gateway.submit({"model": "claude", "messages": [{"role": "user", "content": "hi"}]}, tag="chat")
        result = await asyncio.wait_for(job.future, 5)
        task.cancel()
        await upstream.close()
        return seen, result

    seen, result = asyncio.run(scenario())
    path, headers, body = seen[0]
    assert path == "/v1/messages" and headers["x-api-key"] == "sk-secret-key"
    assert headers["anthropic-version"] == "2023-06-01" and body["model"] == "claude"
    assert result["choices"][0]["message"]["content"] == "OK"


def test_a_worker_at_its_cap_stops_taking_jobs_another_takes_them_and_the_owner_is_told_once():
    async def scenario():
        told, records = [], []
        ledger = SpendLedger([{"worker": "api", "usd": 0.99, "ts": DAY}], clock=lambda: DAY)
        gateway = ModelGateway([WorkerAuth("api", TOKEN, ("gpt",)), WorkerAuth("other", "o" * 32, ("gpt",))])
        relay = RelayWorker(spec("https://x.invalid/v1", daily_cap=1.0), gateway, ledger, clock=lambda: DAY,
                            notify=lambda title, message, **kw: told.append(title),
                            journal=lambda name, record: records.append(record))
        await relay.beat()
        assert relay.capped is None and gateway.workers["api"].ready
        ledger.add("api", 0.02, DAY)          # 1.01 USD today
        await relay.beat()
        await relay.beat()
        await gateway.heartbeat("other", {"ready": True, "loaded_model": "gpt"})
        job = await gateway.submit({"model": "gpt", "messages": []}, tag="t1")
        taken_by_api = await gateway.next_job("api", 0)
        taken_by_other = await gateway.next_job("other", 0)
        gateway.cancel(job)
        tomorrow = RelayWorker(relay.spec, gateway, ledger, clock=lambda: DAY + 86400)
        await tomorrow.beat()
        return told, records, taken_by_api, taken_by_other, relay.capped, gateway.workers["api"].ready

    told, records, by_api, by_other, capped, ready_tomorrow = asyncio.run(scenario())
    assert by_api is None and by_other is not None
    assert told == ["Kite: worker api hết hạn mức"] and "1.01/1.00 USD hôm nay" in capped
    assert [r["event"] for r in records] == ["capped"] and ready_tomorrow


def test_a_free_worker_goes_first_and_a_task_keeps_its_worker():
    async def scenario():
        now = [1000.0]
        gateway = ModelGateway([WorkerAuth("kaggle", TOKEN, ("gpt", "qwen")), WorkerAuth("api", "p" * 32, ("gpt",))],
                               clock=lambda: now[0])
        gateway.paid = {"api"}
        await gateway.heartbeat("kaggle", {"ready": True, "loaded_model": "gpt"})
        await gateway.heartbeat("api", {"ready": True, "loaded_model": "gpt", "slots": 4})
        first = await gateway.submit({"model": "gpt", "messages": []}, tag="chat-20261123-aa01")
        paid_early = await gateway.next_job("api", 0)               # the free worker could take it
        now[0] += 6
        paid_later = await gateway.next_job("api", 0)               # nobody did: the paid one may
        assert paid_later is first
        gateway.complete("api", first.job_id, {"status": "ok", "response": {"choices": []}})
        second = await gateway.submit({"model": "gpt", "messages": []}, tag="chat-20261123-aa01")
        by_kaggle = await gateway.next_job("kaggle", 0)              # the task stays on "api"
        by_api = await gateway.next_job("api", 0)
        gateway.complete("api", second.job_id, {"status": "ok", "response": {"choices": []}})
        await gateway.heartbeat("api", {"ready": False})             # out of quota: the task moves
        third = await gateway.submit({"model": "gpt", "messages": []}, tag="chat-20261123-aa01")
        moved = await gateway.next_job("kaggle", 0)
        gateway.cancel(third)
        return paid_early, by_kaggle, by_api is second, moved is third

    paid_early, by_kaggle, stayed, moved = asyncio.run(scenario())
    assert paid_early is None and by_kaggle is None and stayed and moved


def test_kite_auto_goes_to_any_free_worker_which_answers_with_its_own_model():
    async def scenario():
        gateway = ModelGateway([WorkerAuth("kaggle", TOKEN, ("qwen3.6",)), WorkerAuth("api", "p" * 32, ("gpt",))])
        assert gateway.no_worker_note(ANY_MODEL).startswith("Chưa có worker nào đang bật")
        await gateway.heartbeat("api", {"ready": True, "loaded_model": "gpt"})
        assert gateway.no_worker_note(ANY_MODEL) is None
        job = await gateway.submit({"model": ANY_MODEL, "messages": []}, tag="chat-20261123-bb02")
        taken = await gateway.next_job("api", 0)
        gateway.cancel(job)
        return taken

    taken = asyncio.run(scenario())
    assert taken.model == "gpt" and taken.body["model"] == "gpt"


def test_a_retryable_error_is_tried_once_more_then_the_request_fails_with_the_worker_named():
    async def scenario():
        seen = []
        upstream = TestServer(fake_provider([(503, {"error": "busy"}), (429, {"error": "slow down"})], seen))
        await upstream.start_server()
        gateway = ModelGateway([WorkerAuth("api", TOKEN, ("gpt",))])
        relay = RelayWorker(spec(str(upstream.make_url("/v1"))), gateway, SpendLedger(), retry_delay=0)
        task = asyncio.create_task(relay.run(beat_every=0.05))
        job = await gateway.submit({"model": "gpt", "messages": []}, tag="chat")
        with pytest.raises(Exception) as caught:
            await asyncio.wait_for(job.future, 5)
        task.cancel()
        await upstream.close()
        return len(seen), str(caught.value)

    calls, error = asyncio.run(scenario())
    assert calls == 2 and error.startswith("api: HTTP 429")


def relay_provider(tmp_path: Path, **relay) -> dict:
    (tmp_path / "secrets").mkdir(exist_ok=True)
    (tmp_path / "secrets" / "api_key_openrouter").write_text("sk-or-secret\n")
    return {"id": "openrouter", "adapter": "relay", "enabled": True, "model": "gpt", "models": ["claude"],
            "capabilities": ["coding"], "quota": {"gpu_seconds": 0, "concurrency": 3},
            "secrets": {"api_key": f"file:{tmp_path / 'secrets' / 'api_key_openrouter'}"},
            "relay": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1",
                      "upstream": {"gpt": "openai/gpt-5-mini"}, "price": {"input": 0.25, "output": 2},
                      "caps": {"daily_usd": 1, "monthly_usd": 10}, **relay}}


def test_relay_providers_in_the_config(tmp_path):
    raw = raw_config(tmp_path)
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("w" * 40 + "\n")
    raw["providers"].append(relay_provider(tmp_path))
    config = validate_config(raw)
    [relay] = relay_specs(config)
    assert (relay.worker_id, relay.kind, relay.api_key, relay.slots) == ("openrouter", "openai", "sk-or-secret", 3)
    assert relay.upstream_model("gpt") == "openai/gpt-5-mini" and relay.upstream_model("claude") == "claude"
    assert (relay.price_in, relay.price_out, relay.daily_cap, relay.monthly_cap) == (0.25, 2.0, 1.0, 10.0)
    workers = {w.worker_id: w for w in gateway_workers(config)}
    assert set(workers) == {"kaggle-han", "openrouter"} and len(workers["openrouter"].token) >= 24
    assert {"gpt", "claude"} <= set(gateway_models(config))

    for bad, message in (({"base_url": "http://openrouter.ai/v1"}, "must use https"),
                         ({"kind": "gemini"}, "kind must be openai or anthropic"),
                         ({"upstream": {"other": "x"}}, "not one of this provider's models"),
                         ({"price": {"input": -1}}, "non-negative"),
                         ({"timeout_seconds": 0}, "between 1 and 3600")):
        raw["providers"][-1] = relay_provider(tmp_path, **bad)
        with pytest.raises(ConfigError, match=message):
            validate_config(raw)
    raw["providers"][-1] = relay_provider(tmp_path, base_url="http://127.0.0.1:11434/v1")   # Ollama here: fine
    validate_config(raw)
    anthropic = relay_provider(tmp_path, kind="anthropic", base_url=None, upstream={})
    anthropic["secrets"] = {}
    raw["providers"][-1] = anthropic
    with pytest.raises(ConfigError, match="Anthropic needs secrets.api_key"):
        validate_config(raw)


def test_the_journal_summary_shows_spend_per_worker(tmp_path):
    journal = Journal(tmp_path / "journal")
    for usd in (0.5, 0.25):
        journal.write("spend", {"ts": 1795400000.0, "worker": "openrouter", "usd": usd,
                                "prompt_tokens": 100, "completion_tokens": 10})
    spend = summarize(journal)["spend"]
    assert spend == {"openrouter": {"usd": 0.75, "requests": 2, "prompt_tokens": 200, "completion_tokens": 20}}


def test_openai_body_keeps_only_standard_arguments():
    body = openai_body(spec("https://x/v1"), {"model": "gpt", "messages": [], "stream": True, "min_p": 0.1,
                                              "tools": [], "max_tokens": 9})
    assert body == {"model": "gpt-up", "messages": [], "tools": [], "max_tokens": 9, "stream": False}


def test_worker_add_api_and_spend_commands(tmp_path, monkeypatch, capsys):
    from poc.kaggle_worker import cli

    raw = raw_config(tmp_path)
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("w" * 40 + "\n")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    asked = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": asked.append(prompt) or "sk-or-new")
    assert cli.main(["--config", str(path), "worker", "add-api", "openrouter", "--kind", "openai",
                     "--base-url", "https://openrouter.ai/api/v1", "--model", "gpt=openai/gpt-5-mini",
                     "--model", "claude", "--price-in", "0.25", "--price-out", "2", "--daily-cap", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    key = tmp_path / "secrets" / "api_key_openrouter"
    assert key.read_text() == "sk-or-new\n" and key.stat().st_mode & 0o777 == 0o600 and len(asked) == 1
    assert "sk-or-new" not in json.dumps(report) and "warning" not in report
    saved = json.loads(path.read_text())["providers"][-1]
    assert saved["relay"] == {"kind": "openai", "price": {"input": 0.25, "output": 2.0},
                              "base_url": "https://openrouter.ai/api/v1", "upstream": {"gpt": "openai/gpt-5-mini"},
                              "caps": {"daily_usd": 1.0}}
    assert (saved["model"], saved["models"], saved["quota"]["concurrency"]) == ("gpt", ["claude"], 2)
    assert cli.main(["--config", str(path), "worker", "add-api", "openrouter", "--kind", "openai",
                     "--base-url", "https://x.ai/v1", "--model", "a"]) != 0
    assert "already exists" in capsys.readouterr().err
    Journal(tmp_path / "journal").write("spend", {"ts": __import__("time").time(), "worker": "openrouter",
                                                  "usd": 0.4})
    assert cli.main(["--config", str(path), "worker", "spend"]) == 0
    [row] = json.loads(capsys.readouterr().out)["workers"]
    assert (row["worker"], row["today_usd"], row["daily_cap"]) == ("openrouter", 0.4, 1.0)


def test_doctor_shows_api_workers_and_warns_near_the_cap(tmp_path):
    import time as clock

    from poc.kaggle_worker.doctor import run_checks
    from tests.poc.kaggle_worker.test_doctor import probes

    raw = raw_config(tmp_path)
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("w" * 40 + "\n")
    raw["providers"].append(relay_provider(tmp_path))
    config = validate_config(raw)
    Journal(tmp_path / "journal").write("spend", {"ts": clock.time(), "worker": "openrouter", "usd": 0.85})
    checks = {c.name: c for c in run_checks(config, tmp_path / "config.yaml", probes()[0])}
    check = checks["worker API openrouter"]
    assert check.status == "warn" and "0.85/1.00 USD hôm nay" in check.fix
