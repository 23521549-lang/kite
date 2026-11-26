"""Engine chưng cất tài liệu kinh nghiệm (Gói 10, worker (a)): luật, gộp trùng,
gắn cờ bài học bị bác bỏ, render Markdown — toàn bộ kiểm bằng máy."""

from __future__ import annotations

from poc.kaggle_worker import experience


def test_normalize_gop_khoang_trang_chu_thuong_va_bo_dau_cau_cuoi():
    assert experience._normalize_text("  Chạy  Test   Trước\nKhi Commit. ") == "chạy test trước khi commit"
    assert experience._normalize_text("Không Đổi Tên Hàm Public!") == "không đổi tên hàm public"
    assert experience._normalize_text("giữ nguyên") == "giữ nguyên"


def test_content_words_chi_giu_tu_dai_va_bo_stopword():
    assert experience._content_words("Không dùng print để debug khi test rớt") == {"print", "debug", "test"}
    assert experience._content_words("các những với cho của") == set()
    assert experience._content_words("Chạy test trước khi commit code") == {"chạy", "test", "commit", "code"}


def test_distill_bad_thanh_dont_good_thanh_do_khong_verdict_thanh_convention():
    rules = experience.distill(
        [{"date": "2026-11-20", "task_id": "t1", "text": "Không đổi tên hàm public"},
         {"date": "2026-11-20", "task_id": "t2", "text": "Viết test biên cho hàm mới"},
         {"date": "2026-11-20", "task_id": "t3", "text": "Commit gom theo task"}],
        [{"task_id": "t1", "verdict": "bad", "text": "Không đổi tên hàm public"},
         {"task_id": "t2", "verdict": "good", "text": "Viết test biên cho hàm mới"}],
        [])
    assert [(r["text"], r["kind"]) for r in rules] == [
        ("Không đổi tên hàm public", "dont"),
        ("Viết test biên cho hàm mới", "do"),
        ("Commit gom theo task", "convention"),
    ]
    # Bài học và ghi chú cùng task/text gộp thành một rule, cộng dồn nguồn.
    assert rules[0]["sources"] == [
        {"task_id": "t1", "date": "2026-11-20", "verdict": "bad"},
        {"task_id": "t1", "date": None, "verdict": "bad"},
    ]
    assert all(r["refuted"] is False for r in rules)


def test_distill_gop_trung_giu_text_dau_cong_don_sources():
    rules = experience.distill(
        [{"date": "2026-11-20", "task_id": "t1", "text": "Chạy test trước khi commit."},
         {"date": "2026-11-21", "task_id": "t2", "text": "  chạy   test trước khi commit  "}],
        [], [])
    assert len(rules) == 1
    assert rules[0]["text"] == "Chạy test trước khi commit."
    assert rules[0]["sources"] == [
        {"task_id": "t1", "date": "2026-11-20", "verdict": None},
        {"task_id": "t2", "date": "2026-11-21", "verdict": None},
    ]


def test_distill_bai_hoc_lap_tren_2_task_thanh_pattern_va_note_good_thanh_rule_do():
    rules = experience.distill(
        [{"date": "2026-11-20", "task_id": "t1", "text": "Quên cập nhật README"},
         {"date": "2026-11-21", "task_id": "t2", "text": "Quên cập nhật README"}],
        [{"task_id": "t3", "verdict": "good", "text": "README cập nhật đầy đủ"}],
        [])
    assert [(r["text"], r["kind"]) for r in rules] == [
        ("Quên cập nhật README", "pattern"),
        ("README cập nhật đầy đủ", "do"),
    ]


def test_distill_bo_text_rong_va_dau_vao_rong():
    assert experience.distill([], [], []) == []
    assert experience.distill([{"date": "2026-11-20", "task_id": "t1", "text": "   "}],
                              [{"task_id": "t1", "verdict": "bad", "text": ""}], []) == []


def _lesson(text="Không đổi tên hàm public", date="2026-11-20", task="t1"):
    return {"date": date, "task_id": task, "text": text}


