"""Docling fallback for the Knowledge store (Gói 6, worker (b)).

CI only installs ``.[test]`` (no fastapi, no docling), so this module is
skipped there; the real gate runs it in a venv with ``.[test,web,docling]``.
Fixtures build the document folder + meta.json by hand from the contract
schema, so no test depends on ``knowledge.py`` (worker (a), in parallel).
"""

import json
import os
from pathlib import Path
import subprocess
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("docling")

from poc.kaggle_worker import joblock
from poc.kaggle_worker.web import docling_job


def _write_meta(doc_dir: Path, meta: dict) -> None:
    (doc_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_doc(state_dir: Path, doc_id: str = "doc-scan", *, converter: str = "markitdown",
              converter_version: str = "0.1.9", old_markdown: str = "",
              filename: str = "scan.pdf", original: bytes = b"%PDF-1.4 fake\n",
              status: str = "ready") -> Path:
    doc_dir = state_dir / "knowledge" / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / f"original{Path(filename).suffix}").write_bytes(original)
    (doc_dir / "converted.md").write_text(old_markdown, encoding="utf-8")
    _write_meta(doc_dir, {
        "doc_id": doc_id, "filename": filename, "scope": "global",
        "environment_id": None, "tags": [], "status": status,
        "converter": converter, "converter_version": converter_version,
        "converted_at": "2026-11-25T00:00:00+00:00", "error": None,
        "size_bytes": len(original), "history": []})
    return doc_dir


def _read_meta(doc_dir: Path) -> dict:
    return json.loads((doc_dir / "meta.json").read_text(encoding="utf-8"))


def _make_text_pdf(path: Path, lines: list[str]) -> None:
    """A minimal one-page text PDF (Helvetica), written by hand so the test
    needs no PDF library: enough for a real end-to-end Docling conversion."""
    stream_lines = []
    y = 750
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        # One BT/ET per line: Td moves RELATIVE to the text position, so a
        # shared BT block would push later lines off the page.
        stream_lines.append(f"BT /F1 14 Tf 72 {y} Td ({escaped}) Tj ET")
        y -= 20
    stream = "\n".join(stream_lines).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))


# ---------------------------------------------------------------------------------- lock (Mảnh 3)
def test_second_reconvert_while_converting_is_refused(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir)
    lock_path, _token = docling_job._acquire_lock(doc_dir)
    try:
        called = []
        monkeypatch.setattr(docling_job, "convert_with_docling",
                            lambda path: called.append(path) or ("# moi\n", "2.152.0"))
        result = docling_job.run_reconvert(state_dir, "doc-scan", reason="manual")
    finally:
        docling_job._release_lock(doc_dir, pid=os.getpid())
    assert result["status"] == "failed"
    assert "đang được chuyển lại" in result["error"]
    assert called == []  # the converter never ran
    assert _read_meta(doc_dir)["status"] == "ready"  # meta untouched by the refusal


def test_request_reconvert_refuses_while_converting(tmp_path):
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir)
    docling_job._acquire_lock(doc_dir)
    try:
        with pytest.raises(ValueError, match="đang được chuyển lại"):
            docling_job.request_reconvert(state_dir, "doc-scan", reason="manual")
    finally:
        docling_job._release_lock(doc_dir, pid=os.getpid())


def test_stale_lock_is_cleaned_and_reconvert_proceeds(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir)
    dead = subprocess.Popen(["true"])
    dead_pid = dead.pid
    dead.wait()
    (doc_dir / "reconvert.lock").write_text(
        json.dumps({"pid": dead_pid, "started_at": "2026-11-25 00:00:00",
                    "token": "stale"}), encoding="utf-8")
    assert not joblock.pid_alive(dead_pid)
    monkeypatch.setattr(docling_job, "convert_with_docling",
                        lambda path: ("# moi\n", "2.152.0"))
    result = docling_job.run_reconvert(state_dir, "doc-scan", reason="manual")
    assert result["status"] == "ready"
    assert not (doc_dir / "reconvert.lock").exists()


