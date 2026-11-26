"""The secretary: a standing OpenCode chat bubble in Workspace + the 00:00 daily report.

The secretary is one fixed OpenCode session (named "Thư ký") living in its own folder
``<state_dir>/secretary/`` with its own ``opencode.json`` — never the main chat, never a
per-task review assistant. Its history stays so the owner can re-read old reports and keep
asking follow-ups like a chat. Once a day, at 00:00 Asia/Bangkok, the scheduler engine
(worker (a)'s ``schedules.py``; this module does NOT own the engine) calls the built-in job
``secretary_daily_report`` (:func:`daily_report_job`), which builds the report for the
Bangkok day that just ended, saves it under ``secretary/reports/YYYY-MM-DD.md``, posts it
into the secretary session, and sends ONE short ntfy summary.

Two deliberate corrections live here, both from the plan:

- Spend is regrouped by **Bangkok day** from the raw spend rows (``SpendStore.records()``
  or the journal's ``spend`` records). ``SpendLedger`` buckets by UTC day (``relay.py``:
  ``_day`` uses ``time.gmtime``) — that bucketing is NOT used here.
- The 00:00 ntfy is a single summary (a few numbers + ids, no code, no goal text), the
  same discipline as ``notify.task_message``. Each item that needs a decision already got
  its own ntfy the moment it appeared (worker (c)'s ``needs_decision`` scan); this message
  never repeats them one by one.

``needs_decision.py`` (worker (c)) does not exist yet at the time this module was written:
the import below is guarded, so the report still builds with an empty "needs decision"
section and a note saying the scan was unavailable. Once worker (c) lands the module with
the contract signature ``scan_needs_decision(store, journal, state_dir, ledger, *,
near_cap_pct=80)``, the wiring (``run_daily_report`` / the API layer) passes a ``scan_fn``
closure around it; no change is needed in this module.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

# Contract: the built-in job name the scheduler engine registers (schedules.py, worker (a)).
JOB_NAME = "secretary_daily_report"
BANGKOK = ZoneInfo("Asia/Bangkok")

SECRETARY_SESSION_NAME = "Thư ký"
SECRETARY_DIRNAME = "secretary"
REPORTS_DIRNAME = "reports"
SESSION_FILENAME = "session.json"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

try:  # worker (c)'s module; absent until it lands — see the module docstring.
    from .needs_decision import scan_needs_decision as _contract_scan
except Exception:  # pragma: no cover - depends on a sibling worker's file
    # Any failure here (missing file, broken sibling import) only means the scan is
    # unavailable; the secretary itself must still import and build reports.
    _contract_scan = None

SECRETARY_PROMPT = """# Thư ký của Kite

Bạn là thư ký riêng của chủ máy trong Kite. Luôn trả lời bằng **tiếng Việt**.

## Vai của bạn

- Chỉ tư vấn và báo cáo: tóm tắt việc trong ngày, đọc lại báo cáo cũ, nhắc việc đang chờ
  duyệt, cảnh báo đã chạm. Bạn **không tự quyết** việc gì thay chủ máy: không giao task,
  không áp patch, không commit, không bật/tắt worker, không đổi cấu hình.
- Chỉ nói sự thật từ trạng thái Kite mà bạn đọc được (báo cáo ngày trong thư mục này,
  trạng thái task, sổ chi). Chỗ nào không có dữ liệu thì nói rõ là không có, đừng đoán.
- Báo cáo ngày nằm trong thư mục này (`reports/YYYY-MM-DD.md`): khi chủ máy hỏi về một
  ngày, đọc đúng file của ngày đó rồi trả lời.

## Cách dùng tool

Phần dưới đây là hướng dẫn tool của agent trưởng Kite (đọc trạng thái, tra cứu task). Bạn
dùng đúng các tool đọc/truy vấn đó để trả lời; các tool giao việc/áp/commit chỉ dùng khi
chủ máy yêu cầu rõ ràng từng việc, và vẫn phải chờ chủ máy duyệt từng bước.
"""


# ---------------------------------------------------------------------- Bangkok days
def bangkok_day_bounds(day: date) -> tuple[float, float]:
    """Epoch (start, end) of one Bangkok calendar day: [00:00, 00:00 next day)."""
    start = datetime.combine(day, dtime(0, 0), tzinfo=BANGKOK).timestamp()
    return start, start + 86400.0


def bangkok_date_of(ts: float) -> date:
    """The Bangkok calendar date an epoch timestamp falls on."""
    return datetime.fromtimestamp(ts, tz=BANGKOK).date()


def parse_report_date(value: str) -> date | None:
    """Strict ``YYYY-MM-DD`` -> date, or None (also rejects impossible dates)."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def default_report_date(now: datetime | None = None) -> date:
    """The day a 00:00 run reports on: the Bangkok day that just ended (yesterday)."""
    today = (now or datetime.now(BANGKOK)).astimezone(BANGKOK).date()
    return today - timedelta(days=1)


