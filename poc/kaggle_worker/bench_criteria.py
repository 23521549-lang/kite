"""Bench criteria v1: the eight axes, the Dreyfus scale and the overall score.

Pure functions only -- no I/O, no imports from the rest of Kite. Worker (b)
(the loader/importers) and worker (c) (the Bench page, the run driver) use
exactly these names, so a change here is a contract change: see
``docs/bench-standard-v1.md`` section 7 (changing a threshold, the formula
or a required field means a new criteria version, never a silent edit of v1).

Rules frozen in v1 (plan ``ke-hoach-toi-uu-ui-kite.md`` Gói 7 + contract
``HOP-DONG-GIAO-TIEP.md`` Gói 7):

* Eight axes, each scored 0-100. One axis in one run = the mean of the tasks
  that score that axis.
* Overall of one run = the unweighted mean of the eight axes, rounded to one
  decimal place (half-up) BEFORE looking up the level.
* Dreyfus, five levels, even 20-point steps, bilingual names.
* Official overall = the mean of the runs with the same criteria version,
  at least ``MIN_OFFICIAL_RUNS`` runs; fewer runs means "not official yet".
* ``level_unstable`` when the runs used for the official overall do not all
  sit in the same Dreyfus level (they straddle a threshold).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any, Iterable, Mapping

# Criteria version. Bump this (``v2`` ...) whenever a threshold, the overall
# formula or a required task field changes; runs are only ever averaged with
# runs of the same version.
CRITERIA_VERSION = "v1"

# The eight axes, in radar order. These exact strings are the axis names in
# ``task.yaml`` (``axes: [...]``) and the keys of per-run axis scores in
# ``results.json``.
AXES: tuple[str, ...] = (
    "Coding",
    "Phân tích",
    "Suy luận",
    "Tốc độ",
    "Đúng việc",
    "Chi phí",
    "Ổn định",
    "Trí nhớ",
)

# Stable ASCII keys for the axes (radar data, the frontend). The Vietnamese
# names above stay the canonical names; these keys are only identifiers.
AXIS_KEYS: dict[str, str] = {
    "Coding": "coding",
    "Phân tích": "phan-tich",
    "Suy luận": "suy-luan",
    "Tốc độ": "toc-do",
    "Đúng việc": "dung-viec",
    "Chi phí": "chi-phi",
    "Ổn định": "on-dinh",
    "Trí nhớ": "tri-nho",
}

MIN_SCORE = 0.0
MAX_SCORE = 100.0
# One decimal place, half-up (``Decimal`` -- Python's ``round`` is banker's
# rounding, so 19.95 would round to 19.9 instead of 20.0 and land one level
# below the threshold it sits on).
ROUND_DIGITS = 1
# Even Dreyfus steps: 0-<20, 20-<40, 40-<60, 60-<80, 80-100.
LEVEL_STEP = 20
LEVEL_COUNT = 5
# Runs of the same criteria version needed before the overall is official.
MIN_OFFICIAL_RUNS = 3


@dataclass(frozen=True)
class Level:
    """One Dreyfus level. ``low`` is inclusive, ``high`` is exclusive, except
    the last level, which includes 100."""

    number: int  # 1..5
    name_en: str
    name_vi: str
    desc_en: str
    desc_vi: str
    low: float
    high: float

    @property
    def i18n_name_key(self) -> str:
        return f"bench.level.{self.number}.name"

    @property
    def i18n_desc_key(self) -> str:
        return f"bench.level.{self.number}.desc"


LEVELS: tuple[Level, ...] = (
    Level(1, "Novice", "Mới bắt đầu",
          "Needs step-by-step guidance; succeeds only on familiar, guided work.",
          "Cần hướng dẫn từng bước; chỉ làm được việc quen thuộc, có người dẫn.",
          0, 20),
    Level(2, "Advanced Beginner", "Mới có kinh nghiệm",
          "Handles routine work alone; still needs help when the situation changes.",
          "Tự làm được việc thường gặp; vẫn cần giúp khi tình huống thay đổi.",
          20, 40),
    Level(3, "Competent", "Vững việc",
          "Plans and finishes standard work end to end; spots the usual problems.",
          "Tự lập kế hoạch và làm xong việc chuẩn từ đầu tới cuối; thấy được lỗi thường gặp.",
          40, 60),
    Level(4, "Proficient", "Thành thạo",
          "Sees the whole situation, prioritises well and adapts when it changes.",
          "Nhìn được toàn cảnh, biết ưu tiên và ứng biến khi tình huống thay đổi.",
          60, 80),
    Level(5, "Expert", "Chuyên gia",
          "Acts on intuition built from deep experience; handles the hardest cases.",
          "Làm theo trực giác từ kinh nghiệm sâu; xử lý được ca khó nhất.",
          80, 100),
)


def level_i18n_key(number: int, field: str = "name") -> str:
    """The frontend i18n key for a level, e.g. ``bench.level.2.name``."""
    if number not in range(1, LEVEL_COUNT + 1):
        raise ValueError(f"level must be 1..{LEVEL_COUNT}, got {number!r}")
    if field not in ("name", "desc"):
        raise ValueError(f"level i18n field must be 'name' or 'desc', got {field!r}")
    return f"bench.level.{number}.{field}"


def level_to_dict(level: Level | None) -> dict[str, Any] | None:
    """A level as plain JSON data (what goes into ``results.json``)."""
    if level is None:
        return None
    return {"level": level.number, "name_en": level.name_en, "name_vi": level.name_vi,
            "name_i18n_key": level.i18n_name_key, "desc_i18n_key": level.i18n_desc_key,
            "range": [level.low, level.high]}


def round_score(value: float) -> float:
    """Round to one decimal place, half-up (see ``ROUND_DIGITS``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"score must be a finite number, got {value!r}")
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _check_score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number, got {value!r}")
    if not MIN_SCORE <= value <= MAX_SCORE:
        raise ValueError(f"{field} must be within [{MIN_SCORE}, {MAX_SCORE}], got {value!r}")
    return float(value)


