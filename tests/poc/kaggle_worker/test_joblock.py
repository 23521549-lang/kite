"""Locks shared by Kite's processes (joblock.py): the kernel's flock, so they hold across containers
and a crashed holder leaves nothing behind."""

from __future__ import annotations

import json
from pathlib import Path
import threading

from poc.kaggle_worker import joblock
from poc.kaggle_worker.web import docling_job


def test_hold_is_exclusive_and_holder_reads_the_record(tmp_path):
    path = tmp_path / "job.lock"
    handle = joblock.hold(path, {"label": "a"})
    assert handle is not None
    assert joblock.hold(path, {"label": "b"}) is None          # another open file: refused
    assert joblock.holder(path) == {"label": "a"}
    handle.close()
    assert joblock.holder(path) is None                        # file left behind, lock gone
    joblock.hold(path, {"label": "b"}).close()


def test_write_json_replaces_the_file_owner_only_and_leaves_no_temp(tmp_path):
    path = tmp_path / "state" / "queue.json"
    with joblock.updating(path):
        joblock.write_json(path, [1, 2], indent=1)
    assert json.loads(path.read_text(encoding="utf-8")) == [1, 2]
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["queue.json", "queue.json.lock"]


def _pdf_doc(state: Path, doc_id: str) -> Path:
    doc_dir = state / "knowledge" / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "original.pdf").write_bytes(b"%PDF-1.4\n")
    (doc_dir / "meta.json").write_text(json.dumps({
        "doc_id": doc_id, "filename": "b.pdf", "scope": "global", "environment_id": None,
        "tags": [], "status": "converting", "converter": "markitdown", "converter_version": "x",
        "converted_at": None, "error": None, "size_bytes": 9, "history": []}), encoding="utf-8")
    return doc_dir


def test_docling_runs_one_document_at_a_time(tmp_path, monkeypatch):
    # Each Docling run loads GBs of models: a second document waits for the first instead.
    state = tmp_path / "state"
    doc_dir = _pdf_doc(state, "doc-b")
    monkeypatch.setattr(docling_job, "DOCLING_WAIT_POLL_SECONDS", 0.05)
    converted = threading.Event()
    monkeypatch.setattr(docling_job, "convert_with_docling",
                        lambda path: (converted.set(), ("# b\n", "2.0"))[1])
    other = joblock.hold(state / docling_job.DOCLING_LOCK_NAME, {"doc_id": "doc-a"})
    results = []
    run = threading.Thread(target=lambda: results.append(docling_job.run_reconvert(state, "doc-b")))
    run.start()
    assert not converted.wait(0.5)                              # waits while doc-a converts
    other.close()
    run.join(timeout=10)
    assert converted.is_set() and results[0]["status"] == "ready"
    assert joblock.holder(state / docling_job.DOCLING_LOCK_NAME) is None
    assert Path(doc_dir / "converted.md").read_text(encoding="utf-8") == "# b\n"


def test_docling_does_not_wait_forever_behind_a_stuck_conversion(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _pdf_doc(state, "doc-c")
    monkeypatch.setattr(docling_job, "DOCLING_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(docling_job, "DOCLING_WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(docling_job, "convert_with_docling", lambda path: ("# c\n", "2.0"))
    stuck = joblock.hold(state / docling_job.DOCLING_LOCK_NAME, {"doc_id": "doc-a"})
    try:
        result = docling_job.run_reconvert(state, "doc-c")
    finally:
        stuck.close()
    assert result["status"] == "failed" and "quá lâu" in result["error"]
    assert not docling_job.is_converting(state, "doc-c")       # its own lock is released
