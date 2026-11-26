"""Knowledge store: upload a document, convert it to Markdown, keep the original.

A document is DATA, never instructions: nothing here loads document content into a system
prompt or an owner's rule — the stored Markdown is only read back on request (see
``knowledge_read`` in the MCP server and ``GET /api/knowledge/{doc_id}/markdown``).

Storage: ``state_dir/knowledge/<doc_id>/`` holds ``original.<ext>``, ``converted.md`` and
``meta.json`` (written atomically, ``.tmp`` + ``os.replace``, mode 0600, like ``memory.py``).
``doc_id`` is a uuid4 hex string. ``.md``/``.txt`` pass through verbatim; ``.docx``/``.pdf``/
``.pptx`` go through MarkItDown. A MarkItDown result with fewer than
``SCAN_CHAR_THRESHOLD`` non-whitespace characters is treated as a possible scan: the document
stays in status ``"converting"`` for the Docling job (``docling_job.py``) instead of being
silently served empty.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import uuid

KNOWLEDGE_FORMATS = frozenset({".md", ".txt", ".docx", ".pdf", ".pptx"})
MAX_FILE_BYTES = 20 * 1024 * 1024
SCAN_CHAR_THRESHOLD = 50
# The formats MarkItDown converts (as opposed to passthrough): only these can come back
# near-empty because the source had no text layer (a scanned PDF).
CONVERTED_FORMATS = frozenset({".docx", ".pdf", ".pptx"})
_DOC_ID_RE = re.compile(r"[0-9a-f]{32}")
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"


def non_whitespace_chars(text: str | None) -> int:
    """How many characters of ``text`` are not whitespace (0 for None/empty)."""
    if not text:
        return 0
    return sum(1 for char in text if not char.isspace())


def needs_docling(markdown: str | None) -> bool:
    """True when the converted Markdown is too thin to trust (< 50 non-whitespace chars)."""
    return non_whitespace_chars(markdown) < SCAN_CHAR_THRESHOLD


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_filename(filename: str) -> str:
    """The bare file name: any directory part is dropped (no path traversal)."""
    return Path(filename).name.strip()


def _suffix_of(filename: str) -> str:
    return Path(filename).suffix.lower()


def _check_magic(suffix: str, data: bytes) -> None:
    """Reject bytes that cannot be the claimed format.

    MarkItDown falls back to reading an unreadable file as plain text, so a corrupt
    ``.pdf``/``.docx``/``.pptx`` would otherwise be stored as if it converted fine.
    """
    if suffix == ".pdf" and not data.startswith(_PDF_MAGIC):
        raise ValueError("file .pdf không hợp lệ: không phải file PDF")
    if suffix in (".docx", ".pptx") and not data.startswith(_ZIP_MAGIC):
        raise ValueError(f"file {suffix} không hợp lệ: không phải file Office (ZIP)")


def _markitdown_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("markitdown")
    except Exception:
        return None


def convert_to_markdown(path: str | Path, suffix: str) -> tuple[str, str, str | None]:
    """Convert the file at ``path`` to Markdown.

    Returns ``(markdown, converter, converter_version)`` where converter is
    ``"passthrough"`` for ``.md``/``.txt`` and ``"markitdown"`` otherwise. Raises
    ``ValueError`` with a clear message when the file cannot be converted.
    """
    suffix = suffix.lower()
    if suffix not in KNOWLEDGE_FORMATS:
        raise ValueError(f"định dạng {suffix or '(không có đuôi)'} không được nhận; "
                         f"chỉ nhận: {', '.join(sorted(KNOWLEDGE_FORMATS))}")
    path = Path(path)
    if suffix in (".md", ".txt"):
        try:
            return path.read_text(encoding="utf-8"), "passthrough", None
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"không đọc được file {suffix}: {exc}") from exc
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise ValueError("chưa cài MarkItDown: cài extra web (`pip install -e .[web]`)") from exc
    try:
        result = MarkItDown().convert(str(path))
    except Exception as exc:
        raise ValueError(f"không chuyển đổi được file {suffix} sang Markdown: {exc}") from exc
    text = result.text_content or ""
    return text, "markitdown", _markitdown_version()


def _doc_dir(state_dir: str | Path, doc_id: str) -> Path:
    if not _DOC_ID_RE.fullmatch(doc_id or ""):
        raise FileNotFoundError(f"không có tài liệu '{doc_id}'")
    return Path(state_dir) / "knowledge" / doc_id


def write_meta(doc_dir: str | Path, meta: dict) -> Path:
    """Write ``meta.json`` atomically (``.tmp`` + ``os.replace``), mode 0600."""
    doc_dir = Path(doc_dir)
    doc_dir.mkdir(parents=True, exist_ok=True)
    path = doc_dir / "meta.json"
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(meta, stream, indent=1, ensure_ascii=False)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    os.replace(temp, path)
    return path


def load_meta(doc_dir: str | Path) -> dict:
    """Read ``meta.json`` from a document directory (raises FileNotFoundError/ValueError)."""
    path = Path(doc_dir) / "meta.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"meta.json của tài liệu bị hỏng: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("meta.json của tài liệu bị hỏng: không phải object JSON")
    return data


def _write_file_0600(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _normalise_tags(tags) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    seen: list[str] = []
    for tag in tags:
        tag = str(tag).strip()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def ingest_document(state_dir: str | Path, filename: str, data: bytes, scope: str = "global",
                    environment_id: str | None = None, tags=None) -> dict:
    """Store one uploaded document and convert it to Markdown; returns its metadata.

    On any failure (bad format, oversize, unconvertible file) nothing is left behind:
    the document directory is removed and a ``ValueError`` explains the problem.
    """
    if scope not in ("global", "environment"):
        raise ValueError("scope phải là 'global' hoặc 'environment'")
    if scope == "environment" and not (environment_id or "").strip():
        raise ValueError("scope 'environment' cần có environment_id")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"file quá lớn ({len(data)} byte); tối đa {MAX_FILE_BYTES} byte (20 MB)")
    clean = _clean_filename(filename)
    suffix = _suffix_of(clean)
    if not clean or suffix not in KNOWLEDGE_FORMATS:
        raise ValueError(f"định dạng {suffix or '(không có đuôi)'} không được nhận; "
                         f"chỉ nhận: {', '.join(sorted(KNOWLEDGE_FORMATS))}")
    _check_magic(suffix, data)
    doc_id = uuid.uuid4().hex
    doc_dir = Path(state_dir) / "knowledge" / doc_id
    doc_dir.mkdir(parents=True, exist_ok=False)
    try:
        original = doc_dir / f"original{suffix}"
        _write_file_0600(original, data)
        markdown, converter, converter_version = convert_to_markdown(original, suffix)
        # Only a MarkItDown conversion can come back near-empty from a scan; a short
        # .md/.txt passthrough is the document itself, not a failed conversion.
        status = "converting" if converter == "markitdown" and needs_docling(markdown) else "ready"
        now = _utcnow()
        meta = {"doc_id": doc_id, "filename": clean, "scope": scope,
                "environment_id": (environment_id or None) if scope == "environment" else None,
                "tags": _normalise_tags(tags), "status": status, "converter": converter,
                "converter_version": converter_version, "converted_at": now, "error": None,
                "size_bytes": len(data), "history": []}
        _write_file_0600(doc_dir / "converted.md", markdown.encode("utf-8"))
        write_meta(doc_dir, meta)
    except BaseException:
        shutil.rmtree(doc_dir, ignore_errors=True)
        raise
    return meta


def list_documents(state_dir: str | Path, scope: str | None = None,
                   environment_id: str | None = None, tag: str | None = None) -> list[dict]:
    """Every stored document's metadata, newest first, optionally filtered."""
    root = Path(state_dir) / "knowledge"
    docs: list[dict] = []
    if not root.is_dir():
        return docs
    for doc_dir in root.iterdir():
        if not doc_dir.is_dir():
            continue
        try:
            meta = load_meta(doc_dir)
        except (FileNotFoundError, ValueError, OSError):
            continue                      # a half-written directory is not a document
        if scope is not None and meta.get("scope") != scope:
            continue
        if environment_id is not None and meta.get("environment_id") != environment_id:
            continue
        if tag is not None and tag not in (meta.get("tags") or []):
            continue
        docs.append(meta)
    docs.sort(key=lambda m: (m.get("converted_at") or "", m.get("doc_id") or ""), reverse=True)
    return docs


def get_document(state_dir: str | Path, doc_id: str) -> dict:
    """One document's metadata (raises FileNotFoundError when there is no such document)."""
    return load_meta(_doc_dir(state_dir, doc_id))


def read_markdown(state_dir: str | Path, doc_id: str) -> str:
    """The converted Markdown of one document (raises FileNotFoundError when missing)."""
    path = _doc_dir(state_dir, doc_id) / "converted.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise FileNotFoundError(f"không có tài liệu '{doc_id}'") from exc


def delete_document(state_dir: str | Path, doc_id: str) -> bool:
    """Delete a document's whole directory (raises FileNotFoundError when missing)."""
    doc_dir = _doc_dir(state_dir, doc_id)
    if not doc_dir.is_dir():
        raise FileNotFoundError(f"không có tài liệu '{doc_id}'")
    shutil.rmtree(doc_dir)
    return True
