"""Vòng tự cải tiến (Gói 10, worker (c)): reader điểm trục, đề xuất, hàng chờ."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from poc.kaggle_worker import improve


def _run(state: Path, name: str, payload: dict | str, *, started: str = "2026-11-25 10:00") -> None:
    folder = state / "bench" / name
    folder.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    (folder / "results.json").write_text(text, encoding="utf-8")


def _run_payload(*, version="v1", axes=None, started="2026-11-25 10:00", **extra):
    payload = {"started": started, "criteria_version": version, "models": {}}
    if axes is not None:
        payload["axes"] = axes
    payload.update(extra)
    return payload


# ------------------------------------------------------------------ mảnh 1: axis_scores


def test_no_bench_runs_returns_empty_scores(tmp_path):
    assert improve.axis_scores(tmp_path / "state") == {}


def test_axis_scores_averages_runs_and_rounds_one_decimal(tmp_path):
    state = tmp_path / "state"
    _run(state, "run1", _run_payload(axes={"Coding": 80, "Phân tích": 71.24}, started="2026-11-25 10:00"))
    _run(state, "run2", _run_payload(axes={"coding": 82, "Phân tích": 72.76}, started="2026-11-25 11:00"))
    assert improve.axis_scores(state) == {"coding": 81.0, "phan_tich": 72.0}


def test_axis_scores_normalizes_vietnamese_and_english_keys(tmp_path):
    state = tmp_path / "state"
    _run(state, "run1", _run_payload(axes={
        "Coding": 90, "analysis": 80, "reasoning": 70, "speed": 60,
        "relevance": 50, "cost": 40, "stability": 30, "memory": 20,
    }))
    assert improve.axis_scores(state) == {
        "coding": 90.0, "phan_tich": 80.0, "suy_luan": 70.0, "toc_do": 60.0,
        "dung_viec": 50.0, "chi_phi": 40.0, "on_dinh": 30.0, "tri_nho": 20.0,
    }
    _run(state, "run2", _run_payload(
        version="v2",
        axes={"Tốc độ": 61, "Đúng việc": 51, "Chi phí": 41, "Ổn định": 31, "Trí nhớ": 21,
              "Suy luận": 71},
        started="2026-11-25 11:00"))
    assert improve.axis_scores(state, criteria_version="v2") == {
        "toc_do": 61.0, "dung_viec": 51.0, "chi_phi": 41.0,
        "on_dinh": 31.0, "tri_nho": 21.0, "suy_luan": 71.0}


def test_axis_scores_only_selected_version(tmp_path):
    state = tmp_path / "state"
    _run(state, "run1", _run_payload(version="v1", axes={"coding": 10}, started="2026-11-25 10:00"))
    _run(state, "run2", _run_payload(version="v2", axes={"coding": 90}, started="2026-11-25 11:00"))
    assert improve.axis_scores(state) == {"coding": 90.0}  # mặc định: version mới nhất
    assert improve.axis_scores(state, criteria_version="v1") == {"coding": 10.0}
    assert improve.axis_scores(state, criteria_version="v9") == {}


def test_axis_scores_skips_broken_results_files(tmp_path):
    state = tmp_path / "state"
    _run(state, "broken", "{không phải json", started="2026-11-25 09:00")
    _run(state, "notdict", "[1, 2]", started="2026-11-25 09:30")
    _run(state, "good", _run_payload(axes={"coding": 77}, started="2026-11-25 10:00"))
    assert improve.axis_scores(state) == {"coding": 77.0}


def test_axis_scores_reads_axis_map_inside_models(tmp_path):
    state = tmp_path / "state"
    _run(state, "run1", _run_payload(started="2026-11-25 10:00",
                                     models={"qwen": {"axis_scores": {"coding": 66}}}))
    assert improve.axis_scores(state) == {"coding": 66.0}


# ------------------------------------------------------------------ mảnh 5: CLI


def _temp_parser():
    parser = argparse.ArgumentParser(prog="kite")
    parser.add_argument("--config", type=Path, default=Path(".kite/config.yaml"))
    commands = parser.add_subparsers(dest="command", required=True)
    improve.register_improve_commands(commands)
    return parser


def test_register_improve_commands_parses_list_approve_reject():
    parser = _temp_parser()
    args = parser.parse_args(["improve", "list"])
    assert (args.command, args.improve_command) == ("improve", "list")
    args = parser.parse_args(["improve", "approve", "abc123"])
    assert (args.improve_command, args.proposal_id) == ("approve", "abc123")
    args = parser.parse_args(["improve", "reject", "abc123"])
    assert (args.improve_command, args.proposal_id) == ("reject", "abc123")
    with pytest.raises(SystemExit):
        parser.parse_args(["improve"])


class _Args:
    def __init__(self, state, command, proposal_id=None):
        from types import SimpleNamespace

        self.config = SimpleNamespace(parent=state)
        self.improve_command = command
        self.proposal_id = proposal_id


def test_improve_command_list_and_reject(tmp_path, capsys):
    state = tmp_path / "state"
    saved = improve.save_proposals(state, _sample_proposals())
    assert improve._improve_command(_Args(state, "list")) == 0
    out = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in out["proposals"]] == [saved[0]["id"]]
    assert improve._improve_command(_Args(state, "reject", saved[0]["id"])) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "rejected"


def test_improve_command_approve_uses_default_queue_path(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    saved = improve.save_proposals(state, _sample_proposals())
    monkeypatch.setattr(
        improve, "_enqueue_task",
        lambda state_dir, repo, goal, clock=None: {"task_id": "chat-cli-1"})
    assert improve._improve_command(_Args(state, "approve", saved[0]["id"])) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "task_created" and out["task_id"] == "chat-cli-1"


def test_improve_command_unknown_id_raises_config_error(tmp_path):
    from poc.kaggle_worker.config import ConfigError

    with pytest.raises(ConfigError):
        improve._improve_command(_Args(tmp_path / "state", "reject", "không-có"))


# ------------------------------------------------------------------ mảnh 4: hàng chờ


def _sample_proposals():
    return [{"axis": "toc_do", "score": 40.0, "criteria_version": "v1",
             "proposal": "Trục Tốc độ đang yếu (40.0/100, criteria v1). Hướng cải tiến: đo lại."}]


def test_queue_save_list_reject(tmp_path):
    state = tmp_path / "state"
    assert improve.list_proposals(state) == []
    saved = improve.save_proposals(state, _sample_proposals(), clock=lambda: 1_700_000_000.0)
    assert len(saved) == 1 and saved[0]["status"] == "pending"
    assert saved[0]["created_at"] == 1_700_000_000.0 and saved[0]["task_id"] is None
    assert saved[0]["repo"] == str(improve.REPO_ROOT)  # mặc định: repo Kite hiện tại
    listed = improve.list_proposals(state)
    assert [entry["id"] for entry in listed] == [saved[0]["id"]]
    # Lưu thêm không ghi đè entry cũ.
    saved2 = improve.save_proposals(state, _sample_proposals(), clock=lambda: 1_700_000_001.0)
    assert [entry["id"] for entry in improve.list_proposals(state)] == [
        saved[0]["id"], saved2[0]["id"]]
    # Reject: entry rời hàng chờ duyệt (status "rejected", bản ghi vẫn còn).
    rejected = improve.reject_proposal(state, saved[0]["id"])
    assert rejected["status"] == "rejected"
    pending = [e for e in improve.list_proposals(state) if e["status"] == "pending"]
    assert [entry["id"] for entry in pending] == [saved2[0]["id"]]
    with pytest.raises(ValueError):
        improve.reject_proposal(state, "không-có")


def test_approve_creates_task_through_injected_create_task(tmp_path):
    state = tmp_path / "state"
    saved = improve.save_proposals(
        state, [{**_sample_proposals()[0], "repo": str(tmp_path / "repo")}])
    calls: list[tuple[str, str]] = []

    def fake_create_task(repo, goal):
        calls.append((repo, goal))
        return {"task_id": "chat-test-1234"}

    approved = improve.approve_proposal(state, saved[0]["id"], ops={"create_task": fake_create_task})
    assert approved["status"] == "task_created" and approved["task_id"] == "chat-test-1234"
    assert len(calls) == 1 and calls[0][0] == str(tmp_path / "repo")
    assert "Tốc độ" in calls[0][1] and "40.0" in calls[0][1] and "v1" in calls[0][1]
    assert improve.list_proposals(state)[0]["status"] == "task_created"
    with pytest.raises(ValueError):  # duyệt lần hai không được
        improve.approve_proposal(state, saved[0]["id"], ops={"create_task": fake_create_task})
    assert len(calls) == 1
    with pytest.raises(ValueError):
        improve.approve_proposal(state, "không-có", ops={"create_task": fake_create_task})


def test_approve_failure_leaves_entry_pending(tmp_path):
    state = tmp_path / "state"
    saved = improve.save_proposals(state, _sample_proposals())

    def failing_create_task(repo, goal):
        raise RuntimeError("gateway chưa chạy")

    with pytest.raises(RuntimeError):
        improve.approve_proposal(state, saved[0]["id"], ops=failing_create_task)
    assert improve.list_proposals(state)[0]["status"] == "pending"
    assert improve.list_proposals(state)[0]["task_id"] is None


def test_default_create_task_enqueues_like_kite_task_add(tmp_path):
    """Đường mặc định (không ops): xếp task thật vào scheduler.sqlite3."""
    import subprocess

    from poc.kaggle_worker.store import SQLiteSchedulerStore

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    state = tmp_path / "state"
    saved = improve.save_proposals(
        state, [{**_sample_proposals()[0], "repo": str(repo)}])
    approved = improve.approve_proposal(state, saved[0]["id"], clock=lambda: 1_700_000_000.0)
    assert approved["status"] == "task_created"
    with SQLiteSchedulerStore(state / "scheduler.sqlite3") as store:
        row = store.get_task(approved["task_id"])
    assert json.loads(row["payload_json"])["goal"].startswith("Trục Tốc độ đang yếu")
    assert row["repo_head"] == head and row["task_type"] == "coding"
    assert row["branch"] == f"agent/{approved['task_id']}"


def test_build_proposals_empty_without_axis_scores(tmp_path):
    assert improve.build_proposals(tmp_path / "state", tmp_path / "repo") == []


def test_build_proposals_names_weakest_axis_and_generic_direction(tmp_path):
    state = tmp_path / "state"
    _run(state, "run1", _run_payload(axes={"coding": 90, "Tốc độ": 40, "Chi phí": 70}))
    proposals = improve.build_proposals(state, tmp_path / "repo")
    assert [(p["axis"], p["score"], p["criteria_version"]) for p in proposals] == [
        ("toc_do", 40.0, "v1"), ("chi_phi", 70.0, "v1"), ("coding", 90.0, "v1")]
    assert "Tốc độ" in proposals[0]["proposal"] and "40.0" in proposals[0]["proposal"]
    assert "v1" in proposals[0]["proposal"]


def test_build_proposals_quotes_matching_experience_rule(tmp_path):
    from poc.kaggle_worker.memory import repo_key

    state = tmp_path / "state"
    repo = tmp_path / "repo"
    _run(state, "run1", _run_payload(axes={"coding": 90, "Tốc độ": 40}))
    doc = state / "memory" / f"{repo_key(repo)}.experience.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "# Kite: tài liệu kinh nghiệm — repo (v1, 2026-11-25)\n"
        "<!-- kite:experience version=1 updated=2026-11-25 -->\n"
        "## Nên làm\n"
        "- Đo tốc độ từng bước trước khi tối ưu [nguồn: task chat-1]\n"
        "- Viết test trước khi sửa code [nguồn: task chat-2]\n",
        encoding="utf-8")
    proposals = improve.build_proposals(state, repo)
    slow = proposals[0]
    assert slow["axis"] == "toc_do"
    assert "Đo tốc độ từng bước trước khi tối ưu" in slow["proposal"]
    assert "Viết test trước" not in slow["proposal"]  # quy tắc không liên quan trục yếu


def test_weakest_axes_orders_lowest_first():
    scores = {"coding": 90.0, "phan_tich": 20.0, "toc_do": 20.0, "chi_phi": 55.0}
    assert improve.weakest_axes(scores) == [
        ("phan_tich", 20.0), ("toc_do", 20.0), ("chi_phi", 55.0)]
    assert improve.weakest_axes(scores, n=1) == [("phan_tich", 20.0)]
    assert improve.weakest_axes(scores, n=99) == [
        ("phan_tich", 20.0), ("toc_do", 20.0), ("chi_phi", 55.0), ("coding", 90.0)]
    assert improve.weakest_axes({}, n=3) == []
    assert improve.weakest_axes(scores, n=0) == []