def test_request_reconvert_spawns_detached_job(tmp_path):
    state_dir = tmp_path / "state"
    _make_doc(state_dir)
    marker = tmp_path / "spawned.txt"
    fake_kite = tmp_path / "fake-kite"
    fake_kite.write_text(f"#!/bin/sh\necho \"$@\" > {marker}\n")
    fake_kite.chmod(0o755)
    result = docling_job.request_reconvert(state_dir, "doc-scan", reason="manual",
                                           kite_command=[str(fake_kite)])
    assert result["status"] == "converting"
    assert Path(result["log"]).parent == state_dir / "journal"
    assert Path(result["log"]).exists()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    args = marker.read_text(encoding="utf-8")
    assert "knowledge reconvert doc-scan" in args
    assert "--reason manual" in args
    assert "--lock-token" in args
    # Wait until the fake child is really dead. Note the child becomes a
    # zombie of this process (never reaped): the lock must treat zombies as
    # dead, or a crashed reconvert would block the document forever.
    lock = json.loads((state_dir / "knowledge" / "doc-scan" / "reconvert.lock")
                      .read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while joblock.pid_alive(lock["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not joblock.pid_alive(lock["pid"])
    result2 = docling_job.request_reconvert(state_dir, "doc-scan", reason="manual",
                                            kite_command=[str(fake_kite)])
    assert result2["status"] == "converting"


def _fake_kite(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `kite` binary that records its arguments; returns (binary, marker)."""
    marker = tmp_path / "spawned.txt"
    fake_kite = tmp_path / "fake-kite"
    fake_kite.write_text(f"#!/bin/sh\necho \"$@\" > {marker}\n")
    fake_kite.chmod(0o755)
    return fake_kite, marker


def test_request_reconvert_sets_converting_before_spawn(tmp_path):
    # FE polls on meta status == "converting" and disables the button while set.
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir)
    fake_kite, _marker = _fake_kite(tmp_path)
    result = docling_job.request_reconvert(state_dir, "doc-scan", reason="manual",
                                           kite_command=[str(fake_kite)])
    assert result["status"] == "converting"
    assert _read_meta(doc_dir)["status"] == "converting"
    assert (doc_dir / "reconvert.lock").exists()


def test_request_reconvert_after_ingest_converting_meta_spawns(tmp_path):
    # Auto-trigger case: ingest just set meta "converting", no lock exists yet,
    # converted.md may be empty. The request must NOT be refused on the status.
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir, status="converting", old_markdown="")
    fake_kite, _marker = _fake_kite(tmp_path)
    result = docling_job.request_reconvert(state_dir, "doc-scan", reason="auto-scan",
                                           kite_command=[str(fake_kite)])
    assert result["status"] == "converting"
    assert _read_meta(doc_dir)["status"] == "converting"


def test_request_reconvert_spawn_failure_restores_status(tmp_path):
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir)
    with pytest.raises(ValueError, match="không khởi động được job"):
        docling_job.request_reconvert(state_dir, "doc-scan", reason="manual",
                                      kite_command=["/nonexistent/kite-binary"])
    assert _read_meta(doc_dir)["status"] == "ready"  # previous status restored
    assert not (doc_dir / "reconvert.lock").exists()


def test_request_reconvert_without_config_refuses_loudly(tmp_path):
    # Without config.yaml/config.json the detached child could not find the
    # state: refuse now instead of spawning a job that only reports "no doc".
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir)
    with pytest.raises(ValueError, match="không tìm thấy config"):
        docling_job.request_reconvert(state_dir, "doc-scan", reason="manual")
    assert _read_meta(doc_dir)["status"] == "ready"
    assert not (doc_dir / "reconvert.lock").exists()


def test_request_reconvert_refuses_on_live_lock_even_when_meta_converting(tmp_path):
    # Dedup is by LOCK (live pid), never by meta status: meta "converting"
    # plus a live lock is refused, meta "converting" without a lock is not.
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir, status="converting")
    docling_job._acquire_lock(doc_dir)
    try:
        with pytest.raises(ValueError, match="đang được chuyển lại"):
            docling_job.request_reconvert(state_dir, "doc-scan", reason="manual")
    finally:
        docling_job._release_lock(doc_dir, pid=os.getpid())


def test_corrupt_lock_file_is_cleaned(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir)
    (doc_dir / "reconvert.lock").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(docling_job, "convert_with_docling",
                        lambda path: ("# moi\n", "2.152.0"))
    result = docling_job.run_reconvert(state_dir, "doc-scan", reason="manual")
    assert result["status"] == "ready"
    assert not (doc_dir / "reconvert.lock").exists()


# --------------------------------------------------------------------------- run_reconvert (Mảnh 4)
def test_reconvert_success_archives_old_version(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir, old_markdown="# cu\n")
    monkeypatch.setattr(docling_job, "convert_with_docling",
                        lambda path: ("# moi\n\nNoi dung moi.\n", "2.152.0"))
    result = docling_job.run_reconvert(state_dir, "doc-scan", reason="manual")
    assert result["status"] == "ready"
    assert result["converter_version"] == "2.152.0"
    assert (doc_dir / "converted.md").read_text(encoding="utf-8").startswith("# moi")
    archived = list((doc_dir / "versions").glob("*-markitdown.md"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "# cu\n"
    meta = _read_meta(doc_dir)
    assert meta["converter"] == "docling"
    assert meta["converter_version"] == "2.152.0"
    assert meta["status"] == "ready" and meta["error"] is None
    assert meta["converted_at"]
    assert meta["history"][-1]["reason"] == "manual"
    assert meta["history"][-1]["previous_markdown"] == f"versions/{archived[0].name}"
    assert not (doc_dir / "reconvert.lock").exists()


def test_reconvert_empty_result_marks_failed_and_keeps_old_markdown(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir, old_markdown="# cu\n")
    monkeypatch.setattr(docling_job, "convert_with_docling",
                        lambda path: ("   \n\n", "2.152.0"))
    result = docling_job.run_reconvert(state_dir, "doc-scan", reason="auto-scan")
    assert result["status"] == "failed"
    assert "không đọc được" in result["error"]
    assert (doc_dir / "converted.md").read_text(encoding="utf-8") == "# cu\n"
    assert not (doc_dir / "versions").exists()
    meta = _read_meta(doc_dir)
    assert meta["status"] == "failed" and "không đọc được" in meta["error"]
    assert meta["history"][-1]["reason"] == "auto-scan"
    assert not (doc_dir / "reconvert.lock").exists()


def test_reconvert_conversion_error_marks_failed_without_raising(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    doc_dir = _make_doc(state_dir)

    def _boom(path):
        raise RuntimeError("Docling không chuyển được: model lỗi")

    monkeypatch.setattr(docling_job, "convert_with_docling", _boom)
    result = docling_job.run_reconvert(state_dir, "doc-scan", reason="manual")
    assert result["status"] == "failed"
    assert "model lỗi" in result["error"]
    meta = _read_meta(doc_dir)
    assert meta["status"] == "failed" and meta["error"] == result["error"]
    assert not (doc_dir / "reconvert.lock").exists()


def test_reconvert_missing_document_returns_failed(tmp_path):
    state_dir = tmp_path / "state"
    (state_dir / "knowledge").mkdir(parents=True)
    result = docling_job.run_reconvert(state_dir, "khong-co", reason="manual")
    assert result["status"] == "failed" and "không có tài liệu" in result["error"]


# ------------------------------------------------------------------ real conversion (Mảnh 2, mức A)
def test_convert_with_docling_real_text_pdf(tmp_path):
    pdf = tmp_path / "sample.pdf"
    _make_text_pdf(pdf, ["Docling conversion check", "Second line for the table test"])
    # Warm-up (untimed): the first run in a fresh cache downloads the Docling
    # models (~170 MB layout weights + OCR models); that is setup, not the
    # conversion being measured.
    markdown, version = docling_job.convert_with_docling(pdf)
    assert version and version != "unknown"
    assert "Docling conversion check" in markdown
    started = time.monotonic()
    markdown, version = docling_job.convert_with_docling(pdf)
    elapsed = time.monotonic() - started
    assert version and version != "unknown"
    assert "Docling conversion check" in markdown
    assert "Second line" in markdown
    assert elapsed < 300  # real conversion of a 1-page text PDF, CPU-only, models cached


# ---------------------------------------------------------------------------------- CLI (Mảnh 5)
def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "state" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("providers: []\n", encoding="utf-8")
    return config


def test_cli_reconvert_success(tmp_path, monkeypatch, capsys):
    from poc.kaggle_worker.cli import main

    state_dir = tmp_path / "state"
    _make_doc(state_dir)
    monkeypatch.setattr(docling_job, "convert_with_docling",
                        lambda path: ("# moi\n", "2.152.0"))
    config = _write_config(tmp_path)
    code = main(["--config", str(config), "knowledge", "reconvert", "doc-scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["status"] == "ready"


def test_cli_reconvert_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    from poc.kaggle_worker.cli import main

    state_dir = tmp_path / "state"
    _make_doc(state_dir)
    monkeypatch.setattr(docling_job, "convert_with_docling",
                        lambda path: ("  \n", "2.152.0"))
    config = _write_config(tmp_path)
    code = main(["--config", str(config), "knowledge", "reconvert", "doc-scan"])
    err = capsys.readouterr().err
    assert code != 0
    assert "không đọc được" in err
    assert _read_meta(state_dir / "knowledge" / "doc-scan")["status"] == "failed"


def test_cli_reconvert_unknown_doc_exits_nonzero(tmp_path, capsys):
    from poc.kaggle_worker.cli import main

    config = _write_config(tmp_path)
    code = main(["--config", str(config), "knowledge", "reconvert", "khong-co"])
    assert code != 0
    assert "không có tài liệu" in capsys.readouterr().err
