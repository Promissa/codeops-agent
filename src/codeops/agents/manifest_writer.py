"""Manifest and evidence matrix rendering."""

from collections.abc import Sequence

from codeops.agents.reviewer import ReviewResult
from codeops.core.models import TaskState
from codeops.gate.evidence_gate import EvidenceGateResult
from codeops.reporting.summary import determine_verdict, verification_outcome
from codeops.reporting.trace_events import TraceEvent
from codeops.safety.secret_filter import SecretFilter


class ManifestWriter:
    """Render reviewer-facing markdown artifacts."""

    def evidence_matrix(self, state: TaskState, events: Sequence[TraceEvent] = ()) -> str:
        contract = state.acceptance_contract
        requirement = contract.requirement_id if contract else "unknown"
        changed = _changed_file_summary(state)
        verification = _tests_summary(state)
        outcomes = {verification_outcome(result) for result in state.test_results}
        status = "Failed" if "failed" in outcomes else "Passed" if outcomes == {"passed"} else "Pending"
        gate = _gate_details(events)
        if gate:
            if gate.actual_diff_status != "unavailable":
                changed = "[Actual workspace diff](workspace_diff.json)"
            verification += " · [Differential results](differential_results.json) · " + _gate_transitions(gate)
            status = gate.verdict

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

    def intent_manifest(
        self, state: TaskState, review: ReviewResult, events: Sequence[TraceEvent] = (),
    ) -> str:
        content = "\n".join(
            [
                "# Intent",
                "",
                _requirement_summary(state),
                "",
                "# Trust Summary",
                "",
                *_trust_summary(state, review, events),
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
        return SecretFilter().scan(content).redacted_text

    def final_report(
        self, state: TaskState, review: ReviewResult, events: Sequence[TraceEvent] = (),
    ) -> str:
        content = "\n".join(
            [
                "# Final Report",
                "",
                f"- Task: `{state.task_id}`",
                f"- Status: `{state.status}`",
                *_trust_summary(state, review, events),
                f"- Reviewer verified: `{(state.verdict or determine_verdict(state, review, events)) == 'verified'}`",
                f"- Violations: {', '.join(review.violations) or 'none'}",
                f"- Warnings: {', '.join(review.warnings) or 'none'}",
                "",
                "## Tests",
                "",
                _tests_markdown(state),
                "",
                "## Safety Checks",
                "",
                _safety_checks(state, events),
                "",
            ]
        )
        return SecretFilter().scan(content).redacted_text


def _trust_summary(state: TaskState, review: ReviewResult, events: Sequence[TraceEvent]) -> list[str]:
    reason = next(
        (event.details.get("reason") or event.details.get("message") or event.summary
         for event in reversed(events)
         if event.kind == "error" or (event.kind == "policy_check" and event.stage == "run")),
        None,
    )
    lines = [
        f"- Trust verdict: `{state.verdict or determine_verdict(state, review, events)}`",
        f"- Lifecycle: `{state.lifecycle_state or 'unrecorded'}`",
        f"- Termination: `{state.termination_reason or 'unrecorded'}`",
        "- Trace: [Visual report](trace_report.html) · [Event stream](trace_events.jsonl)",
    ]
    if reason:
        lines.append(f"- Termination detail: {reason}")
    gate = _gate_details(events)
    if gate:
        lines.extend([
            f"- Actual net diff policy: `{gate.actual_diff_status}`",
            f"- Differential transitions: {_gate_transitions(gate)}",
            "- Gate evidence: [Verdict](evidence_gate.json) · [Effect map](effect_map.json)",
            *(f"- Next step: {step}" for step in gate.next_steps),
        ])
    return lines


def _gate_details(events: Sequence[TraceEvent]) -> EvidenceGateResult | None:
    for event in reversed(events):
        if event.kind == "evidence_gate" and "gate" in event.details:
            return EvidenceGateResult.model_validate(event.details["gate"])
    return None


def _gate_transitions(gate: EvidenceGateResult) -> str:
    return ", ".join(f"{count} {outcome}" for outcome, count in gate.transitions.items())


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
        f"- `{result.command}`: {verification_outcome(result)}"
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


def _safety_checks(state: TaskState, events: Sequence[TraceEvent]) -> str:
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
    patch_checks = [event for event in events if event.kind == "policy_check" and event.stage == "patch"]
    patch_policy = "denied" if any(event.status == "denied" for event in patch_checks) else (
        "passed" if patch_checks else "not recorded"
    )
    command_checks = [event for event in events if event.kind == "policy_check" and event.stage == "verification"]
    command_policy = "denied" if any(event.status == "denied" for event in command_checks) else (
        "passed" if any(event.status == "allowed" for event in command_checks) else "not recorded"
    )
    secret_status = (
        "redacted"
        if "[REDACTED " in state.issue_text
        else "passed"
        if secret_scan.passed
        else "redacted"
    )
    return "\n".join(
        [
            f"- Command policy: {command_policy}",
            f"- Patch policy: {patch_policy}",
            f"- Secret filter: {secret_status}",
            f"- High-risk paths: {high_risk}",
            f"- CodeGraph version: {codegraph_version}",
        ]
    )
