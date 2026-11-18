"""MCP server that lets a chat agent (Kite's Vietnamese "lead" agent in OpenCode) drive Kite.

Every tool maps onto the Temporal workflow that already runs tasks in the sandbox. The lead agent
itself gets no shell, edit or web permissions (see ``chat_files``): anything that touches code
becomes a Kite task that runs in the offline sandbox, and patches are only applied by the user.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid
from typing import Any, Awaitable, Callable

from .config import KiteConfig, gateway_models, model_policy_from_config
from .protocol import ScheduledTask
from .store import SQLiteSchedulerStore, StoreError

PATCH_PREVIEW_CHARS = 6000
TASK_TYPES = ("coding", "analysis", "review", "docs", "vision")


class KiteToolError(RuntimeError):
    """A tool call that cannot be done; the message is shown to the chat agent."""


class KiteOps:
    """What the MCP tools do. Temporal connection and gateway status are injectable for tests."""

    def __init__(self, config: KiteConfig, *, db_path: Path, state_dir: Path, allowed_roots,
                 connect: Callable[[str], Awaitable[Any]] | None = None,
                 gateway_status: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.db_path = Path(db_path)
        self.state_dir = Path(state_dir)
        self.allowed_roots = [Path(root).expanduser().resolve() for root in allowed_roots]
        self._connect = connect
        self._client = None
        self._gateway_status = gateway_status
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    async def client(self):
        if self._client is None:
            if self._connect is None:
                from temporalio.client import Client

                self._client = await Client.connect(self.config.temporal_endpoint)
            else:
                self._client = await self._connect(self.config.temporal_endpoint)
        return self._client

    def _store(self) -> SQLiteSchedulerStore:
        return SQLiteSchedulerStore(self.db_path, model_policy=model_policy_from_config(self.config))

    def base_models(self) -> list[str]:
        return [m for m in gateway_models(self.config) if m not in self.config.gateway.aliases]

    def _repo(self, repo: str) -> tuple[Path, str]:
        path = Path(repo).expanduser().resolve()
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise KiteToolError(f"{path} nằm ngoài các thư mục được phép: "
                                + ", ".join(str(r) for r in self.allowed_roots))
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
        if head.returncode != 0:
            raise KiteToolError(f"{path} không phải repo Git có ít nhất một commit")
        return path, head.stdout.strip()

    async def _handle(self, task_id: str, typed: bool = False):
        from .temporal_workflow import CodingTaskWorkflow

        client = await self.client()
        if typed:
            return client.get_workflow_handle_for(CodingTaskWorkflow.run, task_id)
        return client.get_workflow_handle(task_id)

    # ------------------------------------------------------------------ tools
    async def start_task(self, repo: str, goal: str, task_type: str = "coding",
                         model: str | None = None) -> dict:
        from .temporal_workflow import CodingTaskInput, CodingTaskWorkflow

        if not goal.strip():
            raise KiteToolError("goal đang trống")
        if task_type not in TASK_TYPES:
            raise KiteToolError(f"task_type phải là một trong: {', '.join(TASK_TYPES)}")
        if model is not None and model not in self.base_models():
            raise KiteToolError(f"model phải là một trong: {', '.join(self.base_models())} (để trống cho Kite tự chọn)")
        path, head = self._repo(repo)
        task_id = f"chat-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        branch = f"agent/{task_id}"
        with self._store() as store:
            store.enqueue(ScheduledTask(task_id, "coder", model, head, branch,
                                        json.dumps({"goal": goal}, ensure_ascii=False, sort_keys=True),
                                        900, 0, self.clock(), task_type=task_type))
        client = await self.client()
        try:
            await client.start_workflow(
                CodingTaskWorkflow.run,
                CodingTaskInput(task_id, goal, str(path), head, branch, (), model, 900,
                                task_type=task_type, role="coder"),
                id=task_id, task_queue=self.config.temporal_task_queue)
        except Exception as exc:
            raise KiteToolError(f"không khởi động được workflow: {exc}") from exc
        return {"task_id": task_id, "repo": str(path), "commit": head, "task_type": task_type,
                "model": model or "Kite tự chọn", "status": "started"}

    async def task_status(self, task_id: str) -> dict:
        from .temporal_workflow import CodingTaskWorkflow

        handle = await self._handle(task_id)
        try:
            description = await handle.describe()
        except Exception as exc:
            raise KiteToolError(f"không tìm thấy task {task_id}: {exc}") from exc
        state = getattr(description.status, "name", "UNKNOWN")
        result: dict[str, Any] = {"task_id": task_id, "workflow": state}
        if state == "RUNNING":
            try:
                result["step"] = await handle.query(CodingTaskWorkflow.current_status)
            except Exception:
                result["step"] = "UNKNOWN"
        with self._store() as store:
            try:
                result["queue_state"] = store.get_task(task_id)["state"]
            except StoreError:
                pass
        return result

    async def task_result(self, task_id: str) -> dict:
        status = await self.task_status(task_id)
        if status["workflow"] != "COMPLETED":
            return {**status, "ready": False,
                    "message": "Task chưa xong (hoặc đã dừng). Dùng task_status để theo dõi."}
        from .backend import task_slug

        handle = await self._handle(task_id, typed=True)
        outcome = await handle.result()
        result: dict[str, Any] = {"task_id": task_id, "ready": True, "status": outcome.status}
        bundle = outcome.review_bundle
        if bundle is not None:
            patch = bundle.patch
            if len(patch) > PATCH_PREVIEW_CHARS:
                patch = patch[:PATCH_PREVIEW_CHARS] + f"\n... (còn {len(bundle.patch) - PATCH_PREVIEW_CHARS} ký tự, xem file patch)"
            result.update(summary=bundle.summary, patch=patch, worker=bundle.worker_id)
        patch_path = self.state_dir / "tasks" / task_slug(task_id) / "result.patch"
        repo_path = None
        if outcome.checkpoint_path and Path(outcome.checkpoint_path).is_file():
            repo_path = json.loads(Path(outcome.checkpoint_path).read_text(encoding="utf-8")).get("repo_path")
        result["patch_path"] = str(patch_path)
        result["apply_with"] = f"git -C {repo_path} apply {patch_path}" if repo_path else f"git apply {patch_path}"
        return result

    async def _signal(self, task_id: str, name: str, *args: str) -> dict:
        from .temporal_workflow import CodingTaskWorkflow

        signal = {"pause": CodingTaskWorkflow.pause, "resume": CodingTaskWorkflow.resume_with_instruction,
                  "cancel": CodingTaskWorkflow.cancel}[name]
        handle = await self._handle(task_id)
        try:
            await handle.signal(signal, *args)
        except Exception as exc:
            raise KiteToolError(f"không gửi được tới task {task_id}: {exc}") from exc
        return {"task_id": task_id, "signal": name, "sent": True}

    async def send_instruction(self, task_id: str, instruction: str) -> dict:
        if not instruction.strip():
            raise KiteToolError("chỉ dẫn đang trống")
        return await self._signal(task_id, "resume", instruction.strip())

    async def pause_task(self, task_id: str) -> dict:
        return await self._signal(task_id, "pause")

    async def resume_task(self, task_id: str) -> dict:
        return await self._signal(task_id, "resume", "")

    async def cancel_task(self, task_id: str) -> dict:
        return await self._signal(task_id, "cancel")

    async def list_tasks(self, limit: int = 10) -> list[dict]:
        with self._store() as store:
            tasks = store.list_tasks()
        tasks.sort(key=lambda task: task["created_at"], reverse=True)
        listed = []
        for task in tasks[:max(1, min(limit, 50))]:
            try:
                goal = json.loads(task["payload_json"]).get("goal", "")
            except (ValueError, AttributeError):
                goal = ""
            listed.append({"task_id": task["task_id"], "state": task["state"], "task_type": task["task_type"],
                           "goal": goal[:200],
                           "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(task["created_at"]))})
        return listed

    async def worker_status(self) -> dict:
        if self._gateway_status is not None:
            return self._gateway_status()

        def fetch() -> dict:
            url = f"http://{self.config.gateway.host}:{self.config.gateway.port}/status"
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.loads(response.read())

        try:
            return await asyncio.to_thread(fetch)
        except Exception as exc:
            raise KiteToolError(f"không đọc được trạng thái gateway: {exc}") from exc


def build_server(ops: KiteOps):
    """FastMCP server exposing KiteOps as tools (descriptions are read by the chat model)."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("kite", instructions=(
        "Kite giao việc code cho agent chạy trong sandbox không Internet, trên GPU Kaggle. "
        "Tạo task bằng start_task, theo dõi bằng task_status, lấy kết quả bằng task_result. "
        "Không bao giờ tự áp patch: đưa lệnh apply_with cho người dùng."))

    @server.tool(description="Giao một việc cho Kite trên một repo Git. goal phải cụ thể (file, hành vi mong muốn, "
                             "cách kiểm tra). task_type: coding | analysis | review | docs | vision. "
                             "model: để trống cho Kite tự chọn.")
    async def start_task(repo: str, goal: str, task_type: str = "coding", model: str | None = None) -> dict:
        return await ops.start_task(repo, goal, task_type, model)

    @server.tool(description="Trạng thái một task: workflow (RUNNING/COMPLETED/...) và bước hiện tại "
                             "(WAITING_FOR_WORKER, CODING, READY_FOR_REVIEW...).")
    async def task_status(task_id: str) -> dict:
        return await ops.task_status(task_id)

    @server.tool(description="Kết quả task đã xong: tóm tắt, kết quả test do Kite tự chạy, patch và lệnh apply_with "
                             "để người dùng tự áp.")
    async def task_result(task_id: str) -> dict:
        return await ops.task_result(task_id)

    @server.tool(description="Gửi thêm chỉ dẫn cho task CHƯA xong; agent sẽ làm tiếp trên cùng bản clone.")
    async def send_instruction(task_id: str, instruction: str) -> dict:
        return await ops.send_instruction(task_id, instruction)

    @server.tool(description="Tạm dừng task.")
    async def pause_task(task_id: str) -> dict:
        return await ops.pause_task(task_id)

    @server.tool(description="Tiếp tục task đang tạm dừng.")
    async def resume_task(task_id: str) -> dict:
        return await ops.resume_task(task_id)

    @server.tool(description="Hủy task.")
    async def cancel_task(task_id: str) -> dict:
        return await ops.cancel_task(task_id)

    @server.tool(description="Các task gần đây (mới nhất trước).")
    async def list_tasks(limit: int = 10) -> list[dict]:
        return await ops.list_tasks(limit)

    @server.tool(description="Trạng thái worker GPU (Kaggle): còn sống không, đang nạp model nào.")
    async def worker_status() -> dict:
        return await ops.worker_status()

    return server


