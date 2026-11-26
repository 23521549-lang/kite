# kite-tb-hello — bài mẫu Kite v1 kiểu Terminal-Bench

Dạng Kite v1 của bài `tb-hello/` (thư mục nguồn Terminal-Bench bên cạnh),
đúng như importer (`bench_importers.import_terminal_bench`) viết ra — trừ
`files/README.md` (ghi chú giải thích vì sao `files/` gần như rỗng; bản
importer viết ra để trống hoàn toàn).

- **Chấm trục:** Coding (duy nhất).
- **Ngôn ngữ:** `en`.
- **Nạp:** `bench_loader.load_task_dir` đạt, không lỗi.
- **Chạy thử tay:** `sh tb-solution/solution.sh` rồi
  `python -m pytest -q tests/test_outputs.py` (test kiểm file `answer.txt`).
