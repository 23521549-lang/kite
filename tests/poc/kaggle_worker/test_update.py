"""`update.plan`: the branch without an upstream reports a stable code, so the
web UI can translate it instead of showing the backend's Vietnamese string."""

from __future__ import annotations

import subprocess
from pathlib import Path

from poc.kaggle_worker import update


def _runner_ok_fetch_no_upstream(cmd, **kwargs):
    if cmd[-2:] == ["rev-parse", "@{u}"]:
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: no upstream configured\n")
    if cmd[-2:] == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_plan_without_upstream_carries_a_stable_error_code(tmp_path):
    report = update.plan(Path(tmp_path), runner=_runner_ok_fetch_no_upstream)
    assert report["error_code"] == "no_upstream"
    assert report["error"] == "nhánh hiện tại không theo dõi nhánh nào trên GitHub"
