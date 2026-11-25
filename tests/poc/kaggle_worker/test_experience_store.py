"""Experience documents: propose -> diff -> approve (versioned) / reject.

Worker (a)'s ``experience.py`` is not delivered yet, so these tests stub it in
``sys.modules`` with a renderer that follows the agreed interface exactly:
``render_document(repo, rules, *, version, updated, clock) -> str`` in the contract
format (title ``# Kite: tài liệu kinh nghiệm — <repo> (vN, ngày)`` + marker
``<!-- kite:experience version=N updated=YYYY-MM-DD -->`` + one ``- <rule>
[nguồn: ...]`` line per rule).
"""

from __future__ import annotations

import sys
import types

import pytest

from poc.kaggle_worker import experience_store as store

_SECTIONS = {"do": "Nên làm", "dont": "Không nên làm", "pitfall": "Mẫu lỗi hay lặp",
             "convention": "Quy ước repo"}


def _render(repo, rules, *, version, updated, clock):
    lines = [f"# Kite: tài liệu kinh nghiệm — {repo} (v{version}, {updated})",
             f"<!-- kite:experience version={version} updated={updated} -->", ""]
    grouped: dict[str, list[dict]] = {}
    for rule in rules:
        grouped.setdefault(_SECTIONS.get(rule.get("kind", "do"), "Nên làm"), []).append(rule)
    for title, items in grouped.items():
        lines.append(f"## {title}")
        for rule in items:
            sources = rule.get("sources") or []
            suffix = f" [nguồn: {sources[0]}]" if sources else ""
            lines.append(f"- {rule['text']}{suffix}")
        lines.append("")
    return "\n".join(lines)


@pytest.fixture(autouse=True)
def _stub_experience(monkeypatch):
    """Stand in for worker (a)'s module until it lands; the store imports it lazily."""
    module = types.ModuleType("poc.kaggle_worker.experience")
    module.render_document = _render
    module.distill = lambda lessons, notes, runs, *, clock=None: []
    monkeypatch.setitem(sys.modules, "poc.kaggle_worker.experience", module)
    yield
    # monkeypatch reverts sys.modules automatically


