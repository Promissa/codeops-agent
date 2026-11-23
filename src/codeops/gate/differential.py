"""Run the same approved test commands on the baseline and candidate."""

from collections.abc import Callable
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import Literal
import xml.etree.ElementTree as ET

from pydantic import BaseModel, Field, ValidationError

from codeops.core.models import TestCommand, TestResult
from codeops.gate.effect_map import EffectMapBuilder, is_test_file
from codeops.runtime.lifecycle import RunTerminated, TerminationReason
from codeops.runtime.sandbox import BwrapSandboxBackend
from codeops.runtime.workspace import open_regular
from codeops.safety.command_policy import CommandPolicy
from codeops.tools.test_runner import TestRunner
from codeops.trust.policy import TrustPolicy


class DifferentialOutcome(StrEnum):
    FIXED = "fixed"
    REGRESSED = "regressed"
    STILL_PASSING = "still_passing"
    STILL_FAILING = "still_failing"
    UNOBSERVED = "unobserved"


class CaseTransition(BaseModel):
    case: str
    outcome: DifferentialOutcome
    relevant: bool = False


class CommandTransition(BaseModel):
    command: list[str]
    outcome: DifferentialOutcome
    baseline: TestResult
    candidate: TestResult
    replay: TestResult | None = None
    cases: list[CaseTransition] = Field(default_factory=list)
    relevant: bool = False


class DifferentialResults(BaseModel):
    commands: list[CommandTransition] = Field(default_factory=list)
    observed_lines: dict[str, set[int]] = Field(default_factory=dict)
    witnessed_lines: dict[str, set[int]] = Field(default_factory=dict)
    failed_lines: dict[str, set[int]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def transitions(self) -> list[CommandTransition | CaseTransition]:
        return [item for command in self.commands for item in (command, *command.cases)]


class _Case(BaseModel):
    status: Literal["passed", "failed", "skipped"] = "skipped"
    lines: dict[str, list[int]] = Field(default_factory=dict)


_PROBE_PREFIX = "CODEOPS_PYTEST_EVIDENCE "
_PROBE_PATH = Path(__file__).with_name("_codeops_pytest_probe.py")
_CARGO_CASE = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)\b", re.MULTILINE)
_DISTINCT_TESTS = re.compile(r"\b[1-9]\d* (?:tests? )?(?:passed|failed)\b", re.I)
_SUREFIRE_SUMMARY = re.compile(r"Tests run: (\d+), Failures: \d+, Errors: \d+, Skipped: (\d+)")
_SOURCE_SUFFIXES = {
    "go": {".go"}, "cargo": {".rs"}, "mvn": {".java", ".kt"}, "./gradlew": {".java", ".kt", ".kts"},
    **dict.fromkeys(("npm", "pnpm", "yarn", "bun"), {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}),
}


