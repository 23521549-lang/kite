# kite-swe-mini — bài mẫu Kite v1 kiểu SWE-bench

Dạng Kite v1 của instance `swe-mini-instance.json` (bên cạnh), đúng như
importer (`bench_importers.import_swebench`) viết ra.

- **Chấm trục:** Coding + Đúng việc (`expected_files: [calc.py]`).
- **Ngôn ngữ:** `en`.
- **Nạp:** `bench_loader.load_task_dir` đạt, không lỗi.
- **Lưu ý:** repo `sample/calc` và `base_commit: abc1234` là GIẢ — bài mẫu
  chỉ để nạp/validate và để test dispatch (stub `run_code`), KHÔNG chạy
  được trên repo thật. Bài chạy thật cần repo thật + SHA thật (xem test
  importer, nơi repo giả được dựng bằng git và test patch được áp thật).
