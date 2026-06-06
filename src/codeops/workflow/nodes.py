"""Workflow helper nodes."""

from pathlib import Path

from typing import TypeVar

from codeops.core.models import ImpactEnvelope, TestCommand, VerificationPlan


T = TypeVar("T")


class VerificationPlanBuilder:
    """Select focused tests from the current impact envelope."""

    def build(
        self,
        impact_envelope: ImpactEnvelope,
        acceptance_tests: list[str] | None = None,
        static_checks: list[object] | None = None,
        repo_path: Path | None = None,
        language: str = "Python",
    ) -> VerificationPlan:
        cwd = (repo_path or Path(".")).resolve()
        affected_tests = _dedupe(impact_envelope.affected_tests)
        acceptance = _dedupe(acceptance_tests or affected_tests)
        return VerificationPlan(
            acceptance_tests=[
                _pytest_command(test, cwd, "acceptance", language)
                for test in acceptance
            ],
            affected_tests=[
                _pytest_command(test, cwd, "affected", language)
                for test in affected_tests
            ],
            module_tests=[
                _pytest_command(test, cwd, "module", language)
                for test in affected_tests
            ],
            full_tests=[
                TestCommand(
                    id="pytest_full",
                    command=["pytest", "-q"],
                    cwd=cwd,
                    scope="full",
                    language=language,
                    parse_format="pytest",
                )
            ],
            static_checks=static_checks or [],
            behavior_diff_required=True,
            coverage_required=False,
        )

    def commands(
        self, plan: VerificationPlan, risk_level: str = "low"
    ) -> list[TestCommand]:
        tests = _dedupe([*plan.acceptance_tests, *plan.affected_tests])
        if risk_level in {"medium", "high"}:
            tests = _dedupe([*tests, *plan.module_tests])
        if risk_level == "high":
            tests = _dedupe([*tests, *plan.full_tests])
        return tests


def _pytest_command(
    test: str,
    cwd: Path,
    scope: str,
    language: str,
) -> TestCommand:
    if test.startswith("pytest "):
        argv = test.split()
        command_id = "_".join(argv)
    else:
        argv = ["pytest", test, "-q"]
        command_id = f"pytest_{scope}_{test.replace('/', '_').replace(':', '_')}"
    return TestCommand(
        id=command_id,
        command=argv,
        cwd=cwd,
        scope=scope,
        language=language,
        parse_format="pytest",
    )


def _dedupe(items: list[T]) -> list[T]:
    seen: set[object] = set()
    ordered: list[T] = []
    for item in items:
        key: object
        if isinstance(item, TestCommand):
            key = tuple(item.command)
        else:
            key = item
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered
