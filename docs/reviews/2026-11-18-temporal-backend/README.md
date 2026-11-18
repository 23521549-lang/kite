# Backend production — task chạy qua Temporal trong sandbox (18/11/2026)

Tiếp nối `2026-11-18-phase2-3`. Mọi số liệu là output thật trên EC2 (Ubuntu 24.04, ~2 GB RAM + 2 GB swap, ổ gốc nới lên 20 GB).

## Đã làm

| Commit (tóm tắt) | Nội dung |
|---|---|
| Sandbox: bước setup | Bước cài thư viện (có mạng) chạy trước; agent và test không mạng nhưng dùng thư viện ở `/deps`; file do setup sinh ra không lẫn vào patch; log bỏ mã màu |
| Fail fast | `sandbox run` dừng ngay khi không có worker sống (trước đó chờ 30 phút); `sandbox build` in log trực tiếp; gateway trả đúng tên model |
| Backend production | `SandboxActivityBackend` cài đặt mọi bước của `CodingTaskWorkflow`; `kite workflow worker` + systemd `kite-workflow-worker`; `kite workflow result` |
| Sửa lỗi lộ ra khi chạy thật | `run_worker` không có trong `temporal_worker.py`; namespace `default` chưa được tạo; quota của owner bị coi là "cũ" 5 phút sau `provider sync` nên bộ điều phối không bao giờ giao task; CLI segfault lúc thoát |

## Kiểm thử tự động

`186 passed` sau backend → `187 passed` sau sửa quota → `189 passed` sau đợt này (namespace, lỗi Temporal một dòng).

## Cài thư viện, chạy không mạng

Repo thử cần `python-dateutil`: bước setup `pip install -r requirements.txt` (có mạng) → test chạy trong container `--network none` vẫn import được thư viện từ `/deps`: `1 passed`.

## Task trọn vẹn qua Temporal: `demo-1`

`kite workflow start --task-id demo-1 --repo ~/kite-sandbox --goal "Sửa calc.py ..."`

| | |
|---|---|
| Tuyến | Temporal → bộ điều phối (`kaggle-han`, `qwen3.6`) → sandbox không Internet → Kite tự chạy test → review bundle |
| Agent | `ok`, 22,8 s; đọc thư mục, glob `**/*.py`, đọc `calc.py` + `test_calc.py`, sửa `calc.py`, tự chạy pytest |
| Kite tự kiểm chứng | `python -m pytest -q` không mạng: **PASSED** (2 passed) |
| Patch | chỉ `calc.py`, đúng 2 lỗi cố ý |
| Quota | ghi 22 GPU-giây cho owner `han` |
| Trạng thái cuối | `READY_FOR_REVIEW` |

## Tình trạng E1–E6

| Lỗi | Tình trạng |
|---|---|
| E2, E3, E4, E6 | Đã sửa (Pha 1) |
| E1, E5 | Không còn trên cả hai tuyến đang dùng (`kite sandbox run` và Temporal worker): agent chạy `opencode run` trong container không mạng, không remote, không credential, không có prompt xin quyền. `opencode.py` (tuyến OpenCode HTTP cũ) không còn được worker dùng; `evidence/test_evidence.py` của đợt phase1 vẫn tái hiện E1/E5 trên module đó |

## Giới hạn đã biết

- Gateway trả lời một lần (giả streaming).
- Sandbox không Internet: thư viện phải được cài ở bước setup.
- Một worker Kaggle chỉ nạp được một model lớn tại một thời điểm (2×T4, 30 GB).
- RAM EC2 ~2 GB là sát giới hạn khi một task đang chạy; đã thêm 2 GB swap.
