"""Relay workers: Kite's own workers for providers it calls (paid APIs, servers it reaches by URL).

A relay worker lives in the gateway process and takes jobs from the same queue as the Kaggle pull
workers (heartbeat, next_job, complete). Choosing a worker, the journal, the stickiness of a task to
its worker and the retries therefore work the same for every provider. Each job goes to the provider:

- ``openai``: any OpenAI-compatible ``/chat/completions`` (OpenAI, OpenRouter, Groq, DeepSeek,
  Gemini's compatible endpoint, vLLM, Ollama, llama-server...);
- ``anthropic``: Anthropic's Messages API, translated to and from the OpenAI chat format.

With ``stream`` (always for a rented machine, pods.py), the answer is read as it is generated (SSE)
and its chunks go straight to a streaming client, like a Kaggle worker's; RunPod's proxy closes a
request that has sent nothing for 100 seconds, so a long answer needs it.

The token usage of every answer is priced and written to the spend ledger (journal file ``spend``).
A worker that reaches its daily or monthly cap stops taking jobs, so the queue goes to the other
workers, and the owner is told once (ntfy). Keys never reach a log, the journal or a prompt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import secrets as token_secrets
import time
from typing import Any, Callable

KINDS = ("openai", "anthropic")
ANTHROPIC_VERSION = "2023-06-01"
# Parameters an OpenAI-style API accepts; llama-server extras (top_k, min_p, chat_template_kwargs,
# cache_prompt...) from Kite's aliases are dropped: OpenAI answers 400 to arguments it does not know.
OPENAI_PARAMS = ("model", "messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens",
                 "max_completion_tokens", "stop", "response_format", "seed", "n", "presence_penalty",
                 "frequency_penalty", "parallel_tool_calls", "user", "logprobs", "top_logprobs",
                 "reasoning_effort")
RETRYABLE = (408, 409, 425, 429, 500, 502, 503, 504, 529)


class RelayError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class RelayCancelled(Exception):
    """The client no longer wants the answer (it went away, the task was cancelled)."""


ChunkSink = Callable[[dict], bool]          # a streamed chunk -> False when nobody wants the rest


@dataclass(frozen=True)
class RelaySpec:
    worker_id: str
    kind: str                           # openai | anthropic
    base_url: str
    api_key: str | None
    models: tuple[str, ...]             # Kite's names for the models this worker serves
    upstream: dict[str, str] = field(default_factory=dict)   # Kite name -> the provider's model name
    slots: int = 1
    price_in: float = 0.0               # USD per million prompt tokens
    price_out: float = 0.0              # USD per million completion tokens
    daily_cap: float | None = None      # USD
    monthly_cap: float | None = None    # USD
    timeout: float = 600.0
    stream: bool = False                # read the answer as SSE (OpenAI-compatible only)
    pod: Any = None                     # config.PodConfig of a rented machine Kite starts and stops

    @property
    def paid(self) -> bool:
        return self.price_in > 0 or self.price_out > 0 or self.pod is not None

    def upstream_model(self, model: str) -> str:
        return self.upstream.get(model, model)

    def cost(self, usage: dict[str, Any] | None) -> float:
        usage = usage or {}
        prompt, completion = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        return (prompt * self.price_in + completion * self.price_out) / 1_000_000


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _month(ts: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


class SpendLedger:
    """USD spent per worker, per UTC day and month, rebuilt from the journal's ``spend`` records."""

    def __init__(self, records: list[dict] = (), *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.days: dict[tuple[str, str], float] = {}
        self.months: dict[tuple[str, str], float] = {}
        for record in records:
            worker, usd, ts = record.get("worker"), record.get("usd"), record.get("ts")
            if isinstance(worker, str) and isinstance(usd, (int, float)) and isinstance(ts, (int, float)):
                self._count(worker, float(usd), float(ts))

    def _count(self, worker: str, usd: float, ts: float) -> None:
        self.days[(worker, _day(ts))] = self.days.get((worker, _day(ts)), 0.0) + usd
        self.months[(worker, _month(ts))] = self.months.get((worker, _month(ts)), 0.0) + usd

    def add(self, worker: str, usd: float, ts: float | None = None) -> None:
        self._count(worker, usd, self.clock() if ts is None else ts)

    def spent(self, worker: str, now: float | None = None) -> tuple[float, float]:
        now = self.clock() if now is None else now
        return self.days.get((worker, _day(now)), 0.0), self.months.get((worker, _month(now)), 0.0)

    def over_cap(self, spec: RelaySpec, now: float | None = None) -> str | None:
        """Why this worker may not take more jobs now, or None."""
        day, month = self.spent(spec.worker_id, now)
        if spec.daily_cap is not None and day >= spec.daily_cap:
            return f"đã chi {day:.2f}/{spec.daily_cap:.2f} USD hôm nay (làm mới 00:00 UTC)"
        if spec.monthly_cap is not None and month >= spec.monthly_cap:
            return f"đã chi {month:.2f}/{spec.monthly_cap:.2f} USD tháng này (làm mới ngày 1, UTC)"
        return None


# ---------------------------------------------------------------- OpenAI-compatible
def openai_body(spec: RelaySpec, body: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in body.items() if k in OPENAI_PARAMS}
    out["model"] = spec.upstream_model(str(body.get("model", "")))
    out["stream"] = False
    return out


async def openai_call(spec: RelaySpec, body: dict[str, Any], session, sink: ChunkSink | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if spec.api_key:
        headers["Authorization"] = f"Bearer {spec.api_key}"
    if spec.stream:
        return await _stream(session, spec.base_url.rstrip("/") + "/chat/completions",
                             {**openai_body(spec, body), "stream": True, "stream_options": {"include_usage": True}},
                             headers, spec.timeout, sink)
    data = await _post(session, spec.base_url.rstrip("/") + "/chat/completions", openai_body(spec, body),
                       headers, spec.timeout)
    if not isinstance(data.get("choices"), list) or not data["choices"]:
        raise RelayError("the provider's answer has no choices")
    return data


# ---------------------------------------------------------------- Anthropic
def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") in ("text", "input_text"))
    return "" if content is None else str(content)


def _image_block(part: dict) -> dict | None:
    url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) \
        else part.get("image_url")
    if not isinstance(url, str):
        return None
    if url.startswith("data:") and ";base64," in url:
        media, data = url[5:].split(";base64,", 1)
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    if url.startswith("https://"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def anthropic_body(spec: RelaySpec, body: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI chat request as an Anthropic Messages request."""
    system: list[str] = []
    messages: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        blocks = [b for b in blocks if not (b.get("type") == "text" and not b.get("text"))]
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:        # Anthropic wants turns to alternate
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for message in body.get("messages") or []:
        role, content = message.get("role"), message.get("content")
        if role in ("system", "developer"):
            system.append(_text(content))
        elif role == "user":
            blocks = []
            for part in content if isinstance(content, list) else [{"type": "text", "text": _text(content)}]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image = _image_block(part)
                    if image:
                        blocks.append(image)
                elif isinstance(part, dict):
                    blocks.append({"type": "text", "text": part.get("text", "")})
            add("user", blocks)
        elif role == "assistant":
            blocks = [{"type": "text", "text": _text(content)}]
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": function.get("arguments")}
                blocks.append({"type": "tool_use", "id": call.get("id") or f"call_{len(blocks)}",
                               "name": function.get("name", ""),
                               "input": arguments if isinstance(arguments, dict) else {"value": arguments}})
            add("assistant", blocks)
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": message.get("tool_call_id", ""),
                          "content": _text(content)}])
    out: dict[str, Any] = {
        "model": spec.upstream_model(str(body.get("model", ""))),
        "messages": messages,
        "max_tokens": int(body.get("max_tokens") or body.get("max_completion_tokens") or 8192),
    }
    if any(system):
        out["system"] = "\n\n".join(s for s in system if s)
    for key in ("temperature", "top_p"):
        if isinstance(body.get(key), (int, float)):
            out[key] = body[key]
    stop = body.get("stop")
    if stop:
        out["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    choice = body.get("tool_choice")
    tools = body.get("tools") or []
    if tools and choice != "none":
        out["tools"] = [{"name": (t.get("function") or {}).get("name", ""),
                         "description": (t.get("function") or {}).get("description", ""),
                         "input_schema": (t.get("function") or {}).get("parameters")
                         or {"type": "object", "properties": {}}}
                        for t in tools if isinstance(t, dict)]
        if choice == "required":
            out["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            out["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    return out


STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
                "pause_turn": "stop", "refusal": "stop"}


def anthropic_to_openai(data: dict[str, Any], model: str) -> dict[str, Any]:
    text, calls = [], []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block.get("id", ""), "type": "function",
                          "function": {"name": block.get("name", ""),
                                       "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}})
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = calls
    usage = data.get("usage") or {}
    prompt = sum(int(usage.get(k) or 0) for k in
                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    completion = int(usage.get("output_tokens") or 0)
    return {"id": f"chatcmpl-{data.get('id', token_secrets.token_hex(8))}", "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": STOP_REASONS.get(data.get("stop_reason"), "stop")}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": prompt + completion}}


async def anthropic_call(spec: RelaySpec, body: dict[str, Any], session, sink: ChunkSink | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
    if spec.api_key:
        headers["x-api-key"] = spec.api_key
    data = await _post(session, spec.base_url.rstrip("/") + "/v1/messages", anthropic_body(spec, body),
                       headers, spec.timeout)
    return anthropic_to_openai(data, str(body.get("model", "")))


async def _post(session, url: str, payload: dict, headers: dict, timeout: float) -> dict:
    import aiohttp

    try:
        async with session.post(url, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            text = await response.text()
            if response.status >= 400:
                # The provider's own message, cut short; it never contains the key Kite sent.
                raise RelayError(f"HTTP {response.status}: {text[:300]}", retryable=response.status in RETRYABLE)
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RelayError("the provider did not answer with JSON") from exc
    except asyncio.TimeoutError as exc:
        raise RelayError(f"no answer within {timeout:.0f}s", retryable=True) from exc
    except aiohttp.ClientError as exc:
        raise RelayError(f"cannot reach the provider ({type(exc).__name__})", retryable=True) from exc


async def _stream(session, url: str, payload: dict, headers: dict, timeout: float,
                  sink: ChunkSink | None) -> dict:
    """POST with stream: true; pass each chunk to ``sink`` and return the whole answer."""
    import aiohttp

    from .pull_worker import assemble_stream

    chunks: list[dict] = []
    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(
                total=timeout, sock_read=min(timeout, 300))) as response:
            if response.status >= 400:
                text = await response.text()
                raise RelayError(f"HTTP {response.status}: {text[:300]}", retryable=response.status in RETRYABLE)
            async for raw in response.content:
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise RelayError("the provider sent a stream that is not JSON") from exc
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk:
                    raise RelayError(f"the provider stopped: {str(chunk['error'])[:300]}")
                chunks.append(chunk)
                if sink is not None and chunk.get("choices") and not sink(chunk):
                    raise RelayCancelled()
    except asyncio.TimeoutError as exc:
        raise RelayError(f"no answer within {timeout:.0f}s", retryable=not chunks) from exc
    except aiohttp.ClientError as exc:
        raise RelayError(f"cannot reach the provider ({type(exc).__name__})", retryable=not chunks) from exc
    if not any(chunk.get("choices") for chunk in chunks):
        raise RelayError("the provider's stream had no answer", retryable=True)
    return assemble_stream(chunks)


CALLS = {"openai": openai_call, "anthropic": anthropic_call}


# ---------------------------------------------------------------- the worker
class RelayWorker:
    """Takes jobs for ``spec`` from the gateway, answers them through the provider, counts the cost."""

    def __init__(self, spec: RelaySpec, gateway, ledger: SpendLedger, *, notify=None,
                 journal: Callable[[str, dict], None] | None = None, clock: Callable[[], float] = time.time,
                 retry_delay: float = 2.0, available: Callable[[], bool] | None = None) -> None:
        self.spec, self.gateway, self.ledger = spec, gateway, ledger
        self.notify, self.journal, self.clock = notify, journal, clock
        self.retry_delay = retry_delay
        self.capped: str | None = None
        # A rented machine is up only while Kite runs it (pods.py): no heartbeat while it is off, so the
        # gateway sees no worker for its models and may start it.
        self.available = available
        self.beating = False
        self.active = 0                       # jobs being answered now
        self.last_active = clock()            # when the last job started or ended (idle stop)

    async def beat(self) -> None:
        if self.available is not None and not self.available():
            if self.beating:                  # leave at once instead of after the heartbeat timeout
                self.beating = False
                await self.gateway.heartbeat(self.spec.worker_id, {
                    "ready": False, "draining": True, "slots": self.spec.slots,
                    "version": f"relay-{self.spec.kind}"})
            return
        self.beating = True
        reason = self.ledger.over_cap(self.spec, self.clock())
        if reason and reason != self.capped:
            self._record("launches", {"event": "capped", "worker": self.spec.worker_id, "reason": reason})
            if self.notify:
                self.notify(f"Kite: worker {self.spec.worker_id} hết hạn mức",
                            f"{reason}. Việc mới chuyển sang worker khác đang bật.", tags=("money_with_wings",))
        elif not reason and self.capped:
            self._record("launches", {"event": "resumed", "worker": self.spec.worker_id})
        self.capped = reason
        await self.gateway.heartbeat(self.spec.worker_id, {
            "ready": reason is None, "loaded_model": self.spec.models[0], "slots": self.spec.slots,
            "version": f"relay-{self.spec.kind}"})

    async def serve(self, job, session) -> None:
        self.active += 1
        self.last_active = self.clock()
        try:
            await self._serve(job, session)
        finally:
            self.active -= 1
            self.last_active = self.clock()

    async def _serve(self, job, session) -> None:
        call = CALLS[self.spec.kind]
        error = None
        sent = [0]

        def sink(chunk: dict) -> bool:
            # In-process: the chunk goes straight to the client's stream (False: the client went away).
            if not self.gateway.add_chunks(self.spec.worker_id, job.job_id, sent[0], [chunk]):
                return False
            sent[0] += 1
            return True

        for attempt in range(2):
            try:
                response = await call(self.spec, job.body, session, sink)
                break
            except RelayCancelled:
                return                        # the gateway already dropped the job
            except RelayError as exc:
                error = exc
                if not exc.retryable or attempt or sent[0]:
                    self.gateway.complete(self.spec.worker_id, job.job_id,
                                          {"status": "error", "error": f"{self.spec.worker_id}: {exc}"})
                    return
                await asyncio.sleep(self.retry_delay)
        else:     # pragma: no cover - the loop always breaks or returns
            raise AssertionError(error)
        usd = self.spec.cost(response.get("usage"))
        if usd:
            self.ledger.add(self.spec.worker_id, usd, self.clock())
        usage = response.get("usage") or {}
        self._record("spend", {"worker": self.spec.worker_id, "model": job.model,
                               "upstream": self.spec.upstream_model(job.model), "tag": job.tag,
                               "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                               "completion_tokens": int(usage.get("completion_tokens") or 0),
                               "usd": round(usd, 6)})
        self.gateway.complete(self.spec.worker_id, job.job_id, {"status": "ok", "response": response})
        if self.ledger.over_cap(self.spec, self.clock()):
            await self.beat()             # stop taking jobs now, not at the next heartbeat

    async def run(self, *, session_factory=None, beat_every: float = 20.0) -> None:
        import aiohttp

        async with (session_factory or aiohttp.ClientSession)() as session:
            async def beats() -> None:
                while True:
                    try:
                        await self.beat()
                    except Exception as exc:          # a heartbeat must never end the worker
                        print(f"kite relay {self.spec.worker_id}: {exc}")
                    await asyncio.sleep(beat_every)

            async def slot() -> None:
                while True:
                    if self.available is not None and not self.available():
                        # Waiting for jobs counts as being connected: a pod that is off must not look up.
                        await asyncio.sleep(min(beat_every, 1.0))
                        continue
                    job = await self.gateway.next_job(self.spec.worker_id, beat_every)
                    if job is not None:
                        await self.serve(job, session)

            await asyncio.gather(beats(), *(slot() for _ in range(self.spec.slots)))

    def _record(self, name: str, record: dict) -> None:
        if self.journal is None:
            return
        now = self.clock()
        try:
            self.journal(name, {"ts": round(now, 3), "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
                                **record})
        except Exception as exc:              # the journal must never break a request
            print(f"kite relay: journal error: {exc}")


def relay_specs(config) -> list[RelaySpec]:
    """RelaySpec for every enabled provider with adapter ``relay`` (secrets resolved here)."""
    from .config import resolve_secret

    specs = []
    for p in config.providers:
        if p.adapter != "relay" or not p.enabled or p.relay is None:
            continue
        r = p.relay
        key = resolve_secret(p.secrets["api_key"]) if p.secrets.get("api_key") else None
        specs.append(RelaySpec(p.effective_worker_id, r.kind, r.base_url, key, p.model_ids, dict(r.upstream),
                               slots=p.concurrency, price_in=r.price_in, price_out=r.price_out,
                               daily_cap=r.daily_cap, monthly_cap=r.monthly_cap, timeout=r.timeout,
                               stream=r.stream, pod=r.pod))
    return specs


def internal_token() -> str:
    """A worker token for the gateway's bookkeeping; a relay worker never authenticates over HTTP."""
    return token_secrets.token_urlsafe(32)
