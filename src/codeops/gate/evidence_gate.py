"""Adjudicate measured evidence into a reviewer-facing verdict."""

from collections import Counter

from pydantic import BaseModel, Field

from codeops.agents.reviewer import ReviewResult
from codeops.core.models import GraphEvidence, TrustVerdict
from codeops.gate.differential import CaseTransition, DifferentialOutcome, DifferentialResults
from codeops.gate.effect_map import EffectMap
from codeops.safety.patch_policy import PatchPolicyResult


class EvidenceGateResult(BaseModel):
    verdict: TrustVerdict
    actual_diff_status: str
    transitions: dict[str, int] = Field(default_factory=dict)
    violations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class GateEvidence(BaseModel):
    actual_policy: PatchPolicyResult | None = None
    effects: EffectMap = Field(default_factory=EffectMap)
    differential: DifferentialResults = Field(default_factory=DifferentialResults)
    findings: list[str] = Field(default_factory=list)
    rejected: bool = False


class EvidenceGate:
    """Require bounded workspace changes, discriminating tests, and caller exposure."""

    def adjudicate(
        self, evidence: GateEvidence, review: ReviewResult, graph: GraphEvidence | None, *,
        blocked: bool = False, allow_graph_fallback: bool = False,
    ) -> EvidenceGateResult:
        actual = evidence.actual_policy
        transitions = evidence.differential.transitions
        counts = Counter(item.outcome for item in transitions)
        result = EvidenceGateResult(
            verdict="inconclusive",
            actual_diff_status="unavailable" if actual is None else "passed" if actual.allowed else "rejected",
            transitions={outcome.value: counts[outcome] for outcome in DifferentialOutcome},
            violations=(
                list(actual.violations) if actual is not None
                else list(evidence.findings) if evidence.rejected else []
            ),
        )
        if blocked:
            result.verdict = "blocked"
            result.next_steps.append("Resolve the trust or sandbox denial recorded in the execution trace.")
            return result
        if evidence.rejected or result.violations or (actual is not None and not actual.allowed):
            result.verdict = "regressed"
            result.next_steps.append("Resolve the patch policy findings and rerun verification.")
        if counts[DifferentialOutcome.REGRESSED] or counts[DifferentialOutcome.STILL_FAILING]:
            result.verdict = "regressed"
            result.next_steps.extend(
                f"Investigate {item.case if isinstance(item, CaseTransition) else ' '.join(item.command)} ({item.outcome})."
                for item in transitions
                if item.outcome in {DifferentialOutcome.REGRESSED, DifferentialOutcome.STILL_FAILING}
            )
        if result.verdict == "regressed":
            result.next_steps = list(dict.fromkeys(result.next_steps))
            return result
        if not any(item.outcome == DifferentialOutcome.FIXED and item.relevant for item in transitions):
            result.next_steps.append(
                "Run a regression test that reproduces the reported behavior on baseline source "
                "and passes while exercising the edited code on the candidate."
            )
        for caller in evidence.effects.unobserved_callers:
            result.next_steps.append(
                f"Run a test that exercises {caller.symbol} in {caller.path}"
                f"{f':{caller.line}' if caller.line else ''} and reaches the changed behavior."
            )
        result.next_steps.extend(evidence.findings)
        result.next_steps.extend(evidence.effects.warnings)
        result.next_steps.extend(evidence.differential.warnings)
        result.next_steps.extend(
            f"Resolve review finding: {item}." for item in (*review.violations, *review.warnings)
            if not (allow_graph_fallback and item == "graph confidence is below auto-patch threshold")
        )
        if not review.verified and not review.violations and not review.warnings:
            result.next_steps.append("Resolve the reviewer assessment and rerun verification.")
        if graph is None or any(check.status == "fail" for check in graph.cross_checks):
            result.next_steps.append("Corroborate the effect map and resolve conflicting graph evidence.")
        if actual is None or not actual.summary.changed_files:
            result.next_steps.append("Confirm a non-empty net workspace change and rerun its policy checks.")
        if not result.next_steps:
            result.verdict = "verified"
        return result
