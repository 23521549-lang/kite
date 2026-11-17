# Pha 2 + Pha 3 — Gateway, Kaggle pull-worker, sandbox (18/11/2026)

Hồ sơ minh chứng cho việc nối EC2 với Kaggle và chạy agent trong sandbox cách ly. Mọi số liệu dưới đây là output thật chạy trên EC2 (Ubuntu 24.04, ~2 GB RAM) và Kaggle (2× T4).

## Commit

| Commit | Nội dung |
|---|---|
| `9f88077` | Pha 2: gateway (`gateway.py`), pull-worker (`pull_worker.py`), adapter `pull`, lệnh `gateway serve`, `worker token`, notebook "Worker mode" |
| `9d1ab6a` | Đưa `test_gateway.py` vào `tests/` |
| `7efe69f` | Caddy dùng compose project riêng (`kite-gateway`), tách khỏi Temporal |
| `331e205` | HTTPS của gateway chuyển sang cổng 8443 (cổng 443 đang dùng cho SSH) |
| `3480dea` | systemd unit `kite-gateway.service` |
| `9fb66fb` | Pha 3: sandbox Docker, `kite sandbox build/doctor/run` |

## Kiến trúc đã chạy

```
OpenCode (EC2 hoặc sandbox) ──/v1──▶ gateway 127.0.0.1 / 172.19.0.1 :8787
                                         ▲
Kaggle notebook (pull-worker) ──HTTPS──▶ Caddy :8443 ──/worker/* only──┘
        └─ llama-server 127.0.0.1:8080 (qwen3.6 Q4_K_M, 2× T4)
```

- Worker Kaggle chỉ gọi ra ngoài (long-poll), không mở cổng nào.
- Caddy chỉ publish `/worker/*`; `/v1/*` không ra Internet (`curl https://.../v1/models` → `404`).
- Mỗi worker có token riêng (256 bit); request thiếu/sai token → `401`.

## Kết quả kiểm thử tự động (EC2)

| Mốc | Kết quả |
|---|---|
| Sau Pha 1 | `161 passed` |
| Sau Pha 2 | `170 passed` (+9 test gateway/pull-worker) |
| Sau Pha 3 | `177 passed` (+7 test sandbox) |

## Số đo Kaggle (qwen3.6-35B-A3B Q4_K_M, llama.cpp `9048bad`)

| Chỉ số | Giá trị |
|---|---|
| Tải model (~21,5 GiB) | 91 s |
| Nạp model từ đĩa | 96–102 s |
| VRAM | 12,4 + 10,8 GB / 30 GB |
| Tốc độ sinh | 54–58 token/s |
| Thinking chiếm (câu hỏi đơn giản, chế độ mặc định) | ~80% số token |

## Gateway: một request tiếng Việt qua EC2 → Kaggle

`qwen3.6-fast` (thinking tắt): tổng **2,84 s**, trong đó model 2,15 s (prompt 0,22 s + sinh 1,93 s), chi phí gateway + mạng + long-poll ≈ 0,7 s.

## OpenCode qua gateway (chưa sandbox)

Repo thử `~/kite-sandbox` (hàm `average` có 2 lỗi cố ý, 2 test fail). Agent tự đọc file, chạy pytest, sửa `calc.py`, chạy lại test: **2 passed**, **23,3 s**, context 7,2K token (22% của 32K). Tool calling của qwen3.6 qua gateway hoạt động.

## Sandbox: kiểm tra cách ly (`kite sandbox doctor`)

| Kiểm tra | Kết quả |
|---|---|
| Mạng `kite-sandbox-net` là internal | `true` |
| Sandbox tới được gateway (`172.19.0.1:8787`) | `200` |
| Sandbox ra được Internet | không (`URLError`) |
| Sandbox thấy `/var/lib/kite` (secret) | không |
| Chạy bằng root | không (uid 1000) |
| OpenCode trong image | `v2.3.0` |

## Sandbox: một task trọn vẹn (`kite sandbox run`)

Task `sbx-20261117-235339-4ef9c7`, repo `~/kite-sandbox` tại `8eaca97`:

| | |
|---|---|
| Agent | `ok`, 21,2 s, không hỏi quyền lần nào (E1 không xảy ra trên đường chạy này) |
| Patch | chỉ `calc.py`; `__pycache__` bị loại |
| Kite tự chạy test (container `--network none`) | `2 passed`, exit 0, 1,1 s |
| `verified` | `true` |
| Tổng | 23,1 s |

Patch đã `git apply` vào repo gốc và khớp với bản đã kiểm chứng.

## Tình trạng các lỗi E1–E6

| Lỗi | Tình trạng |
|---|---|
| E2, E3, E4, E6 | Đã sửa ở Pha 1 |
| E1 (agent treo khi xin quyền) | Không xảy ra với `kite sandbox run` (sandbox là ranh giới an toàn, agent không phải hỏi). Đường chạy Temporal (`opencode.py`) chưa sửa |
| E5 (lách rule chặn `git push`, shell chạy trên máy chủ) | Với `kite sandbox run`: container không có mạng, không có remote, không có credential. Đường chạy Temporal chưa sửa |

Script `evidence/test_evidence.py` của đợt 2026-11-18-phase1 vẫn tái hiện E1 và E5, vì nó kiểm tra `opencode.py` (đường chạy Temporal).

## Giới hạn đã biết

- Gateway trả lời một lần cả câu (giả streaming), nên số `tok/s` OpenCode hiển thị không phải tốc độ thật.
- Trường `model` trong câu trả lời là đường dẫn file GGUF trên Kaggle, chưa phải tên model.
- `agent_output_tail` còn mã màu terminal.
- Sandbox không có Internet: repo cần cài thư viện thì chưa chạy test được.
- Kaggle `Run All` từng dừng giữa chừng sau mục 5 (chưa rõ nguyên nhân); chạy từng ô thì được.
- `sslip.io` dùng chung tên miền, có thể bị Let's Encrypt giới hạn số chứng chỉ.

## Chạy lại

```sh
cd ~/kite && .venv/bin/python -m pytest -q
.venv/bin/kite --config /var/lib/kite/kite/config.yaml sandbox doctor
curl -s 127.0.0.1:8787/status
# bật worker Kaggle rồi:
.venv/bin/kite --config /var/lib/kite/kite/config.yaml sandbox run --repo ~/kite-sandbox --goal "..."
```