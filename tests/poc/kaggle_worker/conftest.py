"""Shared test setup: keep the graphify code map and the fast path's gateway call out of tests
unless a test asks for them."""

from __future__ import annotations

import socket

import pytest

from poc.kaggle_worker import codemap, sandbox


def free_loopback_port() -> int:
    """A free TCP port on 127.0.0.1 for the Temporal test server.

    temporalio's test server picks its own port with sdk-core's
    get_free_port("0.0.0.0"), which self-connects to 0.0.0.0 — that
    loopback self-connect is intercepted by this VM's egress proxy, so
    start_time_skipping() hangs forever before spawning the server.
    Passing an explicit port skips get_free_port entirely, and binding
    on 127.0.0.1 is not intercepted.
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def pytest_configure(config):
    config.addinivalue_line("markers", "graphify: test uses the real graphify command (skipped if missing)")
    config.addinivalue_line("markers", "gateway_asker: test uses the real sandbox.gateway_asker (with a fake urlopen)")
    config.addinivalue_line("markers", "idle_helpers: test uses the real sandbox.idle_helpers (with a fake urlopen)")


@pytest.fixture(autouse=True)
def _no_graphify_by_default(request, monkeypatch):
    # With graphify installed (the EC2 venv), every sandbox test would otherwise build a real
    # code map and see different prompts; tests opt in with @pytest.mark.graphify or a fake build.
    if request.node.get_closest_marker("graphify") is None:
        monkeypatch.setattr(codemap, "graphify_cli", lambda: None)


@pytest.fixture(autouse=True)
def _no_gateway_fast_path(request, monkeypatch):
    # The fast path asks the real gateway; tests pass their own `asker` (or a fake gateway_asker).
    if request.node.get_closest_marker("gateway_asker") is None:
        monkeypatch.setattr(sandbox, "gateway_asker", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _no_second_worker(request, monkeypatch):
    # Best-of-N looks for idle workers on the real gateway; tests say which helpers exist.
    if request.node.get_closest_marker("idle_helpers") is None:
        monkeypatch.setattr(sandbox, "idle_helpers", lambda *args, **kwargs: [])
