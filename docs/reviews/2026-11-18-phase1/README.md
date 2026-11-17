# Review 2026-11-18 — Phase 1 + task-type dispatcher

Hồ sơ minh chứng cho đợt review và cải tiến Kite ngày 18/11/2026.

| File | Nội dung |
|---|---|
| `review.md` | Báo cáo review và đề xuất cải tiến, export từ doc "Kite — Review & đề xuất cải tiến (có minh chứng)" |
| `evidence/test_evidence.py` | Script tái hiện 6 lỗi E1–E6. Mỗi test khẳng định hành vi **lỗi**: test pass nghĩa là lỗi vẫn còn, test fail nghĩa là lỗi đã được sửa |

Test hồi quy cho các lỗi đã sửa nằm trong bộ test chính: `tests/poc/kaggle_worker/test_phase1.py` (14 test).

## Cách chạy lại

Chạy từ thư mục gốc repo. File minh chứng nằm ngoài `tests/`, nên lệnh `pytest` mặc định không chạy nó.

```sh
python -m pytest docs/reviews/2026-11-18-phase1/evidence/test_evidence.py -q -rA
```

## Kết quả

| Lỗi | Mô tả | Trước khi sửa (commit `1a6e274`) | Sau Phase 1 |
|---|---|---|---|
| E1 | `permission.asked` không ai trả lời, run treo tới timeout | tái hiện | **vẫn còn** (Phase 3) |
| E2 | Task thành công không trừ quota GPU | tái hiện | đã sửa |
| E3 | `required_capabilities` không ảnh hưởng routing | tái hiện | đã sửa |
| E4 | `workflow start` không enqueue task vào store | tái hiện | đã sửa |
| E5 | Rule chặn `git push` bị vượt; shell chạy thẳng trên control plane | tái hiện | **vẫn còn** (Phase 3) |
| E6 | Chờ worker làm history Temporal tăng không giới hạn | tái hiện | đã sửa |

Output thực tế:

- Trước khi sửa: `6 passed` (cả 6 lỗi đều tái hiện).
- Sau Phase 1: `4 failed, 2 passed` (E2, E3, E4, E6 không còn tái hiện; E1, E5 còn).
- Bộ test chính sau Phase 1, trong môi trường không tải được Temporal test server: `153 passed, 8 failed`. 8 test hỏng đều là test Temporal cần tải test server, cũng hỏng như vậy ở commit gốc. Cần chạy lại trên máy có internet để xác nhận đủ 161/161.

## File đã sửa trong Phase 1

`protocol.py`, `routing.py`, `store.py`, `config.py`, `cli.py`, `temporal_workflow.py`, `README.md`, `kaggle_model_worker.ipynb` (đều trong `poc/kaggle_worker/`), và file test mới `tests/poc/kaggle_worker/test_phase1.py`.
