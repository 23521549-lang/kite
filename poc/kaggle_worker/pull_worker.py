"""Pull worker: runs next to llama-server (for example in a Kaggle notebook) and serves the gateway.

Standard library only, so it can be pasted into a notebook cell without installing anything.

Loop: send a heartbeat, long-poll ``GET /worker/jobs``, make sure the requested model is loaded,
forward the request to the local llama-server, post the answer back. A background thread keeps
heartbeating while a long request runs, so the gateway does not think the worker died.

Every answer is read from llama-server as it is generated and its chunks go to the gateway about
twice a second (``POST /worker/jobs/<id>/chunks``); if the gateway no longer wants them (the client
went away, the task was cancelled), generation stops at once instead of running to the end.

With ``slots`` > 1 (llama-server started with ``--parallel N``), up to N jobs run at the same time
on the loaded model: the person chatting is answered while a task's long request is generating.
A job that needs another model waits until the worker is idle, then switches.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

# Version of the worker code in the Kaggle notebook: a hash of all its code cells (this line written
# as ``WORKER_VERSION = ""``). The worker reports it in its heartbeat, so EC2 can tell when a notebook
# started by hand runs older code than the repository (``kite doctor``, ``kite worker status``).
# tests/poc/kaggle_worker/test_launcher.py checks it and prints the right value when a cell changes.
WORKER_VERSION = "024cf8ecfa01"


class PullWorker:
    def __init__(
        self,
        gateway_url: str,
        token: str,
        *,
        ensure_model: Callable[[str], None],
        loaded_model: Callable[[], str | None],
        cached_models: Callable[[], list[str]] = lambda: [],
        llama_url: str = "http://127.0.0.1:8080",
        poll_wait_seconds: int = 50,
        heartbeat_seconds: float = 20.0,
        request_timeout_seconds: float = 1800.0,
        stop_after_seconds: float | None = None,
        stop_when_idle_seconds: float | None = None,
        stream_flush_seconds: float = 0.5,
        slots: int | Callable[[], int] = 1,
        log: Callable[[str], None] = print,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.token = token
        self.ensure_model = ensure_model
        self.loaded_model = loaded_model
        self.cached_models = cached_models
        self.llama_url = llama_url.rstrip("/")
        self.poll_wait_seconds = poll_wait_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.deadline = time.monotonic() + stop_after_seconds if stop_after_seconds else None
        # Started by Kite (not by hand): stop after this long without a job so the GPU quota is not
        # spent on an idle session. Kite starts the notebook again when work arrives.
        self.stop_when_idle_seconds = stop_when_idle_seconds
        self.last_job_at = time.monotonic()
        self.stream_flush_seconds = stream_flush_seconds
        self.log = log
        self.ready = True
        self.draining = False
        self._stop = threading.Event()
        self.jobs_done = 0
        self._slots = slots                  # or a callable: llama-server's --parallel of the loaded model
        self._busy = threading.Condition()
        self._running = 0
        self._threads: list[threading.Thread] = []

    @property
    def slots(self) -> int:
        try:
            return max(1, int(self._slots() if callable(self._slots) else self._slots))
        except (TypeError, ValueError):
            return 1

    # ------------------------------------------------------------------ HTTP
    def _call(self, method: str, url: str, body: Any = None, *, timeout: float = 70,
              auth: bool = True) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return exc.code, raw.decode("utf-8", "replace")

    def heartbeat(self) -> None:
        status, body = self._call("POST", f"{self.gateway_url}/worker/heartbeat", {
            "ready": self.ready and not self._stop.is_set(),
            "draining": self.draining,
            "loaded_model": self.loaded_model(),
            "cached_models": self.cached_models(),
            "version": WORKER_VERSION,
            "slots": self.slots,
            "running": self._running,
        }, timeout=30)
        if status == 401:
            raise PermissionError("gateway rejected the worker token")
        if status != 200:
            raise RuntimeError(f"heartbeat failed: HTTP {status} {body}")
        if isinstance(body, dict) and body.get("stop") and not self._stop.is_set():
            # `kite worker stop` on EC2: finish the current job, then end the session.
            self.log("Gateway yêu cầu dừng worker (kite worker stop).")
            self.draining = True
            self._stop.set()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self.heartbeat()
            except PermissionError:
                self.log("Token bị gateway từ chối; dừng worker.")
                self._stop.set()
            except Exception as exc:  # network blips must not kill the worker
                self.log(f"heartbeat lỗi (sẽ thử lại): {exc}")

    # ------------------------------------------------------------------ jobs
    def run_job(self, job: dict[str, Any]) -> None:
        job_id, model = job["job_id"], job["model"]
        started = time.monotonic()
        try:
            if self.loaded_model() != model:
                with self._busy:            # never swap the model under another slot's request
                    self._busy.wait_for(lambda: self._running <= 1 or self._stop.is_set())
                self.log(f"[{job_id[:8]}] nạp model {model}...")
                self.ready = False
                self._safe_heartbeat()
                self.ensure_model(model)
                self.ready = True
                self._safe_heartbeat()
            # Always streamed from llama-server, even when the client wants one piece: a cancelled
            # task or a gone client then stops generation at once (the gateway refuses the chunks).
            payload = {"status": "ok", **self._stream(job_id, job["body"])}
        except Exception as exc:
            self.ready = True
            payload = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:2000]}
        status, body = self._call("POST", f"{self.gateway_url}/worker/jobs/{job_id}/result", payload)
        seconds = time.monotonic() - started
        self.jobs_done += 1
        self.last_job_at = time.monotonic()
        self.log(f"[{job_id[:8]}] {model} {payload['status']} sau {seconds:.1f}s (gateway: HTTP {status})")

    def _start_job(self, job: dict[str, Any]) -> None:
        """Run a job; with more than one slot in a thread, so the next one can be fetched meanwhile."""
        with self._busy:
            self._running += 1

        def work() -> None:
            try:
                self.run_job(job)
            finally:
                with self._busy:
                    self._running -= 1
                    self._busy.notify_all()

        if self.slots == 1:
            work()
            return
        thread = threading.Thread(target=work, name=f"kite-job-{job['job_id'][:8]}", daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()

    def _wait_for_slot(self) -> bool:
        """Block until a slot is free; False when the worker is stopping."""
        with self._busy:
            while self._running >= self.slots and not self._stop.is_set():
                self._busy.wait(1.0)
        return not self._stop.is_set()

    def _stream(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Stream the answer from llama-server, passing its chunks to the gateway as they come.

        Returns the whole answer assembled as a normal (non-streamed) completion, for the gateway's
        journal, plus the chunks the gateway has not acknowledged yet (``start`` = index of the first).
        """
        pending: list[dict] = []            # chunks the gateway has not acknowledged yet
        state = {"sent": 0, "gone": False}
        lock = threading.Lock()
        finished = threading.Event()

        def flush() -> None:
            with lock:
                batch, start = list(pending), state["sent"]
            if not batch:
                return
            status, _ = self._call("POST", f"{self.gateway_url}/worker/jobs/{job_id}/chunks",
                                   {"start": start, "chunks": batch}, timeout=30)
            if status == 200:
                with lock:
                    del pending[:len(batch)]
                    state["sent"] = start + len(batch)
            elif status in (404, 409):
                state["gone"] = True        # the client went away or the job was given up

        def flusher() -> None:
            while not finished.wait(self.stream_flush_seconds):
                try:
                    flush()
                except Exception:           # a network blip: the chunks stay pending, sent next time
                    pass

        request = urllib.request.Request(
            f"{self.llama_url}/v1/chat/completions", method="POST",
            data=json.dumps({**body, "stream": True, "stream_options": {"include_usage": True}}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        chunks: list[dict] = []
        thread = threading.Thread(target=flusher, daemon=True)
        thread.start()
        try:
            try:
                response = urllib.request.urlopen(request, timeout=self.request_timeout_seconds)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"llama-server HTTP {exc.code}: "
                                   f"{exc.read().decode('utf-8', 'replace')[:500]}") from exc
            with response:
                for raw in response:
                    if state["gone"]:
                        raise RuntimeError("the gateway no longer wants this answer (client gone)")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk:
                        raise RuntimeError(f"llama-server: {str(chunk['error'])[:500]}")
                    chunks.append(chunk)
                    with lock:
                        pending.append(chunk)
        finally:
            finished.set()
            thread.join()
        with lock:
            return {"response": assemble_stream(chunks), "start": state["sent"], "chunks": list(pending)}

    def _safe_heartbeat(self) -> None:
        try:
            self.heartbeat()
        except PermissionError:
            raise
        except Exception as exc:
            self.log(f"heartbeat lỗi: {exc}")

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.heartbeat()   # fail fast on a wrong URL or token
        self.log(f"Worker đã kết nối {self.gateway_url}, model đang nạp: {self.loaded_model()}")
        beat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        beat.start()
        backoff = 1.0
        try:
            while not self._stop.is_set():
                if self.deadline is not None and time.monotonic() > self.deadline:
                    self.log("Hết thời gian session dự kiến: chuyển sang draining và dừng.")
                    self.draining = True
                    self._safe_heartbeat()
                    break
                if not self._wait_for_slot():
                    break
                idle = time.monotonic() - self.last_job_at
                if self.stop_when_idle_seconds and idle > self.stop_when_idle_seconds and not self._running:
                    self.log(f"Không có việc {idle / 60:.0f} phút: tự tắt để đỡ tốn quota GPU "
                             "(Kite sẽ tự bật lại khi có việc).")
                    self.draining = True
                    self._safe_heartbeat()
                    break
                try:
                    status, body = self._call(
                        "GET", f"{self.gateway_url}/worker/jobs?wait={self.poll_wait_seconds}",
                        timeout=self.poll_wait_seconds + 20)
                except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                    self.log(f"Mất kết nối gateway ({exc}); thử lại sau {backoff:.0f}s")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 1.0
                if status == 204:
                    continue
                if status == 401:
                    raise PermissionError("gateway rejected the worker token")
                if status != 200 or not isinstance(body, dict):
                    self.log(f"GET /worker/jobs trả HTTP {status}: {body}")
                    self._stop.wait(5)
                    continue
                self._start_job(body)
        finally:
            self.ready = False
            self._stop.set()
            for thread in self._threads:              # jobs already running finish first
                thread.join(timeout=self.request_timeout_seconds)
            try:
                self.heartbeat_after_stop()
            except Exception:
                pass

    def heartbeat_after_stop(self) -> None:
        self._call("POST", f"{self.gateway_url}/worker/heartbeat", {
            "ready": False, "draining": True, "loaded_model": self.loaded_model(),
            "cached_models": self.cached_models()}, timeout=10)


def assemble_stream(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """The chunks of a streamed chat completion, put back together as one non-streamed completion."""
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish, usage, timings, first = None, None, None, (chunks[0] if chunks else {})
    for chunk in chunks:
        usage = chunk.get("usage") or usage
        timings = chunk.get("timings") or timings
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                slot = calls.setdefault(call.get("index", 0), {
                    "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                function = call.get("function") or {}
                slot["id"] = slot["id"] or call.get("id")
                if function.get("name") and not slot["function"]["name"]:
                    slot["function"]["name"] = function["name"]
                slot["function"]["arguments"] += function.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [{**calls[i], "id": calls[i]["id"] or f"call_{i}"} for i in sorted(calls)]
    response: dict[str, Any] = {
        "id": first.get("id") or "chatcmpl-kite", "object": "chat.completion",
        "created": first.get("created") or int(time.time()), "model": first.get("model") or "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}]}
    if usage:
        response["usage"] = usage
    if timings:
        response["timings"] = timings
    return response
