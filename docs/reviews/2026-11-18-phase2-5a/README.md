# Phase 2.5A — Chat tiếng Việt với Kite qua MCP (18/11/2026)

Tiếp nối `2026-11-18-temporal-backend`. Mọi số liệu là output thật trên EC2 và ảnh chụp OpenCode 2.3.0; worker Kaggle 2×T4 chạy `qwen3.6`.

## Đã làm

| Thành phần | Nội dung |
|---|---|
| `kite mcp serve` | MCP server (stdio) cho agent trưởng. 13 tool: `start_task`, `task_status`, `task_result`, `send_instruction`, `pause_task`, `resume_task`, `cancel_task`, `list_tasks`, `worker_status`, `apply_result`, `commit_result`, `repo_status`, `run_command` |
| `kite chat setup` | Tạo `~/kite-chat` (`opencode.json` + `AGENTS.md` tiếng Việt), `git init` thư mục chat, tắt OpenCode/MCP server cũ |
| `kite chat reload` | Chỉ tắt `opencode serve` nền và `kite mcp serve` cũ của đúng config này (không đụng agent trong container) |
| `run_command` | Lệnh shell trong container không mạng, trên bản copy của repo (symlink ra ngoài giữ nguyên là link, không chép nội dung) |
| `apply_result` / `commit_result` | Hai tool duy nhất đổi repo thật; chỉ khi người dùng nói rõ; kiểm tra trước (`git apply --check`, repo sạch, chỉ commit đúng file của patch, không push) |
| Quyền của agent trưởng | Không sửa file, không web, không đọc ngoài thư mục chat (`external_directory: deny`); shell chế độ `ask`, chặn hẳn `sudo`, `docker`, `curl`, `ssh`, `git push`, đường dẫn secrets |

## Kiểm thử tự động

`189` → `196 passed` (MCP server) → `197` (`apply_result`) → `201` (`run_command`, `repo_status`, `commit_result`) → `202` (`chat reload`).

## Lỗi lộ ra khi chạy thật

| # | Triệu chứng | Nguyên nhân | Sửa |
|---|---|---|---|
| C1 | `opencode mcp list`: "No MCP servers configured" | OpenCode 2.3.0 chỉ đọc `opencode.json` của project khi thư mục là repo Git | `chat setup` chạy `git init` |
| C2 | Agent trưởng tự đọc 3 file trong repo | Chỉ chặn edit/bash/web, chưa chặn đọc ngoài thư mục chat | `external_directory: deny`; đọc code qua `run_command` |
| C3 | Người dùng gõ "áp" → agent báo **"Patch đã được áp thành công"**, nhưng `git status` của repo thật trống | Gọi tool lỗi → agent tạo task mới để "git apply" (task chạy trên bản copy) rồi báo như đã áp vào repo thật | Mô tả tool + AGENTS.md: task không bao giờ đổi repo thật; gặp lỗi thì báo đúng lỗi; sau khi áp/commit phải xác nhận bằng `repo_status`. Đổi model trưởng `qwen3.6-fast` → `qwen3.6` (bật thinking) |
| C4 | Code mới đã lên EC2 (`201 passed`) nhưng agent chỉ thấy 9 tool đầu tiên | `opencode serve --service` chạy nền **2 giờ 43 phút**, giữ `kite mcp serve` cũ sống qua mọi lần mở TUI | Tắt tiến trình cũ; `chat setup` tự tắt; thêm `kite chat reload` |
| C5 | Không gõ được tiếng Việt có dấu trong TUI | Unikey/EVKey giả lập phím Backspace, xung đột với TUI (phía máy người dùng) | Không sửa trong code: dùng bàn phím Vietnamese Telex của Windows hoặc chế độ clipboard của Unikey |

## Chạy thật sau khi sửa (session mới, `qwen3.6`)

| Câu chat | Tool agent gọi | Kiểm chứng phía máy chủ |
|---|---|---|
| "chạy pytest trong repo /home/ubuntu/kite-sandbox" | `kite_run_command` | Gọi trực tiếp: exit 0, **0,7 s**, `2 passed`; không tạo task |
| "áp patch của task chat-20261118-011626-bfdb" | `kite_apply_result`, `kite_repo_status` | 2 file, +27/−1 |
| "commit với message \"Add median\"" | `kite_commit_result` | `git log`: `434f3a0 Add median`; `git status` trống; chưa push |

Task gốc `chat-20261118-011626-bfdb` (thêm `median`) được tạo từ một câu chat tiếng Việt: agent ở sandbox sửa `calc.py` + `test_calc.py`, Kite tự chạy test không mạng: **6 passed**.

## Còn mở

- Độ trễ: câu chạy pytest mất 53 s, gần hết là thời gian `qwen3.6` suy nghĩ (tool chỉ 0,7 s). So với Gemma 4 và Qwen3-Coder-Next ở bước B.
- Mỗi lần đổi `mcp_server.py`: chạy `kite chat reload` (hoặc `chat setup --force`) rồi mở session mới.