def axis_score(scores: Iterable[float | None]) -> float | None:
    """One axis in one run: the mean of the tasks that score that axis.

    ``None`` entries (a task that does not score this axis) are skipped.
    Returns ``None`` when no task scored the axis.
    """
    usable = [_check_score(s, "task score") for s in scores if s is not None]
    if not usable:
        return None
    return round_score(sum(usable) / len(usable))


def run_overall(axis_scores: Mapping[str, float | None]) -> float | None:
    """Overall of one run: the unweighted mean of all eight axes, rounded to
    one decimal place. ``None`` when any axis is missing or unscored -- an
    overall over fewer than eight axes is not an overall."""
    values = []
    for axis in AXES:
        score = axis_scores.get(axis)
        if score is None:
            return None
        values.append(_check_score(score, f"axis score {axis!r}"))
    return round_score(sum(values) / len(values))


def level_for(overall: float | None) -> Level | None:
    """The Dreyfus level for an (already rounded) overall. ``None`` in,
    ``None`` out. A score of exactly 100 is Expert (the last band is closed
    on both ends); a score on a boundary belongs to the higher level."""
    if overall is None:
        return None
    score = _check_score(overall, "overall")
    for level in LEVELS:
        if level.low <= score < level.high or (level.number == LEVEL_COUNT and score == MAX_SCORE):
            return level
    raise AssertionError(f"unreachable: score {score} matches no Dreyfus level")  # pragma: no cover


def radar_data(axis_scores: Mapping[str, float | None]) -> list[dict[str, Any]]:
    """Axis scores in radar order for the Bench page (``RadarChart``).

    One entry per axis: ``{"axis": <Vietnamese name>, "key": <ascii key>,
    "score": <0-100 or None>}``. Unscored axes keep ``score: None`` so the
    chart can show the gap instead of silently plotting zero.
    """
    out = []
    for axis in AXES:
        score = axis_scores.get(axis)
        if score is not None:
            score = _check_score(score, f"axis score {axis!r}")
        out.append({"axis": axis, "key": AXIS_KEYS[axis], "score": score})
    return out


