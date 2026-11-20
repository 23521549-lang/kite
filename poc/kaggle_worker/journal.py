"""Kite's journal: append-only JSONL records of what happened, to trace a task later and improve Kite.

Files under ``<state>/journal/`` (each has exactly one writer process):

- ``gateway.jsonl``  one line per model request (gateway): who asked (``X-Kite-Task`` tag), model, worker,
                     queue and run seconds, tokens, speed, outcome. Prompts and answers are NOT stored.
- ``tasks.jsonl``    one line per finished agent run (workflow worker): route, steps, fix rounds, self-review,
                     Kite's test result, patch size, verdict.
- ``actions.jsonl``  one line per lead-agent tool call (MCP server): tool, arguments, result or error.
- ``notes.jsonl``    the user's own verdict on a task: ``kite journal note TASK --bad "why"``.

Per task, ``<state>/tasks/<slug>/trace.jsonl`` records every step as it happens.

Writing never breaks Kite: an unwritable journal prints one warning and is skipped.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Iterable

TAG = re.compile(r"[A-Za-z0-9._:-]{1,120}")
FILES = ("gateway", "tasks", "actions", "notes")


def clean_tag(value: str | None) -> str | None:
    """A request tag from a header, or None when missing or malformed."""
    if not value:
        return None
    value = value.strip()
    return value if TAG.fullmatch(value) else None


def iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(timestamp))


def short(value: Any, limit: int = 300) -> Any:
    """Shorten long strings (and strings inside lists/dicts) so one record stays small."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"... (+{len(value) - limit})"
    if isinstance(value, dict):
        return {k: short(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [short(v, limit) for v in value[:50]]
    return value


_warned: set[str] = set()


def append_jsonl(path: Path, record: dict, *, clock: Callable[[], float] = time.time) -> bool:
    """Append one record (with ``ts`` and ``at``); False, with a single warning, if it cannot be written."""
    line = {"ts": round(clock(), 3), "at": iso(clock()), **record}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        return True
    except OSError as exc:
        if str(path) not in _warned:
            _warned.add(str(path))
            print(f"kite journal: cannot write {path}: {exc}", file=sys.stderr)
        return False


def read_jsonl(path: Path) -> list[dict]:
    """Every well-formed record of a JSONL file (missing file: empty list; broken lines are skipped)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


class Journal:
    """The journal folder of one Kite state directory."""

    def __init__(self, root: Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root) if root is not None else None
        self.clock = clock

    def path(self, name: str) -> Path:
        assert name in FILES, name
        return self.root / f"{name}.jsonl"

    def write(self, name: str, record: dict) -> None:
        if self.root is not None:
            append_jsonl(self.path(name), record, clock=self.clock)

    def read(self, name: str, since: float | None = None) -> list[dict]:
        if self.root is None:
            return []
        records = read_jsonl(self.path(name))
        return [r for r in records if since is None or r.get("ts", 0) >= since]


# ---------------------------------------------------------------------- gateway records
def request_record(*, job_id: str, tag: str | None, requested: str, model: str, worker: str | None,
                   pinned: str | None = None,
                   attempts: int, created_at: float, assigned_at: float | None, finished_at: float,
                   body: dict, result: dict | None, error: str | None) -> dict:
    """One gateway line: everything about a model request except its content."""
    record: dict[str, Any] = {
        "job_id": job_id, "tag": tag, "requested": requested, "model": model, "worker": worker, "pinned": pinned,
        "attempts": attempts, "outcome": "ok" if error is None else "error",
        "queue_s": round((assigned_at or finished_at) - created_at, 2),
        "run_s": round(finished_at - assigned_at, 2) if assigned_at else None,
        "messages": len(body.get("messages") or []), "tools": len(body.get("tools") or []),
        "max_tokens": body.get("max_tokens"),
    }
    if error is not None:
        record["error"] = short(error, 300)
    if result:
        usage = result.get("usage") or {}
        timings = result.get("timings") or {}
        choices = result.get("choices") or [{}]
        message = (choices[0] or {}).get("message") or {}
        record.update({
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "cached_tokens": timings.get("cache_n"),
            "prompt_tps": round(timings["prompt_per_second"], 1) if timings.get("prompt_per_second") else None,
            "gen_tps": round(timings["predicted_per_second"], 1) if timings.get("predicted_per_second") else None,
            "finish_reason": (choices[0] or {}).get("finish_reason"),
            "tool_calls": [c.get("function", {}).get("name") for c in message.get("tool_calls") or []],
            # Where the generated tokens went (sizes only, never the text), and how much speculative
            # decoding drafted and the model accepted.
            "reasoning_chars": len(message.get("reasoning_content") or ""),
            "content_chars": len(message.get("content") or ""),
            "tool_args_chars": sum(len(str((c.get("function") or {}).get("arguments") or ""))
                                   for c in message.get("tool_calls") or []),
            "draft_n": timings.get("draft_n"), "draft_accepted": timings.get("draft_n_accepted"),
        })
    return record


# ---------------------------------------------------------------------- reading it back
def task_reason(run: dict) -> str:
    """Why a run ended the way it did, in one word."""
    if run.get("status") == "error":
        return "error"
    if run.get("agent_status") not in (None, "ok"):
        return f"agent_{run.get('agent_status')}"
    if run.get("test_exit") not in (None, 0):
        return "tests_failed"
    if not run.get("changed_files") and run.get("task_type", "coding") == "coding":
        return "no_changes"
    return "verified" if run.get("verified") else "done"


def _avg(values: Iterable[float | None]) -> float | None:
    values = [v for v in values if isinstance(v, (int, float))]
    return round(sum(values) / len(values), 1) if values else None


def _p90(values: Iterable[float | None]) -> float | None:
    values = sorted(v for v in values if isinstance(v, (int, float)))
    return values[min(len(values) - 1, int(len(values) * 0.9))] if values else None


def _source(tag: str | None) -> str:
    if not tag:
        return "untagged"
    if tag == "chat":
        return "chat"
    return "bench" if tag.startswith("bench-") else "task"


def summarize(journal: Journal, *, since: float | None = None) -> dict:
    """Success rates, time and tokens per model and task type, failure reasons, gateway health."""
    runs = journal.read("tasks", since)
    actions = journal.read("actions", since)
    requests = journal.read("gateway", since)
    notes = journal.read("notes", since)

    def group(key: str) -> dict:
        out = {}
        buckets: dict[str, list[dict]] = defaultdict(list)
        for run in runs:
            buckets[str(run.get(key))].append(run)
        for name, items in sorted(buckets.items()):
            out[name] = {
                "runs": len(items), "verified": sum(bool(r.get("verified")) for r in items),
                "avg_seconds": _avg(r.get("seconds") for r in items),
                "avg_fix_rounds": _avg(r.get("fix_rounds") for r in items),
                "review": dict(Counter(r.get("review", "skipped") for r in items)),
            }
        return out

    applied = {a.get("task_id") for a in actions if a.get("tool") == "apply_result" and a.get("ok")}
    committed = {a.get("task_id") for a in actions if a.get("tool") == "commit_result" and a.get("ok")}
    tool_errors = Counter(a.get("tool") for a in actions if not a.get("ok"))
    by_model: dict[str, list[dict]] = defaultdict(list)
    for request in requests:
        by_model[str(request.get("model"))].append(request)
    return {
        "since": iso(since) if since else None,
        "tasks": {
            "runs": len(runs),
            "reasons": dict(Counter(task_reason(r) for r in runs)),
            "by_model": group("model"), "by_task_type": group("task_type"),
            "applied": len(applied - {None}), "committed": len(committed - {None}),
            "user_notes": dict(Counter(n.get("verdict") for n in notes)),
            "recent_failures": [
                {"task_id": r.get("task_id"), "model": r.get("model"), "reason": task_reason(r), "at": r.get("at")}
                for r in runs if task_reason(r) not in ("verified", "done")][-10:],
        },
        "lead_agent": {"tool_calls": dict(Counter(a.get("tool") for a in actions)),
                       "tool_errors": dict(tool_errors)},
        "gateway": {
            "requests": len(requests),
            "by_source": dict(Counter(_source(r.get("tag")) for r in requests)),
            "errors": dict(Counter(r.get("error", "?")[:80] for r in requests if r.get("outcome") != "ok")),
            "by_model": {model: {
                "requests": len(items),
                "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in items),
                "completion_tokens": sum(r.get("completion_tokens") or 0 for r in items),
                "avg_queue_s": _avg(r.get("queue_s") for r in items),
                "avg_run_s": _avg(r.get("run_s") for r in items), "p90_run_s": _p90(r.get("run_s") for r in items),
                "avg_prompt_tps": _avg(r.get("prompt_tps") for r in items),
                "avg_gen_tps": _avg(r.get("gen_tps") for r in items),
                "truncated": sum(r.get("finish_reason") == "length" for r in items),
            } for model, items in sorted(by_model.items())},
        },
    }


def show_task(journal: Journal, task_id: str, trace_path: Path | None) -> dict:
    """Everything recorded about one task: runs, step trace, model requests, lead-agent actions, notes."""
    requests = [r for r in journal.read("gateway") if r.get("tag") == task_id]
    actions = [a for a in journal.read("actions")
               if a.get("task_id") == task_id or (a.get("args") or {}).get("task_id") == task_id]
    return {
        "task_id": task_id,
        "runs": [r for r in journal.read("tasks") if r.get("task_id") == task_id],
        "trace": read_jsonl(trace_path) if trace_path else [],
        "requests": {
            "count": len(requests),
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in requests),
            "completion_tokens": sum(r.get("completion_tokens") or 0 for r in requests),
            "model_seconds": round(sum(r.get("run_s") or 0 for r in requests), 1),
            "queue_seconds": round(sum(r.get("queue_s") or 0 for r in requests), 1),
            "errors": [r.get("error") for r in requests if r.get("outcome") != "ok"],
            "items": requests[-200:],
        },
        "actions": actions,
        "notes": [n for n in journal.read("notes") if n.get("task_id") == task_id],
    }
