"""P6, on the host: kite-stack updates from the stable tag, checks every service, puts the previous release
back when the new one is not healthy, waits while something runs, and carries out the web's requests.
Docker is a fake that keeps registry digests, releases and container health."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("kite_stack", ROOT / "deploy" / "stack" / "kite_stack.py")
ks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ks)


class FakeDocker:
    def __init__(self):
        self.registry = {"kite": "sha256:" + "a" * 64, "sandbox": "sha256:" + "b" * 64}
        self.revision = {"kite": "1111aaaa"}
        self.unhealthy: set[str] = set()          # kite digests whose web service never gets healthy
        self.running = None                      # KITE_IMAGE of the last `compose up`
        self.busy = {"busy": False, "tasks": [], "bench": None, "chat": 0}
        self.cosign_ok = True                    # what `cosign verify` says about the new images
        self.calls: list[list[str]] = []

    def done(self, out="", code=0, err=""):
        return subprocess.CompletedProcess([], code, out, err)

    def __call__(self, cmd, timeout=None, capture=True, tty=False, env=None):
        self.calls.append(cmd)
        if cmd[:2] == ["systemctl", "daemon-reload"] or cmd[0] == "systemctl":
            return self.done()
        if cmd[:2] == ["docker", "pull"]:
            return self.done()
        if cmd[:3] == ["docker", "image", "inspect"]:
            ref = cmd[3]
            name = "sandbox" if "sandbox" in ref else "kite"
            repo = ref.rsplit(":", 1)[0]
            return self.done(json.dumps([{"RepoDigests": [f"{repo}@{self.registry[name]}"],
                                          "Config": {"Labels": {"org.opencontainers.image.revision":
                                                                self.revision.get(name, "")}},
                                          "Created": "2026-11-24T00:00:00Z"}]))
        if cmd[:2] == ["docker", "cp"]:
            dest = Path(cmd[3])
            shutil.copytree(ROOT / "deploy", dest)
            return self.done()
        if cmd[:2] == ["docker", "network"]:
            return self.done("true 172.30.230.1")
        if cmd[:2] == ["docker", "compose"]:
            return self.compose(cmd)
        if cmd[0].endswith("cosign"):
            return self.done(code=0) if self.cosign_ok else self.done(code=1, err="no matching signatures")
        return self.done()                        # create, rm, tag, rmi, image prune

    def compose(self, cmd):
        env_files = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--env-file"]
        images = ks.read_env(Path(env_files[-1]))
        args = cmd[[i for i, p in enumerate(cmd) if p.endswith("compose.yaml")][-1] + 1:]
        if args[:2] == ["up", "-d"]:
            self.running = images["KITE_IMAGE"]
            return self.done()
        if args[:1] == ["ps"]:
            digest = (self.running or "@").split("@", 1)[1]
            rows = [{"Service": s, "State": "running", "Health": "healthy" if s in ks.KITE_SERVICES else ""}
                    for s in (*ks.KITE_SERVICES, *ks.INFRA_SERVICES)]
            rows += [{"Service": s, "State": "exited", "ExitCode": 0, "Health": ""} for s in ks.ONE_SHOT]
            if digest in self.unhealthy:
                rows[2]["Health"] = "unhealthy"
            return self.done("\n".join(json.dumps(r) for r in rows))
        if args[:1] == ["exec"] and args[-1] == "busy":
            return self.done(json.dumps(self.busy))
        if args[:1] == ["logs"]:
            return self.done("web | Traceback: boom")
        return self.done()


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "LIB", tmp_path / "lib")
    monkeypatch.setattr(ks, "BIN", tmp_path / "bin" / "kite-stack")
    monkeypatch.setattr(ks, "UNIT_DIR", tmp_path / "units")
    (tmp_path / "bin").mkdir()
    (tmp_path / "units").mkdir()
    docker = FakeDocker()
    clock = {"now": 1000.0}

    def sleep(seconds):
        clock["now"] += seconds

    stack = ks.Stack(tmp_path / "data", run=docker, clock=lambda: clock["now"], sleep=sleep, out=lambda _m: None,
                     health_timeout=120)
    stack.dir.mkdir(parents=True)
    ks.write_env(stack.env_file, {"KITE_IMAGE_REPO": "ghcr.io/han/kite", "KITE_SANDBOX_REPO": "ghcr.io/han/kite-sandbox",
                                  "KITE_CHANNEL": "stable", "KITE_AUTO_UPDATE": "1"}, "test")
    return {"stack": stack, "docker": docker, "clock": clock, "tmp": tmp_path}


def compose_ups(docker):
    return [c for c in docker.calls if c[:2] == ["docker", "compose"] and "up" in c]


def test_the_first_release_runs_pinned_by_digest_and_kite_stack_adopts_its_own_new_version(host):
    stack, docker = host["stack"], host["docker"]
    result = stack.update(force=True)
    assert result["last"]["result"] == "updated"
    current = stack.release()
    assert current.name == "a" * 12
    images = ks.read_env(current / "images.env")
    assert images["KITE_IMAGE"] == "ghcr.io/han/kite@sha256:" + "a" * 64
    assert images["KITE_SANDBOX_IMAGE"] == "ghcr.io/han/kite-sandbox@sha256:" + "b" * 64
    assert (current / "deploy" / "stack" / "compose.yaml").is_file()
    assert ["docker", "tag", images["KITE_SANDBOX_IMAGE"], "kite-sandbox:latest"] in docker.calls
    assert ["docker", "network", "inspect", "-f", "{{.Internal}} {{(index .IPAM.Config 0).Gateway}}",
            "kite-sandbox-net"] in docker.calls
    assert (host["tmp"] / "lib" / "kite_stack.py").read_text() == (ROOT / "deploy/stack/kite_stack.py").read_text()
    assert (host["tmp"] / "units" / "kite-stack-update.timer").is_file()
    status = json.loads(stack.status_file.read_text())
    assert status["current"]["revision"] == "1111aaaa" and status["state"] == "idle"
    assert stack.status_file.stat().st_mode & 0o777 == 0o644          # the web container reads it


def test_the_same_digest_is_up_to_date_and_nothing_restarts(host):
    stack, docker = host["stack"], host["docker"]
    stack.update(force=True)
    ups = len(compose_ups(docker))
    assert stack.update(timer=True)["last"]["result"] == "up_to_date"
    assert len(compose_ups(docker)) == ups


def test_a_new_release_waits_while_a_task_a_bench_or_a_chat_runs(host):
    stack, docker = host["stack"], host["docker"]
    stack.update(force=True)
    docker.registry["kite"] = "sha256:" + "c" * 64
    docker.busy = {"busy": True, "tasks": ["chat-1"], "bench": {"label": "v7"}, "chat": 1}
    result = stack.update(timer=True)
    assert result["last"]["result"] == "busy" and "chat-1" in result["last"]["message"]
    assert "v7" in result["last"]["message"] and stack.release().name == "a" * 12
    assert result["available"]["digest"] == "sha256:" + "c" * 64      # the Settings tab shows it is coming
    docker.busy = {"busy": False}
    assert stack.update(timer=True)["last"]["result"] == "updated"
    assert stack.release().name == "c" * 12 and stack.release(stack.previous_link).name == "a" * 12


def test_an_unhealthy_release_goes_back_to_the_previous_one_and_is_not_retried_alone(host):
    stack, docker = host["stack"], host["docker"]
    stack.update(force=True)
    bad = "sha256:" + "d" * 64
    docker.registry["kite"] = bad
    docker.unhealthy.add(bad)
    result = stack.update(timer=True)
    assert result["last"]["result"] == "rolled_back"
    assert "web: unhealthy" in result["last"]["message"] and "Traceback" in result["last"]["message"]
    assert stack.release().name == "a" * 12                            # the old one runs again
    assert docker.running.endswith("a" * 64)
    assert bad in result["bad_digests"]
    tags = [c for c in docker.calls if c[:2] == ["docker", "tag"] and c[-1] == "kite-sandbox:latest"]
    assert tags[-1][2].endswith("b" * 64)                               # its sandbox image too
    ups = len(compose_ups(docker))
    again = stack.update(timer=True)
    assert again["last"]["result"] == "failed" and "đã lỗi" in again["last"]["message"]
    assert len(compose_ups(docker)) == ups
    docker.unhealthy.clear()                                            # fixed by hand: --force tries it again
    assert stack.update(force=True)["last"]["result"] == "updated" and stack.release().name == "d" * 12


def test_automatic_updates_can_be_switched_off(host):
    stack, docker = host["stack"], host["docker"]
    env = stack.env()
    env["KITE_AUTO_UPDATE"] = "0"
    ks.write_env(stack.env_file, env, "test")
    stack.update(timer=True)
    assert not any(c[:2] == ["docker", "pull"] for c in docker.calls)
    assert stack.update()["last"]["result"] == "updated"                # asked from the web: still done


def test_requests_from_the_web_restart_the_chat_with_the_gateway_and_ignore_the_rest(host):
    stack, docker = host["stack"], host["docker"]
    stack.update(force=True)
    stack.requests_dir.mkdir(parents=True)
    (stack.requests_dir / "1-restart.json").write_text(json.dumps({"action": "restart", "services": ["gateway"]}))
    (stack.requests_dir / "2-rm.json").write_text(json.dumps({"action": "rm", "services": ["/"]}))
    (stack.requests_dir / "3-restart.json").write_text(json.dumps({"action": "restart", "services": ["postgres"]}))
    (stack.requests_dir / ".4-update.tmp").write_text("{}")             # still being written: left alone
    done = stack.requests()
    assert [d["action"] for d in done] == ["restart", "rm", "restart"]
    restarts = [c for c in docker.calls if c[:2] == ["docker", "compose"] and "restart" in c]
    assert restarts[0][-2:] == ["gateway", "opencode"] and len(restarts) == 1
    assert done[2]["result"]["result"] == "failed"
    assert [p.name for p in stack.requests_dir.iterdir()] == [".4-update.tmp"]


def test_compose_always_gets_the_machines_settings_and_the_releases_pinned_images(host):
    stack = host["stack"]
    stack.update(force=True)
    cmd = stack.compose("ps")
    assert cmd[:4] == ["docker", "compose", "-p", "kite"]
    files = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--env-file"]
    assert files == [str(stack.env_file), str(stack.release() / "images.env")]
    assert cmd[-1] == "ps"


def test_secrets_stay_in_a_root_only_file(tmp_path):
    path = tmp_path / "stack.env"
    ks.write_env(path, {"TEMPORAL_POSTGRES_PASSWORD": "p"}, "x")
    assert path.stat().st_mode & 0o777 == 0o600 and ks.read_env(path) == {"TEMPORAL_POSTGRES_PASSWORD": "p"}


def require_signatures(host, monkeypatch, present=True):
    stack = host["stack"]
    env = stack.env()
    env["KITE_VERIFY_IMAGE_SIGNATURES"] = "1"
    ks.write_env(stack.env_file, env, "test")
    monkeypatch.setattr(ks, "find_cosign", lambda: "/usr/local/bin/cosign" if present else None)


def cosign_calls(docker):
    return [c for c in docker.calls if c[0].endswith("cosign")]


def test_an_update_with_signature_checking_verifies_both_images_by_digest_first(host, monkeypatch):
    stack, docker = host["stack"], host["docker"]
    require_signatures(host, monkeypatch)
    assert stack.update(force=True)["last"]["result"] == "updated"
    calls = cosign_calls(docker)
    assert [c[-1] for c in calls] == ["ghcr.io/han/kite@sha256:" + "a" * 64,
                                      "ghcr.io/han/kite-sandbox@sha256:" + "b" * 64]
    for call in calls:
        assert call[1] == "verify"
        assert call[call.index("--certificate-oidc-issuer") + 1] == "https://token.actions.githubusercontent.com"
        identity = call[call.index("--certificate-identity-regexp") + 1]
        assert identity == r"^https://github\.com/han/kite/\.github/workflows/ci\.yml@"


def test_an_update_whose_signature_fails_is_refused_and_the_running_release_is_untouched(host, monkeypatch):
    stack, docker = host["stack"], host["docker"]
    stack.update(force=True)                                   # release a*12 runs, checking still off
    require_signatures(host, monkeypatch)
    docker.registry["kite"] = "sha256:" + "c" * 64
    docker.cosign_ok = False
    ups = len(compose_ups(docker))
    result = stack.update(timer=True)
    assert result["last"]["result"] == "failed" and "chữ ký" in result["last"]["message"]
    assert stack.release().name == "a" * 12                    # the old release still runs
    assert docker.running.endswith("a" * 64) and len(compose_ups(docker)) == ups
    assert not (stack.releases / ("c" * 12)).exists()          # nothing of the refused one was prepared


def test_an_update_is_refused_when_cosign_is_missing(host, monkeypatch):
    stack, docker = host["stack"], host["docker"]
    require_signatures(host, monkeypatch, present=False)
    result = stack.update(force=True)
    assert result["last"]["result"] == "failed" and "cosign" in result["last"]["message"]
    assert stack.release() is None and not cosign_calls(docker)
    assert not any(c[:2] == ["docker", "create"] for c in docker.calls)


def test_signature_checking_is_off_until_the_machine_asks_for_it(host):
    stack, docker = host["stack"], host["docker"]
    assert stack.update(force=True)["last"]["result"] == "updated"
    assert not cosign_calls(docker)


def test_each_temporal_consumer_gets_its_own_generated_password_and_existing_ones_survive():
    made = ks.generated_secrets({})
    assert set(made) == set(ks.SECRET_KEYS) and len(set(made.values())) == len(made)
    assert all(len(value) >= 32 for value in made.values())
    kept = ks.generated_secrets({"TEMPORAL_POSTGRES_PASSWORD": "giu-nguyen", "KITE_WEB_DB_PASSWORD": "cung-the"})
    assert kept["TEMPORAL_POSTGRES_PASSWORD"] == "giu-nguyen" and kept["KITE_WEB_DB_PASSWORD"] == "cung-the"
    assert ks.generated_secrets(kept) == kept                  # a re-run changes nothing
