"""Model gateway: OpenAI-compatible endpoint on EC2, served by pull workers (for example Kaggle).

OpenCode (or any OpenAI-compatible client) sends ``POST /v1/chat/completions`` to the gateway on
loopback. The gateway queues the request; a worker that has long-polled ``GET /worker/jobs``
takes it, runs it on its own llama-server and posts the answer back. Workers only make outbound
HTTPS calls, so nothing has to be opened on the worker side.

Client routes (``/v1/*``) are meant to stay on loopback; only ``/worker/*`` is published through
the reverse proxy. Every worker call carries ``Authorization: Bearer <token>`` and the token
decides which worker is calling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import sys
import time
import uuid
from typing import Any

from aiohttp import web

from .journal import clean_tag, request_record


@dataclass(frozen=True)
class WorkerAuth:
    worker_id: str
    token: str
    model_ids: tuple[str, ...]


@dataclass(frozen=True)
class ModelAlias:
    """A client-visible model name mapped onto a base model plus extra request fields.

    Example: ``qwen3.6-fast`` -> base ``qwen3.6`` with
    ``{"chat_template_kwargs": {"enable_thinking": false}}`` to skip the thinking phase.
    """

    name: str
    base_model: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class WorkerState:
    worker_id: str
    model_ids: tuple[str, ...]
    ready: bool = False
    draining: bool = False
    loaded_model: str | None = None
    cached_models: tuple[str, ...] = ()
    last_seen: float = 0.0
    current_job: str | None = None


@dataclass
class Job:
    job_id: str
    model: str
    body: dict[str, Any]
    created_at: float
    future: asyncio.Future
    attempts: int = 0
    worker_id: str | None = None
    assigned_at: float | None = None
    tag: str | None = None          # X-Kite-Task: which task (or "chat", "bench-...") asked
    requested: str = ""             # model name as the client asked (alias included)


StatusHook = Callable[[WorkerState, bool], Awaitable[None] | None]


class GatewayError(RuntimeError):
    pass


class ModelGateway:
    """In-memory job queue between OpenAI-style clients and pull workers."""

    def __init__(
        self,
        workers: list[WorkerAuth],
        *,
        aliases: list[ModelAlias] = (),
        job_timeout_seconds: float = 1800.0,
        heartbeat_timeout_seconds: float = 90.0,
        max_attempts: int = 2,
        keepalive_seconds: float = 15.0,
        clock: Callable[[], float] = time.time,
        on_worker_status: StatusHook | None = None,
        journal: Callable[[dict], None] | None = None,
    ) -> None:
        if not workers:
            raise ValueError("at least one worker must be configured")
        ids = [w.worker_id for w in workers]
        if len(set(ids)) != len(ids):
            raise ValueError("worker IDs must be unique")
        digests = [self._digest(w.token) for w in workers]
        if len(set(digests)) != len(digests):
            raise ValueError("each worker needs its own token")
        if any(len(w.token) < 24 for w in workers):
            raise ValueError("worker tokens must be at least 24 characters")
        self._tokens = {self._digest(w.token): w.worker_id for w in workers}
        self.workers = {w.worker_id: WorkerState(w.worker_id, tuple(w.model_ids)) for w in workers}
        self.aliases = {a.name: a for a in aliases}
        self.job_timeout_seconds = job_timeout_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.max_attempts = max_attempts
        self.keepalive_seconds = keepalive_seconds
        self.clock = clock
        self.on_worker_status = on_worker_status
        self.journal = journal            # receives one record per finished request (no prompt content)
        self.jobs: dict[str, Job] = {}
        self._queue: list[str] = []
        self._changed = asyncio.Event()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def authenticate(self, header: str | None) -> str | None:
        if not header or not header.startswith("Bearer "):
            return None
        digest = self._digest(header[len("Bearer "):].strip())
        for known, worker_id in self._tokens.items():
            if hmac.compare_digest(known, digest):
                return worker_id
        return None

    def served_models(self) -> list[str]:
        base = sorted({m for w in self.workers.values() for m in w.model_ids})
        return base + sorted(a for a, alias in self.aliases.items() if alias.base_model in base)

    def resolve_model(self, requested: str) -> tuple[str, dict[str, Any]]:
        alias = self.aliases.get(requested)
        if alias is not None:
            return alias.base_model, dict(alias.params)
        return requested, {}

    def _alive(self, worker: WorkerState, now: float) -> bool:
        return worker.ready and now - worker.last_seen <= self.heartbeat_timeout_seconds

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    # ------------------------------------------------------------------ client side
    async def submit(self, body: dict[str, Any], *, tag: str | None = None) -> Job:
        requested = body.get("model")
        if not isinstance(requested, str) or not requested.strip():
            raise GatewayError("request must name a model")
        model, params = self.resolve_model(requested)
        if not any(model in w.model_ids for w in self.workers.values()):
            raise GatewayError(f"no configured worker serves model {requested}")
        forwarded = {**body, **params, "model": model, "stream": False}
        forwarded.pop("stream_options", None)
        job = Job(uuid.uuid4().hex, model, forwarded, self.clock(),
                  asyncio.get_running_loop().create_future(), tag=tag, requested=requested)
        self.jobs[job.job_id] = job
        self._queue.append(job.job_id)
        self._notify()
        return job

    def _finish(self, job: Job, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        if job.worker_id and self.workers.get(job.worker_id) and \
                self.workers[job.worker_id].current_job == job.job_id:
            self.workers[job.worker_id].current_job = None
        if job.job_id in self._queue:
            self._queue.remove(job.job_id)
        self.jobs.pop(job.job_id, None)
        if self.journal is not None and not job.future.done():
            try:
                self.journal(request_record(
                    job_id=job.job_id, tag=job.tag, requested=job.requested, model=job.model,
                    worker=job.worker_id, attempts=job.attempts, created_at=job.created_at,
                    assigned_at=job.assigned_at, finished_at=self.clock(), body=job.body,
                    result=result, error=error))
            except Exception as exc:          # the journal must never break a request
                print(f"kite gateway: journal error: {exc}", file=sys.stderr)
        if not job.future.done():
            if error is not None:
                job.future.set_exception(GatewayError(error))
            else:
                job.future.set_result(result)
        self._notify()

    def cancel(self, job: Job) -> None:
        self._finish(job, error="client disconnected")

    # ------------------------------------------------------------------ worker side
    async def heartbeat(self, worker_id: str, payload: Mapping[str, Any]) -> WorkerState:
        worker = self.workers[worker_id]
        was_ready = self._alive(worker, self.clock())
        worker.ready = bool(payload.get("ready", False))
        worker.draining = bool(payload.get("draining", False))
        loaded = payload.get("loaded_model")
        worker.loaded_model = loaded if isinstance(loaded, str) and loaded else None
        cached = payload.get("cached_models", [])
        worker.cached_models = tuple(m for m in cached if isinstance(m, str)) if isinstance(cached, list) else ()
        worker.last_seen = self.clock()
        if self.on_worker_status is not None:
            outcome = self.on_worker_status(worker, not was_ready and self._alive(worker, self.clock()))
            if asyncio.iscoroutine(outcome):
                await outcome
        self._notify()
        return worker

    def _pick_job(self, worker: WorkerState) -> Job | None:
        """Prefer jobs for the model already loaded, then the oldest job this worker can serve."""
        candidates = [self.jobs[j] for j in self._queue if self.jobs[j].model in worker.model_ids]
        if not candidates:
            return None
        loaded = [j for j in candidates if j.model == worker.loaded_model]
        if loaded:
            return loaded[0]
        # Do not make a worker switch models while another live worker already has that model loaded.
        now = self.clock()
        for job in candidates:
            holder = any(
                other.worker_id != worker.worker_id and self._alive(other, now)
                and not other.draining and other.loaded_model == job.model
                for other in self.workers.values()
            )
            if not holder or now - job.created_at > 30:
                return job
        return None

    async def next_job(self, worker_id: str, wait_seconds: float) -> Job | None:
        worker = self.workers[worker_id]
        worker.last_seen = self.clock()
        deadline = self.clock() + wait_seconds
        while True:
            self.reap()
            if worker.ready and not worker.draining and worker.current_job is None:
                job = self._pick_job(worker)
                if job is not None:
                    self._queue.remove(job.job_id)
                    job.worker_id = worker_id
                    job.assigned_at = self.clock()
                    job.attempts += 1
                    worker.current_job = job.job_id
                    return job
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                pass

    def complete(self, worker_id: str, job_id: str, payload: Mapping[str, Any]) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.worker_id != worker_id:
            return False
        if payload.get("status") == "ok" and isinstance(payload.get("response"), dict):
            self._finish(job, result=payload["response"])
        else:
            error = payload.get("error")
            self._finish(job, error=str(error)[:2000] if error else "worker reported an error")
        return True

    def reap(self) -> None:
        """Requeue jobs whose worker went silent or overran; fail them after max attempts."""
        now = self.clock()
        for job in list(self.jobs.values()):
            if job.worker_id is None:
                if now - job.created_at > self.job_timeout_seconds:
                    self._finish(job, error="no worker took the request in time")
                continue
            worker = self.workers[job.worker_id]
            stale = now - worker.last_seen > self.heartbeat_timeout_seconds
            overran = job.assigned_at is not None and now - job.assigned_at > self.job_timeout_seconds
            if not (stale or overran):
                continue
            if worker.current_job == job.job_id:
                worker.current_job = None
            if job.attempts >= self.max_attempts:
                self._finish(job, error="worker stopped responding")
            else:
                job.worker_id = None
                job.assigned_at = None
                self._queue.insert(0, job.job_id)
                self._notify()

    def status(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "workers": [{
                "worker_id": w.worker_id, "alive": self._alive(w, now), "draining": w.draining,
                "models": list(w.model_ids),
                "loaded_model": w.loaded_model, "cached_models": list(w.cached_models),
                "busy": w.current_job is not None,
                "seconds_since_heartbeat": round(now - w.last_seen, 1) if w.last_seen else None,
            } for w in self.workers.values()],
            "queued_jobs": len(self._queue),
            "running_jobs": sum(1 for j in self.jobs.values() if j.worker_id),
        }


# ---------------------------------------------------------------------- SSE re-encoding
def completion_to_sse(response: Mapping[str, Any]) -> list[bytes]:
    """Re-encode a non-streaming chat completion as OpenAI-style SSE chunks.

    The worker answers in one piece; clients such as OpenCode request ``stream: true``, so the
    gateway replays the finished answer as two chunks (content/tool calls, then finish + usage).
    """
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("reasoning_content"):
        delta["reasoning_content"] = message["reasoning_content"]
    if message.get("content"):
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [
            {"index": i, "id": call.get("id") or f"call_{i}", "type": "function",
             "function": {"name": (call.get("function") or {}).get("name", ""),
                          "arguments": (call.get("function") or {}).get("arguments", "")}}
            for i, call in enumerate(message["tool_calls"])
        ]
    base = {"id": response.get("id", "chatcmpl-kite"), "object": "chat.completion.chunk",
            "created": response.get("created", int(time.time())), "model": response.get("model", "")}
    first = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}]}
    if response.get("usage"):
        last["usage"] = response["usage"]
    return [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode() for chunk in (first, last)] \
        + [b"data: [DONE]\n\n"]


# ---------------------------------------------------------------------- aiohttp app
def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": {"message": message, "type": "kite_gateway"}}, status=status)


def create_app(gateway: ModelGateway, *, max_body_bytes: int = 32 * 1024 * 1024) -> web.Application:
    app = web.Application(client_max_size=max_body_bytes)

    async def models(_request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [
            {"id": m, "object": "model", "owned_by": "kite"} for m in gateway.served_models()]})

    async def chat(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "body must be a JSON object")
        try:
            job = await gateway.submit(body, tag=clean_tag(request.headers.get("X-Kite-Task")))
        except GatewayError as exc:
            return _error(404, str(exc))
        if not body.get("stream"):
            try:
                result = await job.future
            except GatewayError as exc:
                return _error(502, str(exc))
            except asyncio.CancelledError:
                gateway.cancel(job)
                raise
            # Answer with the model name the client asked for, not the worker's GGUF file path.
            return web.json_response({**result, "model": body["model"]})

        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                             "Cache-Control": "no-cache"})
        await stream.prepare(request)
        try:
            while True:
                try:
                    result = await asyncio.wait_for(asyncio.shield(job.future), gateway.keepalive_seconds)
                    break
                except asyncio.TimeoutError:
                    await stream.write(b": waiting for worker\n\n")   # SSE comment keeps the connection open
        except GatewayError as exc:
            error = {"error": {"message": str(exc), "type": "kite_gateway"}}
            await stream.write(f"data: {json.dumps(error)}\n\n".encode())
            await stream.write_eof()
            return stream
        except (asyncio.CancelledError, ConnectionResetError):
            gateway.cancel(job)
            raise
        for chunk in completion_to_sse({**result, "model": body["model"]}):
            await stream.write(chunk)
        await stream.write_eof()
        return stream

    def worker_id_or_401(request: web.Request) -> str:
        worker_id = gateway.authenticate(request.headers.get("Authorization"))
        if worker_id is None:
            raise web.HTTPUnauthorized(text="invalid worker token")
        return worker_id

    async def heartbeat(request: web.Request) -> web.Response:
        worker_id = worker_id_or_401(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return _error(400, "heartbeat must be a JSON object")
        worker = await gateway.heartbeat(worker_id, payload)
        return web.json_response({"worker_id": worker_id, "models": list(worker.model_ids)})

    async def jobs(request: web.Request) -> web.Response:
        worker_id = worker_id_or_401(request)
        try:
            wait = max(0.0, min(float(request.query.get("wait", "50")), 55.0))
        except ValueError:
            return _error(400, "wait must be a number of seconds")
        job = await gateway.next_job(worker_id, wait)
        if job is None:
            return web.Response(status=204)
        return web.json_response({"job_id": job.job_id, "model": job.model, "body": job.body})

    async def result(request: web.Request) -> web.Response:
        worker_id = worker_id_or_401(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return _error(400, "result must be a JSON object")
        if not gateway.complete(worker_id, request.match_info["job_id"], payload):
            return _error(409, "job is not assigned to this worker (it may have timed out)")
        return web.json_response({"accepted": True})

    async def status(_request: web.Request) -> web.Response:
        return web.json_response(gateway.status())

    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/status", status)
    app.router.add_post("/worker/heartbeat", heartbeat)
    app.router.add_get("/worker/jobs", jobs)
    app.router.add_post("/worker/jobs/{job_id}/result", result)
    return app


def opencode_provider_config(gateway_url: str, models: list[str], *, context: int | dict[str, int] = 32768,
                             output: int = 8192) -> dict[str, Any]:
    """The ``provider`` block to put in OpenCode's ``opencode.json`` so it talks to the gateway.

    ``context`` is one limit for every model or a per-model mapping (missing models: 32768). OpenCode
    compacts the conversation before it reaches this limit, so it must match the worker's real window.
    """
    limits = context if isinstance(context, dict) else {}
    default = context if isinstance(context, int) else 32768
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "kite": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Kite gateway",
                "options": {"baseURL": gateway_url.rstrip("/") + "/v1"},
                "models": {m: {"name": m, "tool_call": True,
                               "limit": {"context": limits.get(m, default), "output": output}}
                           for m in models},
            }
        },
    }