def _bad_note(text, date, task="t2"):
    return {"task_id": task, "verdict": "bad", "text": text, "date": date}


def test_refuted_note_bad_sau_chia_se_tu_noi_dung_cung_repo():
    rules = experience.distill(
        [_lesson()],
        [_bad_note("Đổi tên hàm public làm vỡ import", "2026-11-22")],
        [{"task_id": "t1", "repo": "/r/a"}, {"task_id": "t2", "repo": "/r/a"}])
    assert rules[0]["refuted"] is True          # rule vẫn còn, chỉ gắn cờ
    # Không có ghi chú nào trên chính t1 nên không có verdict -> convention;
    # cờ bác bỏ vẫn gắn vì note bad sau đó (t2) chia sẻ từ "public".
    assert rules[0]["kind"] == "convention"


def test_khong_refuted_khi_note_bad_truoc_bai_hoc():
    rules = experience.distill(
        [_lesson()], [_bad_note("Đổi tên hàm public làm vỡ import", "2026-11-19")], [])
    assert rules[0]["refuted"] is False


def test_khong_refuted_khi_note_bad_cung_ngay_hoac_khong_ngay():
    rules = experience.distill(
        [_lesson()], [_bad_note("Đổi tên hàm public làm vỡ import", "2026-11-20")], [])
    assert rules[0]["refuted"] is False
    rules = experience.distill(
        [_lesson()], [{"task_id": "t2", "verdict": "bad", "text": "Đổi tên hàm public làm vỡ import"}], [])
    assert rules[0]["refuted"] is False


def test_khong_refuted_khi_khac_repo_hoac_khong_chia_se_tu_noi_dung():
    runs = [{"task_id": "t1", "repo": "/r/a"}, {"task_id": "t2", "repo": "/r/b"}]
    rules = experience.distill(
        [_lesson()], [_bad_note("Đổi tên hàm public làm vỡ import", "2026-11-22")], runs)
    assert rules[0]["refuted"] is False
    rules = experience.distill(
        [_lesson()], [_bad_note("Quên viết changelog cho bản mới", "2026-11-22")], [])
    assert rules[0]["refuted"] is False


def test_refuted_khi_khong_tra_duoc_repo_va_ngay_note_lay_tu_at():
    rules = experience.distill(
        [_lesson()],
        [{"task_id": "t2", "verdict": "bad", "text": "Đổi tên hàm public làm vỡ import",
          "at": "2026-11-22T10:00:00"}],
        [])
    assert rules[0]["refuted"] is True
    # Bài học thiếu ngày thì lấy ngày từ lần chạy (tasks.jsonl) để so "sau".
    rules = experience.distill(
        [{"task_id": "t1", "text": "Không đổi tên hàm public"}],
        [_bad_note("Đổi tên hàm public làm vỡ import", "2026-11-22")],
        [{"task_id": "t1", "repo": "/r/a", "at": "2026-11-20T09:00:00"}])
    assert rules[0]["refuted"] is True
    assert rules[0]["sources"][0]["date"] == "2026-11-20"