# ---------------------------------------------------------------------- spend, regrouped by Bangkok day
def spend_for_bangkok_day(records: list[dict], day: date) -> tuple[float, dict[str, float]]:
    """(total USD, per-worker USD) of spend rows whose ``ts`` falls on Bangkok ``day``.

    Rows are the journal-shaped ``{worker, usd, ts}`` records from ``SpendStore.records()``
    (or the journal's ``spend`` lines). Malformed rows are skipped. This regroups from the
    raw rows on purpose: ``SpendLedger`` buckets by UTC day and must not be reused here.
    """
    start, end = bangkok_day_bounds(day)
    by_worker: dict[str, float] = {}
    for record in records or []:
        worker, usd, ts = record.get("worker"), record.get("usd"), record.get("ts")
        if not isinstance(worker, str) or not isinstance(usd, (int, float)) \
                or not isinstance(ts, (int, float)) or isinstance(usd, bool) or isinstance(ts, bool):
            continue
        if start <= float(ts) < end:
            by_worker[worker] = by_worker.get(worker, 0.0) + float(usd)
    return round(sum(by_worker.values()), 6), {w: round(v, 6) for w, v in sorted(by_worker.items())}


# ---------------------------------------------------------------------- tasks done / failed in a Bangkok day
def classify_task_runs(runs: list[dict], day: date) -> tuple[list[dict], list[dict]]:
    """(done, failed) task runs whose ``ts`` falls on Bangkok ``day``, one entry per task.

    Source: the journal's ``tasks`` records (``{task_id, status, ts}``) — the task store has
    no completion timestamp (``store.py`` only records ``created_at``), so the journal is
    the only place a finish time exists. A task with several runs that day counts once,
    by its latest run; ``status == "error"`` is failed, anything else is done.
    """
    start, end = bangkok_day_bounds(day)
    latest: dict[str, dict] = {}
    for run in runs or []:
        task_id, ts = run.get("task_id"), run.get("ts")
        if not isinstance(task_id, str) or not task_id or not isinstance(ts, (int, float)) \
                or isinstance(ts, bool):
            continue
        if start <= float(ts) < end and (task_id not in latest or float(ts) >= float(latest[task_id]["ts"])):
            latest[task_id] = {"task_id": task_id, "status": run.get("status"),
                               "reason": run.get("reason") or run.get("status"), "ts": float(ts)}
    done = [r for r in latest.values() if r["status"] != "error"]
    failed = [r for r in latest.values() if r["status"] == "error"]
    key = lambda r: r["task_id"]
    return sorted(done, key=key), sorted(failed, key=key)


# ---------------------------------------------------------------------- report data + markdown
def collect_daily_data(*, day: date, runs: list[dict] | None = None,
                       spend_records: list[dict] | None = None,
                       needs_decision_items: list[dict] | None = None,
                       needs_decision_available: bool = True) -> dict:
    """Pure aggregation for one Bangkok day: tasks, spend, items needing a decision."""
    done, failed = classify_task_runs(runs or [], day)
    total_usd, by_worker = spend_for_bangkok_day(spend_records or [], day)
    items = list(needs_decision_items or [])
    by_kind: dict[str, int] = {}
    for item in items:
        kind = str(item.get("kind") or "khác")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"date": day.isoformat(), "done": done, "failed": failed,
            "spend_total_usd": total_usd, "spend_by_worker": by_worker,
            "needs_decision": items, "needs_decision_by_kind": by_kind,
            "needs_decision_available": bool(needs_decision_available)}


