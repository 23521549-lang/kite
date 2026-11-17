import json

import pytest

from poc.kaggle_worker.config import ConfigError, load_config, redact_config, validate_config


def valid_config():
    return {
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "kite"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": ".kite/harness2", "cli": "harness2"},
        "providers": [{
            "id": "local-kaggle", "adapter": "kaggle", "enabled": False,
            "model": "qwen/coder-7b", "capabilities": ["coding", "python"],
            "quota": {"gpu_seconds": 3600, "concurrency": 1},
            "secrets": {"kaggle_token": "env:KAGGLE_TOKEN"},
        }],
    }


def test_valid_config_loads_with_provider_disabled(tmp_path):
    path = tmp_path / "kite.yaml"
    import yaml
    path.write_text(yaml.safe_dump(valid_config()), encoding="utf-8")
    config = load_config(path)
    assert config.providers[0].enabled is False
    assert config.providers[0].adapter == "kaggle"


@pytest.mark.parametrize("field,value", [
    ("id", "bad id"), ("model", "../secret"), ("capabilities", ["Bad Capability"]),
])
def test_rejects_malformed_provider_fields(field, value):
    raw = valid_config()
    raw["providers"][0][field] = value
    with pytest.raises(ConfigError):
        validate_config(raw)


def test_rejects_unknown_adapter_and_missing_secret_reference():
    raw = valid_config()
    raw["providers"][0]["adapter"] = "mystery"
    with pytest.raises(ConfigError, match="adapter"):
        validate_config(raw)
    raw = valid_config()
    raw["providers"][0]["secrets"]["kaggle_token"] = ""
    with pytest.raises(ConfigError, match="secret"):
        validate_config(raw)
    raw = valid_config()
    raw["providers"][0]["secrets"] = {}
    with pytest.raises(ConfigError, match="requires a token"):
        validate_config(raw)


def test_inline_credential_values_are_rejected_and_redacted():
    raw = valid_config()
    raw["providers"][0]["secrets"]["kaggle_token"] = "super-secret-token"
    with pytest.raises(ConfigError, match="reference"):
        validate_config(raw)
    raw = valid_config()
    config = validate_config(raw)
    safe = json.dumps(redact_config(config))
    assert "KAGGLE_TOKEN" not in safe
    assert "env:KAGGLE_TOKEN" not in safe

