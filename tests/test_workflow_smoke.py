import json
from pathlib import Path
import shutil

from typer.testing import CliRunner

from codeops.core.errors import CommandRejected
from codeops.cli import app
from codeops.tools.test_runner import TestRunner as CodeOpsTestRunner


def test_cli_imports_and_shows_help():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "run" in result.output


def test_test_runner_runs_only_allowlisted_commands():
    runner = CodeOpsTestRunner(Path.cwd())

    result = runner.run(["pytest", "--version"])

    assert result.passed
    assert "pytest" in result.stdout.lower()

    try:
        runner.run(["python", "-c", "print(1)"])
    except CommandRejected:
        pass
    else:
        raise AssertionError("arbitrary python command should be rejected")


def test_cli_completes_fixture_workflow_and_writes_expected_artifacts(tmp_path):
    repo = tmp_path / "mini_data_pipeline"
    shutil.copytree("examples/fixtures/mini_data_pipeline", repo)
    out = tmp_path / ".runs" / "demo_csv_bug"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--issue",
            "examples/issues/csv_trailing_empty_column.md",
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    for artifact in [
        "acceptance_contract.yaml",
        "graph_evidence.json",
        "impact_envelope.yaml",
        "patch_plan.yaml",
        "patch.diff",
        "test_results.json",
        "evidence_matrix.md",
        "intent_manifest.md",
        "final_report.md",
        "cost_trace.json",
    ]:
        assert (out / artifact).exists(), artifact

    assert "test_trailing_empty_column_returns_none" in (
        repo / "tests/test_parser.py"
    ).read_text()
    task = json.loads((out / "task.json").read_text())
    assert task["project_profile"]["primary_language"] == "Python"
    assert task["project_profile"]["language_profiles"] == ["python"]
    assert "completed demo_csv_bug" in result.output
