"""Environment context (F4): the four parts of Diem 9's Context tab.

* the repo description, written by the owner (stored in the ``repos`` table);
* the codemap — graphify's code graph of the repo, with a rebuild job;
* the repo's own ``AGENTS.md``, when it has one;
* the "what Kite sees" preview — the static context the lead agent actually
  gets for this repo, assembled from the same sources the task pipeline
  loads (the approved experience document, or the approved lessons as a
  fallback), plus the description, the codemap summary, the knowledge
  documents scoped to this environment and the recent tasks.

Pure helpers over the state dir; the web endpoints in ``app.py`` call these.
Nothing here calls a model: the preview is a summary of data that already
exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import codemap as codemap_mod

AGENTS_MD_MAX_CHARS = 100_000
TOP_FILES = 8

# Meta (status/built_at/error) lives in the codemap dir, written atomically
# like the knowledge meta (0600, .tmp + os.replace).
_META_NAME = "meta.json"
_LOCK_NAME = "rebuild.lock"


def _check_env_id(env_id: str) -> str:
    """An environment id is a path segment under the codemap dir: refuse
    anything that could escape it."""
    if not env_id or env_id in (".", "..") or "/" in env_id or "\\" in env_id:
        raise ValueError(f"env_id không hợp lệ: {env_id!r}")
    return env_id


def codemap_dir(state_dir: str | Path, env_id: str) -> Path:
    return Path(state_dir) / "codemap" / _check_env_id(env_id)


def read_codemap_meta(state_dir: str | Path, env_id: str) -> dict:
    try:
        return json.loads((codemap_dir(state_dir, env_id) / _META_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_codemap_meta(state_dir: str | Path, env_id: str, meta: dict) -> Path:
    from .knowledge import write_meta as _write_knowledge_meta

    return _write_knowledge_meta(codemap_dir(state_dir, env_id), meta)


def _empty_symbols() -> dict[str, int]:
    return {"total": 0, "modules": 0, "classes": 0, "functions": 0, "methods": 0}


def _graph_stats(graph: codemap_mod.CodeGraph) -> tuple[dict[str, int], list[dict]]:
    symbols = _empty_symbols()
    for symbol in graph.symbols.values():
        symbols["total"] += 1
        if symbol.kind == "module":
            symbols["modules"] += 1
        elif symbol.kind == "class":
            symbols["classes"] += 1
        elif symbol.kind == "function":
            symbols["functions"] += 1
        elif symbol.kind == "method":
            symbols["methods"] += 1
    top = sorted(((path, len(items)) for path, items in graph.by_file.items()),
                 key=lambda item: (-item[1], item[0]))[:TOP_FILES]
    return symbols, [{"path": path, "symbols": count} for path, count in top]


def codemap_status(state_dir: str | Path, env_id: str, repo_path: str | None,
                   *, building: bool = False) -> dict:
    """The codemap's state for one environment. The graph is never built here:
    a GET only reads what already exists (``graph.json``), so a slow build
    cannot stall the page; rebuilding is a POST that starts a background job.

    ``building`` is the live-lock truth from ``codemap_job.is_building`` — a
    meta status of ``"building"`` alone does not count, so a job that died
    without writing its meta cannot leave the tab stuck on "building" forever.
    """
    base = {"status": "not_built", "graphify_available": codemap_mod.graphify_cli() is not None,
            "built_at": None, "files": 0, "symbols": _empty_symbols(), "top_files": [],
            "error": None}
    if not repo_path:
        base["status"] = "none"
        return base
    if building:
        base["status"] = "building"
        return base
    graph_path = codemap_dir(state_dir, env_id) / "graphify-out" / "graph.json"
    meta = read_codemap_meta(state_dir, env_id)
    if not graph_path.is_file():
        if meta.get("status") == "failed":
            base["status"] = "failed"
            base["error"] = meta.get("error")
        return base
    graph = codemap_mod.CodeGraph.load(graph_path, Path(repo_path))
    if graph is None:
        base["status"] = "failed"
        base["error"] = "không đọc được graph.json đã tạo"
        return base
    symbols, top_files = _graph_stats(graph)
    base.update({"status": "ready", "files": len(graph.by_file), "symbols": symbols,
                 "top_files": top_files,
                 "built_at": meta.get("built_at") or graph_path.stat().st_mtime})
    return base


def read_agents_md(repo_path: str | Path | None) -> dict:
    """The repo's own AGENTS.md, when it has one (capped; the tab shows the
    cap). The file is the repo's own instructions — data the agent reads in
    the workspace, never an instruction Kite follows blindly."""
    if not repo_path:
        return {"present": False, "content": None, "truncated": False, "chars": 0}
    path = Path(repo_path) / "AGENTS.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"present": False, "content": None, "truncated": False, "chars": 0}
    truncated = len(text) > AGENTS_MD_MAX_CHARS
    return {"present": True, "content": text[:AGENTS_MD_MAX_CHARS], "truncated": truncated,
            "chars": len(text)}


def codemap_summary_line(codemap: dict) -> str:
    """One line about the codemap for the preview."""
    status = codemap.get("status")
    if status == "ready":
        symbols = codemap.get("symbols") or {}
        return (f"Bản đồ code: {codemap.get('files', 0)} file, {symbols.get('total', 0)} ký hiệu "
                f"({symbols.get('classes', 0)} lớp, {symbols.get('functions', 0)} hàm, "
                f"{symbols.get('methods', 0)} phương thức).")
    if status == "building":
        return "Bản đồ code: đang tạo lại…"
    if status == "failed":
        return f"Bản đồ code: lần tạo gần nhất lỗi ({codemap.get('error') or 'không rõ lý do'})."
    if status == "none":
        return "Bản đồ code: môi trường này không gắn repo."
    if not codemap.get("graphify_available"):
        return "Bản đồ code: chưa có (chưa cài graphify — cài thêm bằng `pip install -e \".[codemap]\"`)."
    return "Bản đồ code: chưa tạo (bấm Tạo lại để tạo)."


def assemble_preview(*, env_name: str, description: str | None, experience_text: str,
                     lessons_text: str, codemap: dict, agents_md: dict,
                     knowledge_docs: list[dict] | None = None,
                     recent_tasks: list[dict] | None = None) -> str:
    """The preview text: exactly the static context the lead agent gets for
    this repo. ``experience_text`` is ``experience_note()`` verbatim (the
    block a task's goal gets when an approved document exists);
    ``lessons_text`` is ``lessons_note()`` verbatim, used only when there is
    no approved document — the same precedence ``backend.py`` uses."""
    lines = [f"# Ngữ cảnh repo {env_name}", ""]
    lines.append(description.strip() if description and description.strip()
                 else "(chưa có mô tả — chủ viết ở mục Ngữ cảnh)")
    lines.append("")
    if experience_text.strip():
        lines.append(experience_text.strip())
    elif lessons_text.strip():
        lines.append(lessons_text.strip())
    else:
        lines.append("(chưa có bài học nào được duyệt cho repo này)")
    lines.append("")
    lines.append("## Bản đồ code")
    lines.append(codemap_summary_line(codemap))
    lines.append("")
    lines.append("## AGENTS.md")
    if agents_md.get("present"):
        lines.append(f"Repo có AGENTS.md ({agents_md.get('chars', 0)} ký tự)"
                     + (" — hiển thị tối đa "
                        f"{AGENTS_MD_MAX_CHARS} ký tự." if agents_md.get("truncated") else "."))
    else:
        lines.append("Repo chưa có AGENTS.md.")
    lines.append("")
    lines.append("## Tài liệu kiến thức gắn môi trường")
    docs = knowledge_docs or []
    if docs:
        lines += [f"- {doc.get('title') or doc.get('doc_id')}" for doc in docs]
    else:
        lines.append("Không có tài liệu nào gắn môi trường này.")
    lines.append("")
    lines.append("## Task gần đây")
    tasks = recent_tasks or []
    if tasks:
        for task in tasks[:5]:
            goal = str(task.get("goal") or "").strip().replace("\n", " ")
            goal = (goal[:80] + "…") if len(goal) > 80 else goal
            lines.append(f"- {task.get('task_id')} — {task.get('state')}"
                         + (f" — {goal}" if goal else ""))
    else:
        lines.append("Chưa có task nào trong môi trường này.")
    return "\n".join(lines) + "\n"
