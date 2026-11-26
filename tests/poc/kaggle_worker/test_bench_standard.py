"""Bench standard v1: the validator accepts a good task and rejects each kind
of bad task with a structured error naming the missing field."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from poc.kaggle_worker import bench_standard as bs


def valid_data(**overrides) -> dict:
    data = {
        "id": "t1-speed",
        "goal": "Sửa hàm mean để test qua.",
        "kind": "code",
        "language": "vi",
        "axes": ["Coding", "Tốc độ"],
        "difficulty": "dễ",
        "timeouts": {"setup": 600, "test": 300},
        "environment": {"setup": "pip install -q -r requirements.txt"},
        "par_seconds": 120,
    }
    data.update(overrides)
    return data


def write_task(tmp_path: Path, data: dict, *, with_files: bool = True, with_tests: bool = True) -> Path:
    task_dir = tmp_path / "t1-speed"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    if with_files:
        (task_dir / "files").mkdir(exist_ok=True)
        (task_dir / "files" / "stats.py").write_text("def mean(v): return 0\n", encoding="utf-8")
    if with_tests:
        (task_dir / "tests").mkdir(exist_ok=True)
        (task_dir / "tests" / "test_stats.py").write_text("def test_mean(): pass\n", encoding="utf-8")
    return task_dir / "task.yaml"


def errors_for(data: dict) -> list[bs.ValidationError]:
    return bs.validate_task_data(data)


def fields(errors) -> list[str]:
    return [e.field for e in errors]


# ---------------------------------------------------------------- happy path
def test_valid_task_parses(tmp_path):
    path = write_task(tmp_path, valid_data())
    task, errors = bs.load_task_file(path)
    assert errors == []
    assert task is not None
    assert task.id == "t1-speed"
    assert task.axes == ("Coding", "Tốc độ")
    assert task.par_seconds == 120
    assert task.source_path == path
    assert task.to_dict()["axes"] == ["Coding", "Tốc độ"]


def test_valid_task_text_round_trip():
    task, errors = bs.load_task_text(yaml.safe_dump(valid_data(), allow_unicode=True))
    assert errors == [] and task is not None and task.kind == "code"


def test_repo_kind_uses_repo_and_base_commit_not_files(tmp_path):
    data = valid_data(kind="repo", repo="owner/repo", base_commit="abc1234",
                      axes=["Coding"], par_seconds=None)
    data.pop("par_seconds")
    path = write_task(tmp_path, data, with_files=False, with_tests=True)
    task, errors = bs.load_task_file(path)
    assert errors == [] and task is not None
    assert task.repo == "owner/repo" and task.base_commit == "abc1234"


def test_parse_task_data_raises_structured_error():
    with pytest.raises(bs.BenchTaskInvalid) as excinfo:
        bs.parse_task_data(valid_data(kind="nope"))
    assert excinfo.value.errors
    assert all(isinstance(e, bs.ValidationError) for e in excinfo.value.errors)
    assert "kind" in str(excinfo.value)


# ---------------------------------------------------------------- enums
@pytest.mark.parametrize("kind", ["code", "repo", "scenario", "qa", "terminal"])
def test_all_kinds_valid(kind):
    data = valid_data(kind=kind)
    if kind == "scenario":
        data["scenario"] = {"prompt": "Làm gì đó?", "expect": ["start_task"]}
    if kind == "repo":
        data.update(repo="o/r", base_commit="abc1234")
    assert errors_for(data) == []


def test_bad_kind_and_language_are_invalid_enum():
    errs = errors_for(valid_data(kind="chat"))
    assert any(e.field == "kind" and e.code == "invalid_enum" for e in errs)
    errs = errors_for(valid_data(language="fr"))
    assert any(e.field == "language" and e.code == "invalid_enum" for e in errs)


def test_bad_axis_is_invalid_enum_and_duplicate_is_flagged():
    errs = errors_for(valid_data(axes=["Coding", "Nhanh"]))
    assert any(e.field == "axes[1]" and e.code == "invalid_enum" for e in errs)
    errs = errors_for(valid_data(axes=["Coding", "Coding"]))
    assert any(e.code == "duplicate_axis" for e in errs)
    errs = errors_for(valid_data(axes=[]))
    assert any(e.field == "axes" for e in errs)


def test_missing_required_top_level_fields():
    data = valid_data()
    for key in ("id", "goal", "kind", "language", "axes", "difficulty", "timeouts", "environment"):
        errs = errors_for({k: v for k, v in data.items() if k != key})
        assert any(e.field == key and e.code == "missing_field" for e in errs), key


def test_top_level_must_be_a_mapping():
    assert bs.validate_task_data(["not", "a", "mapping"])[0].code == "invalid_type"
    assert bs.validate_task_data(None)[0].code == "invalid_type"
    task, errs = bs.load_task_text("")
    assert task is None and errs and errs[0].code == "missing_field"


def test_bad_yaml_is_one_structured_error():
    task, errs = bs.load_task_text("id: [unclosed")
    assert task is None
    assert len(errs) == 1 and errs[0].code == "yaml_error"


def test_missing_file_is_one_structured_error(tmp_path):
    task, errs = bs.load_task_file(tmp_path / "nope" / "task.yaml")
    assert task is None
    assert len(errs) == 1 and errs[0].code == "missing_file"


# ---------------------------------------------------------------- per-axis required fields
@pytest.mark.parametrize("axes,missing", [
    (["Tốc độ"], "par_seconds"),
    (["Trí nhớ"], "lesson_ref"),
    (["Phân tích"], "expected_files"),
    (["Đúng việc"], "expected_files"),
])
def test_axis_without_its_required_field_is_invalid(axes, missing):
    data = valid_data(axes=axes)
    data.pop("par_seconds", None)
    errs = errors_for(data)
    assert any(e.field == missing and e.code == "missing_required_for_axis" for e in errs), errs


def test_axes_sharing_one_field_need_it_once():
    # Phân tích + Đúng việc both need expected_files: one error, not two.
    data = valid_data(axes=["Phân tích", "Đúng việc"])
    data.pop("par_seconds", None)
    errs = errors_for(data)
    assert [e for e in errs if e.code == "missing_required_for_axis"] != []
    assert len([e for e in errs if e.field == "expected_files"]) == 1


def test_axis_fields_satisfied():
    data = valid_data(axes=["Tốc độ", "Trí nhớ", "Phân tích", "Đúng việc"],
                      lesson_ref="lessons/2026-11-20.md#mean",
                      expected_files=["stats.py"])
    assert errors_for(data) == []


def test_par_seconds_must_be_positive_number():
    for bad in (0, -5, "120", True):
        errs = errors_for(valid_data(par_seconds=bad))
        assert any(e.field == "par_seconds" for e in errs), bad


def test_stability_needs_no_per_task_field():
    # Ổn định is measured at run level; a task may claim it with no extra field.
    data = valid_data(axes=["Ổn định"])
    data.pop("par_seconds", None)
    assert errors_for(data) == []
    # ...and an optional fault block must still be a mapping when present.
    assert errors_for(valid_data(axes=["Ổn định"], fault={"type": "kill-gpu"})) == []
    errs = errors_for(valid_data(axes=["Ổn định"], fault="kill-gpu"))
    assert any(e.field == "fault" for e in errs)


# ---------------------------------------------------------------- scenario block
def test_scenario_kind_requires_scenario_block():
    data = valid_data(kind="scenario")
    data.pop("par_seconds", None)
    errs = errors_for(data)
    assert any(e.field == "scenario" and e.code == "missing_field" for e in errs)


def test_scenario_block_mirrors_bench_scenario():
    data = valid_data(kind="scenario", axes=["Coding"], par_seconds=None,
                      scenario={"prompt": "Giao task?", "expect": ["start_task"],
                                "must": ["chat-bench"], "forbid": ["bash"]})
    data.pop("par_seconds")
    assert errors_for(data) == []
    bad = valid_data(kind="scenario", axes=["Coding"],
                     scenario={"expect": ["start_task"]})  # no prompt
    assert any(e.field == "scenario.prompt" for e in errors_for(bad))
    bad = valid_data(kind="scenario", axes=["Coding"],
                     scenario={"prompt": "x"})  # no expect
    assert any(e.field == "scenario.expect" for e in errors_for(bad))


# ---------------------------------------------------------------- network hosts
@pytest.mark.parametrize("host", [
    "api.example.com", "localhost", "a-b.c-d.example.co.uk", "x1.y2",
])
def test_valid_hostnames(host):
    assert bs.is_valid_hostname(host) is True


@pytest.mark.parametrize("host", [
    "https://api.example.com", "http://x.com", "*.example.com", "api.*.com",
    "1.2.3.4", "::1", "2001:db8::1", "api.example.com:443", "api_example.com",
    "-bad.example.com", "bad-.example.com", "api.example.com.", ".example.com",
    "exa mple.com", "", None, 123, "a" * 64 + ".com", "api/example",
])
def test_invalid_hostnames(host):
    assert bs.is_valid_hostname(host) is False


def test_network_hosts_rules():
    assert bs.validate_network_hosts(["api.example.com"]) == []
    errs = bs.validate_network_hosts([])
    assert len(errs) == 1 and errs[0].code == "empty_hosts"
    errs = bs.validate_network_hosts([f"h{i}.example.com" for i in range(11)])
    assert any(e.code == "too_many_hosts" for e in errs)
    assert bs.validate_network_hosts([f"h{i}.example.com" for i in range(10)]) == []
    errs = bs.validate_network_hosts(["ok.example.com", "https://bad.example.com", "1.2.3.4"])
    assert [e.field for e in errs] == ["environment.network.hosts[1]", "environment.network.hosts[2]"]
    assert all(e.code == "invalid_hostname" for e in errs)
    assert bs.validate_network_hosts("api.example.com")[0].code == "invalid_type"


def test_network_declaration_rules_in_task():
    # Offline task: no network key at all is fine.
    assert errors_for(valid_data()) == []
    # network: under requires: is invalid -- the only declaration is environment.network.
    data = valid_data(environment={"requires": {"network": ["api.example.com"]}})
    assert any(e.field == "environment.requires.network" for e in errors_for(data))
    # network block without hosts / with an unknown key / empty hosts: invalid.
    for network in ({"hosts": []}, {"hosts": ["*.example.com"]}, {"mode": "allowlist"},
                    {"hosts": ["api.example.com"], "proxy": "x"}):
        data = valid_data(environment={"network": network})
        assert errors_for(data), network
    # A valid allowlist passes.
    data = valid_data(environment={"network": {"hosts": ["api.example.com", "pypi.org"]}})
    assert errors_for(data) == []


def test_repo_and_base_commit_must_come_together():
    assert any(e.field == "base_commit" for e in errors_for(valid_data(repo="o/r")))
    assert any(e.field == "repo" for e in errors_for(valid_data(base_commit="abc1234")))
    assert any(e.field == "base_commit" for e in errors_for(valid_data(base_commit="not-a-sha")))


# ---------------------------------------------------------------- layout
def test_layout_requires_files_or_repo(tmp_path):
    data = valid_data()
    path = write_task(tmp_path, data, with_files=False, with_tests=True)
    _, errs = bs.load_task_file(path)
    assert any(e.field == "files" for e in errs)


def test_layout_requires_tests_dir_for_code_kind(tmp_path):
    data = valid_data()
    path = write_task(tmp_path, data, with_files=True, with_tests=False)
    _, errs = bs.load_task_file(path)
    assert any(e.field == "tests" for e in errs)


def test_layout_scenario_kind_needs_no_tests_dir(tmp_path):
    data = valid_data(kind="scenario", axes=["Coding"], par_seconds=None,
                      scenario={"prompt": "Làm gì?", "expect": []})
    data.pop("par_seconds")
    path = write_task(tmp_path, data, with_files=True, with_tests=False)
    task, errs = bs.load_task_file(path)
    assert errs == [] and task is not None
