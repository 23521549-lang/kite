"""Hidden tests for the tb-hello sample (kept verbatim on import)."""

from pathlib import Path


def test_answer_exists():
    answer = Path("answer.txt")
    assert answer.is_file(), "answer.txt chưa được tạo"
    assert answer.read_text(encoding="utf-8").strip() == "hello kite"
