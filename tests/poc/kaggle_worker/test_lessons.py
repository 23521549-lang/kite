"""Lessons from Claude Code and Muse Code: tool economy, patch size, cost per task, repo memory, notifications."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from poc.kaggle_worker import cli, memory, notify
from poc.kaggle_worker import sandbox as sbx
from poc.kaggle_worker.journal import Journal, task_usage

OPENCODE_RUN = """\x1b[0m> build · qwen3.6
\x1b[0m→ \x1b[0mRead calc.py
> build · qwen3.6
→ Read calc.py
> build · qwen3.6
→ Read calc.py [offset=10, limit=20]
> build · qwen3.6
✱ Grep "average" (3 matches)
> build · qwen3.6
← Edit calc.py
> build · qwen3.6
$ python -m pytest -q
2 passed
> build · qwen3.6
→ Read calc.py
Xong: sửa average.
"""


def test_tool_stats_count_turns_calls_and_rereads_of_the_same_thing():
    stats = sbx.tool_stats(OPENCODE_RUN)
    assert (stats["agent_steps"], stats["tool_calls"], stats["repeat_reads"]) == (7, 7, 2)
    assert stats["most_reread"] == ["calc.py"]          # reading another range of a file is not a re-read
    assert sbx.patch_size("--- a/x\n+++ b/x\n@@\n-a\n+b\n+c\n ctx\n") == (2, 1)


def test_prompts_ask_for_the_smallest_diff_and_reading_each_file_once():
    prompt = sbx.agent_prompt("Sửa average", "pytest", context="calc.py: ...")
    assert "diff nhỏ nhất" in prompt and "mỗi file chỉ đọc một lần" in prompt
    assert "diff càng nhỏ càng tốt" in sbx.fast_prompt("Sửa average", "calc.py", "pytest")


def test_task_usage_sums_the_task_and_its_helpers_only(tmp_path):
    journal = Journal(tmp_path)
    for tag, run_s, tokens in (("t1", 60.0, 100), ("t1-b2", 30.0, 50), ("t10", 500.0, 9), ("t1x", 1.0, 1)):
        journal.write("gateway", {"tag": tag, "run_s": run_s, "prompt_tokens": tokens, "completion_tokens": 1,
                                  "cached_tokens": 10})
    assert task_usage(journal, "t1") == {"model_requests": 2, "gpu_seconds": 90.0, "prompt_tokens": 150,
                                         "completion_tokens": 2, "cached_tokens": 20}


def test_repo_memory_keeps_the_newest_lessons_per_repository(tmp_path):
    state, repo, other = tmp_path / "state", tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    assert memory.lessons_note(state, repo) == ""
    for i in range(12):
        memory.remember(state, repo, f"t{i}", f"bài học {i}")
    found = memory.lessons(state, repo)
    assert len(found) == 10 and found[0].endswith("(t2): bài học 2") and found[-1].endswith("(t11): bài học 11")
    assert memory.lessons(state, other) == []                                   # per repository
    assert memory.memory_path(state, repo).read_text(encoding="utf-8").startswith("# Kite: lưu ý cho repo")
    # A bad note with a reason on a finished task becomes a lesson for that task's repository.
    Journal(state / "journal").write("tasks", {"task_id": "t-new", "repo": str(other)})
    assert memory.remember_note(state, "t-new", "good", "tốt") is None
    assert memory.remember_note(state, "t-new", "bad", "") is None
    assert memory.remember_note(state, "unknown", "bad", "sai") is None
    path = memory.remember_note(state, "t-new", "bad", "Quên cập nhật README")
    assert path and memory.lessons(state, other)[0].endswith("(t-new): Quên cập nhật README")


def test_notifications_carry_no_code_and_never_break_a_task():
    assert notify.split_url("https://ntfy.sh/kite-abc") == ("https://ntfy.sh", "kite-abc")
    with pytest.raises(ValueError):
        notify.split_url("ntfy.sh/kite")
    topic = notify.new_topic()
    assert topic.startswith("kite-") and len(topic) >= 20 and topic == topic.lower()

    requests = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def read(self): return b"{}"

    def opener(request, timeout):
        requests.append((request.full_url, json.loads(request.data)))
        return Response()

    notify.send("https://ntfy.sh/kite-abc", "Kite: task xong", "t1\nchi tiết", tags=("bell",), opener=opener)
    url, body = requests[0]
    assert url == "https://ntfy.sh/" and body["topic"] == "kite-abc" and body["title"] == "Kite: task xong"

    title, message, tags, _ = notify.task_message({"task_id": "t1", "verified": False, "changed_files": ["a.py"],
                                                    "wall_seconds": 600, "model": "qwen3.6", "goal": "BÍ MẬT"})
    assert title == "Kite: task xong nhưng chưa qua test" and "10.0 phút" in message and "BÍ MẬT" not in message
    assert notify.task_message({"task_id": "t2", "status": "error", "error": "OOM"})[0] == "Kite: task dừng vì lỗi"

    def broken(*args, **kwargs):
        raise OSError("no network")

    notifier = notify.Notifier("https://ntfy.sh/kite-abc", sender=broken, background=False)
    notifier("t", "m")                                         # swallowed, one warning
    notifier("t", "m")
    assert not notify.Notifier(None)


def _config(tmp_path: Path) -> Path:
    raw = {"schema_version": 1, "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "q"},
           "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
           "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
           "providers": [{"id": "kaggle-han", "adapter": "pull", "enabled": True, "owner": "han",
                          "model": "qwen3.6", "capabilities": ["coding"],
                          "quota": {"gpu_seconds": 108000, "concurrency": 1},
                          "secrets": {"worker_token": f"file:{tmp_path / 'token'}"}}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return path


def test_notify_setup_keeps_the_topic_secret_and_test_sends_through_it(tmp_path, capsys, monkeypatch):
    from poc.kaggle_worker.config import load_config
    from poc.kaggle_worker.mcp_server import chat_files

    config_path = _config(tmp_path)
    assert cli.main(["--config", str(config_path), "notify", "setup"]) == 0
    report = json.loads(capsys.readouterr().out)
    secret = tmp_path / "secrets" / "ntfy_url"
    assert oct(secret.stat().st_mode & 0o777) == "0o600" and report["topic"] in secret.read_text()
    assert report["topic"] not in config_path.read_text()                    # the config only points to the file
    config = load_config(config_path)
    assert config.notify.ntfy == f"file:{secret}"
    assert "## Thông báo khi task xong" in chat_files(config, lead_model="qwen3.6", kite_command=["kite"],
                                                      allowed_roots=[tmp_path])["AGENTS.md"]
    assert cli._notifier(config).url == secret.read_text().strip()
    sent = []
    monkeypatch.setattr(notify, "send", lambda url, title, message, **kw: sent.append((url, title)))
    assert cli.main(["--config", str(config_path), "notify", "test"]) == 0
    assert sent and sent[0][0].endswith(report["topic"])
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "notify", "setup", "--off"]) == 0
    assert load_config(config_path).notify.ntfy is None and not cli._notifier(load_config(config_path))


def test_a_bad_note_from_the_cli_or_the_chat_becomes_a_lesson(tmp_path, capsys):
    from test_mcp_server import FakeClient, make_ops

    config_path = _config(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    Journal(tmp_path / "journal").write("tasks", {"task_id": "t-cli", "repo": str(repo)})
    assert cli.main(["--config", str(config_path), "journal", "note", "t-cli", "--bad", "Thiếu test biên"]) == 0
    assert json.loads(capsys.readouterr().out)["remembered_in"].endswith(".md")
    assert cli.main(["--config", str(config_path), "memory", "show", str(repo)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["lessons"][0].endswith("(t-cli): Thiếu test biên") and shown["used_in_tasks"] == shown["lessons"]

    ops = make_ops(tmp_path, FakeClient())
    Journal(tmp_path / "state" / "journal").write("tasks", {"task_id": "t-chat", "repo": str(repo)})
    saved = asyncio.run(ops.note_task("t-chat", "bad", "Đổi tên hàm public"))
    assert saved["remembered_for_repo"] is True
    assert memory.lessons(tmp_path / "state", repo)[-1].endswith("(t-chat): Đổi tên hàm public")
    assert asyncio.run(ops.note_task("t-chat", "good", "ổn"))["remembered_for_repo"] is False
