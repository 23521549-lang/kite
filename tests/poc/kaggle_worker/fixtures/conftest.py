# Bench-task fixtures are DATA, not repo tests. Their tests/ dirs hold hidden
# tests for the bench tasks themselves and must never be collected by pytest
# (they would collide with real test modules during collection).
# Any future fixture task dir gets the same treatment automatically.
from pathlib import Path

collect_ignore = [
    str(p.relative_to(Path(__file__).parent))
    for p in Path(__file__).parent.glob("bench/*/tests")
]
