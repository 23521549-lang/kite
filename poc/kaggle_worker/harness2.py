"""Bounded Harness² execution and immutable evaluation records.

This adapter invokes the upstream CLI rather than copying benchmark or
refinement logic. Evaluations use a private output namespace and never promote
their candidate harness implicitly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
import uuid


class Harness2Error(RuntimeError):
    """Harness² is unavailable, failed, or produced invalid evidence."""


@dataclass(frozen=True)
class Harness2Spec:
    benchmark: str
    evidence_path: Path
    only_domains: tuple[str, ...] = ()
    max_tasks: int = 1
    seed: int = 0
    subset: str | None = None
    dry_run: bool = False
    evaluation_id: str | None = None

    def validate(self) -> None:
        if self.benchmark not in {"lab", "jb", "wb"}:
            raise ValueError("benchmark must be lab, jb, or wb")
        if self.max_tasks < 1:
            raise ValueError("max_tasks must be at least one")
        if self.benchmark == "wb" and self.subset not in {"office", "web", "code"}:
            raise ValueError("WorkBuddy-Bench requires subset office, web, or code")
        if self.benchmark != "wb" and self.subset is not None:
            raise ValueError("subset is only supported for WorkBuddy-Bench")
        if any(not value or value.startswith("-") for value in self.only_domains):
            raise ValueError("domain names must be non-empty values, not options")
        if self.evaluation_id is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", self.evaluation_id):
            raise ValueError("evaluation_id contains unsupported characters")


@dataclass(frozen=True)
class Harness2EvaluationRecord:
    evaluation_id: str
    status: str
    benchmark: str
    harness_commit: str
    harness_tree_hash: str
    mode: str
    k: int
    seed: int
    max_tasks: int
    domains: tuple[str, ...]
    subset: str | None
    evidence_paths: tuple[str, ...]
    command_args_json: str
    score_paths: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    candidate_hash: str
    promotion_decision: str
    promotion_reviewer: str | None
    promotion_at: str | None
    created_at: str
    completed_at: str | None
    error: str | None
    record_path: str


class Harness2Runner:
    """Execute one small Harness² evaluation using its documented CLI."""

    _COMMANDS = {
        "check": "harness2-check",
        "run": "harness2",
        "judge": "harness2-judge",
        "report": "harness2-report",
    }

    def __init__(
        self,
        checkout_path: Path,
        output_root: Path,
        *,
        commands: Mapping[str, str | Path] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        base_environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 12 * 60 * 60,
    ) -> None:
        self.checkout_path = Path(checkout_path).expanduser().resolve()
        self.output_root = Path(output_root).expanduser().resolve()
        self.commands = dict(self._COMMANDS)
        if commands:
            unknown = set(commands) - set(self._COMMANDS)
            if unknown:
                raise ValueError(f"unknown Harness² command keys: {', '.join(sorted(unknown))}")
            self.commands.update({key: str(value) for key, value in commands.items()})
        if any(not value.strip() for value in self.commands.values()):
            raise ValueError("Harness² command paths must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.runner = runner
        self.base_environment = dict(os.environ if base_environment is None else base_environment)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _git(checkout: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise Harness2Error("Harness² checkout is not a readable Git revision")
        return result.stdout.strip()

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _common_args(spec: Harness2Spec) -> list[str]:
        args = ["--bench", spec.benchmark, "--k", "1", "--mode", "sequential"]
        if spec.subset:
            args.extend(("--subset", spec.subset))
        if spec.only_domains:
            args.extend(("--only-domains", *spec.only_domains))
        args.extend(("--max-tasks", str(spec.max_tasks), "--seed", str(spec.seed)))
        return args

    def _invoke(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], stage: str,
    ) -> subprocess.CompletedProcess:
        try:
            result = self.runner(
                argv, cwd=str(cwd), env=env, capture_output=True, text=True,
                check=False, timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise Harness2Error(f"{stage} timed out") from exc
        except OSError as exc:
            raise Harness2Error(f"could not start {stage}: {type(exc).__name__}") from exc
        if result.returncode:
            # Never include captured stdout/stderr; they can contain credentials.
            raise Harness2Error(f"{stage} exited with code {result.returncode}")
        return result

    def evaluate(self, spec: Harness2Spec) -> Harness2EvaluationRecord:
        spec.validate()
        evidence_path = Path(spec.evidence_path).expanduser().resolve()
        if not evidence_path.exists():
            raise Harness2Error("evaluation evidence path does not exist")
        if not self.checkout_path.is_dir() or not (self.checkout_path / "pyproject.toml").is_file():
            raise Harness2Error("Harness² checkout is missing pyproject.toml")
        commit = self._git(self.checkout_path, "rev-parse", "HEAD")
        tree_hash = self._git(self.checkout_path, "rev-parse", "HEAD^{tree}")
        self.output_root.mkdir(parents=True, exist_ok=True)
        evaluation_id = spec.evaluation_id or uuid.uuid4().hex
        evaluation_dir = self.output_root / evaluation_id
        run_root = evaluation_dir / "runs"
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        run_root.mkdir(parents=True, exist_ok=True)
        record_path = evaluation_dir / "evaluation.json"
        prior_record = self.get_record(evaluation_id)
        if prior_record:
            same_inputs = (
                prior_record.benchmark == spec.benchmark
                and prior_record.harness_commit == commit
                and prior_record.harness_tree_hash == tree_hash
                and prior_record.seed == spec.seed
                and prior_record.max_tasks == spec.max_tasks
                and prior_record.domains == tuple(spec.only_domains)
                and prior_record.subset == spec.subset
                and prior_record.evidence_paths == (str(evidence_path),)
            )
            if not same_inputs:
                raise Harness2Error("evaluation ID already belongs to different inputs")
            if prior_record.status in {"completed", "preview"}:
                return prior_record
        command_records: list[list[str]] = []
        record = Harness2EvaluationRecord(
            evaluation_id=evaluation_id,
            status="running",
            benchmark=spec.benchmark,
            harness_commit=commit,
            harness_tree_hash=tree_hash,
            mode="sequential",
            k=1,
            seed=spec.seed,
            max_tasks=spec.max_tasks,
            domains=tuple(spec.only_domains),
            subset=spec.subset,
            evidence_paths=(str(evidence_path),),
            command_args_json="[]",
            score_paths=(),
            artifact_paths=(),
            candidate_hash="",
            promotion_decision="pending",
            promotion_reviewer=None,
            promotion_at=None,
            created_at=datetime.now(timezone.utc).isoformat(),
            completed_at=None,
            error=None,
            record_path=str(record_path),
        )
        self._atomic_json(record_path, asdict(record))
        env = dict(self.base_environment)
        env["HARNESS2_RUNS_DIR"] = str(run_root)
        common = self._common_args(spec)

        try:
            check_args = [self.commands["check"], "--bench", spec.benchmark]
            if spec.subset:
                check_args.extend(("--subset", spec.subset))
            command_records.append(check_args)
            self._invoke(check_args, cwd=self.checkout_path, env=env, stage="harness2-check")

            run_args = [self.commands["run"], *common]
            if spec.dry_run:
                run_args.append("--dry-run")
            command_records.append(run_args)
            self._invoke(run_args, cwd=self.checkout_path, env=env, stage="harness2")

            if spec.dry_run:
                record = replace(
                    record, status="preview", command_args_json=json.dumps(command_records),
                    artifact_paths=self._artifact_paths(run_root),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                judge_args = [self.commands["judge"], *common]
                report_args = [self.commands["report"], *common]
                for argv, stage in ((judge_args, "harness2-judge"), (report_args, "harness2-report")):
                    command_records.append(argv)
                    result = self._invoke(argv, cwd=self.checkout_path, env=env, stage=stage)
                    if stage == "harness2-report":
                        report_text = result.stdout or ""
                        for name, secret in env.items():
                            if any(part in name.lower() for part in ("token", "secret", "password", "api_key", "credential")) and secret:
                                report_text = report_text.replace(secret, "[REDACTED]")
                        if not report_text.strip():
                            raise Harness2Error("Harness² report artifact is missing")
                        (run_root / "harness2-report.txt").write_text(report_text, encoding="utf-8")
                score_files = list(run_root.rglob("judged.json"))
                if not score_files:
                    raise Harness2Error("Harness² judged.json report is missing")
                for score_file in score_files:
                    try:
                        data = json.loads(score_file.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise Harness2Error("Harness² judged.json is malformed") from exc
                    if not isinstance(data, dict):
                        raise Harness2Error("Harness² judged.json is malformed")
                artifacts = self._artifact_paths(run_root)
                record = replace(
                    record,
                    status="completed",
                    command_args_json=json.dumps(command_records, ensure_ascii=False),
                    score_paths=tuple(str(path.relative_to(evaluation_dir)) for path in score_files),
                    artifact_paths=artifacts,
                    candidate_hash=self._candidate_hash(run_root),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            self._atomic_json(record_path, asdict(record))
            return record
        except Harness2Error as exc:
            record = replace(
                record,
                status="failed",
                command_args_json=json.dumps(command_records, ensure_ascii=False),
                artifact_paths=self._artifact_paths(run_root),
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=str(exc),
            )
            self._atomic_json(record_path, asdict(record))
            raise Harness2Error(f"{exc}; evaluation record: {record_path}") from exc

    def get_record(self, evaluation_id: str | None) -> Harness2EvaluationRecord | None:
        if not evaluation_id:
            return None
        record_path = (self.output_root / evaluation_id / "evaluation.json").resolve()
        try:
            record_path.relative_to(self.output_root)
        except ValueError as exc:
            raise Harness2Error("invalid evaluation ID") from exc
        if not record_path.exists():
            return None
        try:
            raw = json.loads(record_path.read_text(encoding="utf-8"))
            for field in ("domains", "evidence_paths", "score_paths", "artifact_paths"):
                raw[field] = tuple(raw.get(field, ()))
            return Harness2EvaluationRecord(**raw)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise Harness2Error("evaluation record is malformed") from exc

    async def reconcile_operation(self, operation_key: str, spec: Harness2Spec):
        """Tell a Temporal retry whether this evaluation already completed."""
        from .temporal_state import RecoveryDecision

        record = await asyncio.to_thread(self.get_record, spec.evaluation_id)
        if record and record.status in {"completed", "preview"}:
            return RecoveryDecision(recovered=True, result=record)
        return RecoveryDecision(recovered=False)

    @staticmethod
    def _artifact_paths(root: Path) -> tuple[str, ...]:
        if not root.exists():
            return ()
        return tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()))

    @classmethod
    def _candidate_hash(cls, root: Path) -> str:
        digest = hashlib.sha256()
        for relative in cls._artifact_paths(root):
            path = root / relative
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        return digest.hexdigest()

    def record_promotion(
        self,
        evaluation_id: str,
        *,
        approved: bool,
        reviewer: str,
    ) -> Harness2EvaluationRecord:
        if not reviewer.strip():
            raise Harness2Error("promotion requires a named reviewer")
        record_path = (self.output_root / evaluation_id / "evaluation.json").resolve()
        try:
            record_path.relative_to(self.output_root)
        except ValueError as exc:
            raise Harness2Error("invalid evaluation ID") from exc
        try:
            raw = json.loads(record_path.read_text(encoding="utf-8"))
            record = Harness2EvaluationRecord(**raw)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise Harness2Error("evaluation record is missing or malformed") from exc
        if record.status != "completed":
            raise Harness2Error("only completed evaluations can be considered for promotion")
        if record.promotion_decision != "pending":
            raise Harness2Error("promotion decision has already been recorded")
        updated = replace(
            record,
            promotion_decision="approved" if approved else "rejected",
            promotion_reviewer=reviewer.strip(),
            promotion_at=datetime.now(timezone.utc).isoformat(),
        )
        self._atomic_json(record_path, asdict(updated))
        return updated
