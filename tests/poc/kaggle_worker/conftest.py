"""Shared test setup: keep the graphify code map and the fast path's gateway call out of tests
unless a test asks for them."""

from __future__ import annotations

import pytest

from poc.kaggle_worker import codemap, sandbox


def pytest_configure(config):
    config.addinivalue_line("markers", "graphify: test uses the real graphify command (skipped if missing)")
    config.addinivalue_line("markers", "gateway_asker: test uses the real sandbox.gateway_asker (with a fake urlopen)")


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