def build_daily_report(data: dict, *, day: date | None = None) -> str:
    """Markdown for one Bangkok day, from :func:`collect_daily_data` output."""
    day = day or date.fromisoformat(data["date"])
    label = day.strftime("%d/%m/%Y")
    done, failed = data["done"], data["failed"]
    lines = [f"# Báo cáo ngày {label}", "",
             f"Ngày Bangkok {day.isoformat()} (00:00–24:00 Asia/Bangkok).", "",
             "## Task",
             f"- Xong: {len(done)}",
             f"- Hỏng: {len(failed)}"]
    for run in done:
        lines.append(f"  - {run['task_id']} (xong)")
    for run in failed:
        reason = run.get("reason") or run.get("status") or ""
        lines.append(f"  - {run['task_id']} (hỏng: {reason})")
    lines += ["", "## Chi phí",
              f"- Tổng chi ngày: ${data['spend_total_usd']:.4f}"]
    for worker, usd in data["spend_by_worker"].items():
        lines.append(f"  - {worker}: ${usd:.4f}")
    if not data["spend_by_worker"]:
        lines.append("  - (không có khoản chi nào trong ngày)")
    lines += ["", "## Patch chờ duyệt và cảnh báo"]
    if not data["needs_decision_available"]:
        lines.append("- (chưa quét được mục cần quyết: module needs_decision chưa có)")
    elif not data["needs_decision"]:
        lines.append("- Không có mục nào chờ duyệt, không có cảnh báo đã chạm.")
    else:
        for kind, count in sorted(data["needs_decision_by_kind"].items()):
            lines.append(f"- {kind}: {count}")
        for item in data["needs_decision"]:
            task = f" (task {item['task_id']})" if item.get("task_id") else ""
            lines.append(f"  - {item.get('title') or item.get('kind')}{task}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- report files (atomic, like memory.py)
def secretary_dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / SECRETARY_DIRNAME


def reports_dir(state_dir: str | Path) -> Path:
    return secretary_dir(state_dir) / REPORTS_DIRNAME


def save_report(state_dir: str | Path, day: date, markdown: str) -> Path:
    """Write ``secretary/reports/YYYY-MM-DD.md`` atomically (.tmp + os.replace)."""
    folder = reports_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day.isoformat()}.md"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(markdown, encoding="utf-8")
    os.replace(temp, path)
    return path


def report_title(markdown: str, day: date) -> str:
    """First ``# ...`` heading, or the fallback ``Báo cáo ngày dd/mm/yyyy``."""
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return f"Báo cáo ngày {day.strftime('%d/%m/%Y')}"


def list_reports(state_dir: str | Path) -> list[dict]:
    """``[{date, title}]`` for every saved report, newest first."""
    folder = reports_dir(state_dir)
    out = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("????-??-??.md"), reverse=True):
        day = parse_report_date(path.stem)
        if day is None:
            continue
        try:
            title = report_title(path.read_text(encoding="utf-8"), day)
        except OSError:
            continue
        out.append({"date": day.isoformat(), "title": title})
    return out


def load_report(state_dir: str | Path, day: str | date) -> str | None:
    """Markdown of one saved report, or None (unknown date / bad date / unreadable)."""
    parsed = day if isinstance(day, date) else parse_report_date(day)
    if parsed is None:
        return None
    try:
        return (reports_dir(state_dir) / f"{parsed.isoformat()}.md").read_text(encoding="utf-8")
    except OSError:
        return None


# ---------------------------------------------------------------------- the one ntfy summary (numbers + ids only)
def daily_report_message(day: date, data: dict) -> tuple[str, str, tuple[str, ...], int]:
    """(title, body, tags, priority) for the single 00:00 ntfy — no code, no goal text.

    Same discipline as ``notify.task_message``: only counts, ids and amounts.
    """
    title = f"Kite: báo cáo ngày {day.strftime('%d/%m/%Y')}"
    pending = len(data["needs_decision"])
    body = "\n".join([
        f"Task xong: {len(data['done'])}, hỏng: {len(data['failed'])}",
        f"Chi ngày: ${data['spend_total_usd']:.2f}",
        f"Patch chờ duyệt/cảnh báo: {pending}",
        "Chi tiết trong chat Thư ký.",
    ])
    return title, body, ("bar_chart",), 3


def send_daily_report(notifier: Any, day: date, data: dict) -> bool:
    """Send the summary through a ``Notifier`` (None/off: no-op, returns False)."""
    if notifier is None or not bool(notifier):
        return False
    title, message, tags, priority = daily_report_message(day, data)
    notifier(title, message, tags=tags, priority=priority)
    return True


