"""Bench standard v1: load and validate a bench task (``task.yaml``).

The frozen task format is specified in ``docs/bench-standard-v1.md``. This
module is the machine-readable side of that spec: the loader (worker (b))
and the run driver (worker (c)) accept a task only when this validator says
so, so an invalid task can never reach a run and skew a score.

Design notes:

* The main entry points return ``(task, errors)`` instead of raising for a
  bad task: a caller that lists many tasks needs the structured errors, not
  an exception halfway through the list. ``parse_task_data`` raises
  ``BenchTaskInvalid`` (which carries the same ``.errors`` list) for callers
  that load exactly one task and want to fail fast.
* Every error is a ``ValidationError`` with a machine-readable ``code`` and
  the dotted ``field`` it belongs to (e.g. ``environment.network.hosts[2]``),
  so the Bench page and the CLI can point at the exact problem.
* Policy numbers are named constants bound to criteria version v1 (see
  ``bench_criteria.CRITERIA_VERSION``): ``MAX_NETWORK_HOSTS`` here,
  ``LEVEL_STEP`` / ``MIN_OFFICIAL_RUNS`` there. Changing one means a new
  criteria version, never a silent edit.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import ipaddress
from pathlib import Path
import re
from typing import Any

import yaml

from .bench_criteria import AXES, CRITERIA_VERSION

# Maximum hosts in one task's network allowlist (v1 policy).
MAX_NETWORK_HOSTS = 10
MAX_TASK_ID_LEN = 128

TASK_KINDS: tuple[str, ...] = ("code", "repo", "scenario", "qa", "terminal")
TASK_LANGUAGES: tuple[str, ...] = ("vi", "en")
# Kinds whose scoring is a hidden test suite run in the sandbox: the task
# directory must carry ``tests/``. ``scenario`` is scored by ``score_scenario``
# and ``qa`` by its own checker, so neither needs ``tests/`` in v1.
KINDS_REQUIRING_TESTS_DIR: tuple[str, ...] = ("code", "repo", "terminal")

# Per-axis required fields: an axis named in ``axes:`` without its field
# makes the whole task invalid (this is what stops a v1 task from claiming
# to measure an axis it cannot describe). Stability ("Ổn định") has no
# per-task field: it is measured at run level, plus an optional ``fault:``
# block (see the spec).
REQUIRED_FIELDS_PER_AXIS: dict[str, tuple[str, ...]] = {
    "Tốc độ": ("par_seconds",),
    "Trí nhớ": ("lesson_ref",),
    "Phân tích": ("expected_files",),
    "Đúng việc": ("expected_files",),
}

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BASE_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class ValidationError:
    """One structured validation failure: dotted ``field``, machine ``code``,
    human ``message`` (Vietnamese, since the task author reads it)."""

    field: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


class BenchTaskInvalid(ValueError):
    """A task that failed validation; ``.errors`` is the structured list."""

    def __init__(self, errors: list[ValidationError]):
        self.errors = list(errors)
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in self.errors) or "invalid bench task")


@dataclass(frozen=True)
class BenchTask:
    """A validated v1 bench task (parsed ``task.yaml`` + layout checks)."""

    id: str
    goal: str
    kind: str
    language: str
    axes: tuple[str, ...]
    difficulty: str | int
    timeouts: dict[str, Any]
    environment: dict[str, Any]
    par_seconds: float | None = None
    lesson_ref: str | None = None
    expected_files: tuple[str, ...] | None = None
    forbid_tools: tuple[str, ...] = ()
    scenario: dict[str, Any] | None = None
    fault: dict[str, Any] | None = None
    repo: str | None = None
    base_commit: str | None = None
    source_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "goal": self.goal, "kind": self.kind, "language": self.language,
            "axes": list(self.axes), "difficulty": self.difficulty,
            "timeouts": dict(self.timeouts), "environment": dict(self.environment),
            "forbid_tools": list(self.forbid_tools),
        }
        if self.par_seconds is not None:
            out["par_seconds"] = self.par_seconds
        if self.lesson_ref is not None:
            out["lesson_ref"] = self.lesson_ref
        if self.expected_files is not None:
            out["expected_files"] = list(self.expected_files)
        if self.scenario is not None:
            out["scenario"] = dict(self.scenario)
        if self.fault is not None:
            out["fault"] = dict(self.fault)
        if self.repo is not None:
            out["repo"] = self.repo
        if self.base_commit is not None:
            out["base_commit"] = self.base_commit
        return out


# --------------------------------------------------------------------------------------- hostnames
def is_valid_hostname(host: Any) -> bool:
    """A bare hostname for the network allowlist: no scheme, no wildcard, no
    bare IP, labels of letters/digits/hyphens that do not start or end with a
    hyphen. Single-label names (``localhost``) are allowed; an empty string,
    a trailing dot and a port (``host:443``) are not."""
    if not isinstance(host, str) or not host or len(host) > 253:
        return False
    if "://" in host or "*" in host or host.startswith(".") or host.endswith("."):
        return False
    if ":" in host or "/" in host or " " in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # a bare IP is not a hostname
    except ValueError:
        pass
    return all(_HOSTNAME_LABEL_RE.match(label) for label in host.split("."))


def validate_network_hosts(hosts: Any, *, field_name: str = "environment.network.hosts") -> list[ValidationError]:
    """Validate one allowlist. An empty list is invalid: declaring ``network:``
    with no host means "network, but nowhere", which is never what the author
    meant -- an offline task simply omits ``network:``."""
    errors: list[ValidationError] = []
    if not isinstance(hosts, list):
        return [ValidationError(field_name, "invalid_type", "hosts phải là danh sách hostname")]
    if not hosts:
        return [ValidationError(field_name, "empty_hosts",
                                "network.hosts rỗng là task invalid: bài offline thì bỏ hẳn khối network:")]
    if len(hosts) > MAX_NETWORK_HOSTS:
        errors.append(ValidationError(
            field_name, "too_many_hosts",
            f"network.hosts tối đa {MAX_NETWORK_HOSTS} entry, đang có {len(hosts)}"))
    for i, host in enumerate(hosts):
        if not is_valid_hostname(host):
            errors.append(ValidationError(
                f"{field_name}[{i}]", "invalid_hostname",
                f"hostname không hợp lệ: {host!r} (hostname trần, không scheme, không wildcard *, không IP trần)"))
    return errors


# --------------------------------------------------------------------------------------- helpers
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _str_list(value: Any, *, field_name: str, errors: list[ValidationError],
              allow_empty: bool = False) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        errors.append(ValidationError(field_name, "invalid_type", "phải là danh sách chuỗi"))
        return None
    if not allow_empty and not value:
        errors.append(ValidationError(field_name, "invalid_value", "danh sách không được rỗng"))
        return None
    bad = [v for v in value if not _non_empty_str(v)]
    if bad:
        errors.append(ValidationError(field_name, "invalid_value",
                                      f"mọi phần tử phải là chuỗi không rỗng, sai: {bad[:3]!r}"))
        return None
    return tuple(value)


# --------------------------------------------------------------------------------------- validation
def validate_task_data(data: Any) -> list[ValidationError]:
    """Validate parsed ``task.yaml`` content (no filesystem access)."""
    errors: list[ValidationError] = []
    if not isinstance(data, dict):
        return [ValidationError("$", "invalid_type", "task.yaml phải là một mapping YAML ở cấp cao nhất")]

    def required(name: str) -> Any:
        if name not in data or data[name] is None:
            errors.append(ValidationError(name, "missing_field", f"thiếu trường bắt buộc {name!r}"))
            return None
        return data[name]

    task_id = required("id")
    if task_id is not None:
        if not _non_empty_str(task_id):
            errors.append(ValidationError("id", "invalid_value", "id phải là chuỗi không rỗng"))
        elif len(task_id) > MAX_TASK_ID_LEN or not _TASK_ID_RE.match(task_id):
            errors.append(ValidationError(
                "id", "invalid_value",
                "id chỉ gồm chữ/số/dấu . _ -, bắt đầu bằng chữ/số, tối đa "
                f"{MAX_TASK_ID_LEN} ký tự"))

    goal = required("goal")
    if goal is not None and not _non_empty_str(goal):
        errors.append(ValidationError("goal", "invalid_value", "goal phải là chuỗi không rỗng"))

    kind = required("kind")
    if kind is not None and kind not in TASK_KINDS:
        errors.append(ValidationError("kind", "invalid_enum",
                                      f"kind phải là một trong {list(TASK_KINDS)}, đang là {kind!r}"))

    language = required("language")
    if language is not None and language not in TASK_LANGUAGES:
        errors.append(ValidationError("language", "invalid_enum",
                                      f"language phải là 'vi' hoặc 'en', đang là {language!r}"))

    axes_raw = required("axes")
    axes: list[str] = []
    if axes_raw is not None:
        if not isinstance(axes_raw, list) or not axes_raw:
            errors.append(ValidationError("axes", "invalid_value", "axes phải là danh sách không rỗng"))
        else:
            for i, axis in enumerate(axes_raw):
                if axis not in AXES:
                    errors.append(ValidationError(
                        f"axes[{i}]", "invalid_enum",
                        f"trục không hợp lệ: {axis!r} (8 trục v1: {list(AXES)})"))
                elif axis in axes:
                    errors.append(ValidationError(f"axes[{i}]", "duplicate_axis",
                                                  f"trục {axis!r} khai trùng"))
                else:
                    axes.append(axis)

    difficulty = required("difficulty")
    if difficulty is not None and not (_non_empty_str(difficulty)
                                       or (_is_number(difficulty) and difficulty > 0)):
        errors.append(ValidationError("difficulty", "invalid_value",
                                      "difficulty phải là chuỗi không rỗng hoặc số dương"))

    timeouts = required("timeouts")
    if timeouts is not None:
        if not isinstance(timeouts, dict) or not timeouts:
            errors.append(ValidationError("timeouts", "invalid_value",
                                          "timeouts phải là mapping không rỗng (vd {setup: 600, test: 300})"))
        else:
            for key, value in timeouts.items():
                if not _is_number(value) or value <= 0:
                    errors.append(ValidationError(
                        f"timeouts.{key}", "invalid_value",
                        f"timeout {key!r} phải là số dương (giây), đang là {value!r}"))

    environment = required("environment")
    if environment is not None:
        if not isinstance(environment, dict):
            errors.append(ValidationError("environment", "invalid_type", "environment phải là mapping"))
        else:
            for key in ("setup", "dockerfile"):
                if key in environment and not _non_empty_str(environment[key]):
                    errors.append(ValidationError(f"environment.{key}", "invalid_value",
                                                  f"{key} phải là chuỗi không rỗng"))
            requires = environment.get("requires")
            if requires is not None:
                if not isinstance(requires, dict):
                    errors.append(ValidationError("environment.requires", "invalid_type",
                                                  "requires phải là mapping"))
                else:
                    if "network" in requires:
                        errors.append(ValidationError(
                            "environment.requires.network", "invalid_value",
                            "khai báo mạng duy nhất ở environment.network, không dùng requires.network"))
                    if "gpu" in requires and not isinstance(requires["gpu"], bool):
                        errors.append(ValidationError("environment.requires.gpu", "invalid_type",
                                                      "requires.gpu phải là true/false"))
            network = environment.get("network")
            if network is not None:
                if not isinstance(network, dict):
                    errors.append(ValidationError("environment.network", "invalid_type",
                                                  "network phải là mapping {hosts: [...]}"))
                else:
                    for key in network:
                        if key != "hosts":
                            errors.append(ValidationError(
                                f"environment.network.{key}", "invalid_value",
                                f"v1 chỉ cho khóa 'hosts' trong network, đang có {key!r}"))
                    if "hosts" not in network:
                        errors.append(ValidationError("environment.network.hosts", "missing_field",
                                                      "network phải khai hosts"))
                    else:
                        errors.extend(validate_network_hosts(network["hosts"]))

    # Optional scalar fields: validated when present, whatever the axes say.
    par_seconds = data.get("par_seconds")
    if par_seconds is not None and (not _is_number(par_seconds) or par_seconds <= 0):
        errors.append(ValidationError("par_seconds", "invalid_value",
                                      f"par_seconds phải là số dương (giây), đang là {par_seconds!r}"))
    lesson_ref = data.get("lesson_ref")
    if lesson_ref is not None and not _non_empty_str(lesson_ref):
        errors.append(ValidationError("lesson_ref", "invalid_value",
                                      "lesson_ref phải là chuỗi không rỗng"))
    expected_files = data.get("expected_files")
    if expected_files is not None:
        _str_list(expected_files, field_name="expected_files", errors=errors)
    forbid_tools = data.get("forbid_tools")
    if forbid_tools is not None:
        _str_list(forbid_tools, field_name="forbid_tools", errors=errors, allow_empty=True)

    fault = data.get("fault")
    if fault is not None:
        if not isinstance(fault, dict):
            errors.append(ValidationError("fault", "invalid_type", "fault phải là mapping"))
        elif "type" in fault and not _non_empty_str(fault["type"]):
            errors.append(ValidationError("fault.type", "invalid_value",
                                          "fault.type phải là chuỗi không rỗng"))

    repo = data.get("repo")
    if repo is not None and not _non_empty_str(repo):
        errors.append(ValidationError("repo", "invalid_value", "repo phải là chuỗi không rỗng"))
    base_commit = data.get("base_commit")
    if base_commit is not None:
        if not isinstance(base_commit, str) or not _BASE_COMMIT_RE.match(base_commit):
            errors.append(ValidationError("base_commit", "invalid_value",
                                          "base_commit phải là SHA git (7-40 ký tự hex)"))
    if kind == "repo":
        if repo is None:
            errors.append(ValidationError("repo", "missing_field",
                                          "kind=repo bắt buộc có repo"))
        if base_commit is None:
            errors.append(ValidationError("base_commit", "missing_field",
                                          "kind=repo bắt buộc có base_commit"))
    if (repo is None) != (base_commit is None):
        which = "repo" if repo is None else "base_commit"
        errors.append(ValidationError(which, "missing_field",
                                      "repo và base_commit phải đi cùng nhau"))

    scenario = data.get("scenario")
    if kind == "scenario" and scenario is None:
        errors.append(ValidationError("scenario", "missing_field",
                                      "kind=scenario bắt buộc có khối scenario:"))
    if scenario is not None:
        errors.extend(_validate_scenario(scenario))

    # Per-axis required fields: the axis is claimed, so its field must exist.
    # Axes that share one field (Phân tích + Đúng việc share expected_files)
    # produce one error per missing field, naming every axis that needs it.
    needed: dict[str, list[str]] = {}
    for axis in axes:
        for name in REQUIRED_FIELDS_PER_AXIS.get(axis, ()):
            needed.setdefault(name, []).append(axis)
    for name, axes_needing in needed.items():
        value = data.get(name)
        missing = value is None or (isinstance(value, (list, str)) and not value)
        if missing:
            errors.append(ValidationError(
                name, "missing_required_for_axis",
                f"trục {', '.join(repr(a) for a in axes_needing)} bắt buộc có trường {name!r} "
                f"(axes khai {', '.join(repr(a) for a in axes_needing)} mà thiếu)"))

    return errors


def _validate_scenario(scenario: Any) -> list[ValidationError]:
    """The ``scenario:`` block mirrors ``bench.Scenario`` (same field names)."""
    errors: list[ValidationError] = []
    if not isinstance(scenario, dict):
        return [ValidationError("scenario", "invalid_type", "scenario phải là mapping")]
    prompt = scenario.get("prompt")
    if not _non_empty_str(prompt):
        errors.append(ValidationError("scenario.prompt", "missing_field",
                                      "scenario.prompt bắt buộc, là chuỗi không rỗng"))
    expect = scenario.get("expect")
    if expect is None:
        errors.append(ValidationError("scenario.expect", "missing_field",
                                      "scenario.expect bắt buộc (danh sách tool, rỗng = trả lời không cần tool)"))
    elif not isinstance(expect, list) or any(not _non_empty_str(t) for t in expect):
        errors.append(ValidationError("scenario.expect", "invalid_value",
                                      "scenario.expect phải là danh sách tên tool"))
    for name in ("must", "must_not", "forbid", "need"):
        if name in scenario:
            _str_list(scenario[name], field_name=f"scenario.{name}", errors=errors, allow_empty=True)
    if "args" in scenario and (not isinstance(scenario["args"], dict)
                               or any(not isinstance(v, str) for v in scenario["args"].values())):
        errors.append(ValidationError("scenario.args", "invalid_value",
                                      "scenario.args phải là mapping tên-tham-số -> regex (chuỗi)"))
    if "results" in scenario and not isinstance(scenario["results"], dict):
        errors.append(ValidationError("scenario.results", "invalid_type",
                                      "scenario.results phải là mapping"))
    if "count" in scenario:
        count = scenario["count"]
        if not isinstance(count, dict) or any(not _is_number(v) or int(v) != v or v < 1
                                              for v in count.values()):
            errors.append(ValidationError("scenario.count", "invalid_value",
                                          "scenario.count phải là mapping tên-tool -> số nguyên >= 1"))
    if "optional" in scenario and not isinstance(scenario["optional"], bool):
        errors.append(ValidationError("scenario.optional", "invalid_type",
                                      "scenario.optional phải là true/false"))
    if "category" in scenario and not _non_empty_str(scenario["category"]):
        errors.append(ValidationError("scenario.category", "invalid_value",
                                      "scenario.category phải là chuỗi không rỗng"))
    return errors


def validate_task_layout(task_dir: Path, task: BenchTask) -> list[ValidationError]:
    """Directory checks for a task folder (``task.yaml`` lives in it).

    * ``files/`` or ``repo`` + ``base_commit`` must supply the starting point
      (``kind=repo`` always needs ``repo`` + ``base_commit``; the other kinds
      may use a local ``files/`` directory instead).
    * ``tests/`` (hidden tests) is required for the kinds scored by a test
      run (``code``/``repo``/``terminal``).
    """
    errors: list[ValidationError] = []
    task_dir = Path(task_dir)
    has_files = (task_dir / "files").is_dir()
    has_repo = task.repo is not None and task.base_commit is not None
    if task.kind != "repo" and not has_files and not has_repo:
        # kind=repo already failed validation without repo+base_commit;
        # do not repeat that error here.
        errors.append(ValidationError(
            "files", "missing_field",
            "bài cần thư mục files/ hoặc cặp repo + base_commit trong task.yaml"))
    if task.kind in KINDS_REQUIRING_TESTS_DIR and not (task_dir / "tests").is_dir():
        errors.append(ValidationError(
            "tests", "missing_field",
            f"kind={task.kind} bắt buộc có thư mục tests/ (test ẩn) trong thư mục bài"))
    return errors


def parse_task_data(data: Any) -> BenchTask:
    """Parse validated data into a ``BenchTask``; raises ``BenchTaskInvalid``
    (with structured ``.errors``) on the first invalid task -- never a bare
    ``KeyError``/``ValueError`` from a half-parsed mapping."""
    errors = validate_task_data(data)
    if errors:
        raise BenchTaskInvalid(errors)
    return BenchTask(
        id=data["id"], goal=data["goal"], kind=data["kind"], language=data["language"],
        axes=tuple(data["axes"]), difficulty=data["difficulty"],
        timeouts=dict(data["timeouts"]), environment=dict(data["environment"]),
        par_seconds=data.get("par_seconds"), lesson_ref=data.get("lesson_ref"),
        expected_files=(tuple(data["expected_files"]) if data.get("expected_files") is not None else None),
        forbid_tools=tuple(data.get("forbid_tools") or ()),
        scenario=(dict(data["scenario"]) if data.get("scenario") is not None else None),
        fault=(dict(data["fault"]) if data.get("fault") is not None else None),
        repo=data.get("repo"), base_commit=data.get("base_commit"))


def load_task_text(text: str) -> tuple[BenchTask | None, list[ValidationError]]:
    """Parse ``task.yaml`` text; returns ``(task, [])`` or ``(None, errors)``."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [ValidationError("$", "yaml_error", f"task.yaml lỗi cú pháp YAML: {exc}")]
    if data is None:
        return None, [ValidationError("$", "missing_field", "task.yaml rỗng")]
    errors = validate_task_data(data)
    if errors:
        return None, errors
    return parse_task_data(data), []


def load_task_file(path: Path) -> tuple[BenchTask | None, list[ValidationError]]:
    """Load ``task.yaml`` from disk, including the directory-layout checks
    (``files/``/``tests/`` next to it). Returns ``(task, [])`` or
    ``(None, errors)``; a missing or unreadable file is one structured error,
    not an exception."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [ValidationError("task.yaml", "missing_file",
                                      f"không đọc được {path}: {exc.strerror or exc}")]
    task, errors = load_task_text(text)
    if task is None:
        return None, errors
    layout_errors = validate_task_layout(path.parent, task)
    if layout_errors:
        return None, errors + layout_errors
    return replace(task, source_path=path), []
