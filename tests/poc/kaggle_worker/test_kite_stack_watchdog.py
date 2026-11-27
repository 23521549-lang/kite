"""The watchdog: a service confirmed unhealthy on two ticks in a row is restarted, at most 3
restarts per service per hour, with 15 minutes between restarts of the same service. Over the
limit it is not restarted again — the owner is told once. Docker is a fake whose container
states the test controls."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("kite_stack", ROOT / "deploy" / "stack" / "kite_stack.py")
ks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ks)


def healthy_states() -> dict[str, dict]:
    return {s: {"state": "running", "health": "healthy" if s in ks.KITE_SERVICES else "", "exit": 0}
            for s in ks.WATCHDOG_SERVICES}


class FakeDocker:
    def __init__(self, states=None):
        self.states: dict[str, dict] = states if states is not None else healthy_states()
        self.calls: list[list[str]] = []
        self.fail_restart = False        # `compose restart` itself fails
        self.stay_unhealthy: set[str] = set()   # restart runs, the service stays bad

    def done(self, out="", code=0, err=""):
        return subprocess.CompletedProcess([], code, out, err)

    def __call__(self, cmd, timeout=None, capture=True, tty=False, env=None):
        self.calls.append(cmd)
        if cmd[0] == "systemctl":
            return self.done()
        if cmd[:2] == ["docker", "compose"]:
            return self.compose(cmd)
        return self.done()

    def compose(self, cmd):
        args = cmd[[i for i, p in enumerate(cmd) if p.endswith("compose.yaml")][-1] + 1:]
        if args[:1] == ["ps"]:
            rows = [{"Service": name, "State": st["state"], "Health": st["health"], "ExitCode": st["exit"]}
                    for name, st in self.states.items()]
            return self.done("\n".join(json.dumps(r) for r in rows))
        if args[:1] == ["restart"]:
            if self.fail_restart:
                return self.done(code=1, err="cannot restart")
            for name in args[1:]:
                if name not in self.stay_unhealthy:
                    self.states[name] = {"state": "running",
                                         "health": "healthy" if name in ks.KITE_SERVICES else "", "exit": 0}
            return self.done()
        if args[:1] == ["logs"]:
            return self.done("boom")
        return self.done()


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"{}"


class FakeOpener:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return FakeResponse()


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

    stack = ks.Stack(tmp_path / "data", run=docker, clock=lambda: clock["now"], sleep=sleep,
                     out=lambda _m: None, health_timeout=120)
    stack.dir.mkdir(parents=True)
    ks.write_env(stack.env_file, {"KITE_IMAGE_REPO": "ghcr.io/han/kite", "KITE_SANDBOX_REPO": "ghcr.io/han/kite-sandbox",
                                  "KITE_CHANNEL": "stable", "KITE_AUTO_UPDATE": "1"}, "test")
    # A release the stack points at, so compose() can build its commands (no update() run here).
    (stack.releases / "r1").mkdir(parents=True)
    stack.current_link.symlink_to(stack.releases / "r1", target_is_directory=True)
    return {"stack": stack, "docker": docker, "clock": clock, "tmp": tmp_path}


def restarts_of(docker):
    return [c for c in docker.calls if c[:2] == ["docker", "compose"] and "restart" in c]


# ------------------------------------------------------------------ classify
@pytest.mark.parametrize("state,expected", [
    ({"state": "running", "health": "healthy"}, "good"),
    ({"state": "running", "health": ""}, "good"),
    ({"state": "running", "health": "unhealthy"}, "bad"),
    ({"state": "running", "health": "starting"}, "bad"),
    ({"state": "exited", "health": "", "exit": 1}, "bad"),
    ({"state": "dead", "health": "", "exit": 0}, "bad"),
    ({"state": "restarting", "health": "", "exit": 0}, "bad"),
    ({"state": "created", "health": "", "exit": 0}, "unknown"),
    ({"state": "paused", "health": "", "exit": 0}, "unknown"),
    ({}, "unknown"),
])
def test_watchdog_classify(state, expected):
    assert ks.watchdog_classify(state) == expected


# ------------------------------------------------------------------ decide (pure)
def test_decide_healthy_does_nothing_and_resets():
    states = healthy_states()
    history = {"services": {"temporal": {"bad_ticks": 2, "last_restart": 900.0, "restarts": [900.0],
                                         "alerted": True}}}
    actions, new = ks.watchdog_decide(states, history, 1000.0)
    assert actions == []
    assert new["services"]["temporal"]["bad_ticks"] == 0
    assert new["services"]["temporal"]["alerted"] is False


def test_decide_first_bad_tick_only_watches():
    states = healthy_states()
    states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    actions, new = ks.watchdog_decide(states, {}, 1000.0)
    assert actions == [{"service": "temporal", "action": "watch",
                        "reason": "xấu 1/2 lần kiểm liên tiếp"}]
    assert new["services"]["temporal"]["bad_ticks"] == 1


def test_decide_second_bad_tick_restarts():
    states = healthy_states()
    states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    history = {"services": {"temporal": {"bad_ticks": 1, "last_restart": None, "restarts": [],
                                         "alerted": False}}}
    actions, new = ks.watchdog_decide(states, history, 1000.0)
    assert [a["action"] for a in actions] == ["restart"]
    assert new["services"]["temporal"]["bad_ticks"] == 2


def test_decide_cooldown_suppresses():
    states = healthy_states()
    states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    history = {"services": {"temporal": {"bad_ticks": 1, "last_restart": 700.0, "restarts": [700.0],
                                         "alerted": False}}}
    actions, _new = ks.watchdog_decide(states, history, 1000.0)
    assert [a["action"] for a in actions] == ["suppressed"]
    assert "phút" in actions[0]["reason"]


def test_decide_rate_limit_suppresses():
    states = healthy_states()
    states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    history = {"services": {"temporal": {"bad_ticks": 1, "last_restart": 100.0,
                                         "restarts": [100.0, 400.0, 700.0], "alerted": False}}}
    actions, _new = ks.watchdog_decide(states, history, 1000.0)
    assert [a["action"] for a in actions] == ["suppressed"]
    assert "3 lần" in actions[0]["reason"]


def test_decide_old_restarts_do_not_count():
    states = healthy_states()
    states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    history = {"services": {"temporal": {"bad_ticks": 1, "last_restart": 100.0,
                                         "restarts": [100.0, 200.0, 300.0], "alerted": False}}}
    actions, new = ks.watchdog_decide(states, history, 1000.0 + 3600.0)
    assert [a["action"] for a in actions] == ["restart"]
    assert new["services"]["temporal"]["restarts"] == []


def test_decide_ignores_one_shots_and_unknown_states():
    states = healthy_states()
    states["temporal-setup"] = {"state": "exited", "health": "", "exit": 1}
    states["caddy"] = {"state": "created", "health": "", "exit": 0}
    actions, _new = ks.watchdog_decide(states, {}, 1000.0)
    assert actions == []


def test_decide_unknown_tick_keeps_the_bad_counter():
    states = healthy_states()
    states["temporal"] = {"state": "created", "health": "", "exit": 0}
    history = {"services": {"temporal": {"bad_ticks": 1, "last_restart": None, "restarts": [],
                                         "alerted": False}}}
    actions, new = ks.watchdog_decide(states, history, 1000.0)
    assert actions == []
    assert new["services"]["temporal"]["bad_ticks"] == 1


# ------------------------------------------------------------------ restart() and the stack
def test_restart_accepts_infra_services_only_when_asked(host):
    stack = host["stack"]
    with pytest.raises(ks.StackError):
        stack.restart(["temporal"])
    with pytest.raises(ks.StackError):
        stack.restart(["nope"])
    report = stack.restart(["temporal"], allow_infra=True)
    assert report["last"]["result"] == "restarted"
    assert restarts_of(host["docker"])[-1][-1] == "temporal"


def test_watchdog_restarts_after_two_ticks_and_records_history(host):
    stack, docker, clock = host["stack"], host["docker"], host["clock"]
    docker.states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    alerts: list[tuple] = []

    def notify(title, message, priority=3):
        alerts.append((title, priority))
        return True

    first = stack.watchdog(notify=notify)
    assert first["watching"] == ["temporal"] and first["restarted"] == []
    assert restarts_of(docker) == []
    clock["now"] += 300.0
    second = stack.watchdog(notify=notify)
    assert second["restarted"] == ["temporal"]
    assert len(restarts_of(docker)) == 1
    saved = json.loads((stack.dir / "watchdog.json").read_text(encoding="utf-8"))
    entry = saved["services"]["temporal"]
    assert entry["restarts"] == [1300.0] and entry["last_restart"] == 1300.0
    assert entry["bad_ticks"] == 0 and entry["last_result"] == "restarted"
    assert [title for title, _p in alerts] == ["Kite: đã khởi động lại temporal"]


def test_watchdog_skips_a_tick_while_an_update_or_restart_holds_the_lock(host):
    stack, docker = host["stack"], host["docker"]
    docker.states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    with stack.lock():
        report = stack.watchdog(notify=lambda *a, **k: True)
    assert report["skipped"] and report["watching"] == [] and restarts_of(docker) == []
    assert not (stack.dir / "watchdog.json").exists()      # no bad tick counted
    assert stack.watchdog(notify=lambda *a, **k: True)["watching"] == ["temporal"]


def test_watchdog_check_only_changes_nothing(host):
    stack, docker = host["stack"], host["docker"]
    docker.states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    stack.dir.joinpath("watchdog.json").write_text(json.dumps(
        {"services": {"temporal": {"bad_ticks": 1, "last_restart": None, "restarts": [],
                                   "alerted": False}}}), encoding="utf-8")
    report = stack.watchdog(check_only=True, notify=lambda *a, **k: True)
    assert report["would_restart"] == ["temporal"] and report["restarted"] == []
    assert restarts_of(docker) == []
    saved = json.loads((stack.dir / "watchdog.json").read_text(encoding="utf-8"))
    assert saved["services"]["temporal"]["bad_ticks"] == 1


def test_watchdog_suppressed_alerts_once_per_episode(host):
    stack, docker, clock = host["stack"], host["docker"], host["clock"]
    docker.states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    stack.dir.joinpath("watchdog.json").write_text(json.dumps(
        {"services": {"temporal": {"bad_ticks": 1, "last_restart": 100.0,
                                   "restarts": [100.0, 400.0, 700.0], "alerted": False}}}),
        encoding="utf-8")
    alerts: list[tuple] = []

    def notify(title, message, priority=3):
        alerts.append((title, priority))
        return True

    first = stack.watchdog(notify=notify)
    assert first["suppressed"] == ["temporal"] and restarts_of(docker) == []
    assert len(alerts) == 1 and alerts[0][1] == 4
    clock["now"] += 300.0
    stack.watchdog(notify=notify)
    assert len(alerts) == 1  # same episode: no second push
    docker.states["temporal"] = {"state": "running", "health": "healthy", "exit": 0}
    stack.watchdog(notify=notify)
    saved = json.loads((stack.dir / "watchdog.json").read_text(encoding="utf-8"))
    assert saved["services"]["temporal"]["alerted"] is False  # a good tick clears the flag


def test_watchdog_cooldown_suppresses(host):
    stack, docker = host["stack"], host["docker"]
    docker.states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    stack.dir.joinpath("watchdog.json").write_text(json.dumps(
        {"services": {"temporal": {"bad_ticks": 1, "last_restart": 700.0, "restarts": [700.0],
                                   "alerted": False}}}), encoding="utf-8")
    report = stack.watchdog(notify=lambda *a, **k: True)
    assert report["suppressed"] == ["temporal"] and restarts_of(docker) == []


def test_watchdog_failed_restart_retries_next_tick(host):
    stack, docker, clock = host["stack"], host["docker"], host["clock"]
    docker.states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    docker.fail_restart = True
    stack.dir.joinpath("watchdog.json").write_text(json.dumps(
        {"services": {"temporal": {"bad_ticks": 1, "last_restart": None, "restarts": [],
                                   "alerted": False}}}), encoding="utf-8")
    first = stack.watchdog(notify=lambda *a, **k: True)
    assert first["failed"] == ["temporal"]
    saved = json.loads((stack.dir / "watchdog.json").read_text(encoding="utf-8"))
    assert saved["services"]["temporal"]["restarts"] == []  # nothing really restarted
    assert saved["services"]["temporal"]["bad_ticks"] == 2   # still confirmed bad: retried
    clock["now"] += 300.0
    stack.watchdog(notify=lambda *a, **k: True)
    assert len(restarts_of(docker)) == 2


def test_watchdog_restarted_but_still_unhealthy_counts_and_alerts_louder(host):
    stack, docker = host["stack"], host["docker"]
    docker.states["temporal"] = {"state": "running", "health": "unhealthy", "exit": 0}
    docker.stay_unhealthy.add("temporal")
    stack.dir.joinpath("watchdog.json").write_text(json.dumps(
        {"services": {"temporal": {"bad_ticks": 1, "last_restart": None, "restarts": [],
                                   "alerted": False}}}), encoding="utf-8")
    alerts: list[tuple] = []

    def notify(title, message, priority=3):
        alerts.append((title, priority))
        return True

    report = stack.watchdog(notify=notify)
    assert report["failed"] == ["temporal"] and report["restarted"] == []
    saved = json.loads((stack.dir / "watchdog.json").read_text(encoding="utf-8"))
    assert saved["services"]["temporal"]["restarts"] == [1000.0]  # it did restart: counts
    assert saved["services"]["temporal"]["last_restart"] == 1000.0
    assert alerts == [("Kite: temporal khởi động lại rồi vẫn chưa khỏe", 4)]


def test_watchdog_empty_states_alerts_once(host):
    stack, docker, clock = host["stack"], host["docker"], host["clock"]
    docker.states = {}
    alerts: list[tuple] = []

    def notify(title, message, priority=3):
        alerts.append((title, priority))
        return True

    first = stack.watchdog(notify=notify)
    assert first["error"] == "không đọc được trạng thái container"
    assert len(alerts) == 1
    clock["now"] += 300.0
    stack.watchdog(notify=notify)
    assert len(alerts) == 1  # same episode: no second push
    docker.states = healthy_states()
    stack.watchdog(notify=notify)
    saved = json.loads((stack.dir / "watchdog.json").read_text(encoding="utf-8"))
    assert saved.get("alerted_no_states") is False


# ------------------------------------------------------------------ ntfy alert
def test_watchdog_notify_reads_the_configured_topic(host, tmp_path):
    stack = host["stack"]
    secret = tmp_path / "ntfy_url"
    secret.write_text("https://ntfy.sh/kite-abc123", encoding="utf-8")
    (stack.data / "kite").mkdir(parents=True)
    (stack.data / "kite" / "config.yaml").write_text(
        "notify:\n  ntfy: file:" + str(secret) + "\n", encoding="utf-8")
    opener = FakeOpener()
    assert stack.watchdog_notify("T", "M", opener=opener) is True
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == "https://ntfy.sh/"
    body = json.loads(request.data.decode("utf-8"))
    assert body["topic"] == "kite-abc123" and body["title"] == "T" and body["tags"] == ["watchdog"]


def test_watchdog_notify_off_without_a_topic(host):
    stack = host["stack"]
    opener = FakeOpener()
    assert stack.watchdog_notify("T", "M", opener=opener) is False
    assert opener.requests == []
    (stack.data / "kite").mkdir(parents=True)
    (stack.data / "kite" / "config.yaml").write_text(
        "notify:\n  ntfy: env:NTFY_URL\n", encoding="utf-8")
    assert stack.watchdog_notify("T", "M", opener=opener) is False
    assert opener.requests == []


# ------------------------------------------------------------------ CLI wiring
def test_cli_watchdog_check_only(host, monkeypatch, capsys):
    monkeypatch.setattr(ks, "Stack", lambda *args, **kwargs: host["stack"])
    assert ks.main(["watchdog", "--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["would_restart"] == [] and out["error"] == ""