class DifferentialVerifier:
    """Compare observed outcomes, optionally replaying changed tests on baseline source."""

    def __init__(
        self, baseline: TestRunner, candidate: TestRunner, *, policy: TrustPolicy,
        replay: TestRunner | None = None,
    ) -> None:
        self.baseline = baseline
        self.candidate = candidate
        self.replay = replay
        self.policy = policy

    def run(
        self, commands: list[TestCommand], *, command_root: Path, effects: EffectMapBuilder,
        changed_tests: set[str] | None = None,
        on_result: Callable[[str, TestResult], None] | None = None,
    ) -> DifferentialResults:
        report = DifferentialResults()
        changed_tests = changed_tests or set()
        for command in commands:
            argv = CommandPolicy().enforce(command.command)
            if argv not in self.policy.allowed_test_commands:
                raise RunTerminated(
                    TerminationReason.POLICY_DENIED, f"Command outside the approved trust contract: {' '.join(argv)}"
                )
            if not command.cwd.is_relative_to(command_root):
                raise RunTerminated(TerminationReason.POLICY_DENIED, "Verification cwd is outside the approved workspace.")
            relative = command.cwd.relative_to(command_root)
            source_suffixes = _SOURCE_SUFFIXES.get(argv[0], set())
            has_source_edit = any(
                Path(path).suffix in source_suffixes and not is_test_file(path)
                for path in effects.diff.changed_files
            )

            if relative.is_absolute() or ".." in relative.parts:
                raise RunTerminated(TerminationReason.POLICY_DENIED, "Verification cwd contains path traversal.")

            def execute(role: str, runner: TestRunner) -> tuple[TestResult, dict[str, _Case]]:
                root = runner.sandbox_backend.workspace_view if isinstance(runner.sandbox_backend, BwrapSandboxBackend) else runner.repo_path
                junit_root = root / relative
                before = _junit_reports(junit_root, argv)
                result, cases = _extract_cases(runner.run(command.model_copy(update={
                    "cwd": runner.repo_path / relative,
                    "timeout_seconds": min(command.timeout_seconds, self.policy.sandbox.timeout_seconds),
                })), argv)
                if argv[0] in {"mvn", "./gradlew"}:
                    cases.update(_read_junit_cases(junit_root, argv, before))
                status = _command_status(result, argv, cases)
                result = result.model_copy(update={"execution_outcome": "unobserved" if status == "skipped" else status})
                if on_result is not None:
                    on_result(role, result)
                return result, cases

            base, old_cases = execute("baseline", self.baseline)
            replay, replay_cases = execute("test replay", self.replay) if self.replay is not None else (None, {})
            head, new_cases = execute("candidate", self.candidate)
            for case in new_cases.values():
                if case.status in {"passed", "failed"}:
                    reached = {(relative / path).as_posix(): numbers for path, numbers in case.lines.items()}
                    witnesses_edit = effects.observes_edit(reached, candidate=True)
                    for repo_path, numbers in reached.items():
                        report.observed_lines.setdefault(repo_path, set()).update(numbers)
                        if witnesses_edit:
                            report.witnessed_lines.setdefault(repo_path, set()).update(numbers)
                        if witnesses_edit and case.status == "failed":
                            report.failed_lines.setdefault(repo_path, set()).update(numbers)
            cases = []
            for name in sorted(old_cases.keys() | new_cases.keys() | replay_cases.keys()):
                old = old_cases.get(name, _Case())
                new = new_cases.get(name, _Case())
                witness = replay_cases.get(name, _Case()) if replay is not None else old
                original = _transition(old.status, new.status)
                outcome = _transition(witness.status, new.status)
                if original in {DifferentialOutcome.REGRESSED, DifferentialOutcome.STILL_FAILING}:
                    outcome = original
                before_lines = {(relative / path).as_posix(): lines for path, lines in witness.lines.items()}
                after_lines = {(relative / path).as_posix(): lines for path, lines in new.lines.items()}
                relevant = outcome == DifferentialOutcome.FIXED and (
                    effects.observes_edit(before_lines, candidate=False)
                    and effects.observes_edit(after_lines, candidate=True)
                    if _is_pytest(argv) else has_source_edit and not changed_tests
                )
                if old.status in {"passed", "failed"} and new.status == "skipped":
                    report.warnings.append(f"Previously executed test became unobserved: {name}")
                cases.append(CaseTransition(case=name, outcome=outcome, relevant=relevant))

            outcome = _transition(_command_status(base, argv, old_cases), _command_status(head, argv, new_cases))
            replay_outcome = _transition(
                _command_status(replay, argv, replay_cases), _command_status(head, argv, new_cases),
            ) if replay is not None else outcome
            if replay_outcome in {DifferentialOutcome.REGRESSED, DifferentialOutcome.STILL_FAILING}:
                outcome = replay_outcome
            elif outcome not in {DifferentialOutcome.REGRESSED, DifferentialOutcome.STILL_FAILING}:
                outcome = replay_outcome
            relevant = outcome == DifferentialOutcome.FIXED and has_source_edit and not changed_tests
            if replay is not None and _command_status(base, argv, old_cases) == "passed" and not old_cases:
                report.warnings.append("Test changes prevent tracking the original test cases for " + base.command)
            if _command_status(head, argv, new_cases) == "skipped" and _command_status(base, argv, old_cases) != "skipped":
                report.warnings.append("Previously executed command became unobserved: " + head.command)
            report.commands.append(CommandTransition(
                command=argv, outcome=outcome, cases=cases, relevant=relevant,
                baseline=base, candidate=head, replay=replay,
            ))
        report.warnings = list(dict.fromkeys(report.warnings))
        return report


def verifier_runner(workspace: Path, backend: BwrapSandboxBackend | None = None) -> TestRunner:
    if backend is not None:
        backend.pytest_probe = _PROBE_PATH
    return TestRunner(workspace, sandbox_backend=backend, pytest_probe=_PROBE_PATH)


def _is_pytest(argv: list[str]) -> bool:
    return argv[:1] == ["pytest"] or argv[:3] == ["python", "-m", "pytest"]


