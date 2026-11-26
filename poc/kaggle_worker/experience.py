"""Chưng cất bài học theo repo thành tài liệu kinh nghiệm (Gói 10).

Engine xác định, KHÔNG gọi model: chỉ áp luật lên dữ liệu thô đã đọc
(bài học từ ``memory.lesson_lines``, ghi chú ``notes.jsonl``, lần chạy
``tasks.jsonl``). Vì vậy kết quả kiểm được bằng máy và không tốn chi phí.

Luồng dùng: worker (b) đọc dữ liệu thô rồi gọi :func:`distill`, sau đó
gọi :func:`render_document` để ra Markdown lưu ở
``state_dir/memory/<repo_key>.experience.md`` (định dạng xem hợp đồng
Gói 10 trong ``HOP-DONG-GIAO-TIEP.md``).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

KINDS = ("do", "dont", "pattern", "convention")

# Từ không mang nội dung (chỉ dùng khi xét "bài học bị thực tế bác bỏ"):
# toàn từ chức năng/chung chung, tiếng Việt và tiếng Anh. "task"/"repo"
# là định danh, không phải nội dung; "test"/"code" vẫn tính là nội dung.
STOPWORDS = frozenset({
    # tiếng Việt
    "các", "những", "nhưng", "được", "trong", "ngoài", "trước", "sau",
    "khi", "nếu", "thì", "với", "cho", "của", "này", "đó", "việc",
    "cách", "rằng", "rất", "cũng", "đang", "phải", "luôn", "thường",
    "nhiều", "người", "dùng", "không", "chưa", "thêm", "cùng", "giữa",
    "trên", "dưới", "vào", "đến", "bị", "nữa", "hơn",
    # tiếng Anh
    "that", "this", "with", "from", "when", "should", "would", "could",
    "about", "after", "before", "there", "here", "have", "been", "were",
    "what", "which", "your", "yours", "them", "then", "than", "also",
    "into", "over", "under", "only", "just", "task", "repo",
})

_TRAILING_PUNCT = ".,;:!?…。)]}»”’"
_WORD = re.compile(r"\w+", re.UNICODE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_text(text: str) -> str:
    """Chuẩn hóa để gộp trùng: chữ thường, gấp khoảng trắng, bỏ dấu câu cuối."""
    return " ".join(str(text).split()).lower().rstrip(_TRAILING_PUNCT)


def _run_date_by_task(runs: list[dict]) -> dict[str, str | None]:
    """Ngày ``at`` mới nhất theo từng task_id (fallback khi bài học thiếu ngày)."""
    dates: dict[str, str | None] = {}
    for run in runs or []:
        task_id = run.get("task_id")
        if task_id:
            dates[str(task_id)] = _valid_date(str(run.get("at") or "")[:10])
    return dates


def _mark_refuted(rules: list[dict], notes: list[dict], repos: dict[str, str | None]) -> None:
    """Gắn cờ ``refuted`` cho rule có bài học bị thực tế bác bỏ.

    Một bài học (ngày D, task T, text L) bị bác bỏ khi có ghi chú ``bad``
    N thỏa cả ba: ngày của N LATER hơn D (so chuỗi ``YYYY-MM-DD``); N
    cùng repo với T (tra từ ``runs``; không tra được repo của bên nào thì
    coi như cùng repo vì ``distill`` vốn được gọi theo từng repo); text
    của N chia sẻ ít nhất 1 từ nội dung với L (:func:`_content_words`:
    dài >= 4 ký tự, bỏ stopword tiếng Việt/Anh cơ bản). Rule bị bác bỏ
    KHÔNG bị xóa — :func:`render_document` đưa nó vào mục riêng với cờ
    "cần xem lại".
    """
    bad_notes = []
    for note in notes:
        if note.get("verdict") != "bad":
            continue
        text = _clean_text(note.get("text", ""))
        date = _note_date(note)
        if not text or date is None:
            continue
        bad_notes.append((text, date, str(note["task_id"]) if note.get("task_id") else None))
    for rule in rules:
        refuted = False
        for lesson_text, lesson_date, lesson_task in rule.get("_lessons", []):
            if lesson_date is None:
                continue
            lesson_words = _content_words(lesson_text)
            lesson_repo = repos.get(lesson_task) if lesson_task else None
            for note_text, note_date, note_task in bad_notes:
                if note_date <= lesson_date:
                    continue
                note_repo = repos.get(note_task) if note_task else None
                if lesson_repo is not None and note_repo is not None and lesson_repo != note_repo:
                    continue
                if lesson_words & _content_words(note_text):
                    refuted = True
                    break
            if refuted:
                break
        rule["refuted"] = refuted


def _content_words(text: str) -> set[str]:
    """Từ nội dung của một câu: dài >= 4 ký tự, bỏ stopword cơ bản."""
    return {w for w in _WORD.findall(str(text).lower()) if len(w) >= 4 and w not in STOPWORDS}


def _clean_text(text: str) -> str:
    """Text gọn một dòng (gấp khoảng trắng); "" khi rỗng."""
    return " ".join(str(text or "").split())


def _valid_date(value: Any) -> str | None:
    """Ngày ``YYYY-MM-DD`` hợp lệ, hoặc None."""
    return str(value) if isinstance(value, str) and _DATE.match(value) else None


def _note_date(note: dict) -> str | None:
    """Ngày của một ghi chú: ``date`` rồi ``at`` (ISO), rồi ``ts`` (epoch)."""
    date = _valid_date(note.get("date")) or _valid_date(str(note.get("at") or "")[:10])
    if date:
        return date
    ts = note.get("ts")
    if isinstance(ts, (int, float)):
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    return None


def _repo_by_task(runs: list[dict]) -> dict[str, str | None]:
    """Repo mới nhất theo từng task_id (như ``memory.repo_of_task``)."""
    repos: dict[str, str | None] = {}
    for run in runs or []:
        task_id = run.get("task_id")
        if task_id:
            repos[str(task_id)] = str(run["repo"]) if run.get("repo") else None
    return repos


def _verdict_of_task(notes: list[dict], task_id: str | None) -> str | None:
    """Verdict của task: ``bad`` thắng ``good`` khi cả hai đều có."""
    verdicts = {str(n.get("verdict")) for n in notes or []
                if n.get("task_id") == task_id and n.get("verdict") in ("good", "bad")}
    if "bad" in verdicts:
        return "bad"
    if "good" in verdicts:
        return "good"
    return None


def _classify(verdicts: set[str | None], distinct_tasks: int) -> str:
    """Phân loại quy tắc từ verdict và số task khác nhau ghi cùng nội dung.

    bad trên >= 2 task -> ``pattern`` (mẫu lỗi hay lặp); bad trên 1 task
    -> ``dont``; good -> ``do``; không verdict mà lặp lại >= 2 task
    (bài học vốn sinh ra từ ghi chú bad) -> ``pattern``; còn lại là
    ``convention`` (quy ước repo).
    """
    if "bad" in verdicts:
        return "pattern" if distinct_tasks >= 2 else "dont"
    if "good" in verdicts:
        return "do"
    return "pattern" if distinct_tasks >= 2 else "convention"


def distill(lessons: list[dict], notes: list[dict], runs: list[dict],
            *, clock: Callable[[], float] = time.time) -> list[dict]:
    """Chưng cất dữ liệu thô thành quy tắc kinh nghiệm, KHÔNG gọi model.

    Inputs: ``lessons`` là ``{date, task_id, text}`` (dòng bài học theo
    repo); ``notes`` là ``{task_id, verdict, text}`` (ghi chú của chủ);
    ``runs`` là dict từ ``tasks.jsonl`` (chỉ dùng để tra repo theo task
    khi xét bác bỏ, xem :func:`_mark_refuted`). ``clock`` giữ cho khớp
    chữ ký với :func:`render_document`; ngày thiếu thì để None chứ
    không đoán (không kết luận được "sau" thì không gắn cờ bác bỏ).

    Mỗi rule trả về ``{text, kind, sources, refuted}``: ``kind`` trong
    ``{"do", "dont", "pattern", "convention"}`` (xem :func:`_classify`);
    ``sources`` là list ``{task_id, date, verdict}`` trích đúng nguồn
    gốc, theo thứ tự xuất hiện; ``refuted`` xem :func:`_mark_refuted`.
    Trùng xác định (chuẩn hóa bằng :func:`_normalize_text`) thì gộp:
    cộng dồn sources, giữ text của rule đầu tiên, giữ thứ tự xuất hiện.
    """
    repos = _repo_by_task(runs)
    run_dates = _run_date_by_task(runs)
    merged: dict[str, dict] = {}
    order: list[str] = []

    def add(text: str, task_id: str | None, date: str | None, verdict: str | None,
            from_lesson: bool) -> None:
        cleaned = _clean_text(text)
        if not cleaned:
            return
        key = _normalize_text(cleaned)
        source = {"task_id": task_id, "date": date, "verdict": verdict}
        if key not in merged:
            merged[key] = {"text": cleaned, "sources": [], "verdicts": set(),
                           "tasks": set(), "lesson_texts": []}
            order.append(key)
        entry = merged[key]
        if source not in entry["sources"]:
            entry["sources"].append(source)
        if verdict is not None:
            entry["verdicts"].add(verdict)
        if task_id:
            entry["tasks"].add(task_id)
        if from_lesson:
            entry["lesson_texts"].append((cleaned, date, task_id))

    for lesson in lessons or []:
        task_id = str(lesson["task_id"]) if lesson.get("task_id") else None
        date = _valid_date(lesson.get("date")) or (run_dates.get(task_id) if task_id else None)
        add(lesson.get("text", ""), task_id, date, _verdict_of_task(notes, task_id), True)
    for note in notes or []:
        verdict = str(note["verdict"]) if note.get("verdict") in ("good", "bad") else None
        add(note.get("text", ""), str(note["task_id"]) if note.get("task_id") else None,
            _note_date(note), verdict, False)

    rules = []
    for key in order:
        entry = merged[key]
        rules.append({"text": entry["text"],
                      "kind": _classify(entry["verdicts"], len(entry["tasks"])),
                      "sources": entry["sources"], "refuted": False,
                      "_lessons": entry["lesson_texts"]})
    _mark_refuted(rules, notes or [], repos)
    for rule in rules:
        del rule["_lessons"]
    return rules


# ---------------------------------------------------------------------- render
# Định dạng chốt trong hợp đồng Gói 10 (HOP-DONG-GIAO-TIEP.md): dòng đầu
# "# Kite: tài liệu kinh nghiệm — <repo> (vN, ngày)" + dòng marker version;
# bốn mục theo kind; rule bị bác bỏ CHỈ ở mục riêng "Cần xem lại" (không
# lặp lại ở mục kind của nó) với cờ "cần xem lại", không bị xóa.

_SECTIONS = (("do", "## Nên làm"), ("dont", "## Không nên làm"),
             ("pattern", "## Mẫu lỗi hay lặp"), ("convention", "## Quy ước repo"))
_REFUTED_HEADING = "## Cần xem lại"


def _rule_line(rule: dict) -> str:
    """Một quy tắc, một dòng: ``- <quy tắc> [nguồn: task <id> / bài học <date>]``.

    Cùng một task xuất hiện 2 lần trong sources (một lần từ dòng bài học có ngày,
    một lần từ ghi chú verdict không ngày) thì chỉ giữ một mảnh — mảnh có ngày.
    """
    by_task: dict[str | None, str] = {}
    order: list[str | None] = []
    for source in rule.get("sources") or []:
        parts = []
        if source.get("task_id"):
            parts.append(f"task {source['task_id']}")
        if source.get("date"):
            parts.append(f"bài học {source['date']}")
        fragment = " / ".join(parts) if parts else "không rõ nguồn"
        key = source.get("task_id")
        if key not in by_task:
            by_task[key] = fragment
            order.append(key)
        elif source.get("date") and "bài học" not in by_task[key]:
            by_task[key] = fragment
    fragments = [by_task[key] for key in order]
    text = _clean_text(rule.get("text", ""))
    line = f"- {text} [nguồn: {', '.join(fragments) if fragments else 'không rõ'}]"
    if rule.get("refuted"):
        line += " (cần xem lại)"
    return line


def render_document(repo: str, rules: list[dict], *, version: int, updated: Any = None,
                    clock: Callable[[], float] = time.time) -> str:
    """Xuất tài liệu kinh nghiệm Markdown cho ``repo`` theo định dạng đã chốt.

    ``version`` là số nguyên >= 1; ``updated`` là ngày ``YYYY-MM-DD``
    (None thì lấy ngày hiện tại từ ``clock``; nhận cả ``date``/``datetime``).
    Ngày/version sai định dạng hoặc rule có ``kind`` ngoài
    ``{"do", "dont", "pattern", "convention"}`` thì raise ``ValueError``
    thay vì ghi marker sai. Tài liệu rỗng (không có rule) vẫn render đủ
    khung bốn mục + ghi chú "chưa có bài học".
    """
    try:
        version = int(version)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"version phải là số nguyên >= 1, nhận {version!r}")
    if version < 1:
        raise ValueError(f"version phải là số nguyên >= 1, nhận {version!r}")
    if updated is None:
        date = time.strftime("%Y-%m-%d", time.localtime(clock()))
    elif hasattr(updated, "isoformat"):
        date = _valid_date(updated.isoformat()[:10])
    else:
        date = _valid_date(str(updated).strip())
    if date is None:
        raise ValueError(f"updated phải là ngày YYYY-MM-DD, nhận {updated!r}")
    for rule in rules or []:
        if rule.get("kind") not in KINDS:
            raise ValueError(f"kind không nhận: {rule.get('kind')!r}")

    lines = [f"# Kite: tài liệu kinh nghiệm — {_clean_text(repo)} (v{version}, {date})",
             f"<!-- kite:experience version={version} updated={date} -->"]
    if not rules:
        lines.append("")
        lines.append("Chưa có bài học nào được chưng cất cho repo này.")
    kept = [r for r in rules or [] if _clean_text(r.get("text", ""))]
    refuted = [r for r in kept if r.get("refuted")]
    for kind, heading in _SECTIONS:
        lines.append("")
        lines.append(heading)
        for rule in kept:
            if rule.get("kind") == kind and not rule.get("refuted"):
                lines.append(_rule_line(rule))
    if refuted:
        lines.append("")
        lines.append(_REFUTED_HEADING)
        lines.append("Các quy tắc dưới đây đã bị một nhận xét bad sau đó bác bỏ; "
                     "giữ lại để xem xét, không áp dụng máy móc.")
        for rule in refuted:
            lines.append(_rule_line(rule))
    return "\n".join(lines) + "\n"
