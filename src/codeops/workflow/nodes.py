"""Workflow helper nodes."""

from codeops.core.models import ImpactEnvelope, VerificationPlan


class VerificationPlanBuilder:
    """Select focused tests from the current impact envelope."""

    def build(
        self,
        impact_envelope: ImpactEnvelope,
        acceptance_tests: list[str] | None = None,
        static_checks: list[str] | None = None,
    ) -> VerificationPlan:
        affected_tests = _dedupe(impact_envelope.affected_tests)
        acceptance = _dedupe(acceptance_tests or affected_tests)
        return VerificationPlan(
            acceptance_tests=acceptance,
            affected_tests=affected_tests,
            module_tests=affected_tests,
            full_tests=["pytest -q"],
            static_checks=static_checks or [],
            behavior_diff_required=True,
            coverage_required=False,
        )

    def commands(self, plan: VerificationPlan, risk_level: str = "low") -> list[str]:
        tests = _dedupe([*plan.acceptance_tests, *plan.affected_tests])
        if risk_level in {"medium", "high"}:
            tests = _dedupe([*tests, *plan.module_tests])
        if risk_level == "high":
            tests = _dedupe([*tests, *plan.full_tests])

        commands = []
        for test in tests:
            commands.append(test if test.startswith("pytest ") else f"pytest {test} -q")
        return commands


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
