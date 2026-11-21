"""Tell the person when a task is done: a push notification through ntfy (ntfy.sh or a self-hosted one).

A task takes many minutes and the lead agent only answers when spoken to, so Kite itself sends one
short message to the person's phone when a task finishes. ntfy needs no account: the topic name,
random and kept in a 0600 secret file, is the only key, and whoever knows it can read the topic, so
the message carries no code and no goal text: only the task id, the outcome and the numbers.

Set up with ``kite notify setup`` (prints the topic to subscribe to in the ntfy app), check with
``kite notify test``. Sending never breaks a task: errors are printed once and dropped.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
from typing import Callable

DEFAULT_SERVER = "https://ntfy.sh"


def new_topic() -> str:
    return "kite-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24].lower()


def split_url(url: str) -> tuple[str, str]:
    """``https://ntfy.sh/kite-abc`` -> (``https://ntfy.sh``, ``kite-abc``)."""
    parsed = urllib.parse.urlsplit(url.strip())
    topic = parsed.path.strip("/")
    if parsed.scheme not in ("https", "http") or not parsed.netloc or not topic or "/" in topic:
        raise ValueError("ntfy URL must look like https://ntfy.sh/<topic>")
    return f"{parsed.scheme}://{parsed.netloc}", topic


def send(url: str, title: str, message: str, *, tags: tuple[str, ...] = (), priority: int = 3,
         opener: Callable = urllib.request.urlopen, timeout: float = 10) -> None:
    """Publish one message (JSON publishing: the title may hold any Unicode)."""
    server, topic = split_url(url)
    body = json.dumps({"topic": topic, "title": title[:250], "message": message[:3000], "tags": list(tags),
                       "priority": priority}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(server + "/", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with opener(request, timeout=timeout) as response:
        response.read()


class Notifier:
    """Sends in the background; ``None`` URL means notifications are off."""

    def __init__(self, url: str | None, *, sender: Callable[..., None] = send, background: bool = True):
        self.url, self.sender, self.background = url, sender, background
        self._warned = False

    def __bool__(self) -> bool:
        return bool(self.url)

    def __call__(self, title: str, message: str, *, tags: tuple[str, ...] = (), priority: int = 3) -> None:
        if not self.url:
            return

        def deliver() -> None:
            try:
                self.sender(self.url, title, message, tags=tags, priority=priority)
            except Exception as exc:          # a lost notification must never fail a task
                if not self._warned:
                    self._warned = True
                    print(f"kite notify: could not send ({type(exc).__name__}: {exc})", file=sys.stderr)

        if self.background:
            threading.Thread(target=deliver, name="kite-notify", daemon=True).start()
        else:
            deliver()


def task_message(run: dict) -> tuple[str, str, tuple[str, ...], int]:
    """Title, body, tags and priority for a finished task (no code, no goal text)."""
    minutes = round((run.get("wall_seconds") or run.get("seconds") or 0) / 60, 1)
    files = len(run.get("changed_files") or [])
    if run.get("status") == "error":
        return ("Kite: task dừng vì lỗi", f"{run.get('task_id')}\n{str(run.get('error') or '')[:300]}",
                ("warning",), 4)
    if run.get("verified"):
        title, tags = "Kite: task xong, test qua", ("white_check_mark",)
    else:
        title, tags = "Kite: task xong nhưng chưa qua test", ("warning",)
    lines = [str(run.get("task_id")),
             f"{files} file đổi, {minutes} phút, model {run.get('model')}"
             + (f", {round((run.get('gpu_seconds') or 0) / 60, 1)} phút GPU" if run.get("gpu_seconds") else ""),
             "Hỏi trong chat: kết quả task này?"]
    return title, "\n".join(lines), tags, 3
