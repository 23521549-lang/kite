"""`kite doctor`: real checks with fake probes (no services, Docker or network needed)."""

from __future__ import annotations

import json
from pathlib import Path

from poc.kaggle_worker import cli
from poc.kaggle_worker.config import validate_config
from poc.kaggle_worker.doctor import Probes, classify_address, listeners, render, run_checks, summary


def config_for(tmp_path: Path, *, launch: bool = False) -> tuple[dict, Path]:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    token = secrets / "worker_token_kaggle-han"
    token.write_text("t" * 40 + "\n")
    token.chmod(0o600)
    provider = {
        "id": "kaggle-han", "adapter": "pull", "enabled": True, "owner": "han",
        "model": "qwen3.6", "capabilities": ["coding"], "quota": {"gpu_seconds": 1, "concurrency": 1},
        "secrets": {"worker_token": f"file:{token}"},
    }
    if launch:
        key = secrets / "kaggle_api_token_kaggle-han"
        key.write_text("KGAT\n")
        key.chmod(0o644)                                   # too open: doctor warns
        provider["secrets"]["kaggle_api_token"] = f"file:{key}"
        provider["launch"] = {"kernel": "hanle/kite-worker"}
    raw = {
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "q"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": str(tmp_path / "h2"), "cli": "x"},
        "providers": [provider],
        "sandbox": {"image": "kite-sandbox:latest"},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return raw, path


SS = """LISTEN 0 4096 127.0.0.1:8787 0.0.0.0:*
LISTEN 0 4096 172.19.0.1:8787 0.0.0.0:*
LISTEN 0 4096 0.0.0.0:7233 0.0.0.0:*
LISTEN 0 4096 [::1]:4096 [::]:*
LISTEN 0 511 0.0.0.0:8443 0.0.0.0:*
"""


def probes(*, gateway=None, temporal=True, services_ok=True, docker_ok=True, free=40.0, ss=SS):
    calls = []

    def run(command, timeout):
        calls.append(command)
        if command[0] == "systemctl":
            return (0, "active\n") if services_ok else (3, "failed\n")
        if command[:2] == ["ss", "-ltnH"]:
            return 0, ss
        if command[0] == "docker":
            if not docker_ok:
                return 1, "Cannot connect to the Docker daemon"
            if command[1] == "version":
                return 0, "27.3.1\n"
            if command[1] == "network":
                return 0, "true\n"
            return 0, "sha256:abc\n"
        return 127, "unexpected"

    def http_json(url, timeout):
        if gateway is None:
            raise OSError("Connection refused")
        return gateway

    return Probes(run=run, http_json=http_json, tcp=lambda h, p, t: temporal, disk_free=lambda path: free,
                  which=lambda name: f"/usr/bin/{name}", has_module=lambda name: True), calls


def by_name(checks):
    return {c.name: c for c in checks}


def test_healthy_install_reports_ok_and_flags_a_temporal_port_open_to_all(tmp_path):
    raw, path = config_for(tmp_path)
    status = {"workers": [{"worker_id": "kaggle-han", "alive": True, "loaded_model": "qwen3.6", "busy": False}],
              "queued_jobs": 0}
    fake, calls = probes(gateway=status)
    checks = by_name(run_checks(validate_config(raw), path, fake))
    assert checks["gateway"].status == "ok" and "1/1 worker sống" in checks["gateway"].detail
    assert checks["worker kaggle-han"].status == "ok" and "qwen3.6" in checks["worker kaggle-han"].detail
    assert checks["cổng 8787 (gateway)"].status == "ok"            # loopback + the private sandbox address
    assert checks["cổng 7233 (temporal)"].status == "warn" and "ufw" in checks["cổng 7233 (temporal)"].fix
    assert checks["cổng 4096 (opencode)"].status == "ok"
    assert checks["secret kaggle-han.worker_token"].status == "ok"
    assert checks["dịch vụ kite-gateway"].status == "ok" and checks["sandbox network"].status == "ok"
    assert checks["harness2"].status == "skip"
    assert ["ss", "-ltnH"] in calls


def test_broken_install_says_what_to_run(tmp_path):
    raw, path = config_for(tmp_path, launch=True)
    fake, _calls = probes(gateway=None, temporal=False, services_ok=False, docker_ok=False, free=0.5)
    checks = run_checks(validate_config(raw), path, fake)
    named = by_name(checks)
    assert named["gateway"].status == "fail" and "systemctl status kite-gateway" in named["gateway"].fix
    assert named["temporal"].status == "fail"
    assert named["dịch vụ kite-workflow-worker"].status == "fail"
    assert named["docker"].status == "fail"
    assert named["đĩa dữ liệu"].status == "fail"
    assert named["secret kaggle-han.kaggle_api_token"].status == "warn"
    assert named["secret kaggle-han.kaggle_api_token"].fix.startswith("chmod 600")
    report = summary(checks)
    assert report["status"] == "needs_attention" and report["fail"] >= 5
    text = render(checks, path)
    assert "✗ gateway" in text and "→ sudo systemctl status kite-gateway" in text and "KGAT" not in text


def test_worker_states_from_autostart(tmp_path):
    raw, path = config_for(tmp_path, launch=True)
    status = {"workers": [{"worker_id": "kaggle-han", "alive": False,
                           "launch": {"state": "error", "detail": "Kaggle không nhận notebook: 403"}}]}
    fake, _ = probes(gateway=status)
    named = by_name(run_checks(validate_config(raw), path, fake))
    assert named["worker kaggle-han"].status == "fail" and "403" in named["worker kaggle-han"].detail
    assert "kaggle CLI" in named


def test_missing_secret_file_names_the_command_that_creates_it(tmp_path):
    raw, path = config_for(tmp_path)
    (tmp_path / "secrets" / "worker_token_kaggle-han").unlink()
    fake, _ = probes(gateway={"workers": []})
    named = by_name(run_checks(validate_config(raw), path, fake))
    assert named["secret kaggle-han.worker_token"].fix == "kite worker token kaggle-han"


def test_address_classes_and_ss_parsing():
    assert classify_address("127.0.0.1") == "loopback" and classify_address("[::1]") == "loopback"
    assert classify_address("172.19.0.1") == "private" and classify_address("10.0.0.5") == "private"
    assert classify_address("0.0.0.0") == classify_address("[::]") == classify_address("*") == "all"
    assert classify_address("54.1.2.3") == "public"
    assert listeners(SS)[0] == ("127.0.0.1", 8787) and ("[::1]", 4096) in listeners(SS)


def test_cli_doctor_exit_code_and_json(tmp_path, capsys, monkeypatch):
    raw, path = config_for(tmp_path)
    fake, _ = probes(gateway={"workers": [{"worker_id": "kaggle-han", "alive": True}]})
    monkeypatch.setattr("poc.kaggle_worker.doctor.default_probes", lambda: fake)
    assert cli.main(["--config", str(path), "doctor", "--json"]) == 0     # a warning (port 7233) is not a failure
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok" and report["warn"] >= 1
    fake_down, _ = probes(gateway=None)
    monkeypatch.setattr("poc.kaggle_worker.doctor.default_probes", lambda: fake_down)
    assert cli.main(["--config", str(path), "doctor"]) == 1
    assert "✗ gateway" in capsys.readouterr().out


def test_someone_elses_notebook_that_is_off_is_not_a_warning(tmp_path):
    # Full-test day, A1: doctor warned "kaggle-an chưa kết nối" (another person's notebook) every time.
    raw, path = config_for(tmp_path)
    second = json.loads(json.dumps(raw["providers"][0]))
    second.update(id="kaggle-an", owner="an")
    token = tmp_path / "secrets" / "worker_token_kaggle-an"
    token.write_text("u" * 40 + "\n")
    token.chmod(0o600)
    second["secrets"] = {"worker_token": f"file:{token}"}
    raw["providers"].append(second)
    status = {"workers": [{"worker_id": "kaggle-han", "alive": True, "loaded_model": "qwen3.6", "busy": False},
                          {"worker_id": "kaggle-an", "alive": False}], "queued_jobs": 0}
    fake, _ = probes(gateway=status)
    named = by_name(run_checks(validate_config(raw), path, fake))
    assert named["worker kaggle-an"].status == "skip" and "chưa kết nối" in named["worker kaggle-an"].detail
    alone = {"workers": [{"worker_id": "kaggle-han", "alive": False}, {"worker_id": "kaggle-an", "alive": False}]}
    fake, _ = probes(gateway=alone)
    named = by_name(run_checks(validate_config(raw), path, fake))
    assert named["worker kaggle-an"].status == "warn"           # nothing can serve: then it matters


def test_autostart_checks_the_token_mode_and_reads_the_gpu_quota(tmp_path):
    raw, path = config_for(tmp_path, launch=True)
    raw["providers"][0]["launch"]["token"] = "kaggle-secret"
    status = {"workers": [{"worker_id": "kaggle-han", "alive": False, "launch": {"state": "idle"}}]}
    fake, calls = probes(gateway=status)
    plain_run = fake.run

    def run(command, timeout):
        if command[0] == "env" and command[-2:] == ["quota", "-v"]:
            assert any(part.startswith("KAGGLE_API_TOKEN=") and part.endswith("kaggle_api_token_kaggle-han")
                       for part in command)
            return 0, "resource,used,remaining,total,refreshAt\nGPU,27.50h,2.50h,30.00h,2026-11-28T00:00:00+00:00\n"
        return plain_run(command, timeout)

    fake.run = run
    named = by_name(run_checks(validate_config(raw), path, fake))
    assert named["tự bật kaggle-han"].status == "warn" and "--token per-launch" in named["tự bật kaggle-han"].fix
    assert named["token kaggle-han"].status == "warn" and "rotate-token" in named["token kaggle-han"].fix
    quota = named["quota GPU kaggle-han"]
    assert quota.status == "warn" and quota.detail == "còn 2.50h / 30.00h, làm mới 2026-11-28T00:00"
