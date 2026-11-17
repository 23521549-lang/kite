from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from poc.kaggle_worker.protocol import WorkerResult
try:
    from poc.kaggle_worker.integration import (
        ReviewBundle, StaleWorkerResult, build_review_bundle,
    )
except ModuleNotFoundError:
    ReviewBundle = StaleWorkerResult = build_review_bundle = None


def valid_result(**changes):
    data = dict(task_id="task-a", lease_id="lease-a", worker_id="worker-a",
                base_repo_head="head-a", patch="diff --git a/a b/a", summary="change done",
                usage_gpu_seconds=25)
    data.update(changes)
    return WorkerResult(**data)


def build(result=None, **expected):
    if build_review_bundle is None:
        pytest.skip("review integration has not been implemented yet")
    values = dict(expected_repo_head="head-a", expected_task_id="task-a",
                  expected_lease_id="lease-a", expected_worker_id="worker-a")
    values.update(expected)
    return build_review_bundle(result or valid_result(), **values)


def test_integration_module_exists():
    assert build_review_bundle is not None


def test_valid_result_becomes_review_bundle_without_loss():
    result = valid_result()
    bundle = build(result)
    assert isinstance(bundle, ReviewBundle)
    assert bundle.task_id == result.task_id
    assert bundle.lease_id == result.lease_id
    assert bundle.worker_id == result.worker_id
    assert bundle.base_repo_head == result.base_repo_head
    assert bundle.patch == result.patch
    assert bundle.summary == result.summary
    assert bundle.usage_gpu_seconds == result.usage_gpu_seconds


def test_stale_repo_head_is_rejected():
    if build_review_bundle is None:
        pytest.skip("review integration has not been implemented yet")
    with pytest.raises(StaleWorkerResult, match="HEAD"):
        build(valid_result(base_repo_head="old-head"))


@pytest.mark.parametrize("field,expected", [
    ("task_id", "wrong-task"),
    ("lease_id", "wrong-lease"),
    ("worker_id", "wrong-worker"),
])
def test_mismatched_identity_is_rejected(field, expected):
    if build_review_bundle is None:
        pytest.skip("review integration has not been implemented yet")
    with pytest.raises(StaleWorkerResult, match=field):
        build(valid_result(**{field: expected}))


def test_review_bundle_creation_does_not_modify_repository_files(tmp_path):
    sentinel = tmp_path / "tracked.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    build(valid_result())
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert after == before