# ---------------------------------------------------------------------- secretary session folder
def resolve_lead_model(models: list[str], preferred: str | None = None) -> str:
    """Lead model, same rule as ``kite chat setup`` (cli.py): the preferred model, else
    ``qwen3.6-fast`` when served, else the first served model. No models: ValueError."""
    if preferred is not None:
        if preferred not in models:
            raise ValueError(f"model {preferred} is not served by the gateway")
        return preferred
    if not models:
        raise ValueError("no enabled pull worker serves a model")
    return "qwen3.6-fast" if "qwen3.6-fast" in models else models[0]


def ensure_secretary_dir(*, config: Any, state_dir: str | Path, lead_model: str | None = None,
                         kite_command: list[str] | None = None, config_path: str | Path | None = None,
                         allowed_roots: list[str | Path] | None = None,
                         force: bool = False) -> Path:
    """Create ``<state_dir>/secretary/`` with its own ``opencode.json`` + ``AGENTS.md``.

    Written through ``mcp_server.chat_files`` (the same code path as ``kite chat setup``,
    cli.py ``_chat_command``) — not a hand-built config: the MCP server command, the lead
    model, the permission denials and the preferences block all come from there. The only
    difference from a normal chat folder is the AGENTS.md: the secretary role prompt
    (``SECRETARY_PROMPT``) is prepended, so the role wins over the lead-agent instructions
    that follow. ``allowed_roots`` defaults to the secretary folder itself, so the
    secretary can read its own reports and nothing else. OpenCode only reads a project's
    ``opencode.json`` inside a Git repository (cli.py), so the folder is ``git init``-ed
    when it has no ``.git`` yet. Existing files are kept unless ``force`` is given.
    ``lead_model`` defaults to the configured lead (same rule as ``chat setup``);
    ``kite_command`` defaults to the ``kite --config <config_path> mcp serve ...`` command
    ``chat setup`` writes. Returns the folder path.
    """
    from .mcp_server import chat_files, gateway_models

    folder = secretary_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    roots = [Path(r).expanduser().resolve() for r in (allowed_roots or [folder])]
    lead = resolve_lead_model(gateway_models(config), lead_model)
    if kite_command is None:
        if config_path is None:
            raise ValueError("ensure_secretary_dir cần kite_command hoặc config_path")
        from .cli import _kite_executable

        kite_command = [*_kite_executable(), "--config", str(Path(config_path).expanduser().resolve()),
                        "mcp", "serve", "--lead-model", lead, "--chat-dir", str(folder)]
    files = chat_files(config, lead_model=lead, kite_command=list(kite_command),
                       allowed_roots=roots, state_dir=Path(state_dir))
    agents = SECRETARY_PROMPT.rstrip("\n") + "\n\n---\n\n" + files["AGENTS.md"]
    for name, content in (("opencode.json", files["opencode.json"]), ("AGENTS.md", agents)):
        target = folder / name
        if target.exists() and not force:
            continue
        target.write_text(content, encoding="utf-8")
    if not (folder / ".git").exists():
        import subprocess

        subprocess.run(["git", "init", "-q", str(folder)], check=True)
    return folder


def save_secretary_session_id(state_dir: str | Path, session_id: str) -> Path:
    """Persist the standing secretary OpenCode session id (atomic write)."""
    folder = secretary_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SESSION_FILENAME
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump({"session_id": session_id, "name": SECRETARY_SESSION_NAME}, stream,
                  ensure_ascii=False)
    os.replace(temp, path)
    return path


