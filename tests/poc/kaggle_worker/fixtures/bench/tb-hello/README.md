# tb-hello — bài mẫu kiểu Terminal-Bench

Bài nguồn Terminal-Bench (tự viết, nhỏ, chạy nhanh), để importer
(`bench_importers.import_terminal_bench`) nhập thành bài Kite v1.

- **Chấm trục:** Coding (duy nhất — xem `bench_importers` vì sao importer
  không tự suy `expected_files` cho trục Đúng việc).
- **Ngôn ngữ:** `en` (instruction của Terminal-Bench viết bằng tiếng Anh).
- **Chạy thử tay:** `sh solution.sh` rồi `python -m pytest -q tests/test_outputs.py`
  (test kiểm file `answer.txt` chứa đúng `hello kite`).
