"""Deterministic verdicts from run state and recorded checks."""

from collections.abc import Sequence
import re
from typing import Literal

from codeops.agents.reviewer import ReviewResult
from codeops.core.models import TaskState, TestResult, TrustVerdict
from codeops.reporting.trace_events import TraceEvent
from codeops.runtime.lifecycle import TerminationReason
from codeops.trust.preflight import TaskTrustContract


BLOCKING_TERMINATIONS = {
    TerminationReason.POLICY_DENIED, TerminationReason.RESOURCE_EXHAUSTED,
    TerminationReason.TIMED_OUT, TerminationReason.CANCELLED,
}


def verification_outcome(result: TestResult) -> Literal["passed", "failed", "unobserved"]:
    """Distinguish test results from successful inspection or static commands."""
    if result.execution_outcome is not None:
        return result.execution_outcome
    argv = result.command.split()
    is_pytest = argv[:1] == ["pytest"] or argv[:3] == ["python", "-m", "pytest"]
    if is_pytest and (
        result.exit_code in {4, 5}
        or {"--version", "-V", "--help", "-h", "--co", "--collect-only", "--fixtures",
            "--fixtures-per-test", "--markers", "--setup-only", "--setup-plan"}.intersection(argv)
    ):
        return "unobserved"
    if result.exit_code == 127:
        return "unobserved"
    if not result.passed or result.exit_code != 0:
        return "failed"
    if is_pytest and re.search(r"\b\d+ (?:skipped|xfailed|deselected)\b", result.stdout):
        if not re.search(r"\b[1-9]\d* passed\b", result.stdout):
            return "unobserved"
    if is_pytest or (
        argv and argv[0] in {"go", "cargo", "mvn", "npm", "pnpm", "yarn", "bun", "./gradlew"}
        and (argv[-1] == "test" or argv[-1].endswith(":test") or argv[1:2] == ["test"])
    ):
        return "passed"
    return "unobserved"


def determine_verdict(
    state: TaskState, review: ReviewResult, events: Sequence[TraceEvent] = (),
    contract: TaskTrustContract | None = None,
) -> TrustVerdict:
    if (
        state.termination_reason in BLOCKING_TERMINATIONS
        or (contract is not None and not contract.approved)
        or any(
            event.status == "denied" and (
                event.kind in {"trust_decision", "outbound_filter"}
                or (event.kind == "policy_check" and event.stage != "patch")
            )
            for event in events
        )
    ):
        return "blocked"
    for event in reversed(events):
        if event.kind == "evidence_gate":
            verdict = event.details.get("gate", {}).get("verdict")
            if verdict in {"verified", "regressed", "inconclusive", "blocked"}:
                return verdict
    outcomes = [verification_outcome(result) for result in state.test_results]
    if (
        "failed" in outcomes
        or any(
            (event.kind in {"patch", "test_run"} and event.status == "failed")
            or (event.kind == "policy_check" and event.stage == "patch" and event.status == "denied")
            for event in events
        )
        or any(violation.startswith(("patch changes ", "patch exceeds ")) for violation in review.violations)
    ):
        return "regressed"
    return "inconclusive"
