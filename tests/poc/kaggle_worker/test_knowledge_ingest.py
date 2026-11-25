"""Gói 6 (worker a): ingestion MarkItDown + upload endpoint + lưu trữ.

Fixtures are built in-test (minimal OOXML via zipfile, hand-written minimal PDFs) — no
committed binary files.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import zipfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markitdown")

from fastapi.testclient import TestClient  # noqa: E402

from poc.kaggle_worker.web import knowledge as know  # noqa: E402
from poc.kaggle_worker.web import security as sec  # noqa: E402
from poc.kaggle_worker.web.app import WebContext, create_app  # noqa: E402
from poc.kaggle_worker.web.db import Store, connect, sqlite_url  # noqa: E402
from tests.poc.kaggle_worker.test_launcher import raw_config  # noqa: E402
from tests.poc.kaggle_worker.test_web import (FakeGateway, FakeOps, NOW, PASSWORD, SECRET,  # noqa: E402
                                             signed_in)

# ---------------------------------------------------------------- fixtures (built in-test)
DOCX_TEXT = "Xin chao docx the gioi day la mot doan van ban dai hon nam muoi ky tu khong trang de qua nguong"
PDF_TEXT = "Xin chao pdf co text day la mot doan van ban dai hon nam muoi ky tu khong trang de qua nguong"
PPTX_TEXT = "Xin chao pptx slide mot day la mot doan van ban dai hon nam muoi ky tu khong trang de qua nguong"


def make_docx(text: str = DOCX_TEXT) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def make_pptx(text: str = PPTX_TEXT) -> bytes:
    slide = ('<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
             'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
             '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
             '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></a:xfrm><a:prstGeom prst="rect"/></p:spPr>'
             f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>'
             '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>')
        z.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldIdLst><p:sldId id="256" r:id="rId2" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></p:sldIdLst></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>')
        z.writestr("ppt/slides/slide1.xml", slide)
    return buf.getvalue()


def make_pdf(text: str | None) -> bytes:
    """Minimal one-page PDF; ``text=None`` means a scan-like page with no text object at all."""
    stream = b"" if text is None else f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture
def site(tmp_path):
    raw = raw_config(tmp_path, launch={"kernel": "hanle/kite-worker"})
    (tmp_path / "secrets" / "worker_token_kaggle-han").write_text("w" * 32 + "\n")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw))
    clock = {"now": NOW}
    store = Store(connect(sqlite_url(tmp_path / "web.sqlite3")), clock=lambda: clock["now"])
    store.migrate()
    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    store.create_user("han", sec.hash_password(PASSWORD), sealer.seal(SECRET))
    ops, gateway = FakeOps(), FakeGateway()
    ctx = WebContext(config_path, store, sealer, ops_factory=lambda _config: ops, gateway=gateway,
                     clock=lambda: clock["now"], events_every=0.05)
    client = TestClient(create_app(ctx), base_url="https://testserver")
    return {"client": client, "ctx": ctx, "store": store, "path": tmp_path, "clock": clock}


def _doc_dirs(state_dir: Path) -> set[str]:
    root = state_dir / "knowledge"
    return {p.name for p in root.iterdir()} if root.is_dir() else set()


def _meta_fields(meta: dict) -> None:
    assert set(meta) == {"doc_id", "filename", "scope", "environment_id", "tags", "status", "converter",
                         "converter_version", "converted_at", "error", "size_bytes", "history"}
    assert len(meta["doc_id"]) == 32 and all(c in "0123456789abcdef" for c in meta["doc_id"])
    assert meta["error"] is None and meta["history"] == []


# ---------------------------------------------------------------- threshold helpers
@pytest.mark.parametrize("text,expected", [("", True), ("   \n\t  ", True), ("x" * 49, True),
                                           ("x" * 50, False), ("x" * 51, False),
                                           ("a" * 25 + " " * 100 + "b" * 24, True),
                                           ("a" * 25 + " " * 100 + "b" * 25, False), (None, True)])
def test_needs_docling_threshold_50_non_whitespace_chars(text, expected):
    assert know.needs_docling(text) is expected
    assert know.non_whitespace_chars(text) == (0 if not text else sum(1 for c in text if not c.isspace()))


# ---------------------------------------------------------------- passthrough
def test_passthrough_md_txt_keep_verbatim(tmp_path):
    body = "# Tieu de\n\nNoi dung tieng Viet co dau: Xin chao the gioi.\n"
    for name, data in (("ghi-chu.md", body.encode()), ("ghi-chu.txt", body.encode())):
        meta = know.ingest_document(tmp_path, name, data, "global", None, ["a", " b ", "a", ""])
        _meta_fields(meta)
        assert meta["converter"] == "passthrough" and meta["converter_version"] is None
        assert meta["status"] == "ready" and meta["tags"] == ["a", "b"]
        assert meta["size_bytes"] == len(data) and meta["filename"] == name
        doc_dir = tmp_path / "knowledge" / meta["doc_id"]
        assert (doc_dir / "converted.md").read_text(encoding="utf-8") == body
        assert (doc_dir / f"original.{name.rsplit('.', 1)[1]}").read_bytes() == data
        assert stat.S_IMODE((doc_dir / "meta.json").stat().st_mode) == 0o600
        assert know.read_markdown(tmp_path, meta["doc_id"]) == body


def test_short_passthrough_is_ready_not_converting(tmp_path):
    meta = know.ingest_document(tmp_path, "ngan.md", b"Hi\n", "global")
    assert meta["status"] == "ready" and know.needs_docling("Hi\n") is True


# ---------------------------------------------------------------- MarkItDown conversion
def test_docx_pdf_pptx_convert_via_markitdown(tmp_path):
    cases = (("tai-lieu.docx", make_docx(), DOCX_TEXT, "docx"),
             ("tai-lieu.pdf", make_pdf(PDF_TEXT), PDF_TEXT, "pdf"),
             ("tai-lieu.pptx", make_pptx(), PPTX_TEXT, "pptx"))
    for name, data, expected, _ext in cases:
        meta = know.ingest_document(tmp_path, name, data, "global")
        _meta_fields(meta)
        assert meta["converter"] == "markitdown" and meta["converter_version"] == "0.1.9"
        assert meta["status"] == "ready"
        assert expected in know.read_markdown(tmp_path, meta["doc_id"])


def test_pdf_scan_has_no_text_and_stays_converting(tmp_path):
    meta = know.ingest_document(tmp_path, "scan.pdf", make_pdf(None), "global")
    assert meta["converter"] == "markitdown" and meta["status"] == "converting"
    assert know.read_markdown(tmp_path, meta["doc_id"]).strip() == ""


def test_mixed_pdf_with_few_chars_also_needs_docling(tmp_path):
    meta = know.ingest_document(tmp_path, "hon-hop.pdf", make_pdf("Hi"), "global")
    assert meta["status"] == "converting"
    assert know.non_whitespace_chars(know.read_markdown(tmp_path, meta["doc_id"])) == 2


def test_uppercase_suffix_is_accepted(tmp_path):
    meta = know.ingest_document(tmp_path, "TAI-LIEU.PDF", make_pdf(PDF_TEXT), "global")
    assert meta["status"] == "ready" and PDF_TEXT in know.read_markdown(tmp_path, meta["doc_id"])
    assert (tmp_path / "knowledge" / meta["doc_id"] / "original.pdf").is_file()


# ---------------------------------------------------------------- failures leave nothing behind
def test_corrupt_pdf_is_rejected_and_leaves_nothing(tmp_path):
    before = _doc_dirs(tmp_path)
    with pytest.raises(ValueError, match="không phải file PDF"):
        know.ingest_document(tmp_path, "hong.pdf", b"day khong phai pdf ma la rac\x00\x01\x02", "global")
    assert _doc_dirs(tmp_path) == before


def test_corrupt_docx_is_rejected_and_leaves_nothing(tmp_path):
    before = _doc_dirs(tmp_path)
    with pytest.raises(ValueError, match="không phải file Office"):
        know.ingest_document(tmp_path, "hong.docx", b"rac khong phai zip\x00\x01", "global")
    assert _doc_dirs(tmp_path) == before


@pytest.mark.parametrize("name", ["chuong-trinh.exe", "tap-tin.zip", "tai-lieu.xlsx", "anh.png", "khong-duoi"])
def test_forbidden_format_is_rejected(tmp_path, name):
    with pytest.raises(ValueError, match="không được nhận"):
        know.ingest_document(tmp_path, name, b"x" * 10, "global")
    assert _doc_dirs(tmp_path) == set()


def test_oversize_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="quá lớn"):
        know.ingest_document(tmp_path, "lon.txt", b"x" * (know.MAX_FILE_BYTES + 1), "global")
    assert _doc_dirs(tmp_path) == set()


def test_environment_scope_needs_environment_id(tmp_path):
    with pytest.raises(ValueError, match="environment_id"):
        know.ingest_document(tmp_path, "a.md", b"x", "environment", None)
    with pytest.raises(ValueError, match="scope"):
        know.ingest_document(tmp_path, "a.md", b"x", "repo")


def test_filename_path_parts_are_dropped(tmp_path):
    meta = know.ingest_document(tmp_path, "../../etc/passwd.md", b"# hi\n", "global")
    assert meta["filename"] == "passwd.md"
    assert (tmp_path / "knowledge" / meta["doc_id"] / "original.md").is_file()
    assert not (tmp_path / "etc").exists()


# ---------------------------------------------------------------- store: list/get/delete/meta
def test_list_get_delete_and_filters(tmp_path):
    g = know.ingest_document(tmp_path, "chung.md", b"# chung\n", "global", None, ["docs"])
    e = know.ingest_document(tmp_path, "rieng.md", b"# rieng\n", "environment", "env-1", ["docs", "rieng"])
    e2 = know.ingest_document(tmp_path, "khac.md", b"# khac\n", "environment", "env-2", ["khac"])
    assert {d["doc_id"] for d in know.list_documents(tmp_path)} == {g["doc_id"], e["doc_id"], e2["doc_id"]}
    assert [d["doc_id"] for d in know.list_documents(tmp_path, scope="global")] == [g["doc_id"]]
    assert [d["doc_id"] for d in know.list_documents(tmp_path, environment_id="env-1")] == [e["doc_id"]]
    assert {d["doc_id"] for d in know.list_documents(tmp_path, tag="docs")} == {g["doc_id"], e["doc_id"]}
    assert know.list_documents(tmp_path, tag="khong-co") == []
    assert know.get_document(tmp_path, e["doc_id"])["filename"] == "rieng.md"
    assert know.read_markdown(tmp_path, e["doc_id"]) == "# rieng\n"
    assert know.delete_document(tmp_path, e2["doc_id"]) is True
    assert not (tmp_path / "knowledge" / e2["doc_id"]).exists()
    with pytest.raises(FileNotFoundError):
        know.get_document(tmp_path, e2["doc_id"])
    with pytest.raises(FileNotFoundError):
        know.delete_document(tmp_path, e2["doc_id"])
    with pytest.raises(FileNotFoundError):
        know.get_document(tmp_path, "../../etc/passwd")
    with pytest.raises(FileNotFoundError):
        know.read_markdown(tmp_path, "khong-phai-hex")


def test_write_meta_is_atomic_and_mode_0600(tmp_path):
    doc_dir = tmp_path / "knowledge" / ("a" * 32)
    meta = {"doc_id": "a" * 32, "filename": "a.md"}
    know.write_meta(doc_dir, meta)
    assert know.load_meta(doc_dir) == meta
    assert stat.S_IMODE((doc_dir / "meta.json").stat().st_mode) == 0o600
    assert not (doc_dir / "meta.json.tmp").exists()


# ---------------------------------------------------------------- HTTP routes
def test_upload_list_get_markdown_delete_via_api(site):
    headers = signed_in(site)
    client = site["client"]
    resp = client.post("/api/knowledge", files={"file": ("ghi-chu.md", b"# Xin chao\n", "text/markdown")},
                       data={"scope": "global", "tags": "docs, huong-dan"}, headers=headers)
    assert resp.status_code == 201, resp.text
    meta = resp.json()
    assert meta["status"] == "ready" and meta["converter"] == "passthrough"
    assert meta["tags"] == ["docs", "huong-dan"]
    doc_id = meta["doc_id"]

    assert client.get("/api/knowledge", headers=headers).json()["documents"][0]["doc_id"] == doc_id
    assert client.get(f"/api/knowledge/{doc_id}", headers=headers).json()["filename"] == "ghi-chu.md"
    md = client.get(f"/api/knowledge/{doc_id}/markdown", headers=headers)
    assert md.status_code == 200 and md.text == "# Xin chao\n"

    gone = client.delete(f"/api/knowledge/{doc_id}", headers=headers)
    assert gone.status_code == 200 and gone.json() == {"ok": True, "doc_id": doc_id}
    assert client.get(f"/api/knowledge/{doc_id}", headers=headers).status_code == 404
    assert client.get("/api/knowledge", headers=headers).json()["documents"] == []


def test_knowledge_routes_need_a_session(site):
    client = site["client"]
    assert client.get("/api/knowledge").status_code == 401
    assert client.post("/api/knowledge", files={"file": ("a.md", b"x", "text/markdown")}).status_code == 401
    assert client.get("/api/knowledge/" + "a" * 32).status_code == 401
    assert client.delete("/api/knowledge/" + "a" * 32).status_code == 401


def test_upload_filters_via_api(site):
    headers = signed_in(site)
    client = site["client"]
    client.post("/api/knowledge", files={"file": ("chung.md", b"# c\n", "text/markdown")},
                data={"scope": "global", "tags": "docs"}, headers=headers)
    env = client.post("/api/knowledge", files={"file": ("rieng.md", b"# r\n", "text/markdown")},
                      data={"scope": "environment", "environment_id": "env-1", "tags": "docs"},
                      headers=headers).json()
    assert client.get("/api/knowledge", params={"scope": "environment"}, headers=headers).json()["documents"][0]["doc_id"] == env["doc_id"]
    assert client.get("/api/knowledge", params={"environment_id": "env-1"}, headers=headers).json()["documents"][0]["doc_id"] == env["doc_id"]
    assert len(client.get("/api/knowledge", params={"tag": "docs"}, headers=headers).json()["documents"]) == 2
    assert client.get("/api/knowledge", params={"tag": "khong-co"}, headers=headers).json()["documents"] == []


def test_upload_forbidden_format_returns_400_and_saves_nothing(site):
    headers = signed_in(site)
    client = site["client"]
    resp = client.post("/api/knowledge", files={"file": ("chuong-trinh.exe", b"MZ" + b"x" * 10, "application/octet-stream")},
                       data={"scope": "global"}, headers=headers)
    assert resp.status_code == 400, resp.text
    assert "không được nhận" in resp.json()["error"]
    assert client.get("/api/knowledge", headers=headers).json()["documents"] == []


def test_upload_corrupt_pdf_returns_400_and_saves_nothing(site):
    headers = signed_in(site)
    client = site["client"]
    resp = client.post("/api/knowledge", files={"file": ("hong.pdf", b"rac khong phai pdf\x00\x01", "application/pdf")},
                       data={"scope": "global"}, headers=headers)
    assert resp.status_code == 400, resp.text
    assert client.get("/api/knowledge", headers=headers).json()["documents"] == []


def test_upload_oversize_returns_413(site):
    headers = signed_in(site)
    resp = site["client"].post("/api/knowledge",
                               files={"file": ("lon.txt", b"x" * (know.MAX_FILE_BYTES + 1), "text/plain")},
                               data={"scope": "global"}, headers=headers)
    assert resp.status_code == 413, resp.text[:200]
    assert site["client"].get("/api/knowledge", headers=headers).json()["documents"] == []


def test_upload_scan_pdf_stays_converting_via_api(site):
    headers = signed_in(site)
    resp = site["client"].post("/api/knowledge", files={"file": ("scan.pdf", make_pdf(None), "application/pdf")},
                               data={"scope": "global"}, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "converting"


def test_upload_docx_via_api(site):
    headers = signed_in(site)
    resp = site["client"].post("/api/knowledge",
                               files={"file": ("tai-lieu.docx", make_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                               data={"scope": "global"}, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["converter"] == "markitdown"
    assert DOCX_TEXT in site["client"].get(f"/api/knowledge/{resp.json()['doc_id']}/markdown",
                                            headers=headers).text


def test_reconvert_spawns_docling_job_and_keeps_markdown(site):
    # docling_job.py (worker (b)) exists in this tree: the route calls its
    # request_reconvert, which spawns the detached job and answers "converting".
    # The job itself runs in the background; the stored Markdown must survive it
    # (a failed reconvert never replaces converted.md), so only the answer and
    # the intact document are asserted here — the meta status may move to
    # "failed" once the background job records that Docling is not installed.
    headers = signed_in(site)
    client = site["client"]
    doc_id = client.post("/api/knowledge", files={"file": ("a.md", b"# hi\n", "text/markdown")},
                         data={"scope": "global"}, headers=headers).json()["doc_id"]
    resp = client.post(f"/api/knowledge/{doc_id}/reconvert", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["doc_id"] == doc_id and body["status"] == "converting"
    assert body["log"].endswith(".log") and f"knowledge-reconvert-{doc_id}" in body["log"]
    assert client.get(f"/api/knowledge/{doc_id}/markdown", headers=headers).text == "# hi\n"


def test_reconvert_without_docling_job_answers_converting(site, monkeypatch):
    # The seam the route was written for: when docling_job cannot be imported,
    # the route must still answer {"doc_id", "status": "converting"} and leave
    # the stored document exactly as it was. Simulated by hiding the module.
    import sys

    from poc.kaggle_worker import web as web_pkg

    # Hiding the sys.modules entry alone is not enough: `from . import docling_job`
    # also finds the attribute already set on the parent package by an earlier
    # import, so both must go for the ImportError path to trigger.
    monkeypatch.setitem(sys.modules, "poc.kaggle_worker.web.docling_job", None)
    monkeypatch.delattr(web_pkg, "docling_job", raising=False)
    headers = signed_in(site)
    client = site["client"]
    doc_id = client.post("/api/knowledge", files={"file": ("a.md", b"# hi\n", "text/markdown")},
                         data={"scope": "global"}, headers=headers).json()["doc_id"]
    resp = client.post(f"/api/knowledge/{doc_id}/reconvert", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"doc_id": doc_id, "status": "converting"}
    assert client.get(f"/api/knowledge/{doc_id}", headers=headers).json()["status"] == "ready"
    assert client.get(f"/api/knowledge/{doc_id}/markdown", headers=headers).text == "# hi\n"


def test_reconvert_conflicts_when_already_converting_and_404_when_missing(site):
    headers = signed_in(site)
    client = site["client"]
    scan = client.post("/api/knowledge", files={"file": ("scan.pdf", make_pdf(None), "application/pdf")},
                       data={"scope": "global"}, headers=headers).json()
    assert scan["status"] == "converting"
    assert client.post(f"/api/knowledge/{scan['doc_id']}/reconvert", headers=headers).status_code == 409
    assert client.post("/api/knowledge/" + "b" * 32 + "/reconvert", headers=headers).status_code == 404
