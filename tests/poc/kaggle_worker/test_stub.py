"""P2: the bootstrap notebook (stub) downloads the worker code from Kite, so a Kaggle run never has old code."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
import urllib.error

import pytest
from aiohttp.test_utils import TestClient, TestServer

from poc.kaggle_worker.gateway import ModelGateway, WorkerAuth, create_app
from poc.kaggle_worker.launcher import NOTEBOOK, inject_launch
from poc.kaggle_worker.pull_worker import WORKER_VERSION
from poc.kaggle_worker.stub import launch_for, notebook_cells, stub_notebook, stub_source, worker_bundle

from tests.poc.kaggle_worker.test_launcher import TOKEN_A, code, launch_values, make

GATEWAY = "https://1-2-3-4.sslip.io:8443"


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_stub(source: str, monkeypatch, *, bundle: dict, secret: str | None = "secret-token-from-kaggle",
             run_type: str = "Batch", status: int = 200) -> tuple[dict, list]:
    """Run the stub cell as Kaggle would, with a fake Secrets client and a fake gateway."""
    requests = []

    def urlopen(request, timeout=None):
        requests.append(request)
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
        return Answer(json.dumps(bundle).encode())

    secrets_module = types.ModuleType("kaggle_secrets")

    class UserSecretsClient:
        def get_secret(self, name):
            if secret is None:
                raise RuntimeError("no secret attached")
            assert name == "KITE_WORKER_TOKEN"
            return secret

    secrets_module.UserSecretsClient = UserSecretsClient
    monkeypatch.setitem(sys.modules, "kaggle_secrets", secrets_module)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setenv("KAGGLE_KERNEL_RUN_TYPE", run_type)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    namespace: dict = {}
    exec(compile(source, "<stub>", "exec"), namespace)
    return namespace, requests


SEEN = "SEEN = {'launch': dict(KITE_STUB_LAUNCH), 'token': KITE_TOKEN, 'gateway': KITE_GATEWAY}"


def test_a_stub_saved_by_hand_reads_the_secret_and_runs_the_code_kite_sends(monkeypatch):
    bundle = {"version": "abc", "cells": ["X = 1", "Y = X + 1", SEEN],
              "launch": {"worker_id": "kaggle-han", "model": "gemma4-26b", "idle_minutes": 30, "expires": 2e9}}
    namespace, requests = run_stub(stub_source(GATEWAY), monkeypatch, bundle=bundle)
    assert namespace["Y"] == 2                                         # the cells ran in order, one namespace
    assert requests[0].full_url == GATEWAY + "/worker/code"
    assert requests[0].get_header("Authorization") == "Bearer secret-token-from-kaggle"
    assert requests[0].get_header("X-kite-run-type") == "Batch"
    assert namespace["SEEN"] == {"launch": bundle["launch"], "token": "secret-token-from-kaggle", "gateway": GATEWAY}


def test_a_stub_kite_pushed_uses_its_short_lived_token_and_its_own_values(monkeypatch):
    pushed = inject_launch(stub_notebook(GATEWAY), {"worker_id": "kaggle-han", "model": "qwen3.6",
                                                    "token": "per-launch-token", "expires": 2e9,
                                                    "gateway_url": "https://9-9-9-9.sslip.io:8443"})
    bundle = {"version": "abc", "cells": [SEEN], "launch": {"model": "gemma4-26b", "idle_minutes": 30}}
    namespace, requests = run_stub(code(pushed), monkeypatch, bundle=bundle, secret=None)
    assert requests[0].get_header("Authorization") == "Bearer per-launch-token"   # no Secrets for an API push
    assert requests[0].full_url == "https://9-9-9-9.sslip.io:8443/worker/code"
    assert namespace["SEEN"]["launch"]["model"] == "qwen3.6"           # what Kite pushed wins
    assert namespace["SEEN"]["launch"]["idle_minutes"] == 30


def test_an_old_pushed_stub_saved_again_by_hand_falls_back_to_the_secret(monkeypatch):
    pushed = inject_launch(stub_notebook(GATEWAY), {"worker_id": "kaggle-han", "token": "expired", "expires": 1})
    bundle = {"version": "abc", "cells": [SEEN], "launch": {"model": "gemma4-26b"}}
    namespace, requests = run_stub(code(pushed), monkeypatch, bundle=bundle)
    assert requests[0].get_header("Authorization") == "Bearer secret-token-from-kaggle"
    assert namespace["SEEN"]["launch"] == {"model": "gemma4-26b"}


def test_a_wrong_token_says_which_secret_to_check(monkeypatch):
    with pytest.raises(RuntimeError, match="KITE_WORKER_TOKEN"):
        run_stub(stub_source(GATEWAY), monkeypatch, bundle={}, status=401)


def test_the_notebook_setup_cell_takes_the_stub_values_for_a_saved_run_only(monkeypatch):
    import os
    import time

    setup = next(s for s in notebook_cells() if "KITE_LAUNCH = {}" in s)
    lines = setup.splitlines()
    part = "\n".join(lines[next(i for i, line in enumerate(lines) if line.startswith("KITE_LAUNCH = {}")):
                           next(i for i, line in enumerate(lines) if line.startswith("assert shutil"))])
    stub_values = {"worker_id": "kaggle-han", "model": "gemma4-26b", "expires": time.time() + 600}
    for run_type, expected in (("Batch", stub_values), ("Interactive", {})):
        monkeypatch.setenv("KAGGLE_KERNEL_RUN_TYPE", run_type)
        namespace = {"os": os, "time": time, "KITE_STUB_LAUNCH": stub_values}
        exec(part, namespace)
        assert namespace["KITE_LAUNCH"] == expected
    worker = next(s for s in notebook_cells() if "WORKER_TOKEN = KITE_LAUNCH.get" in s)
    assert 'globals().get("KITE_TOKEN")' in worker and 'globals().get("KITE_GATEWAY")' in worker


def test_the_bundle_is_the_notebook_code_and_its_version():
    bundle = worker_bundle(launch_for("kaggle-han", now=1000.0, model="qwen3.6", gateway_url=GATEWAY))
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert bundle["cells"] == ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]
    assert bundle["version"] == WORKER_VERSION
    assert bundle["launch"] == {"worker_id": "kaggle-han", "idle_minutes": 30, "reason": "stub",
                                "started": bundle["launch"]["started"], "expires": 1000 + 6 * 3600,
                                "model": "qwen3.6", "gateway_url": GATEWAY}
    for source in bundle["cells"]:
        compile(source, "<cell>", "exec", dont_inherit=True)            # each cell compiles on its own


def test_worker_code_needs_a_worker_token_and_names_the_waiting_model():
    async def scenario():
        gateway = ModelGateway([WorkerAuth("kaggle-han", TOKEN_A, ("qwen3.6", "gemma4-26b"))])
        asked = []

        def worker_code(worker_id, headers):
            asked.append((worker_id, headers.get("X-Kite-Run-Type")))
            return worker_bundle(launch_for(worker_id, now=1.0, model=gateway.first_queued_model(worker_id)))

        job = await gateway.submit({"model": "gemma4-26b", "messages": []})
        async with TestClient(TestServer(create_app(gateway, worker_code=worker_code))) as client:
            denied = await client.get("/worker/code")
            answer = await client.get("/worker/code", headers={"Authorization": f"Bearer {TOKEN_A}",
                                                               "X-Kite-Run-Type": "Batch"})
            body = await answer.json()
        gateway.cancel(job)
        return denied.status, answer.status, body, asked

    denied, status, body, asked = asyncio.run(scenario())
    assert denied == 401 and status == 200
    assert body["launch"]["model"] == "gemma4-26b" and body["version"] == WORKER_VERSION
    assert asked == [("kaggle-han", "Batch")]


def test_autostart_pushes_the_stub_and_the_full_notebook_only_when_asked(tmp_path):
    async def push(launch):
        _config, gateway, launcher, fake, _clock = make(tmp_path, launch=launch)
        await launcher.wake(model="qwen3.6")
        return fake

    stub = asyncio.run(push({"kernel": "hanle/kite-worker"})).pushed
    assert "/worker/code" in code(stub) and "class PullWorker" not in code(stub)
    assert f"KITE_GATEWAY = {GATEWAY!r}" in code(stub)
    values = launch_values(stub)
    assert values["model"] == "qwen3.6" and len(values["token"]) >= 32
    full = asyncio.run(push({"kernel": "hanle/kite-worker", "notebook": "full"})).pushed
    assert "class PullWorker" in code(full)


def test_kite_worker_stub_prints_the_cell_to_paste(tmp_path, capsys):
    from poc.kaggle_worker import cli
    from tests.poc.kaggle_worker.test_launcher import raw_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})))
    assert cli.main(["--config", str(path), "worker", "stub", "kaggle-han"]) == 0
    out = capsys.readouterr().out
    assert "https://www.kaggle.com/code/hanle/kite-worker/edit" in out
    assert stub_source(GATEWAY) in out and "KGAT_secret" not in out
