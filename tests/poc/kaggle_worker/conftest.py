"""Shared test setup: keep the graphify code map out of tests unless a test asks for it."""

from __future__ import annotations

import pytest

from poc.kaggle_worker import codemap


def pytest_configure(config):
    config.addinivalue_line("markers", "graphify: test uses the real graphify command (skipped if missing)")


@pytest.fixture(autouse=True)
def _no_graphify_by_default(request, monkeypatch):
    # With graphify installed (the EC2 venv), every sandbox test would otherwise build a real
    # code map and see different prompts; tests opt in with @pytest.mark.graphify or a fake build.
    if request.node.get_closest_marker("graphify") is None:
        monkeypatch.setattr(codemap, "graphify_cli", lambda: None)
