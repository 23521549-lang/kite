"""Rented GPU machines (RunPod pods): Kite starts one when work arrives and stops it when idle.

The owner creates the pod once in RunPod's console: a GPU, an image that serves an OpenAI-compatible
API (vLLM ``vllm serve <model> --host 0.0.0.0 --port 8000 --api-key ...``, or llama-server), the port
exposed as HTTP, and a volume so the model is not downloaded again at each start. Kite never creates
or deletes pods; it only switches the existing one on and off through RunPod's REST API v2
(``POST /v2/pods/{id}/action`` with start or stop, ``GET /v2/pods/{id}``; v1 is retired on
1 Dec 2026) and reaches the model server through RunPod's HTTPS proxy
(``https://<pod id>-<port>.proxy.runpod.net``).

* Start: a request for one of the pod's models arrives (by the p22 rule: from the chat or a task the
  chat started), no worker serves it and no free autostart worker (Kaggle) could; or
  ``kite worker start <id>``. Kite asks RunPod to start the pod, waits until it runs and the model
  server answers ``GET /models``, then its relay worker (relay.py) takes jobs.
* Stop: ``idle_minutes`` without a job, the daily or monthly cap reached, ``kite worker stop <id>``,
  or a start that did not finish within ``start_timeout`` (so a broken pod does not bill all day).
* Cost: hours the pod runs times its hourly price (the owner's ``price_hour``, else the price RunPod
  reports), counted in the spend ledger like an API's tokens; caps work the same.

When Kite restarts while the pod runs, it takes the running pod over (its idle clock starts
again) and keeps billing from where the previous process stopped: every accrual saves a
billing cursor in the spend store (spend_ledger.py), and adoption resumes from that cursor,
so the hours the gateway was down are counted too.
The RunPod key lives in a 0600 file and only ever goes to api.runpod.io.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
import time
from typing import Any, Callable

RUNPOD_API = "https://api.runpod.io/v2"
RUNNING = "RUNNING"
BOOTING = ("PROVISIONING", "STARTING")
STOPPED = ("EXITED", "ERROR")


class PodError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RunPodClient:
    """The two calls Kite needs from RunPod's REST API v2."""

    def __init__(self, api_key: str, *, base_url: str = RUNPOD_API, session_factory=None, timeout: float = 30.0):
        self.api_key, self.base_url, self.timeout = api_key, base_url.rstrip("/"), timeout
        self.session_factory = session_factory

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        import aiohttp

        factory = self.session_factory or aiohttp.ClientSession
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with factory() as session:
                async with session.request(method, self.base_url + path, json=body, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                    text = await response.text()
                    if response.status >= 400:
                        raise PodError(f"RunPod HTTP {response.status}: {text[:200]}", status=response.status)
                    try:
                        import json

                        data = json.loads(text) if text.strip() else {}
                    except ValueError as exc:
                        raise PodError("RunPod did not answer with JSON") from exc
                    return data if isinstance(data, dict) else {}
        except asyncio.TimeoutError as exc:
            raise PodError("RunPod did not answer in time") from exc
        except aiohttp.ClientError as exc:
            raise PodError(f"cannot reach RunPod ({type(exc).__name__})") from exc

    async def get(self, pod_id: str) -> dict:
        return await self._call("GET", f"/pods/{pod_id}")

    async def action(self, pod_id: str, action: str) -> dict:
        return await self._call("POST", f"/pods/{pod_id}/action", {"action": action})


@dataclass
class PodState:
    state: str = "off"             # off | starting | up | stopping | error
    since: float = 0.0
    detail: str = ""
    reason: str = ""
    pod_status: str | None = None  # what RunPod last said: RUNNING, EXITED...
    billed_from: float = 0.0       # counting the hourly price from here (0: not billed)
    unflushed_usd: float = 0.0
    unflushed_hours: float = 0.0
    last_flush: float = 0.0


class PodController:
    """Starts and stops one pod; its RelayWorker answers jobs while the pod is up."""

    def __init__(self, spec, client: RunPodClient, relay, ledger, *, notify=None,
                 journal: Callable[[str, dict], None] | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = asyncio.sleep, health=None, poll_seconds: float = 10.0,
                 flush_seconds: float = 600.0, store=None) -> None:
        self.spec, self.pod = spec, spec.pod
        self.client, self.relay, self.ledger = client, relay, ledger
        # Durable billing cursor (spend_ledger.SpendStore); default: the ledger's own store.
        self.store = store if store is not None else getattr(ledger, "store", None)
        self.notify, self.journal, self.clock, self.sleep = notify, journal, clock, sleep
        self.health = health or self._server_answers
        self.poll_seconds, self.flush_seconds = poll_seconds, flush_seconds
        self.price = self.pod.price_hour       # None: RunPod's own price, read at start
        self.s = PodState(since=clock())
        self.lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        relay.available = lambda: self.s.state == "up"

    @property
    def worker_id(self) -> str:
        return self.spec.worker_id

    def serves(self, model: str) -> bool:
        return model in self.spec.models

    # ------------------------------------------------------------------ reporting
    def _set(self, state: str, detail: str = "") -> None:
        self.s.state, self.s.since, self.s.detail = state, self.clock(), detail

    def status(self) -> dict[str, Any]:
        now = self.clock()
        return {"state": self.s.state, "seconds": round(now - self.s.since), "detail": self.s.detail,
                "reason": self.s.reason, "kind": self.pod.provider, "pod": self.pod.pod_id,
                "pod_status": self.s.pod_status, "price_hour": self.price,
                "idle_minutes": self.pod.idle_minutes,
                "url": f"https://console.runpod.io/pods?id={self.pod.pod_id}"}

    def _record(self, name: str, record: dict) -> None:
        if self.journal is None:
            return
        now = self.clock()
        try:
            self.journal(name, {"ts": round(now, 3), "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
                                "worker": self.worker_id, **record})
        except Exception as exc:
            print(f"kite pods: journal error: {exc}", file=sys.stderr)

    def _tell(self, title: str, message: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(title, message, tags=("computer",))
        except Exception as exc:
            print(f"kite pods: notify error: {exc}", file=sys.stderr)

    def wait_note(self) -> str:
        doing = {"starting": f"Kite đang bật máy RunPod {self.pod.pod_id} ({self.s.detail})",
                 "stopping": "máy đang tắt",
                 "error": f"lần bật trước bị lỗi: {self.s.detail}"}.get(self.s.state, "máy đang tắt")
        return (f"Worker {self.worker_id} chưa chạy nên chưa trả lời được ({doing})."
                " Máy thuê thường lên sau 2–10 phút; khi đó nhắn lại.")

    # ------------------------------------------------------------------ money
    def _save_cursor(self) -> None:
        """Remember (durably) how far this pod's billing has been counted, for a gateway restart."""
        if self.store is not None:
            self.store.set_pod_cursor(self.worker_id, self.s.billed_from or None)

    def _resume_cursor(self) -> float:
        """Where billing should continue from: the saved cursor if the pod was already billing
        when this process started (the gateway was down, RunPod was not), else now."""
        now = self.clock()
        if self.store is not None:
            cursor = self.store.pod_cursor(self.worker_id)
            if cursor is not None and 0 < cursor <= now:
                return cursor
        return now

    def _accrue(self, *, flush: bool = False) -> None:
        """Count the hourly price from the last accrual until now (while RunPod bills the pod)."""
        now = self.clock()
        if self.s.billed_from:
            hours = max(0.0, now - self.s.billed_from) / 3600
            usd = hours * (self.price or 0.0)
            self.s.billed_from = now
            if usd:
                self.ledger.add(self.worker_id, usd, now)
            self.s.unflushed_usd += usd
            self.s.unflushed_hours += hours
            self._save_cursor()
        if self.s.unflushed_hours and (flush or now - self.s.last_flush >= self.flush_seconds):
            self._record("spend", {"kind": "pod", "pod": self.pod.pod_id, "hours": round(self.s.unflushed_hours, 4),
                                   "price_hour": self.price, "usd": round(self.s.unflushed_usd, 6)})
            self.s.unflushed_usd = self.s.unflushed_hours = 0.0
            self.s.last_flush = now

    def _price_from(self, pod: dict) -> None:
        if self.pod.price_hour is None and isinstance(pod.get("cost"), (int, float)) and pod["cost"] > 0:
            self.price = float(pod["cost"])

    # ------------------------------------------------------------------ the model server
    async def _server_answers(self) -> bool:
        import aiohttp

        headers = {"Authorization": f"Bearer {self.spec.api_key}"} if self.spec.api_key else {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.spec.base_url.rstrip("/") + "/models", headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=20)) as response:
                    return response.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    # ------------------------------------------------------------------ start / stop
    async def adopt(self) -> None:
        """At gateway start: a pod that is already running is taken over (and billed from now)."""
        try:
            pod = await self.client.get(self.pod.pod_id)
        except PodError as exc:
            self._set("off", f"không hỏi được RunPod: {exc}")
            return
        self._price_from(pod)
        self.s.pod_status = pod.get("status")
        if self.s.pod_status == RUNNING or self.s.pod_status in BOOTING:
            self.s.billed_from = self._resume_cursor()     # count the gateway's downtime too
            self._save_cursor()
            self.s.reason = "adopted"
            self._set("starting", "máy đang chạy từ trước; chờ model server trả lời")
            self._spawn(self._wait_up())

    def on_demand(self) -> None:
        """Gateway hook (through starters.py): a request for one of this pod's models is waiting."""
        if self.s.state == "off" or (self.s.state == "error" and self.clock() - self.s.since >= 120):
            # Starting from now on: the chat's status note says so, and a second request does not start again.
            self._set("starting", "đang yêu cầu RunPod bật máy")
            self._spawn(self.wake(reason="request", force=True))

    def _spawn(self, coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wake(self, *, reason: str = "manual", force: bool = False) -> dict[str, Any]:
        """Ask RunPod to start the pod; waiting for the model server goes on in the background."""
        async with self.lock:
            if self.s.state in ("up", "starting", "stopping") and not force:
                return self.status()
            capped = self.ledger.over_cap(self.spec, self.clock())
            if capped:
                self._set("off", f"không bật: {capped}")
                return self.status()
            self.s.reason = reason
            self._set("starting", "đang yêu cầu RunPod bật máy")
            try:
                pod = await self.client.action(self.pod.pod_id, "start")
            except PodError as exc:
                if exc.status != 409:             # 409: already running or starting; go on and wait
                    self._set("error", f"RunPod không bật máy: {exc}")
                    self._record("launches", {"event": "failed", "kind": "pod", "error": str(exc)[:300]})
                    return self.status()
                pod = {}
            self._price_from(pod)
            self.s.pod_status = pod.get("status") or self.s.pod_status
            self.s.billed_from = self.s.billed_from or self._resume_cursor()
            self._save_cursor()
            self._record("launches", {"event": "push", "kind": "pod", "pod": self.pod.pod_id, "reason": reason,
                                      "price_hour": self.price})
            self._spawn(self._wait_up())
        return self.status()

    async def _wait_up(self) -> None:
        started = self.clock()
        while self.s.state == "starting":
            if self.clock() - started > self.pod.start_timeout:
                await self.stop(reason="start_timeout", detail=(
                    f"máy không sẵn sàng sau {self.pod.start_timeout / 60:.0f} phút: đã tắt để khỏi tốn tiền"))
                self._set("error", self.s.detail)
                self._tell(f"Kite: máy {self.worker_id} không lên", self.s.detail +
                           f". Xem log pod trên RunPod: {self.status()['url']}")
                return
            try:
                pod = await self.client.get(self.pod.pod_id)
                self.s.pod_status = pod.get("status")
                self._price_from(pod)
            except PodError as exc:
                self.s.detail = f"không hỏi được RunPod: {exc}"
            if self.s.pod_status in STOPPED and self.clock() - started > 3 * self.poll_seconds:
                self._accrue(flush=True)
                self.s.billed_from = 0.0
                self._save_cursor()
                self._set("error", f"RunPod báo máy {self.s.pod_status} khi đang bật")
                self._record("launches", {"event": "failed", "error": self.s.detail})
                return
            if self.s.pod_status == RUNNING and await self.health():
                self.relay.last_active = self.clock()
                self._set("up", "")
                self._record("launches", {"event": "alive", "kind": "pod",
                                          "since_push_s": round(self.clock() - started)})
                await self.relay.beat()             # take jobs now, not at the next heartbeat
                return
            if self.s.pod_status == RUNNING:
                self.s.detail = "máy đã chạy, chờ model server trả lời (đang nạp model)"
            elif self.s.pod_status:
                self.s.detail = f"RunPod: {self.s.pod_status}"
            self._accrue()
            await self.sleep(self.poll_seconds)

    async def stop(self, *, reason: str = "manual", detail: str = "") -> dict[str, Any]:
        if self.s.state == "off" and not self.s.billed_from:
            return self.status()
        self._set("stopping", detail or "đang tắt máy")
        await self.relay.beat()                     # leave the gateway: no new job for this worker
        try:
            pod = await self.client.action(self.pod.pod_id, "stop")
            self.s.pod_status = pod.get("status") or "EXITED"
        except PodError as exc:
            if exc.status != 409:                  # 409: already stopped
                self._set("error", f"RunPod không tắt máy: {exc}. Tắt tay trên RunPod kẻo tốn tiền.")
                self._tell(f"Kite: không tắt được máy {self.worker_id}", self.s.detail)
                return self.status()
        self._accrue(flush=True)
        self.s.billed_from = 0.0
        self._save_cursor()
        self._record("launches", {"event": "stopped", "kind": "pod", "reason": reason})
        self._set("off", detail)
        return self.status()

    async def tick(self) -> None:
        """Every half minute: count the money, stop an idle or capped pod."""
        self._accrue()
        if self.s.state != "up":
            return
        capped = self.ledger.over_cap(self.spec, self.clock())
        if capped:
            await self.stop(reason="cap", detail=f"đã tắt: {capped}")
            self._tell(f"Kite: máy {self.worker_id} hết hạn mức", f"{capped}. Đã tắt máy RunPod.")
            return
        idle = self.clock() - self.relay.last_active
        if not self.relay.active and idle >= self.pod.idle_minutes * 60:
            await self.stop(reason="idle", detail=f"đã tắt sau {self.pod.idle_minutes} phút không có việc")