def _extract_cases(result: TestResult, argv: list[str]) -> tuple[TestResult, dict[str, _Case]]:
    cases: dict[str, _Case] = {}
    if _is_pytest(argv):
        output = []
        for line in result.stdout.splitlines(keepends=True):
            if not line.startswith(_PROBE_PREFIX):
                output.append(line)
                continue
            try:
                payload = json.loads(line.removeprefix(_PROBE_PREFIX))
                if isinstance(payload, dict):
                    cases = {name: _Case.model_validate(value) for name, value in payload.items()}
            except (ValidationError, ValueError):
                cases = {}
        result = result.model_copy(update={"stdout": "".join(output)})
    elif argv[0] == "go":
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("Test") and event.get("Action") in {"pass", "fail", "skip"}:
                status = {"pass": "passed", "fail": "failed", "skip": "skipped"}[event["Action"]]
                cases[f"{event.get('Package')}::{event['Test']}"] = _Case(status=status)
    elif argv[0] == "cargo":
        counts: dict[str, int] = {}
        for name, outcome in _CARGO_CASE.findall(result.stdout):
            occurrence = counts.get(name, 0) + 1
            counts[name] = occurrence
            status = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}[outcome]
            cases[f"{name}#{occurrence}" if occurrence > 1 else name] = _Case(status=status)
    return result, cases


def _command_status(
    result: TestResult | None, argv: list[str], cases: dict[str, _Case],
) -> Literal["passed", "failed", "skipped"]:
    if result is None or result.exit_code in {124, 126, 127} or result.exit_code < 0:
        return "skipped"
    if _is_pytest(argv) and (
        result.exit_code in {4, 5}
        or {"--version", "-V", "--help", "-h", "--co", "--collect-only", "--fixtures",
            "--fixtures-per-test", "--markers", "--setup-only", "--setup-plan"}.intersection(argv)
    ):
        return "skipped"
    if not _is_pytest(argv) and argv[0] not in _SOURCE_SUFFIXES:
        return "skipped"
    if not _is_pytest(argv) and "test" not in argv and not any(
        argument.endswith(":test") or argument.startswith("test:") for argument in argv
    ):
        return "skipped"
    if result.exit_code != 0 or any(case.status == "failed" for case in cases.values()):
        return "failed"
    if cases:
        return "passed" if any(case.status == "passed" for case in cases.values()) else "skipped"
    if _is_pytest(argv) or argv[0] in {"go", "cargo"}:
        return "skipped"
    if _DISTINCT_TESTS.search(result.stdout):
        return "passed"
    if any(int(total) > int(skipped) for total, skipped in _SUREFIRE_SUMMARY.findall(result.stdout)):
        return "passed"
    if re.search(r"(?m)^# pass [1-9]\d*\s*$", result.stdout):
        return "passed"
    return "skipped"


def _junit_reports(root: Path, argv: list[str]) -> dict[Path, tuple[int, int]]:
    if argv[0] == "mvn":
        pattern = "**/target/surefire-reports/TEST-*.xml"
    elif argv[0] == "./gradlew":
        pattern = "**/build/test-results/*/TEST-*.xml"
    else:
        return {}
    reports = {}
    for path in root.glob(pattern):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        reports[path.relative_to(root)] = (info.st_mtime_ns, info.st_size)
    return reports


def _read_junit_cases(
    root: Path, argv: list[str], before: dict[Path, tuple[int, int]],
) -> dict[str, _Case]:
    cases = {}
    for path, signature in _junit_reports(root, argv).items():
        if signature == before.get(path) or signature[1] > 1024 * 1024:
            continue
        try:
            with open_regular(root, path) as source:
                report = ET.fromstring(source.read(1024 * 1024 + 1))
        except (OSError, ET.ParseError, RunTerminated):
            continue
        for case in report.iter("testcase"):
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            if case.find("failure") is not None or case.find("error") is not None:
                cases[name] = _Case(status="failed")
            elif case.find("skipped") is not None:
                cases[name] = _Case(status="skipped")
            else:
                cases[name] = _Case(status="passed")
    return cases


def _transition(before: str, after: str) -> DifferentialOutcome:
    if "skipped" in {before, after}:
        return DifferentialOutcome.UNOBSERVED
    if after == "failed":
        return DifferentialOutcome.REGRESSED if before == "passed" else DifferentialOutcome.STILL_FAILING
    return DifferentialOutcome.FIXED if before == "failed" else DifferentialOutcome.STILL_PASSING
