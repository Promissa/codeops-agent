"""Persist and render evidence for workflows and standalone verification."""

import time

from codeops.agents.manifest_writer import ManifestWriter
from codeops.agents.reviewer import Reviewer
from codeops.core.artifacts import ArtifactWriter
from codeops.core.models import TaskState, TestResult
from codeops.evolution.run import RunEvolution
from codeops.gate.evidence_gate import EvidenceGate, GateEvidence
from codeops.reporting.summary import determine_verdict, verification_outcome
from codeops.reporting.trace_events import TraceRecorder, TraceRedactor, TraceStatus
from codeops.reporting.trace_html import render_trace_report
from codeops.runtime.lifecycle import RunLifecycleState, TerminationReason
from codeops.safety.secret_filter import SecretFilter
from codeops.trust.preflight import TaskTrustContract


def finalize_run(
    writer: ArtifactWriter, state: TaskState, trace: TraceRecorder,
    contract: TaskTrustContract | None, gate: GateEvidence | None = None, *,
    allow_graph_fallback: bool = False, evolution: RunEvolution | None = None,
) -> None:
    started = time.monotonic()
    actual = gate.actual_policy if gate else None
    review = Reviewer(actual.summary if actual else None).review(state)
    manifest_writer = ManifestWriter()
    state.evidence_matrix_path = writer.paths.artifact_path("evidence_matrix")
    state.intent_manifest_path = writer.paths.artifact_path("intent_manifest")
    review_statuses: dict[str, TraceStatus] = {
        "verified": "ok", "regressed": "failed", "inconclusive": "inconclusive", "blocked": "blocked",
    }
    if gate is not None:
        prior = determine_verdict(state, review, trace.events, contract)
        if state.termination_reason != TerminationReason.COMPLETED and prior != "blocked":
            gate.findings.append("Resolve the execution error recorded in the trace and rerun verification.")
        result = EvidenceGate().adjudicate(
            gate, review, state.graph_evidence, blocked=prior == "blocked",
            allow_graph_fallback=allow_graph_fallback,
        )
        writer.write_json("effect_map", TraceRedactor().redact(gate.effects))
        writer.write_json("differential_results", TraceRedactor().redact(gate.differential))
        writer.write_json("evidence_gate", TraceRedactor().redact(result))
        trace.record(
            "evidence_gate", "evidence_gate", review_statuses[result.verdict], "Adjudicate differential evidence",
            details={"gate": result, "effect_map": gate.effects},
            refs=["evidence_gate.json", "effect_map.json", "differential_results.json", "workspace_diff.json"],
        )
    verdict = determine_verdict(state, review, trace.events, contract)
    state.verdict = verdict
    writer.write_markdown("evidence_matrix", manifest_writer.evidence_matrix(state, trace.events))
    review = review.model_copy(update={"verified": verdict == "verified"})
    trace.record(
        "review", "review", review_statuses[verdict], f"Trust verdict: {verdict}",
        details={"review": review, "verdict": verdict},
        refs=["intent_manifest.md", "evidence_matrix.md"], duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    writer.write_markdown("intent_manifest", manifest_writer.intent_manifest(state, review, trace.events))
    writer.write_markdown("final_report", manifest_writer.final_report(state, review, trace.events))
    writer.write_json("test_results", state.test_results)
    writer.write_json("cost_trace", state.cost_trace)
    if evolution is not None:
        evolution.finalize(state)
    writer.write_html(
        "trace_report", render_trace_report(state, review, contract, trace.events),
    )
    writer.write_json("task", state)

def checkpoint(
    writer: ArtifactWriter, state: TaskState, trace: TraceRecorder, phase: RunLifecycleState,
) -> None:
    state.lifecycle_state = phase
    writer.write_json("task", state)
    trace.record(
        "run", "sandbox_lifecycle", "ok", f"Lifecycle: {phase.value}",
        details={"state": phase, "termination_reason": state.termination_reason},
    )


def record_issue(state: TaskState, trace: TraceRecorder, text: str) -> None:
    scan = SecretFilter().scan(text)
    state.issue_text = scan.redacted_text
    trace.record(
        "preflight", "outbound_filter", "ok" if scan.passed else "warning", "Filter issue input",
        details={"destination": "local artifacts and workflow input", "redactions": len(scan.findings)},
    )


def record_verification(state: TaskState, trace: TraceRecorder, role: str, result: TestResult) -> None:
    state.cost_trace.test_runs += 1
    state.cost_trace.tool_calls += 1
    trace.record(
        "verification", "policy_check", "allowed", "Test command passed command policy",
        details={"command": result.command, "workspace": role},
    )
    if role == "candidate":
        state.test_results.append(result)
        _record_test_result(trace, result)
    else:
        trace.record(
            "verification", "test_run", "ok", f"Recorded {role} test result",
            details={"command": result.command, "exit_code": result.exit_code, "workspace": role},
            duration_ms=round(result.duration_seconds * 1000, 2),
        )


def _record_test_result(trace: TraceRecorder, result: TestResult) -> None:
    outcome = verification_outcome(result)
    descriptions: dict[str, tuple[TraceStatus, str]] = {
        "passed": ("ok", "Test command passed"),
        "failed": ("failed", "Test command failed"),
        "unobserved": ("inconclusive", "Test execution unconfirmed"),
    }
    status, summary = descriptions[outcome]
    details = {"command": result.command, "exit_code": result.exit_code, "outcome": outcome}
    if outcome != "passed":
        scan = SecretFilter().scan(result.stdout + "\n" + result.stderr)
        details["output_tail"] = scan.redacted_text[-4000:].strip()
        details["redactions"] = len(scan.findings)
    trace.record(
        "verification", "test_run", status, summary, details=details,
        refs=["test_results.json"], duration_ms=round(result.duration_seconds * 1000, 2),
    )
