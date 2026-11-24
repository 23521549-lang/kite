"""Package A: the brand preset catalog is well-formed static data, and the web serves it behind the
session at /api/provider-presets so the add-worker form can offer brands instead of raw URLs."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker.web import security as sec  # noqa: E402
from poc.kaggle_worker.web.app import WebContext, create_app  # noqa: E402
from poc.kaggle_worker.web.db import Store, connect, sqlite_url  # noqa: E402
from poc.kaggle_worker.web.presets import CATALOG_DATE, PROVIDER_PRESETS  # noqa: E402
from poc.kaggle_worker.web.workers import TYPES  # noqa: E402

from tests.poc.kaggle_worker.test_launcher import raw_config  # noqa: E402
from tests.poc.kaggle_worker.test_web import NOW, PASSWORD, SECRET, FakeGateway, FakeOps  # noqa: E402

EXPECTED_BRANDS = ["openai", "deepseek", "xai", "groq", "openrouter", "together", "mistral", "gemini",
                   "anthropic"]


# ---------------------------------------------------------------- the catalog itself
def test_every_preset_is_well_formed():
    for preset in PROVIDER_PRESETS:
        assert preset["type"] in TYPES and preset["type"] in ("openai", "anthropic"), preset["brand"]
        assert preset["base_url"].startswith("https://"), preset["brand"]
        assert preset["label"] and preset["note"], preset["brand"]
        assert preset["catalog_date"] == CATALOG_DATE == "2026-11-25", preset["brand"]
        assert preset["models"], preset["brand"]
        for model in preset["models"]:
            assert isinstance(model["id"], str) and model["id"], preset["brand"]
            assert model["price_in"] >= 0 and model["price_out"] >= 0, (preset["brand"], model["id"])
            assert isinstance(model["context"], int) and model["context"] > 0, (preset["brand"], model["id"])


def test_brands_are_unique_and_the_expected_ones_are_all_there():
    brands = [p["brand"] for p in PROVIDER_PRESETS]
    assert len(brands) == len(set(brands))
    assert brands == EXPECTED_BRANDS


def test_only_anthropic_uses_the_anthropic_type():
    by_brand = {p["brand"]: p for p in PROVIDER_PRESETS}
    assert by_brand["anthropic"]["type"] == "anthropic"
    assert all(p["type"] == "openai" for brand, p in by_brand.items() if brand != "anthropic")


# ---------------------------------------------------------------- the endpoint
@pytest.fixture
def site(tmp_path):
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("w" * 32 + "\n")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw))
    clock = {"now": NOW}
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")), clock=lambda: clock["now"])
    store.migrate()
    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    store.create_user("han", sec.hash_password(PASSWORD), sealer.seal(SECRET))
    ctx = WebContext(config_path, store, sealer, ops_factory=lambda _config: FakeOps(), gateway=FakeGateway(),
                     clock=lambda: clock["now"], events_every=0.05)
    client = TestClient(create_app(ctx), base_url="https://testserver")
    return {"client": client, "clock": clock}


def signed_in(site):
    response = site["client"].post("/api/auth/login", json={
        "username": "han", "password": PASSWORD,
        "code": sec.totp_code(SECRET, int(site["clock"]["now"] // 30))})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


def test_presets_endpoint_needs_a_session(site):
    assert site["client"].get("/api/provider-presets").status_code == 401


def test_presets_endpoint_serves_the_whole_catalog(site):
    headers = signed_in(site)
    body = site["client"].get("/api/provider-presets", headers=headers).json()
    assert [p["brand"] for p in body["presets"]] == EXPECTED_BRANDS
    assert body["presets"] == PROVIDER_PRESETS
    deepseek = next(p for p in body["presets"] if p["brand"] == "deepseek")
    assert deepseek["base_url"] == "https://api.deepseek.com/v1"
    assert deepseek["models"][0]["id"] == "deepseek-chat"