LEAD_INSTRUCTIONS = """# Agent trưởng của Kite

Bạn là agent trưởng của Kite. Luôn trả lời người dùng bằng **tiếng Việt**; giữ nguyên tên file, hàm, lệnh và code.

## Việc của bạn

- Hiểu yêu cầu. Nếu chưa rõ, hỏi lại tối đa 1–2 câu ngắn; nếu đã đủ rõ thì làm luôn.
- Giao việc bằng tool `start_task`. Viết `goal` cụ thể: file/hàm liên quan, hành vi mong muốn, ràng buộc, cách kiểm tra.
- Chọn `task_type`: `coding` (viết/sửa code), `analysis` (phân tích, không cần sửa), `review` (xem lại code), `docs` (tài liệu).
- Báo cho người dùng `task_id`, rồi theo dõi bằng `task_status`. Khi người dùng hỏi tiến độ, gọi `task_status` thay vì đoán.
- Khi task xong, gọi `task_result` và tóm tắt ngắn: đã đổi file nào, đổi gì, test do Kite tự chạy **PASSED** hay **FAILED**, kèm lệnh `apply_with` để người dùng tự áp patch.
- Muốn sửa thêm trên task CHƯA xong: `send_instruction`. Task đã xong: tạo task mới.
- Hỏi về GPU/worker: `worker_status`. Nếu không có worker nào `alive`, nhắc người dùng bật notebook Kaggle (mục Worker mode); task vẫn được giữ và sẽ chạy khi worker kết nối.

## Bạn không làm

- Không tự sửa file, không chạy lệnh shell, không tải web: bạn không có các quyền đó. Mọi thay đổi code đều phải qua task của Kite (chạy trong sandbox không Internet).
- Không tự áp patch. Người dùng duyệt patch và tự chạy lệnh `apply_with`.
- Không bịa kết quả: chỉ nói test PASSED khi `task_result` ghi như vậy.

## Thông tin

- Repo được phép dùng (phải là repo Git, Kite lấy commit HEAD): ROOTS
- Model trên gateway: MODELS (để trống `model` cho Kite tự chọn theo loại task).
- Một task thường mất 1–5 phút khi worker Kaggle đang chạy.
"""


def chat_files(config: KiteConfig, *, lead_model: str, kite_command: list[str], allowed_roots) -> dict[str, str]:
    """opencode.json + AGENTS.md for the chat folder of the lead agent."""
    from .gateway import opencode_provider_config

    models = gateway_models(config)
    opencode = opencode_provider_config(f"http://{config.gateway.host}:{config.gateway.port}", models)
    opencode["model"] = f"kite/{lead_model}"
    opencode["small_model"] = f"kite/{lead_model}"
    opencode["mcp"] = {"kite": {"type": "local", "command": list(kite_command), "enabled": True}}
    # The lead agent runs on the host, outside the sandbox: no shell, no edits, no web.
    opencode["permission"] = {"edit": "deny", "bash": "deny", "webfetch": "deny"}
    instructions = (LEAD_INSTRUCTIONS
                    .replace("ROOTS", ", ".join(f"`{root}`" for root in allowed_roots))
                    .replace("MODELS", ", ".join(f"`{m}`" for m in models)))
    return {"opencode.json": json.dumps(opencode, indent=2, ensure_ascii=False) + "\n",
            "AGENTS.md": instructions}
