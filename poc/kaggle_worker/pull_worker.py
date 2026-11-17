"""Pull worker: runs next to llama-server (for example in a Kaggle notebook) and serves the gateway.

Standard library only, so it can be pasted into a notebook cell without installing anything.

Loop: send a heartbeat, long-poll ``GET /worker/jobs``, make sure the requested model is loaded,
forward the request to the local llama-server, post the answer back. A background thread keeps
heartbeating while a long request runs, so the gateway does not think the worker died.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable


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
        self.log = log
        self.ready = True
        self.draining = False
        self._stop = threading.Event()
        self.jobs_done = 0

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
        }, timeout=30)
        if status == 401:
            raise PermissionError("gateway rejected the worker token")
        if status != 200:
            raise RuntimeError(f"heartbeat failed: HTTP {status} {body}")

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
                self.log(f"[{job_id[:8]}] nạp model {model}...")
                self.ready = False
                self._safe_heartbeat()
                self.ensure_model(model)
                self.ready = True
                self._safe_heartbeat()
            status, response = self._call("POST", f"{self.llama_url}/v1/chat/completions", job["body"],
                                          timeout=self.request_timeout_seconds, auth=False)
            if status != 200 or not isinstance(response, dict):
                raise RuntimeError(f"llama-server HTTP {status}: {str(response)[:500]}")
            payload = {"status": "ok", "response": response}
        except Exception as exc:
            self.ready = True
            payload = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:2000]}
        status, body = self._call("POST", f"{self.gateway_url}/worker/jobs/{job_id}/result", payload)
        seconds = time.monotonic() - started
        self.jobs_done += 1
        self.log(f"[{job_id[:8]}] {model} {payload['status']} sau {seconds:.1f}s (gateway: HTTP {status})")

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
                self.run_job(body)
        finally:
            self.ready = False
            self._stop.set()
            try:
                self.heartbeat_after_stop()
            except Exception:
                pass

    def heartbeat_after_stop(self) -> None:
        self._call("POST", f"{self.gateway_url}/worker/heartbeat", {
            "ready": False, "draining": True, "loaded_model": self.loaded_model(),
            "cached_models": self.cached_models()}, timeout=10)