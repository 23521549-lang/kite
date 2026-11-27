"""Small, retry-aware client for the local OpenCode HTTP API.

The session event log endpoint is currently experimental in OpenCode. Keep the
HTTP surface isolated here so it can be version-pinned or replaced centrally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import base64
import difflib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .temporal_workflow import CodingRunResult, CodingTaskInput, RouteLease

if TYPE_CHECKING:
    from .temporal_state import RecoveryDecision


class OpenCodeError(RuntimeError):
    """OpenCode returned an error or an invalid response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OpenCodeEvent:
    sequence: int
    type: str
    data: Mapping[str, object]


class OpenCodeClient:
    """HTTP client that defaults to an authenticated loopback server."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        *,
        username: str | None = None,
        password: str | None = None,
        bearer_token: str | None = None,
        allow_remote: bool = False,
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query, or fragment")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            if not allow_remote:
                raise ValueError("OpenCode endpoint must use loopback unless allow_remote is enabled")
            if parsed.scheme != "https":
                raise ValueError("remote OpenCode endpoints require HTTPS")
            if not (bearer_token or (username and password)):
                raise ValueError("remote OpenCode endpoints require authentication")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self.username = username
        self.password = password
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _stable_id(prefix: str, operation_key: str) -> str:
        if not operation_key.strip():
            raise ValueError("operation_key must be non-empty")
        return f"{prefix}_{hashlib.sha256(operation_key.encode('utf-8')).hexdigest()[:32]}"

    @classmethod
    def session_id_for_key(cls, operation_key: str) -> str:
        return cls._stable_id("ses", operation_key)

    @classmethod
    def message_id_for_key(cls, operation_key: str) -> str:
        return cls._stable_id("msg", operation_key)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        accept: str = "application/json",
    ) -> tuple[int, bytes, str]:
        headers = {"Accept": accept}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.username and self.password:
            encoded = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        req = Request(self.base_url + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise OpenCodeError(
                f"OpenCode HTTP {exc.code}: {detail[:1000]}", status_code=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OpenCodeError(f"OpenCode request failed: {exc}") from exc

    @staticmethod
    def _json(payload: bytes) -> object:
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OpenCodeError("OpenCode returned invalid JSON") from exc

    def health(self) -> dict[str, object]:
        _status, payload, _content_type = self._request("GET", "/api/info")
        value = self._json(payload)
        if not isinstance(value, dict):
            raise OpenCodeError("OpenCode info response must be an object")
        return value

    def get_session(self, session_id: str) -> dict[str, object]:
        _status, payload, _content_type = self._request(
            "GET", f"/api/session/{quote(session_id, safe='')}",
        )
        value = self._json(payload)
        if not isinstance(value, dict):
            raise OpenCodeError("OpenCode session response must be an object")
        return value

    def get_message(self, session_id: str, message_id: str) -> dict[str, object]:
        _status, payload, _content_type = self._request(
            "GET", f"/api/session/{quote(session_id, safe='')}/message/{quote(message_id, safe='')}",
        )
        value = self._json(payload)
        if not isinstance(value, dict):
            raise OpenCodeError("OpenCode message response must be an object")
        return value

    def ensure_session(
        self,
        directory: str,
        *,
        provider_id: str,
        model_id: str,
        operation_key: str,
        session_id: str | None = None,
        title: str = "Kite coding task",
    ) -> str:
        """Create or recover one deterministically identified session."""
        if not directory.strip() or not provider_id.strip() or not model_id.strip():
            raise ValueError("directory, provider_id, and model_id must be non-empty")
        session_id = session_id or self.session_id_for_key(operation_key)
        path = f"/api/session/{quote(session_id, safe='')}"
        try:
            existing = self.get_session(session_id)
            existing_directory = existing.get("directory")
            if (isinstance(existing_directory, str)
                    and Path(existing_directory).resolve() != Path(directory).resolve()):
                raise OpenCodeError("existing OpenCode session belongs to a different directory")
            # The server may have accepted creation immediately before a worker
            # crashed; reuse it and explicitly restore the routed model.
            self._request("POST", f"{path}/model", body={
                "model": {"providerID": provider_id, "modelID": model_id},
            })
            return session_id
        except OpenCodeError as exc:
            if exc.status_code != 404 or session_id != self.session_id_for_key(operation_key):
                raise

        policy = [
            {"action": "shell", "resource": "*", "effect": "ask"},
            {"action": "shell", "resource": "git push *", "effect": "deny"},
        ]
        body = {
            "id": session_id,
            "title": title,
            "location": {"directory": directory},
            "model": {"providerID": provider_id, "modelID": model_id},
            "permissions": policy,
            "metadata": {"kite_operation_key": operation_key},
        }
        _status, payload, _content_type = self._request("POST", "/api/session", body=body)
        created = self._json(payload)
        if not isinstance(created, dict) or created.get("id", session_id) != session_id:
            raise OpenCodeError("OpenCode created a session with an unexpected ID")
        return session_id

    def submit_prompt(self, session_id: str, text: str, *, operation_key: str) -> dict[str, object] | None:
        if not session_id.strip() or not text.strip():
            raise ValueError("session_id and text must be non-empty")
        prompt_id = self.message_id_for_key(operation_key)
        session_path = f"/api/session/{quote(session_id, safe='')}"
        try:
            return self.get_message(session_id, prompt_id)
        except OpenCodeError as exc:
            if exc.status_code != 404:
                raise
        try:
            _status, payload, _content_type = self._request(
                "POST", f"{session_path}/prompt", body={"id": prompt_id, "text": text},
            )
        except OpenCodeError as exc:
            if exc.status_code != 409:
                raise
            # A concurrent/retried request may have admitted this message
            # between our lookup and prompt POST. Reconcile by the stable ID.
            try:
                return self.get_message(session_id, prompt_id)
            except OpenCodeError as lookup_error:
                if lookup_error.status_code == 404:
                    raise exc
                raise
        value = self._json(payload)
        return value if isinstance(value, dict) else None

    def read_events(
        self,
        session_id: str,
        *,
        after: int = 0,
        follow: bool = False,
    ) -> Iterator[OpenCodeEvent]:
        """Read durable SSE events after a sequence cursor."""
        if after < 0:
            raise ValueError("after must be non-negative")
        query = urlencode({"after": after, "follow": "true" if follow else "false"})
        path = f"/api/experimental/session/{quote(session_id, safe='')}/log?{query}"
        status, payload, _content_type = self._request("GET", path, accept="text/event-stream")
        if status != 200:
            raise OpenCodeError(f"OpenCode session log returned HTTP {status}")
        for block in payload.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n"):
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if not line or line.startswith(":") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                fields[key] = value.lstrip()
            if "data" not in fields:
                continue
            raw = self._json(fields["data"].encode())
            if not isinstance(raw, dict):
                continue
            sequence = raw.get("sequence", raw.get("seq", fields.get("id", "0")))
            try:
                sequence_number = int(sequence)
            except (TypeError, ValueError):
                continue
            event = raw.get("event", raw)
            if isinstance(event, dict):
                event_type = event.get("type", fields.get("event", "unknown"))
                event_data = event.get("properties", event)
            else:
                event_type = fields.get("event", "unknown")
                event_data = {"value": event}
            if isinstance(event_type, str) and isinstance(event_data, dict):
                yield OpenCodeEvent(sequence_number, event_type, event_data)

    def latest_sequence(self, session_id: str) -> int:
        return max((event.sequence for event in self.read_events(session_id)), default=0)

    def active_session_ids(self) -> set[str]:
        _status, payload, _content_type = self._request("GET", "/api/session/active")
        value = self._json(payload)
        sessions = value.get("sessions") if isinstance(value, dict) else value
        if not isinstance(sessions, list):
            raise OpenCodeError("OpenCode active-session response must contain a session list")
        ids = set()
        for session in sessions:
            if isinstance(session, dict):
                session_id = session.get("id", session.get("sessionID"))
                if isinstance(session_id, str):
                    ids.add(session_id)
        return ids

    def is_session_active(self, session_id: str) -> bool:
        return session_id in self.active_session_ids()

    def interrupt(self, session_id: str) -> dict[str, object]:
        _status, payload, _content_type = self._request(
            "POST", f"/api/session/{quote(session_id, safe='')}/interrupt?resume=false",
        )
        value = self._json(payload)
        if not isinstance(value, dict):
            raise OpenCodeError("OpenCode interrupt response must be an object")
        return value

    def interrupt_and_wait(
        self,
        session_id: str,
        *,
        after: int = 0,
        timeout_seconds: float = 30.0,
        heartbeat: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[dict[str, object], list[OpenCodeEvent], int]:
        """Interrupt current execution and await its durable terminal event."""
        response = self.interrupt(session_id)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            active = self.is_session_active(session_id)
            if not active:
                events = list(self.read_events(session_id, after=after))
                cursor = max((event.sequence for event in events), default=after)
                if heartbeat:
                    heartbeat({"session_id": session_id, "sequence": cursor, "state": "INACTIVE"})
                return response, events, cursor
            if heartbeat:
                heartbeat({"session_id": session_id, "state": "WAITING_FOR_INTERRUPT"})
            time.sleep(0.2)
        raise TimeoutError(f"OpenCode session {session_id} is still active after interrupt")


def wait_for_terminal(
    client: OpenCodeClient,
    session_id: str,
    *,
    after: int = 0,
    timeout_seconds: float = 21600,
    heartbeat: Callable[[dict[str, object]], None] | None = None,
    poll_interval_seconds: float = 1.0,
) -> tuple[list[OpenCodeEvent], int]:
    """Poll the durable session log until completion, interruption, or timeout."""
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("timeouts must be positive")
    deadline = time.monotonic() + timeout_seconds
    cursor = after
    collected: list[OpenCodeEvent] = []
    terminal = {
        "session.execution.succeeded", "session.execution.failed",
        "session.execution.interrupted", "session.idle",
    }
    while time.monotonic() < deadline:
        events = list(client.read_events(session_id, after=cursor, follow=False))
        for event in events:
            cursor = max(cursor, event.sequence)
            collected.append(event)
            if heartbeat:
                heartbeat({"session_id": session_id, "sequence": cursor, "event_type": event.type})
            if event.type in terminal:
                return collected, cursor
        if heartbeat:
            heartbeat({"session_id": session_id, "sequence": cursor, "state": "WAITING"})
        time.sleep(poll_interval_seconds)
    raise TimeoutError(f"OpenCode session {session_id} did not reach a terminal state")


class OpenCodeTaskAdapter:
    """Bind Kite task/route data to OpenCode sessions and review patches."""

    def __init__(
        self,
        client: OpenCodeClient,
        *,
        timeout_seconds: float = 21600,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("timeouts must be positive")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    async def prepare_session(
        self,
        task: CodingTaskInput,
        route: RouteLease,
        workspace_path: str,
        prior_session_id: str | None,
        operation_key: str,
    ) -> str:
        return await asyncio.to_thread(
            self.client.ensure_session,
            workspace_path,
            provider_id=route.provider_id,
            model_id=route.model_id,
            operation_key=operation_key,
            session_id=prior_session_id,
        )

    async def reconcile_operation(
        self, operation_name: str, operation_key: str, *context: object,
    ) -> "RecoveryDecision":
        """Recover session creation; rerun prompt operations through stable IDs."""
        from .temporal_state import RecoveryDecision

        if operation_name == "prepare-opencode-session":
            task, route, workspace_path, prior_session_id = context
            session_id = prior_session_id or self.client.session_id_for_key(operation_key)
            try:
                await asyncio.to_thread(self.client.get_session, session_id)
            except OpenCodeError as exc:
                if exc.status_code == 404:
                    return RecoveryDecision(recovered=False)
                raise
            return RecoveryDecision(recovered=True, result=session_id)
        if operation_name == "run-opencode":
            task, route, workspace_path, instruction, session_id = context
            try:
                await asyncio.to_thread(
                    self.client.get_message,
                    session_id, self.client.message_id_for_key(operation_key),
                )
            except OpenCodeError as exc:
                if exc.status_code == 404:
                    return RecoveryDecision(recovered=False)
                raise
            # Let the activity operation resume from its persisted heartbeat
            # cursor. submit_prompt rechecks this same ID before POSTing.
            return RecoveryDecision(recovered=False)
        if operation_name in {"interrupt-opencode", "stop-opencode-session"}:
            session_id = context[-1]
            active = await asyncio.to_thread(self.client.is_session_active, session_id)
            return RecoveryDecision(recovered=not active, result=None)
        return RecoveryDecision(recovered=False)

    async def stop_session(
        self,
        session_id: str,
        heartbeat: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()

        def thread_heartbeat(details: dict[str, object]) -> None:
            if heartbeat is not None:
                loop.call_soon_threadsafe(heartbeat, details)

        await asyncio.to_thread(
            self.client.interrupt_and_wait, session_id,
            timeout_seconds=min(self.timeout_seconds, 120),
            heartbeat=thread_heartbeat if heartbeat is not None else None,
        )

    async def run(
        self,
        task: CodingTaskInput,
        route: RouteLease,
        workspace_path: str,
        instruction: str,
        session_id: str,
        operation_key: str,
        heartbeat: Callable[[dict[str, object]], None],
        *,
        resume_details: Mapping[str, object] | None = None,
    ) -> CodingRunResult:
        root = Path(workspace_path).resolve()
        actual_root = await asyncio.to_thread(
            self._git, root, "rev-parse", "--show-toplevel",
        )
        if Path(actual_root).resolve() != root:
            raise OpenCodeError("OpenCode workspace is not a Git worktree root")
        actual_head = await asyncio.to_thread(self._git, root, "rev-parse", "HEAD")
        if actual_head != task.repo_head:
            raise OpenCodeError("OpenCode workspace HEAD does not match the task base commit")

        start_sequence = 0
        if resume_details and isinstance(resume_details.get("start_sequence"), int):
            start_sequence = max(0, int(resume_details["start_sequence"]))
        else:
            start_sequence = await asyncio.to_thread(self.client.latest_sequence, session_id)
        heartbeat({
            "task_id": task.task_id, "session_id": session_id,
            "state": "PROMPT_PENDING", "start_sequence": start_sequence,
            "operation_key": operation_key,
        })
        prompt_text = instruction.strip() or task.goal
        await asyncio.to_thread(
            self.client.submit_prompt, session_id, prompt_text,
            operation_key=operation_key,
        )

        deadline = time.monotonic() + self.timeout_seconds
        cursor = start_sequence
        output: list[str] = []
        terminal_event: OpenCodeEvent | None = None
        while time.monotonic() < deadline:
            batch = await asyncio.to_thread(
                lambda: list(self.client.read_events(session_id, after=cursor)),
            )
            for event in batch:
                if event.sequence <= cursor:
                    continue
                cursor = event.sequence
                if event.type == "session.text.delta":
                    delta = event.data.get("text")
                    if isinstance(delta, str):
                        output.append(delta)
                detail: dict[str, object] = {
                    "task_id": task.task_id, "session_id": session_id,
                    "state": "APPROVAL_REQUIRED" if event.type == "permission.asked" else "RUNNING",
                    "event_type": event.type, "event_sequence": cursor,
                    "start_sequence": start_sequence,
                }
                if event.type == "permission.asked":
                    # Keep heartbeat data small and avoid persisting tool input.
                    for field in ("id", "permission"):
                        value = event.data.get(field)
                        if isinstance(value, str):
                            detail[f"permission_{field}"] = value[:200]
                    patterns = event.data.get("patterns")
                    if isinstance(patterns, list):
                        detail["permission_patterns"] = [
                            value[:200] for value in patterns[:20] if isinstance(value, str)
                        ]
                heartbeat(detail)
                if event.type in {
                    "session.execution.succeeded", "session.execution.failed",
                    "session.execution.interrupted", "session.idle",
                }:
                    terminal_event = event
                    break
            if terminal_event:
                break
            heartbeat({
                "task_id": task.task_id, "session_id": session_id,
                "state": "WAITING", "event_sequence": cursor,
                "start_sequence": start_sequence,
            })
            await asyncio.sleep(self.poll_interval_seconds)
        if terminal_event is None:
            raise TimeoutError(f"OpenCode session {session_id} did not reach a terminal state")
        if terminal_event.type == "session.execution.failed":
            message = terminal_event.data.get("error", "OpenCode execution failed")
            raise OpenCodeError(str(message)[:1000])

        patch, changed_files = await asyncio.to_thread(self._review_patch, root, task.repo_head)
        summary = "".join(output).strip() or "OpenCode completed the task without a text summary."
        return CodingRunResult(
            task_id=task.task_id,
            base_repo_head=task.repo_head,
            session_id=session_id,
            patch=patch,
            summary=summary,
            changed_files=changed_files,
            # Provider adapters can add measured device usage later. OpenCode's
            # generic session API does not report Kaggle GPU seconds.
            usage_gpu_seconds=0,
        )

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise OpenCodeError(f"Git {' '.join(args)} failed: {detail}")
        return result.stdout.strip()

    @classmethod
    def _review_patch(cls, root: Path, base_head: str) -> tuple[str, tuple[str, ...]]:
        tracked_patch = cls._git(root, "diff", "--binary", base_head, "--")
        tracked_files = cls._git(root, "diff", "--name-only", "-z", base_head, "--")
        tracked = {name for name in tracked_files.split("\0") if name}
        untracked_raw = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
        untracked = [os.fsdecode(name) for name in untracked_raw.split(b"\0") if name]
        additions: list[str] = []
        for relative in untracked:
            candidate = root / relative
            if candidate.is_symlink():
                raise OpenCodeError("untracked review files cannot be symlinks")
            target = candidate.resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise OpenCodeError("untracked file escapes the task worktree") from exc
            if not target.is_file():
                raise OpenCodeError("untracked review files must be regular files inside the worktree")
            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise OpenCodeError(
                    f"untracked binary file cannot be represented as a review patch: {relative}"
                ) from exc
            diff = list(difflib.unified_diff(
                [], text.splitlines(keepends=True), fromfile="/dev/null",
                tofile=f"b/{relative}", lineterm="\n",
            ))
            if diff:
                additions.append(
                    f"diff --git a/{relative} b/{relative}\nnew file mode 100644\n"
                    + "".join(diff)
                )
            tracked.add(relative)
        patch = tracked_patch
        if additions:
            patch += ("\n" if patch and not patch.endswith("\n") else "") + "".join(additions)
        return patch, tuple(sorted(tracked))
