"""One-time confirmation codes: binding, expiry, single-use, hashed at rest."""

from __future__ import annotations

import json

import pytest

from poc.kaggle_worker import confirm


def test_issue_check_consume_round_trip(tmp_path):
    code = confirm.issue_code(tmp_path, "apply", "task-1", "hash-a", clock=lambda: 1000.0)
    confirm.check_code(tmp_path, "apply", "task-1", "hash-a", code, clock=lambda: 1001.0)
    confirm.consume_code(tmp_path, "apply", "task-1", "hash-a", code, clock=lambda: 1002.0)
    with pytest.raises(confirm.ConfirmError, match="đã được dùng"):
        confirm.check_code(tmp_path, "apply", "task-1", "hash-a", code, clock=lambda: 1003.0)


def test_codes_are_stored_hashed_only(tmp_path):
    code = confirm.issue_code(tmp_path, "commit", "task-1", "hash-a")
    stored = (tmp_path / "confirmations.json").read_text(encoding="utf-8")
    assert code not in stored
    record = json.loads(stored)
    assert len(record) == 1 and list(record.values())[0]["action"] == "commit"


def test_wrong_binding_is_refused(tmp_path):
    code = confirm.issue_code(tmp_path, "apply", "task-1", "hash-a")
    for action, task_id, patch_hash in (("commit", "task-1", "hash-a"),
                                        ("apply", "task-2", "hash-a"),
                                        ("apply", "task-1", "hash-b")):
        with pytest.raises(confirm.ConfirmError, match="không khớp"):
            confirm.check_code(tmp_path, action, task_id, patch_hash, code)


def test_expired_and_unknown_codes_are_refused(tmp_path):
    code = confirm.issue_code(tmp_path, "apply", "task-1", "hash-a", clock=lambda: 1000.0)
    with pytest.raises(confirm.ConfirmError, match="hết hạn"):
        confirm.check_code(tmp_path, "apply", "task-1", "hash-a", code,
                           clock=lambda: 1000.0 + confirm.CODE_TTL_SECONDS + 1)
    with pytest.raises(confirm.ConfirmError, match="không đúng"):
        confirm.check_code(tmp_path, "apply", "task-1", "hash-a", "ma-khong-co-that")
    with pytest.raises(confirm.ConfirmError, match="thiếu mã"):
        confirm.check_code(tmp_path, "apply", "task-1", "hash-a", "")


def test_patch_sha256_matches_file_bytes(tmp_path):
    patch = tmp_path / "result.patch"
    patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
    assert confirm.patch_sha256(patch.read_bytes()) == confirm.patch_sha256("diff --git a/x b/x\n")


def test_a_code_is_burnt_once_even_when_two_callers_use_it_at_the_same_time(tmp_path, monkeypatch):
    # Both callers check before either writes "used": the check and the burn share one lock.
    import threading

    code = confirm.issue_code(tmp_path, "apply", "task-1", "hash-a", clock=lambda: 1000.0)
    checked = threading.Barrier(2, timeout=0.5)
    real_check = confirm.check_code

    def slow_check(*args, **kwargs):
        real_check(*args, **kwargs)
        try:
            checked.wait()          # without the lock both callers get here before either burns it
        except threading.BrokenBarrierError:
            pass

    monkeypatch.setattr(confirm, "check_code", slow_check)
    results = []

    def use():
        try:
            confirm.consume_code(tmp_path, "apply", "task-1", "hash-a", code, clock=lambda: 1001.0)
            results.append("ok")
        except confirm.ConfirmError:
            results.append("refused")

    threads = [threading.Thread(target=use) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok", "refused"]
