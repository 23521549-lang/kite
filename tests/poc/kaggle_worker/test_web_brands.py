"""Provider brands (Gói B): the brand shown for a worker is derived from its relay address
against the preset catalog — never stored in config. Presets are injected fakes here; the real
catalog lands with the presets package and only changes the data, not this logic."""

from __future__ import annotations

from types import SimpleNamespace

from poc.kaggle_worker.config import PodConfig, ProviderConfig, RelayConfig
from poc.kaggle_worker.web import workers as wk

FAKE_PRESETS = [
    {"brand": "deepseek", "label": "DeepSeek", "type": "openai",
     "base_url": "https://api.deepseek.com/v1",
     "models": [{"id": "deepseek-chat", "price_in": 0.27, "price_out": 1.1, "context": 128000}],
     "note": "", "catalog_date": "2026-11-25"},
    {"brand": "openrouter", "label": "OpenRouter", "type": "openai",
     "base_url": "https://openrouter.ai/api/v1",
     "models": [{"id": "openai/gpt-5-mini", "price_in": 0.25, "price_out": 2.0, "context": 400000}],
     "note": "", "catalog_date": "2026-11-25"},
    {"brand": "anthropic", "label": "Anthropic", "type": "anthropic",
     "base_url": "https://api.anthropic.com",
     "models": [{"id": "claude-sonnet-4-5", "price_in": 3.0, "price_out": 15.0, "context": 200000}],
     "note": "", "catalog_date": "2026-11-25"},
]


def _provider(adapter="relay", relay=None, **overrides):
    fields = {"id": "w1", "adapter": adapter, "enabled": True, "model": "m1",
              "capabilities": ("coding",), "quota_gpu_seconds": 0, "concurrency": 1,
              "secrets": {}, "relay": relay}
    fields.update(overrides)
    return ProviderConfig(**fields)


def _relay(base_url, kind="openai", pod=None):
    return RelayConfig(kind=kind, base_url=base_url, pod=pod)


def test_a_relay_on_a_preset_host_takes_the_preset_label():
    assert wk.brand_for(_provider(relay=_relay("https://api.deepseek.com/v1")), FAKE_PRESETS) == "DeepSeek"
    assert wk.brand_for(_provider(relay=_relay("https://openrouter.ai/api/v1")), FAKE_PRESETS) == "OpenRouter"
    anthropic = _provider(relay=_relay("https://api.anthropic.com", kind="anthropic"))
    assert wk.brand_for(anthropic, FAKE_PRESETS) == "Anthropic"


def test_a_relay_on_an_unknown_host_is_custom_with_the_host_named():
    provider = _provider(relay=_relay("https://llm.example.org:8443/v1"))
    assert wk.brand_for(provider, FAKE_PRESETS) == "Tùy chỉnh (llm.example.org)"


def test_a_pull_worker_is_kaggle():
    assert wk.brand_for(_provider(adapter="pull", relay=None), FAKE_PRESETS) == "Kaggle"


def test_a_pod_relay_is_runpod_even_though_its_url_is_a_proxy_host():
    pod = PodConfig(pod_id="pod123")
    provider = _provider(relay=_relay("https://pod123-8000.proxy.runpod.net/v1", pod=pod))
    assert wk.brand_for(provider, FAKE_PRESETS) == "RunPod"


def test_without_presets_a_relay_falls_back_to_its_type_label_once_the_host_is_unmatched():
    # No catalog at all (presets.py absent): the host matches nothing, so the row still gets a
    # stable brand string; with the real catalog the same worker resolves to its preset label.
    provider = _provider(relay=_relay("https://api.anthropic.com", kind="anthropic"))
    assert wk.brand_for(provider, []) == "Tùy chỉnh (api.anthropic.com)"


class _Ledger:
    def __init__(self, spent):
        self._spent = spent

    def spent(self, worker_id):
        return self._spent.get(worker_id, (0.0, 0.0))


def _config():
    return SimpleNamespace(providers=[
        _provider(id="ds-main", relay=_relay("https://api.deepseek.com/v1")),
        _provider(id="ds-backup", relay=_relay("https://api.deepseek.com/v1")),
        _provider(id="kaggle-han", adapter="pull", relay=None),
        _provider(id="pod-a", relay=_relay("https://pod123-8000.proxy.runpod.net/v1",
                                           pod=PodConfig(pod_id="pod123"))),
    ])


def test_worker_rows_carry_the_brand(monkeypatch):
    monkeypatch.setattr(wk, "PROVIDER_PRESETS", FAKE_PRESETS)
    rows = wk.worker_rows(_config(), None, None, lambda wid: False)
    assert {r["id"]: r["brand"] for r in rows} == {
        "ds-main": "DeepSeek", "ds-backup": "DeepSeek", "kaggle-han": "Kaggle", "pod-a": "RunPod"}


def test_month_spend_rolls_up_by_brand_from_the_rows(monkeypatch):
    monkeypatch.setattr(wk, "PROVIDER_PRESETS", FAKE_PRESETS)
    ledger = _Ledger({"ds-main": (0.5, 3.25), "ds-backup": (0.25, 1.75), "kaggle-han": (0.0, 0.0)})
    rows = wk.worker_rows(_config(), None, ledger, lambda wid: False)
    totals: dict[str, float] = {}
    for row in rows:
        totals[row["brand"]] = round(totals.get(row["brand"], 0.0) + row["spend"]["month_usd"], 4)
    assert totals["DeepSeek"] == 5.0
    assert totals["Kaggle"] == 0.0 and totals["RunPod"] == 0.0
