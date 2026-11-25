"""Bench criteria v1: rounding at the boundary, Dreyfus lookup on the exact
thresholds, official overall, the unstable flag, version separation."""

from __future__ import annotations

import pytest

from poc.kaggle_worker import bench_criteria as bc


def _axes(value: float) -> dict[str, float]:
    return {axis: value for axis in bc.AXES}


def _run(overall: float | None, version: str = bc.CRITERIA_VERSION) -> dict:
    return {"criteria_version": version, "overall": overall}


# ---------------------------------------------------------------- rounding
def test_round_score_is_half_up_not_bankers():
    # Python's round() is banker's: round(19.95, 1) == 19.9. v1 freezes half-up.
    assert bc.round_score(19.95) == 20.0
    assert bc.round_score(19.94) == 19.9
    assert bc.round_score(59.95) == 60.0
    assert bc.round_score(0.05) == 0.1


def test_round_score_rejects_non_numbers():
    for bad in (None, True, float("nan"), float("inf"), "80"):
        with pytest.raises(ValueError):
            bc.round_score(bad)


# ---------------------------------------------------------------- axis score
def test_axis_score_means_tasks_and_skips_none():
    assert bc.axis_score([80, 90]) == 85.0
    assert bc.axis_score([80, None, 90]) == 85.0
    assert bc.axis_score([]) is None
    assert bc.axis_score([None]) is None


def test_axis_score_rejects_out_of_range():
    with pytest.raises(ValueError):
        bc.axis_score([101])
    with pytest.raises(ValueError):
        bc.axis_score([-1])


# ---------------------------------------------------------------- run overall
def test_run_overall_is_unweighted_mean_of_eight():
    scores = _axes(80.0)
    scores["Coding"] = 40.0  # (40 + 7*80) / 8 = 75.0
    assert bc.run_overall(scores) == 75.0


def test_run_overall_rounds_before_level_lookup():
    # Mean 19.95 -> rounded 20.0 -> L2, not L1.
    scores = _axes(19.95)
    assert bc.run_overall(scores) == 20.0
    assert bc.level_for(bc.run_overall(scores)).number == 2


def test_run_overall_needs_all_eight_axes():
    scores = _axes(80.0)
    del scores["Trí nhớ"]
    assert bc.run_overall(scores) is None
    scores = _axes(80.0)
    scores["Trí nhớ"] = None
    assert bc.run_overall(scores) is None


# ---------------------------------------------------------------- Dreyfus
@pytest.mark.parametrize("score,number", [
    (0, 1), (19.9, 1), (20, 2), (20.0, 2), (39.9, 2), (40, 3),
    (59.9, 3), (60, 4), (79.9, 4), (80, 5), (99.9, 5), (100, 5),
])
def test_level_for_on_exact_boundaries(score, number):
    level = bc.level_for(score)
    assert level.number == number


def test_level_names_are_bilingual_and_match_contract():
    assert [(lv.name_en, lv.name_vi) for lv in bc.LEVELS] == [
        ("Novice", "Mới bắt đầu"),
        ("Advanced Beginner", "Mới có kinh nghiệm"),
        ("Competent", "Vững việc"),
        ("Proficient", "Thành thạo"),
        ("Expert", "Chuyên gia"),
    ]


def test_level_for_none_is_none_and_rejects_out_of_range():
    assert bc.level_for(None) is None
    with pytest.raises(ValueError):
        bc.level_for(100.1)
    with pytest.raises(ValueError):
        bc.level_for(-0.1)


def test_level_i18n_keys():
    assert bc.level_i18n_key(2) == "bench.level.2.name"
    assert bc.level_i18n_key(2, "desc") == "bench.level.2.desc"
    assert bc.LEVELS[1].i18n_name_key == "bench.level.2.name"
    with pytest.raises(ValueError):
        bc.level_i18n_key(6)
    with pytest.raises(ValueError):
        bc.level_i18n_key(1, "title")


# ---------------------------------------------------------------- official overall
def test_official_overall_needs_three_runs():
    two = bc.official_overall([_run(80.0), _run(82.0)])
    assert two["overall"] == 81.0
    assert two["official"] is False
    assert two["runs"] == 2
    three = bc.official_overall([_run(80.0), _run(82.0), _run(81.0)])
    assert three["overall"] == 81.0
    assert three["official"] is True
    assert three["runs"] == 3


def test_official_overall_rounds_then_looks_up_level():
    # Mean of 19.9, 20.0, 20.0 = 19.966.. -> 20.0 -> L2.
    summary = bc.official_overall([_run(19.9), _run(20.0), _run(20.0)])
    assert summary["overall"] == 20.0
    assert summary["level"]["level"] == 2
    assert summary["official"] is True


