"""Patch planning."""

from codeops.core.models import AcceptanceContract, ImpactEnvelope, PatchPlan


class PatchPlanner:
    """Create a minimal patch plan from a contract and impact envelope."""

    def plan(
        self,
        acceptance_contract: AcceptanceContract,
        impact_envelope: ImpactEnvelope,
    ) -> PatchPlan:
        source_files = [
            path for path in impact_envelope.allowed_files if not path.startswith("tests/")
        ]
        test_files = [
            path for path in impact_envelope.allowed_files if path.startswith("tests/")
        ]

        return PatchPlan(
            hypothesis=_hypothesis(acceptance_contract),
            edit_strategy="Make the smallest code and test change inside the impact envelope.",
            files_to_edit=source_files[:1],
            tests_to_add_or_update=test_files[:1],
            expected_behavior_change=acceptance_contract.user_visible_after
            or acceptance_contract.summary,
            risk_notes=[
                "Rule-based Phase 7 patch generation only supports fixture patterns."
            ],
        )


def _hypothesis(contract: AcceptanceContract) -> str:
    text = f"{contract.summary} {contract.user_visible_before or ''}".lower()
    if "trailing" in text and "csv" in text:
        return "CSV trailing empty fields are dropped or normalized incorrectly."
    return "The observed behavior differs from the acceptance contract."
