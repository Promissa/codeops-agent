import shutil
from pathlib import Path

from typer.testing import CliRunner

from codeops.cli import app


def test_cli_writes_intent_manifest_and_evidence_matrix(tmp_path):
    repo = tmp_path / "mini_data_pipeline"
    shutil.copytree("examples/fixtures/mini_data_pipeline", repo)
    issue = Path("examples/issues/csv_trailing_empty_column.md")
    out = tmp_path / ".runs" / "phase9_demo"

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
    intent_manifest = (out / "intent_manifest.md").read_text()
    evidence_matrix = (out / "evidence_matrix.md").read_text()
    final_report = (out / "final_report.md").read_text()

    for heading in [
        "# Intent",
        "# Behavior Change",
        "# Root Cause Hypothesis",
        "# Graph Evidence",
        "# Impact Envelope",
        "# Tests Run",
        "# Risks",
        "# Reviewer Checklist",
    ]:
        assert heading in intent_manifest

    assert "| Requirement | Changed File / Symbol |" in evidence_matrix
    assert "| R1 |" in evidence_matrix
    assert "pytest tests/test_parser.py -q" in evidence_matrix
    assert "# Final Report" in final_report
    assert "## Safety Checks" in final_report
    assert "- Command policy: passed" in final_report
    assert "- Secret filter: passed" in final_report
    assert "- High-risk paths: none touched" in final_report
    assert "- CodeGraph version:" in final_report
