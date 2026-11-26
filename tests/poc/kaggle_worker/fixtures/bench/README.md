# Bài mẫu bench v1 (worker (b) — Gói 7)

Thư mục này KHÔNG phải một bench root (nó trộn thư mục nguồn bên ngoài với
bài Kite v1) — đừng chạy `discover_tasks` lên cả thư mục này.

| Thư mục / file | Loại | Chấm trục |
|---|---|---|
| `tb-hello/` | Nguồn Terminal-Bench (tự viết) → importer TB | Coding |
| `kite-tb-hello/` | Bài Kite v1 kiểu Terminal-Bench (dạng đã nhập của `tb-hello/`) | Coding |
| `swe-mini-instance.json` + `swe-mini-README.md` | Instance SWE-bench thu nhỏ (tự viết) → importer SWE-bench | Coding, Đúng việc |
| `kite-swe-mini/` | Bài Kite v1 kiểu SWE-bench (dạng đã nhập của instance) | Coding, Đúng việc |

Cả hai bài Kite v1 đều nạp được bằng `bench_loader.load_task_dir` và
validate ĐẠT (xem `test_bench_loader.py`). Cả hai bài nguồn đều nhập được
bằng `bench_importers` (xem `test_bench_importers.py`).
