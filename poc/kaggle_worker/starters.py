"""The gateway's hooks for starting workers: Kaggle notebooks (launcher.py) and rented machines (pods.py).

The gateway has one set of hooks (``on_demand``, ``starting``, ``wait_note``, and ``wake``, ``stop``,
``status`` behind ``/kite/*``); this puts both kinds of startable workers behind them. When a model
is served by both, a free worker comes first: a pod only starts when no Kaggle notebook could start
for that model (none is configured, or its starts keep failing).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from .launcher import MAX_FAILURES


class Starters:
    def __init__(self, kaggle=None, pods=()) -> None:
        self.kaggle = kaggle
        self.pods = {pod.worker_id: pod for pod in pods}

    @property
    def states(self) -> list[str]:
        return sorted([*(self.kaggle.states if self.kaggle else {}), *self.pods])

    def _kaggle_can(self, model: str | None) -> bool:
        if self.kaggle is None or model is None:
            return False
        return any(model in self.kaggle.gateway.workers[wid].model_ids and state.failures < MAX_FAILURES
                   for wid, state in self.kaggle.states.items())

    def _pods_for(self, model: str | None) -> list:
        return [pod for pod in self.pods.values() if model is not None and pod.serves(model)]

    # ------------------------------------------------------------------ gateway hooks
    def on_demand(self, model: str) -> None:
        if self._kaggle_can(model):
            self.kaggle.on_demand(model)
            return
        pods = self._pods_for(model)
        if pods and not any(p.s.state in ("starting", "up") for p in pods):
            pods[0].on_demand()

    def starting(self, model: str) -> bool:
        return bool((self.kaggle is not None and self.kaggle.starting(model))
                    or any(p.s.state == "starting" for p in self._pods_for(model)))

    def wait_note(self, model: str) -> str:
        if self._kaggle_can(model):
            return self.kaggle.wait_note(model)
        pods = self._pods_for(model)
        return pods[0].wait_note() if pods else ""

    # ------------------------------------------------------------------ /kite/*
    async def wake(self, *, model: str | None = None, worker: str | None = None, reason: str = "manual",
                   force: bool = False) -> dict[str, Any]:
        if worker in self.pods:
            return {"worker": worker, **await self.pods[worker].wake(reason=reason, force=force)}
        if self.kaggle is not None and (worker is not None or model is None or self._kaggle_can(model)):
            return await self.kaggle.wake(model=model, worker=worker, reason=reason, force=force)
        if worker is not None:
            raise ValueError(f"worker {worker} is not started by Kite (no launch or pod config)")
        pods = self._pods_for(model)
        if pods:
            return {"worker": pods[0].worker_id, **await pods[0].wake(reason=reason, force=force)}
        return {"state": "none", "detail": "không worker nào Kite tự bật được cho model này"}

    async def stop(self, worker: str) -> dict[str, Any] | None:
        """Stop a pod (Kite turns the machine off); None: not a pod, the gateway asks the worker."""
        if worker not in self.pods:
            return None
        return {"stopped": True, **await self.pods[worker].stop(reason="manual", detail="tắt theo lệnh")}

    def status(self) -> dict[str, dict[str, Any]]:
        report = dict(self.kaggle.status()) if self.kaggle is not None else {}
        report.update({wid: pod.status() for wid, pod in self.pods.items()})
        return report

    async def watch(self, every: float = 30.0) -> None:
        """Kaggle's start checks (every minute) and the pods' money and idle clock (every ``every``)."""
        tasks = [asyncio.ensure_future(self.kaggle.watch())] if self.kaggle is not None else []
        for pod in self.pods.values():
            await pod.adopt()
        try:
            while True:
                await asyncio.sleep(every)
                for pod in self.pods.values():
                    try:
                        await pod.tick()
                    except Exception as exc:
                        print(f"kite pods: {pod.worker_id}: {exc}", file=sys.stderr)
        finally:
            for task in tasks:
                task.cancel()