def test_render_document_dung_dinh_dang_hop_dong_goi_10():
    rules = [
        {"text": "Viết test biên cho hàm mới", "kind": "do", "refuted": False,
         "sources": [{"task_id": "t2", "date": "2026-11-20", "verdict": "good"}]},
        {"text": "Không đổi tên hàm public", "kind": "dont", "refuted": False,
         "sources": [{"task_id": "t1", "date": "2026-11-20", "verdict": "bad"}]},
        {"text": "Quên cập nhật README", "kind": "pattern", "refuted": False,
         "sources": [{"task_id": "t1", "date": "2026-11-20", "verdict": None},
                     {"task_id": "t3", "date": "2026-11-21", "verdict": None}]},
        {"text": "Commit gom theo task", "kind": "convention", "refuted": False,
         "sources": [{"task_id": "t4", "date": "2026-11-21", "verdict": None}]},
        {"text": "Luôn dùng print để debug", "kind": "dont", "refuted": True,
         "sources": [{"task_id": "t5", "date": "2026-11-20", "verdict": None}]},
    ]
    doc = experience.render_document("/r/kite", rules, version=3, updated="2026-11-25")
    assert doc == (
        "# Kite: tài liệu kinh nghiệm — /r/kite (v3, 2026-11-25)\n"
        "<!-- kite:experience version=3 updated=2026-11-25 -->\n"
        "\n## Nên làm\n"
        "- Viết test biên cho hàm mới [nguồn: task t2 / bài học 2026-11-20]\n"
        "\n## Không nên làm\n"
        "- Không đổi tên hàm public [nguồn: task t1 / bài học 2026-11-20]\n"
        "\n## Mẫu lỗi hay lặp\n"
        "- Quên cập nhật README [nguồn: task t1 / bài học 2026-11-20, task t3 / bài học 2026-11-21]\n"
        "\n## Quy ước repo\n"
        "- Commit gom theo task [nguồn: task t4 / bài học 2026-11-21]\n"
        "\n## Cần xem lại\n"
        "Các quy tắc dưới đây đã bị một nhận xét bad sau đó bác bỏ; "
        "giữ lại để xem xét, không áp dụng máy móc.\n"
        "- Luôn dùng print để debug [nguồn: task t5 / bài học 2026-11-20] (cần xem lại)\n"
    )
    # Rule bị bác bỏ CHỈ ở mục riêng, không lặp lại ở mục kind của nó.
    assert doc.count("Luôn dùng print để debug") == 1


def test_render_document_rong_van_du_khung_va_ghi_chu_chua_co_bai_hoc():
    doc = experience.render_document("kite", [], version=1, updated="2026-11-25")
    assert doc.startswith("# Kite: tài liệu kinh nghiệm — kite (v1, 2026-11-25)\n"
                          "<!-- kite:experience version=1 updated=2026-11-25 -->\n")
    assert "Chưa có bài học nào được chưng cất cho repo này." in doc
    for heading in ("## Nên làm", "## Không nên làm", "## Mẫu lỗi hay lặp", "## Quy ước repo"):
        assert heading in doc
    assert "\n- " not in doc and "Cần xem lại" not in doc


def test_render_document_tu_choi_version_ngay_kind_sai():
    import pytest

    rules = [{"text": "x", "kind": "do", "refuted": False, "sources": []}]
    with pytest.raises(ValueError):
        experience.render_document("r", rules, version=0, updated="2026-11-25")
    with pytest.raises(ValueError):
        experience.render_document("r", rules, version=1, updated="25/11/2026")
    with pytest.raises(ValueError):
        experience.render_document("r", [{"text": "x", "kind": "maybe", "sources": []}],
                                   version=1, updated="2026-11-25")


def test_render_document_ngay_mac_dinh_lay_tu_clock_va_vong_tron_distill_render():
    import re

    doc = experience.render_document("r", [], version=2, updated=None,
                                     clock=lambda: 1764028800.0)
    assert re.search(r"updated=\d{4}-\d{2}-\d{2}", doc)
    rules = experience.distill(
        [_lesson("Không đổi tên hàm public")],
        [_bad_note("Đổi tên hàm public làm vỡ import", "2026-11-22")], [])
    doc = experience.render_document("kite", rules, version=1, updated="2026-11-25")
    assert "## Cần xem lại" in doc and "(cần xem lại)" in doc
    assert "Không đổi tên hàm public" in doc


def test_render_document_khong_lap_task_trong_trich_nguon():
    # Cùng một task vừa là nguồn bài học (có ngày) vừa là nguồn ghi chú (không
    # ngày): chỉ giữ một mảnh trích nguồn — mảnh có ngày.
    rules = [{"text": "Thiếu test biên", "kind": "dont", "refuted": False,
              "sources": [{"task_id": "task-001", "date": "2025-11-24", "verdict": "bad"},
                          {"task_id": "task-001", "verdict": "bad"}]}]
    doc = experience.render_document("kite", rules, version=1, updated="2026-11-25")
    assert doc.count("task task-001") == 1
    assert "task task-001 / bài học 2025-11-24" in doc