def test_propose_diff_approve_archives_old_version(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    clock = lambda: 1_800_000_000.0  # 2027-01-15
    first = store.propose(state, repo, [{"text": "Chạy test trước khi commit", "kind": "do",
                                         "sources": ["task t-1"]}], clock=clock)
    entry = first["entry"]
    assert entry["version"] == 1 and entry["status"] == "pending"
    assert entry["rules_added"] == 1 and entry["rules_removed"] == 0 and entry["rules_changed"] == 0
    assert first["diff"] != ""  # v1: diff against empty, the whole document shows as added lines
    assert "+- Chạy test trước khi commit" in first["diff"]
    assert store.experience_note(state, repo) == ""  # proposal only: still inert
    assert store.show(state, repo)["has_doc"] is False

    done = store.approve(state, entry["id"], clock=clock)
    assert done["version"] == 1 and done["status"] == "approved"
    assert store.list_pending(state) == []
    shown = store.show(state, repo)
    assert shown["has_doc"] and shown["version"] == 1 and shown["updated_at"] == "2027-01-15"
    assert "Chạy test trước khi commit" in shown["content"]
    assert "Chạy test trước khi commit" in store.experience_note(state, repo)

    # Second round: one rule edited (same source), one added, the old text gone.
    second = store.propose(state, repo, [
        {"text": "Chạy test + lint trước khi commit", "kind": "do", "sources": ["task t-1"]},
        {"text": "Không sửa file đã commit", "kind": "dont", "sources": ["task t-2"]}], clock=clock)
    assert second["entry"]["rules_added"] == 1 and second["entry"]["rules_changed"] == 1
    assert second["entry"]["rules_removed"] == 0
    diff = second["diff"]
    assert "-- Chạy test trước khi commit" in diff and "+- Chạy test + lint trước khi commit" in diff
    assert "+- Không sửa file đã commit" in diff
    store.approve(state, second["entry"]["id"], clock=clock)
    shown = store.show(state, repo)
    assert shown["version"] == 2
    assert "Chạy test + lint trước khi commit" in shown["content"]
    assert "Không sửa file đã commit" in shown["content"]
    archive = store.archive_path(state, repo, 1)
    assert archive.is_file() and "Chạy test trước khi commit" in archive.read_text(encoding="utf-8")
    assert "Không sửa file đã commit" not in archive.read_text(encoding="utf-8")


def test_counts_stable_for_refuted_rules_across_versions(tmp_path, monkeypatch):
    """Hồi quy: rule bị bác bỏ vẫn đếm đúng khi chưng cất lại.

    Renderer thật ghi rule bị bác bỏ dạng ``- <rule> [nguồn: ...] (cần xem lại)``
    — trích nguồn KHÔNG ở cuối dòng. Bộ đếm cũ neo trích nguồn vào cuối dòng nên
    mất nguồn của các rule này, đếm nhầm thành xóa hết + thêm lại hết ở v2.
    Dùng renderer thật (không dùng stub của file này) để bắt đúng lỗi đó.
    """
    import importlib

    sys.modules.pop("poc.kaggle_worker.experience", None)
    real = importlib.import_module("poc.kaggle_worker.experience")
    monkeypatch.setitem(sys.modules, "poc.kaggle_worker.experience", real)
    state, repo = tmp_path / "state", tmp_path / "repo"
    clock = lambda: 1_800_000_000.0  # 2027-01-15
    refuted = {"text": "Thieu test bien cho ham parse", "kind": "dont",
               "sources": [{"task_id": "task-001", "date": "2025-11-24", "verdict": "bad"}],
               "refuted": True}
    first = store.propose(state, repo, [refuted], clock=clock)
    assert first["entry"]["rules_added"] == 1 and first["entry"]["rules_removed"] == 0
    store.approve(state, first["entry"]["id"], clock=clock)
    assert "(cần xem lại)" in store.experience_note(state, repo)

    second = store.propose(state, repo, [refuted, {
        "text": "Ghi log ro rang khi task loi", "kind": "do",
        "sources": [{"task_id": "task-003", "date": "2025-11-26", "verdict": "good"}],
        "refuted": False}], clock=clock)
    entry = second["entry"]
    assert (entry["rules_added"], entry["rules_removed"], entry["rules_changed"]) == (1, 0, 0)


def test_reject_removes_proposal_and_entry(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    entry = store.propose(state, repo, [{"text": "Quy tắc bị từ chối", "kind": "do",
                                         "sources": ["task t-9"]}])["entry"]
    assert store.proposed_path(state, repo).is_file()
    done = store.reject(state, entry["id"])
    assert done["status"] == "rejected"
    assert not store.proposed_path(state, repo).exists()
    assert store.list_pending(state) == []
    assert store.show(state, repo)["has_doc"] is False
    with pytest.raises(ValueError, match="không có đề xuất"):
        store.reject(state, entry["id"])


def test_approve_twice_fails_without_bumping_version(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    entry = store.propose(state, repo, [{"text": "Một quy tắc", "kind": "do",
                                         "sources": ["task t-1"]}])["entry"]
    store.approve(state, entry["id"])
    with pytest.raises(ValueError, match="đã approved"):
        store.approve(state, entry["id"])
    assert store.show(state, repo)["version"] == 1
    assert not store.archive_path(state, repo, 1).exists()  # nothing archived twice


def test_approve_unknown_id_fails_clearly(tmp_path):
    with pytest.raises(ValueError, match="không có đề xuất"):
        store.approve(tmp_path / "state", "khongco")


def test_second_proposal_while_pending_is_refused(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    store.propose(state, repo, [{"text": "Quy tắc một", "kind": "do", "sources": ["task t-1"]}])
    with pytest.raises(ValueError, match="đang chờ duyệt"):
        store.propose(state, repo, [{"text": "Quy tắc hai", "kind": "do", "sources": ["task t-2"]}])


def test_experience_note_empty_without_doc_and_block_with_doc(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    assert store.experience_note(state, repo) == ""
    entry = store.propose(state, repo, [{"text": "Ghi chú vào journal", "kind": "convention",
                                         "sources": ["bài học 2026-11-25"]}])["entry"]
    store.approve(state, entry["id"])
    note = store.experience_note(state, repo)
    assert note.startswith("\n\nTài liệu kinh nghiệm đã duyệt")
    assert "Ghi chú vào journal" in note


def test_show_reports_pending_proposal(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    entry = store.propose(state, repo, [{"text": "Quy tắc chờ", "kind": "do",
                                         "sources": ["task t-1"]}])["entry"]
    shown = store.show(state, repo)
    assert shown["pending"]["id"] == entry["id"]
    assert shown["version"] == 0 and shown["updated_at"] is None and shown["content"] == ""
