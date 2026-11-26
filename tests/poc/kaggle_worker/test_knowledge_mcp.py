"""MCP tools knowledge_list / knowledge_read (Gói 6, worker c).

The real backend module ``poc.kaggle_worker.web.knowledge`` belongs to worker (a)
and does not exist yet in this worktree, so the MCP layer is tested against a stub
module with the exact contract signatures, backed by hand-built fixture docs
(meta.json + converted.md, contract schema). The one test that needs the real
module is marked "chờ tích hợp với knowledge.py" and skips while it is absent.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from poc.kaggle_worker.config import validate_config
from poc.kaggle_worker.mcp_server import KiteOps, KiteToolError, build_server


def _raw_config(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "temporal": {"endpoint": "127.0.0.1:7233", "task_queue": "kite-coding-tasks"},
        "opencode": {"executable": "opencode", "server": {"host": "127.0.0.1", "port": 4096}},
        "harness2": {"checkout": "/tmp/h2", "cli": "/tmp/h2/cli"},
        "providers": [{
            "id": "kaggle-han", "adapter": "pull", "enabled": True, "owner": "han",
            "model": "qwen3.6", "models": [ "devstral"], "capabilities": ["coding"],
            "quota": {"gpu_seconds": 108000, "concurrency": 1},
            "secrets": {"worker_token": f"file:{tmp_path / 'token'}"},
        }],
        "gateway": {"aliases": {"qwen3.6-fast": {"model": "qwen3.6", "params": {}}}},
    }


def make_ops(tmp_path: Path) -> KiteOps:
    return KiteOps(validate_config(_raw_config(tmp_path)),
                   db_path=tmp_path / "state" / "scheduler.sqlite3",
                   state_dir=tmp_path / "state", allowed_roots=[tmp_path])


def _write_doc(state_dir: Path, doc_id: str, meta: dict, markdown: str) -> None:
    doc_dir = state_dir / "knowledge" / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (doc_dir / "converted.md").write_text(markdown, encoding="utf-8")


@pytest.fixture
def docs(tmp_path: Path) -> tuple[KiteOps, dict[str, dict]]:
    """Two fixture docs on disk, contract schema, plus the ops using that state_dir."""
    state_dir = tmp_path / "state"
    meta_a = {"doc_id": "aaa111", "filename": "bao-cao.md", "scope": "global",
              "environment_id": None, "tags": ["bao-cao"], "status": "ready",
              "converter": "markitdown", "converter_version": "0.1.9",
              "converted_at": "2026-11-25T10:00:00", "error": None, "size_bytes": 42, "history": []}
    meta_b = {"doc_id": "bbb222", "filename": "huong-dan.txt", "scope": "environment",
              "environment_id": "kite", "tags": ["thanh-toan"], "status": "ready",
              "converter": "passthrough", "converter_version": None,
              "converted_at": "2026-11-25T11:00:00", "error": None, "size_bytes": 30, "history": []}
    _write_doc(state_dir, "aaa111", meta_a, "# Báo cáo\nNội dung báo cáo.\n")
    _write_doc(state_dir, "bbb222", meta_b, "Hướng dẫn thanh toán.\n")
    return make_ops(tmp_path), {"aaa111": meta_a, "bbb222": meta_b}


def _install_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `poc.kaggle_worker.web.knowledge` with the exact contract signatures,
    reading the same on-disk layout the real module will use."""
    stub = types.ModuleType("poc.kaggle_worker.web.knowledge")

    def list_documents(state_dir, scope=None, environment_id=None, tag=None):
        found = []
        for meta_path in sorted(Path(state_dir).glob("knowledge/*/meta.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if scope is not None and meta["scope"] != scope:
                continue
            if environment_id is not None and meta.get("environment_id") != environment_id:
                continue
            if tag is not None and tag not in meta.get("tags", []):
                continue
            found.append(meta)
        return found

    def get_document(state_dir, doc_id):
        meta_path = Path(state_dir) / "knowledge" / doc_id / "meta.json"
        if not meta_path.is_file():
            raise KeyError(doc_id)
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def read_markdown(state_dir, doc_id):
        md_path = Path(state_dir) / "knowledge" / doc_id / "converted.md"
        if not md_path.is_file():
            raise FileNotFoundError(doc_id)
        return md_path.read_text(encoding="utf-8")

    stub.list_documents = list_documents
    stub.get_document = get_document
    stub.read_markdown = read_markdown
    monkeypatch.setitem(sys.modules, "poc.kaggle_worker.web.knowledge", stub)


def test_knowledge_list_returns_fixture_docs(docs, monkeypatch):
    ops, _metas = docs
    _install_stub(monkeypatch)
    result = asyncio.run(ops.knowledge_list())
    assert {d["doc_id"] for d in result["documents"]} == {"aaa111", "bbb222"}


def test_knowledge_list_filters_by_scope_tag_environment(docs, monkeypatch):
    ops, _metas = docs
    _install_stub(monkeypatch)
    assert [d["doc_id"] for d in asyncio.run(ops.knowledge_list(scope="global"))["documents"]] == ["aaa111"]
    assert [d["doc_id"] for d in asyncio.run(ops.knowledge_list(tag="thanh-toan"))["documents"]] == ["bbb222"]
    assert [d["doc_id"] for d in asyncio.run(ops.knowledge_list(environment_id="kite"))["documents"]] == ["bbb222"]
    assert asyncio.run(ops.knowledge_list(scope="environment", tag="bao-cao"))["documents"] == []


def test_knowledge_list_rejects_bad_scope(docs):
    ops, _metas = docs
    with pytest.raises(KiteToolError, match="scope"):
        asyncio.run(ops.knowledge_list(scope="repo"))


def test_knowledge_read_returns_meta_plus_markdown(docs, monkeypatch):
    ops, _metas = docs
    _install_stub(monkeypatch)
    result = asyncio.run(ops.knowledge_read("aaa111"))
    assert result["filename"] == "bao-cao.md"
    assert result["converter"] == "markitdown"
    assert result["markdown"] == "# Báo cáo\nNội dung báo cáo.\n"


def test_knowledge_read_unknown_doc_raises(docs, monkeypatch):
    ops, _metas = docs
    _install_stub(monkeypatch)
    with pytest.raises(KiteToolError, match="knowledge_list"):
        asyncio.run(ops.knowledge_read("khong-co"))


def test_knowledge_list_without_module_raises(docs, monkeypatch):
    """Module import blocked: the tool fails loudly instead of silently.

    (Ghi chú lúc ghép: bản đầu của test này xóa module khỏi sys.modules để mô
    phỏng "module chưa tồn tại" lúc 3 worker làm song song; khi đã ghép thì module
    luôn tồn tại nên xóa khỏi sys.modules chỉ khiến Python import lại. Chặn import
    bằng None trong sys.modules mới mô phỏng đúng ImportError.)
    """
    ops, _metas = docs
    monkeypatch.setitem(sys.modules, "poc.kaggle_worker.web.knowledge", None)
    with pytest.raises(KiteToolError, match="chưa sẵn sàng"):
        asyncio.run(ops.knowledge_list())


def test_knowledge_tools_registered(docs):
    pytest.importorskip("mcp")
    ops, _metas = docs
    names = {tool.name for tool in asyncio.run(build_server(ops).list_tools())}
    assert {"knowledge_list", "knowledge_read"} <= names


def test_knowledge_with_real_module(tmp_path: Path):
    """CHỜ TÍCH HỢP với knowledge.py (worker a): ingest thật bằng module thật, rồi
    đọc lại qua tool MCP. Skip khi module chưa tồn tại."""
    knowledge = pytest.importorskip("poc.kaggle_worker.web.knowledge")
    ops = make_ops(tmp_path)
    meta = knowledge.ingest_document(ops.state_dir, "ghi-chu.md", b"# Ghi chu\nNoi dung that.\n",
                                     "global", None, ["ghi-chu"])
    listed = asyncio.run(ops.knowledge_list())
    assert [d["doc_id"] for d in listed["documents"]] == [meta["doc_id"]]
    read = asyncio.run(ops.knowledge_read(meta["doc_id"]))
    assert read["markdown"] == "# Ghi chu\nNoi dung that.\n"
    assert read["filename"] == "ghi-chu.md"
