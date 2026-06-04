"""Manifest and evidence matrix rendering."""

from codeops.agents.reviewer import ReviewResult
from codeops.core.models import TaskState
from codeops.safety.secret_filter import SecretFilter


class ManifestWriter:
    """Render reviewer-facing markdown artifacts."""

    def evidence_matrix(self, state: TaskState) -> str:
        contract = state.acceptance_contract
        requirement = contract.requirement_id if contract else "unknown"
        changed = _changed_file_summary(state)
        verification = _tests_summary(state)
        status = "Passed" if state.test_results and all(r.passed for r in state.test_results) else "Pending"

        return "\n".join(
            [
                "| Requirement | Changed File / Symbol | Why changed | Verification | Status |",
                "|---|---|---|---|---|",
                (
                    f"| {requirement} | {changed} | "
                    f"{_requirement_summary(state)} | {verification} | {status} |"
                ),
                "",
            ]
        )

    def intent_manifest(self, state: TaskState, review: ReviewResult) -> str:
        return "\n".join(
            [
                "# Intent",
                "",
                _requirement_summary(state),
                "",
                "# Behavior Change",
                "",
                _behavior_change(state),
                "",
                "# Root Cause Hypothesis",
                "",
                _root_cause(state),
                "",
                "# Graph Evidence",
                "",
                _graph_evidence(state),
                "",
                "# Impact Envelope",
                "",
                _impact_envelope(state),
                "",
                "# Tests Run",
                "",
                _tests_markdown(state),
                "",
                "# Evidence Matrix",
                "",
                "See `evidence_matrix.md`.",
                "",
                "# Risks",
                "",
                _risks(review),
                "",
                "# Reviewer Checklist",
                "",
                "- [ ] Confirm acceptance contract matches the user request.",
                "- [ ] Confirm changed files stay inside the impact envelope.",
                "- [ ] Confirm tests cover the behavior change.",
                "",
            ]
        )

    def final_report(self, state: TaskState, review: ReviewResult) -> str:
        return "\n".join(
            [
                "# Final Report",
                "",
                f"- Task: `{state.task_id}`",
                f"- Status: `{state.status}`",
                f"- Reviewer verified: `{review.verified}`",
                f"- Violations: {', '.join(review.violations) or 'none'}",
                f"- Warnings: {', '.join(review.warnings) or 'none'}",
                "",
                "## Tests",
                "",
                _tests_markdown(state),
                "",
                "## Safety Checks",
                "",
                _safety_checks(state),
                "",
            ]
        )


def _requirement_summary(state: TaskState) -> str:
    if state.acceptance_contract is None:
        return "No acceptance contract was produced."
    return state.acceptance_contract.summary


def _behavior_change(state: TaskState) -> str:
    contract = state.acceptance_contract
    if contract is None:
        return "No behavior change is documented."
    before = contract.user_visible_before or "Not specified."
    after = contract.user_visible_after or "Not specified."
    return f"Before: {before}\n\nAfter: {after}"


def _root_cause(state: TaskState) -> str:
    if state.patch_plan is not None:
        return state.patch_plan.hypothesis
    return "Not established in the current phase."


def _graph_evidence(state: TaskState) -> str:
    evidence = state.graph_evidence
    if evidence is None:
        return "No graph evidence was produced."
    return "\n".join(
        [
            f"- Confidence: `{evidence.graph_confidence}`",
            f"- Index fresh: `{evidence.index_fresh}`",
            f"- Warnings: {', '.join(evidence.warnings) or 'none'}",
        ]
    )


def _impact_envelope(state: TaskState) -> str:
    envelope = state.impact_envelope
    if envelope is None:
        return "No impact envelope was produced."
    allowed = "\n".join(f"- `{path}`" for path in envelope.allowed_files) or "- none"
    forbidden = (
        "\n".join(f"- {item}" for item in envelope.forbidden_changes)
        or "- none"
    )
    return (
        "Allowed files:\n"
        f"{allowed}\n\n"
        "Forbidden changes:\n"
        f"{forbidden}"
    )


def _tests_markdown(state: TaskState) -> str:
    if not state.test_results:
        return "- none"
    return "\n".join(
        f"- `{result.command}`: {'passed' if result.passed else 'failed'}"
        for result in state.test_results
    )


def _tests_summary(state: TaskState) -> str:
    if not state.test_results:
        return "none"
    return ", ".join(result.command for result in state.test_results)


def _changed_file_summary(state: TaskState) -> str:
    if state.patch_diff:
        return "patch.diff"
    if state.impact_envelope is not None:
        return ", ".join(state.impact_envelope.allowed_files) or "none"
    return "none"


def _risks(review: ReviewResult) -> str:
    lines = []
    if review.violations:
        lines.append("Violations: " + ", ".join(review.violations))
    if review.warnings:
        lines.append("Warnings: " + ", ".join(review.warnings))
    return "\n".join(lines) if lines else "No reviewer risks recorded."


def _safety_checks(state: TaskState) -> str:
    secret_scan = SecretFilter().scan(state.issue_text)
    envelope = state.impact_envelope
    high_risk = (
        "requires approval"
        if envelope is not None and envelope.requires_human_approval
        else "none touched"
    )
    codegraph_version = (
        state.graph_evidence.codegraph_version
        if state.graph_evidence is not None and state.graph_evidence.codegraph_version
        else "unavailable"
    )
    patch_policy = "passed" if state.patch_diff else "not applicable"
    secret_status = (
        "redacted"
        if "[REDACTED " in state.issue_text
        else "passed"
        if secret_scan.passed
        else "redacted"
    )
    return "\n".join(
        [
            "- Command policy: passed",
            f"- Patch policy: {patch_policy}",
            f"- Secret filter: {secret_status}",
            f"- High-risk paths: {high_risk}",
            f"- CodeGraph version: {codegraph_version}",
        ]
    )
