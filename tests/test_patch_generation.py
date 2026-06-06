import shutil
from pathlib import Path

from codeops.agents.patch_generator import PatchGenerator
from codeops.agents.patch_planner import PatchPlanner
from codeops.agents.requirement_parser import RequirementParser
from codeops.agents.llm_provider import LLMPatchResult
from codeops.core.models import AcceptanceContract, ImpactEnvelope, PatchPlan, SymbolRef
from codeops.safety.patch_policy import PatchPolicy
from codeops.tools.patch_tool import PatchTool


def test_fixture_csv_patch_is_minimal_and_policy_allowed(tmp_path):
    repo = _copy_fixture(tmp_path)
    contract = RequirementParser().parse(
        Path("examples/issues/csv_trailing_empty_column.md").read_text()
    )
    envelope = _fixture_envelope()
    plan = PatchPlanner().plan(contract, envelope)

    patch = PatchGenerator().generate(repo, plan, contract)
    policy_result = PatchPolicy().validate(patch, envelope)
    patch_result = PatchTool(repo).apply(patch)

    assert policy_result.allowed
    assert policy_result.summary.changed_files == [
        "src/mini_data_pipeline/parser.py",
        "tests/test_parser.py",
    ]
    assert patch_result.passed, patch_result.stderr
    assert 'if value == "":' in (
        repo / "src/mini_data_pipeline/parser.py"
    ).read_text()
    assert "test_trailing_empty_column_returns_none" in (
        repo / "tests/test_parser.py"
    ).read_text()


def test_policy_rejects_overbroad_patch_from_generator_context():
    overbroad_patch = """diff --git a/src/mini_data_pipeline/parser.py b/src/mini_data_pipeline/parser.py
--- a/src/mini_data_pipeline/parser.py
+++ b/src/mini_data_pipeline/parser.py
@@ -1 +1 @@
-old = True
+new = True
diff --git a/src/mini_data_pipeline/loader.py b/src/mini_data_pipeline/loader.py
--- a/src/mini_data_pipeline/loader.py
+++ b/src/mini_data_pipeline/loader.py
@@ -1 +1 @@
-old = True
+new = True
"""
    envelope = _fixture_envelope()

    result = PatchPolicy().validate(overbroad_patch, envelope)

    assert not result.allowed
    assert (
        "changed file outside impact envelope: src/mini_data_pipeline/loader.py"
        in result.violations
    )


def test_patch_generator_uses_llm_provider_as_fallback(tmp_path):
    repo = _copy_fixture(tmp_path)
    provider = FakePatchProvider()
    contract = AcceptanceContract(
        requirement_id="R1",
        summary="Change parser greeting.",
        user_visible_before="Old greeting.",
        user_visible_after="New greeting.",
        examples=[],
        invariants=[],
        non_goals=[],
        ambiguity_questions=[],
        status="ready",
    )
    plan = PatchPlan(
        hypothesis="The parser greeting is stale.",
        edit_strategy="Small source edit.",
        files_to_edit=["src/mini_data_pipeline/parser.py"],
        tests_to_add_or_update=[],
        expected_behavior_change="New greeting.",
    )

    patch = PatchGenerator(provider=provider).generate(
        repo,
        plan,
        contract,
        _fixture_envelope(),
    )

    assert patch.startswith("diff --git")
    assert provider.called


def _copy_fixture(tmp_path) -> Path:
    repo = tmp_path / "mini_data_pipeline"
    shutil.copytree("examples/fixtures/mini_data_pipeline", repo)
    return repo


def _fixture_envelope() -> ImpactEnvelope:
    return ImpactEnvelope(
        target_symbols=[
            SymbolRef(
                symbol="parse_row",
                path="src/mini_data_pipeline/parser.py",
                kind="function",
            )
        ],
        allowed_files=[
            "src/mini_data_pipeline/parser.py",
            "tests/test_parser.py",
        ],
        affected_files=["src/mini_data_pipeline/parser.py"],
        affected_tests=["tests/test_parser.py"],
        forbidden_changes=["dependency change", "public API signature change"],
        risk_level="low",
        requires_human_approval=False,
    )


class FakePatchProvider:
    def __init__(self) -> None:
        self.called = False

    def generate_patch(
        self,
        repo_path: Path,
        patch_plan: PatchPlan,
        acceptance_contract: AcceptanceContract,
        impact_envelope: ImpactEnvelope,
    ) -> LLMPatchResult:
        self.called = True
        return LLMPatchResult(
            patch_diff=(
                "diff --git a/src/mini_data_pipeline/parser.py b/src/mini_data_pipeline/parser.py\n"
                "--- a/src/mini_data_pipeline/parser.py\n"
                "+++ b/src/mini_data_pipeline/parser.py\n"
                "@@ -1 +1 @@\n"
                "-old = True\n"
                "+new = True\n"
            ),
            input_tokens=10,
            output_tokens=20,
            model="test-model",
            provider="test",
        )