def make_run_stamp(*, system_config: Mapping[str, Any],
                   network: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The stamp every run carries: criteria version + system config + network.

    ``system_config`` describes the system under test (recommended keys:
    ``worker``, ``model``, ``harness``, ``harness2`` -- the worker/model that
    ran plus the harness versions that drove it). ``network`` is ``None`` for
    an offline run, else ``{"mode": "allowlist", "hosts": [...]}`` with the
    hosts the owner approved for that run.
    """
    if not isinstance(system_config, Mapping):
        raise ValueError(f"system_config must be a mapping, got {type(system_config).__name__}")
    stamp: dict[str, Any] = {"criteria_version": CRITERIA_VERSION,
                             "system_config": dict(system_config)}
    if network is None:
        stamp["network"] = {"mode": "offline", "hosts": []}
    else:
        if not isinstance(network, Mapping):
            raise ValueError(f"network must be a mapping or None, got {type(network).__name__}")
        mode = network.get("mode", "allowlist")
        if mode not in ("offline", "allowlist"):
            raise ValueError(f"network mode must be 'offline' or 'allowlist', got {mode!r}")
        hosts = network.get("hosts", [])
        if not isinstance(hosts, list) or any(not isinstance(h, str) for h in hosts):
            raise ValueError(f"network hosts must be a list of strings, got {hosts!r}")
        stamp["network"] = {"mode": mode, "hosts": list(hosts)}
    return stamp


def build_run_result(*, axis_scores: Mapping[str, float | None],
                     system_config: Mapping[str, Any],
                     network: Mapping[str, Any] | None = None,
                     gate_set: str = "gate-v1") -> dict[str, Any]:
    """One run's result record (the per-run shape in ``results.json``).

    ``level_unstable`` is ``None`` here: it is only meaningful across runs,
    so ``annotate_runs`` fills it in once the runs are grouped (see below).
    ``gate_set`` names the fixed gate task set this run used (``gate-v1``);
    official overalls only ever mix runs of the same gate set.
    """
    overall = run_overall(axis_scores)
    record = {"criteria_version": CRITERIA_VERSION,
              "gate_set": gate_set,
              "axes": {axis: (None if axis_scores.get(axis) is None
                              else _check_score(axis_scores[axis], f"axis score {axis!r}"))
                       for axis in AXES},
              "overall": overall,
              "level": level_to_dict(level_for(overall)),
              "level_unstable": None,
              **make_run_stamp(system_config=system_config, network=network)}
    return record


def official_overall(runs: Iterable[Mapping[str, Any]],
                     *, criteria_version: str = CRITERIA_VERSION,
                     gate_set: str | None = None) -> dict[str, Any]:
    """The official overall over a set of runs.

    Only runs whose ``criteria_version`` matches are averaged -- runs from
    another version are counted in ``excluded_runs`` and never mixed in.
    When ``gate_set`` is given, only runs of that gate set are averaged
    (a run of another gate set measured a different task list, so its
    overall is not comparable); pass it whenever the runs carry one.
    The mean is rounded to one decimal place (half-up) before the level is
    looked up. ``official`` is true only with at least ``MIN_OFFICIAL_RUNS``
    matching runs; with fewer, the overall is still reported (so the page
    can show progress) but must be displayed as "not official yet".
    ``level_unstable`` is true when the matching runs do not all sit in the
    same Dreyfus level.
    """
    matching: list[float] = []
    excluded = 0
    for run in runs:
        overall = run.get("overall")
        if run.get("criteria_version", criteria_version) != criteria_version:
            excluded += 1
            continue
        if gate_set is not None and run.get("gate_set", gate_set) != gate_set:
            excluded += 1
            continue
        if overall is None:
            continue
        matching.append(_check_score(overall, "run overall"))
    overall = round_score(sum(matching) / len(matching)) if matching else None
    levels = {level_for(score).number for score in matching}
    return {"criteria_version": criteria_version,
            "gate_set": gate_set,
            "overall": overall,
            "official": len(matching) >= MIN_OFFICIAL_RUNS,
            "runs": len(matching),
            "excluded_runs": excluded,
            "level": level_to_dict(level_for(overall)),
            "level_unstable": len(levels) > 1}


def annotate_runs(runs: Iterable[Mapping[str, Any]],
                  *, criteria_version: str = CRITERIA_VERSION,
                  gate_set: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stamp ``level_unstable`` onto each run record and return the official
    summary alongside: ``(annotated_runs, official_overall(...))``.

    Runs from another criteria version (or, when ``gate_set`` is given,
    another gate set) keep ``level_unstable: None`` -- they do not take part
    in this version's stability judgement.
    """
    summary = official_overall(runs, criteria_version=criteria_version, gate_set=gate_set)
    annotated = []
    for run in runs:
        copy = dict(run)
        in_scope = (run.get("criteria_version", criteria_version) == criteria_version
                    and (gate_set is None or run.get("gate_set", gate_set) == gate_set)
                    and run.get("overall") is not None)
        copy["level_unstable"] = summary["level_unstable"] if in_scope else None
        annotated.append(copy)
    return annotated, summary
