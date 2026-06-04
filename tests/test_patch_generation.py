import shutil
from pathlib import Path

from codeops.agents.patch_generator import PatchGenerator
from codeops.agents.patch_planner import PatchPlanner
from codeops.agents.requirement_parser import RequirementParser
from codeops.core.models import ImpactEnvelope, SymbolRef
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
