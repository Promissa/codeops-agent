import json
from pathlib import Path

from typer.testing import CliRunner

from codeops.cli import app
from codeops.safety.secret_filter import SecretFilter


def test_secret_filter_redacts_common_secret_patterns():
    text = "API_KEY=sk-abcdefghijklmnopqrstuvwxyz password=hunter2"

    result = SecretFilter().scan(text)

    assert not result.passed
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.redacted_text
    assert "[REDACTED openai_api_key]" in result.redacted_text


def test_cli_redacts_issue_text_and_records_no_network_warning(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "parser.py").write_text("def parse_csv(text):\n    return []\n")
    issue = tmp_path / "issue.md"
    issue.write_text(
        "Bug: parser issue\n"
        "Expected: parser should work\n"
        "Actual: fail with API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n"
    )
    out = tmp_path / ".runs" / "hardening_demo"

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
            "--no-network",
            "--llm-provider",
            "kimi-code",
        ],
    )

    assert result.exit_code == 0, result.output
    task = json.loads((out / "task.json").read_text())
    evidence = json.loads((out / "graph_evidence.json").read_text())
    final_report = (out / "final_report.md").read_text()

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in task["issue_text"]
    assert "[REDACTED" in task["issue_text"]
    assert any("no-network mode requested" in warning for warning in evidence["warnings"])
    assert any("LLM provider requested but disabled" in warning for warning in evidence["warnings"])
    assert "- Secret filter: redacted" in final_report
    assert "## Safety Checks" in final_report


def test_codegraph_sidecar_dockerfile_exists():
    dockerfile = Path("docker/codegraph/Dockerfile")

    assert dockerfile.exists()
    assert "@colbymchenry/codegraph@$CODEGRAPH_VERSION" in dockerfile.read_text()
