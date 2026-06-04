from typer.testing import CliRunner

from codeops.cli import app


def test_codeops_eval_writes_report(tmp_path):
    out_root = tmp_path / ".runs"

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "--tasks",
            "examples/eval_tasks.jsonl",
            "--mode",
            "repo_sketch_codegraph",
            "--out-root",
            str(out_root),
        ],
    )

    assert result.exit_code == 0, result.output
    reports = list(out_root.glob("eval_*/eval_report.md"))
    assert len(reports) == 1
    report = reports[0].read_text()
    assert "# Eval Report" in report
    assert "repo_sketch_codegraph" in report
    assert "csv_trailing_empty_column" in report
    assert "| repo_sketch_codegraph |" in report
