from pathlib import Path

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

    result = runner.run("pytest --version")

    assert result.passed
    assert "pytest" in result.stdout.lower()

    try:
        runner.run("python -c 'print(1)'")
    except CommandRejected:
        pass
    else:
        raise AssertionError("arbitrary python command should be rejected")