def test_official_overall_empty():
    summary = bc.official_overall([])
    assert summary["overall"] is None
    assert summary["official"] is False
    assert summary["level"] is None
    assert summary["level_unstable"] is False


def test_level_unstable_when_runs_straddle_a_threshold():
    stable = bc.official_overall([_run(81.0), _run(82.0), _run(83.0)])
    assert stable["level_unstable"] is False
    straddling = bc.official_overall([_run(79.9), _run(80.0), _run(80.1)])
    assert straddling["level_unstable"] is True
    # Same level on both sides of the mean is not enough: one run in L4 is enough.
    assert bc.official_overall([_run(80.0), _run(80.0), _run(79.9)])["level_unstable"] is True


def test_runs_of_another_version_are_never_averaged_in():
    summary = bc.official_overall([_run(80.0), _run(82.0), _run(100.0, version="v2")])
    assert summary["overall"] == 81.0
    assert summary["runs"] == 2
    assert summary["excluded_runs"] == 1
    assert summary["official"] is False  # only 2 runs of v1


def test_runs_of_another_gate_set_are_never_averaged_in():
    runs = [{**_run(80.0), "gate_set": "gate-v1"},
            {**_run(82.0), "gate_set": "gate-v1"},
            {**_run(100.0), "gate_set": "gate-v2"}]
    summary = bc.official_overall(runs, gate_set="gate-v1")
    assert summary["overall"] == 81.0
    assert summary["runs"] == 2
    assert summary["excluded_runs"] == 1
    annotated, _ = bc.annotate_runs(runs, gate_set="gate-v1")
    assert annotated[2]["level_unstable"] is None


def test_runs_with_no_overall_do_not_count():
    summary = bc.official_overall([_run(80.0), _run(82.0), _run(None)])
    assert summary["overall"] == 81.0
    assert summary["runs"] == 2
    assert summary["official"] is False


# ---------------------------------------------------------------- stamp + record
def test_make_run_stamp_offline_by_default():
    stamp = bc.make_run_stamp(system_config={"worker": "kaggle-han", "model": "qwen3.6",
                                             "harness": "bench.py", "harness2": "harness2.py"})
    assert stamp["criteria_version"] == "v1"
    assert stamp["system_config"]["model"] == "qwen3.6"
    assert stamp["network"] == {"mode": "offline", "hosts": []}


def test_make_run_stamp_network_allowlist():
    stamp = bc.make_run_stamp(system_config={"worker": "w", "model": "m"},
                              network={"mode": "allowlist", "hosts": ["api.example.com"]})
    assert stamp["network"] == {"mode": "allowlist", "hosts": ["api.example.com"]}
    with pytest.raises(ValueError):
        bc.make_run_stamp(system_config={}, network={"mode": "bridge", "hosts": []})
    with pytest.raises(ValueError):
        bc.make_run_stamp(system_config=[], network=None)


def test_build_run_result_shape():
    record = bc.build_run_result(axis_scores=_axes(70.0),
                                 system_config={"worker": "w", "model": "m"})
    assert record["criteria_version"] == "v1"
    assert record["gate_set"] == "gate-v1"
    assert record["overall"] == 70.0
    assert record["level"]["level"] == 4
    assert record["level"]["name_vi"] == "Thành thạo"
    assert record["level_unstable"] is None  # only meaningful across runs
    assert set(record["axes"]) == set(bc.AXES)
    assert record["network"]["mode"] == "offline"


def test_annotate_runs_fills_unstable_flag():
    runs = [_run(79.9), _run(80.0), _run(80.1)]
    annotated, summary = bc.annotate_runs(runs)
    assert summary["level_unstable"] is True
    assert all(r["level_unstable"] is True for r in annotated)
    other = [_run(90.0, version="v2")]
    annotated, _ = bc.annotate_runs([_run(80.0), _run(81.0), _run(82.0), *other])
    assert annotated[-1]["level_unstable"] is None


def test_radar_data_keeps_axis_order_and_none():
    scores = _axes(50.0)
    scores["Trí nhớ"] = None
    radar = bc.radar_data(scores)
    assert [row["axis"] for row in radar] == list(bc.AXES)
    assert [row["key"] for row in radar] == [bc.AXIS_KEYS[a] for a in bc.AXES]
    assert radar[-1] == {"axis": "Trí nhớ", "key": "tri-nho", "score": None}
    assert radar[0]["score"] == 50.0
