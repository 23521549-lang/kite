# swe-mini — bài mẫu kiểu SWE-bench

Một instance SWE-bench thu nhỏ (tự viết), để importer
(`bench_importers.import_swebench`) nhập thành bài Kite v1.

- **Chấm trục:** Coding + Đúng việc (`expected_files: [calc.py]` — file
  không phải test mà gold patch chạm tới).
- **Ngôn ngữ:** `en` (problem statement của SWE-bench viết bằng tiếng Anh).
- **`base_commit` trong JSON là placeholder `BASE_COMMIT`:** test thay
  bằng SHA thật của repo giả lập mà test tự dựng bằng git (repo mẫu
  `sample/calc` không tồn tại thật — đây là bài mẫu, không chạy trên repo
  thật). Test dựng repo giả có `calc.py` (chỉ có `mean`) + test cũ qua
  ở base; `test_patch` thêm `tests/test_median.py` (fail ở base, qua
  khi áp gold patch).
- **Giới hạn ghi rõ:** xem docstring module `bench_importers` (Python-centric,
  chỉ 2 trục, setup theo repo không tự động, cần checkout ở base_commit).