def load_secretary_session_id(state_dir: str | Path) -> str | None:
    """The persisted secretary session id, or None when there is none yet."""
    try:
        data = json.loads((secretary_dir(state_dir) / SESSION_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    session_id = data.get("session_id") if isinstance(data, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


# ---------------------------------------------------------------------- wiring: build + save + notify (+ post)
def run_daily_report(state_dir: str | Path, *, day: date | str | None = None,
                     runs: list[dict] | None = None, spend_records: list[dict] | None = None,
                     journal: Any = None, scan_fn: Callable[[], list[dict]] | None = None,
                     notifier: Any = None,
                     post_to_session: Callable[[Path, str], bool] | None = None) -> dict:
    """Build the report for one Bangkok day and deliver it. Returns a result dict.

    ``day`` defaults to the Bangkok day that just ended (yesterday). ``runs`` are the
    journal's ``tasks`` records and ``spend_records`` the ``{worker, usd, ts}`` rows; when
    they are not passed, they are read from ``journal`` (``Journal.read("tasks")`` /
    ``read("spend")``) and from ``SpendStore(<state_dir>/spend.sqlite3)`` when that file
    exists (SpendStore is the durable spend source; the journal lines are its fallback).
    ``scan_fn`` is a zero-arg closure around the contract
    ``scan_needs_decision(store, journal, state_dir, ledger, *, near_cap_pct=...)`` —
    the caller owns the store/ledger because this module must not create them. When no
    scan is supplied and the contract module is unavailable, the report notes that the
    scan was unavailable instead of failing. ``post_to_session`` (injected by the API
    layer) posts the markdown into the secretary OpenCode session; a failure there never
    fails the report — it is reported as ``posted: False``.
    """
    if day is None:
        report_day = default_report_date()
    elif isinstance(day, str):
        report_day = parse_report_date(day)
        if report_day is None:
            raise ValueError(f"ngày báo cáo phải dạng YYYY-MM-DD, nhận được {day!r}")
    else:
        report_day = day
    if runs is None and journal is not None:
        runs = journal.read("tasks")
    if spend_records is None:
        spend_records = _read_spend_records(state_dir, journal)
    if scan_fn is None:
        # No store/ledger is owned here, so the contract scan cannot be called without
        # a caller-supplied closure; the report notes the scan was unavailable.
        items, available = [], False
    else:
        items, available = list(scan_fn() or []), True
    data = collect_daily_data(day=report_day, runs=runs, spend_records=spend_records,
                              needs_decision_items=items, needs_decision_available=available)
    markdown = build_daily_report(data, day=report_day)
    path = save_report(state_dir, report_day, markdown)
    notified = send_daily_report(notifier, report_day, data)
    posted = False
    post_error: str | None = None
    if post_to_session is not None:
        try:
            posted = bool(post_to_session(secretary_dir(state_dir), markdown))
        except Exception as exc:  # posting must never fail the report itself
            post_error = f"{type(exc).__name__}: {exc}"
    return {"date": report_day.isoformat(), "path": str(path), "data": data,
            "notified": notified, "posted": posted, "post_error": post_error,
            "needs_decision_available": available}


def _read_spend_records(state_dir: str | Path, journal: Any) -> list[dict]:
    """Spend rows: the durable SpendStore first, the journal's spend lines as fallback."""
    spend_path = Path(state_dir) / "spend.sqlite3"
    if spend_path.is_file():
        try:
            from .spend_ledger import SpendStore

            store = SpendStore(spend_path)
            try:
                return store.records()
            finally:
                store.close()
        except Exception:
            pass  # fall through to the journal lines
    if journal is not None:
        return journal.read("spend")
    return []


def daily_report_job(state_dir: str | Path, *, notifier: Any = None,
                     scan_fn: Callable[[], list[dict]] | None = None,
                     post_to_session: Callable[[Path, str], bool] | None = None) -> dict:
    """Built-in job ``secretary_daily_report`` for the scheduler engine's job registry
    (``schedules.py`` ``register_builtin``; the wiring — agent trưởng, in ``app.py`` /
    ``cli.py`` — registers it as ``register_builtin("secretary_daily_report",
    daily_at_bangkok("00:00"), func)`` with ``func`` a zero-arg closure around this
    function, because the engine calls ``job.func()`` with no arguments). The 00:00
    Asia/Bangkok run reports on the Bangkok day that just ended. The engine owns the
    schedule, the store/ledger behind ``scan_fn`` and the OpenCode poster behind
    ``post_to_session``; this function only builds and delivers the report."""
    return run_daily_report(state_dir, notifier=notifier, scan_fn=scan_fn,
                            post_to_session=post_to_session)


# Re-export for the API layer / engine: the contract scan when worker (c) has landed it.
__all__ = ["JOB_NAME", "BANGKOK", "SECRETARY_SESSION_NAME", "SECRETARY_PROMPT",
           "bangkok_day_bounds", "bangkok_date_of", "parse_report_date", "default_report_date",
           "spend_for_bangkok_day", "classify_task_runs", "collect_daily_data",
           "build_daily_report", "secretary_dir", "reports_dir", "save_report", "report_title",
           "list_reports", "load_report", "daily_report_message", "send_daily_report",
           "resolve_lead_model", "ensure_secretary_dir", "save_secretary_session_id",
           "load_secretary_session_id", "run_daily_report", "daily_report_job"]
