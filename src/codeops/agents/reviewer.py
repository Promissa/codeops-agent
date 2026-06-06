"""Reviewer checks for run state."""

from pydantic import BaseModel, Field

from codeops.core.models import TaskState
from codeops.tools.patch_tool import PatchTool


class ReviewResult(BaseModel):
    verified: bool
    violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Reviewer:
    """Check whether a task has enough evidence for a PR-style result."""

    def review(self, state: TaskState) -> ReviewResult:
        violations: list[str] = []
        warnings: list[str] = []

        if state.acceptance_contract is None:
            violations.append("missing acceptance contract")
        elif state.acceptance_contract.status == "needs_clarification":
            violations.append("acceptance contract needs clarification")

        if state.graph_evidence is None:
            violations.append("missing graph evidence")
        elif state.graph_evidence.graph_confidence < 0.60:
            warnings.append("graph confidence is below auto-patch threshold")

        if state.impact_envelope is None:
            violations.append("missing impact envelope")

        if state.patch_diff:
            summary = PatchTool(".").summarize(state.patch_diff)
            envelope = state.impact_envelope
            if envelope is not None:
                outside = [
                    path
                    for path in summary.changed_files
                    if path not in envelope.allowed_files
                ]
                if outside:
                    violations.append(
                        "patch changes files outside impact envelope: "
                        + ", ".join(outside)
                    )
                if len(summary.changed_files) > envelope.max_files_changed:
                    violations.append("patch exceeds changed file budget")
                if summary.changed_loc > envelope.max_loc_changed:
                    violations.append("patch exceeds changed LOC budget")
        else:
            warnings.append("no patch diff is present")

        if not state.test_results and state.patch_diff:
            violations.append("missing test results")
        failed_tests = [result.command for result in state.test_results if not result.passed]
        if failed_tests:
            violations.append("failed tests: " + ", ".join(failed_tests))

        return ReviewResult(
            verified=not violations and not warnings,
            violations=violations,
            warnings=warnings,
        )
