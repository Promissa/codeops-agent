import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from codeops.cli import app
from codeops.core.models import ImpactEnvelope, TestCommand
from codeops.workflow.nodes import VerificationPlanBuilder


def test_verification_plan_builder_selects_focused_pytest_command():
    envelope = ImpactEnvelope(
        target_symbols=[],
        allowed_files=["tests/test_parser.py"],
        affected_files=[],
        affected_tests=["tests/test_parser.py"],
        forbidden_changes=[],
        risk_level="low",
        requires_human_approval=False,
    )

    builder = VerificationPlanBuilder()
    plan = builder.build(envelope, repo_path=Path("examples/fixtures/mini_data_pipeline"))

    assert plan.acceptance_tests == [
        TestCommand(
            id="pytest_acceptance_tests_test_parser.py",
            command=["pytest", "tests/test_parser.py", "-q"],
            cwd=Path("examples/fixtures/mini_data_pipeline").resolve(),
            scope="acceptance",
            language="Python",
            parse_format="pytest",
        )
    ]
    assert builder.commands(plan) == plan.acceptance_tests


def test_cli_runs_fixture_parser_tests_and_saves_results(tmp_path):
    repo = tmp_path / "mini_data_pipeline"
    shutil.copytree("examples/fixtures/mini_data_pipeline", repo)
    issue = Path("examples/issues/csv_trailing_empty_column.md")
    out = tmp_path / ".runs" / "phase8_demo"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--issue",
            str(issue),
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    results = json.loads((out / "test_results.json").read_text())
    assert results[0]["command"] == "pytest tests/test_parser.py -q"
    assert results[0]["passed"] is True
    assert (out / "verification_plan.yaml").exists()
    verification_plan = (out / "verification_plan.yaml").read_text()
    assert "command:" in verification_plan
    assert "- \"pytest\"" in verification_plan
