from typer.testing import CliRunner

from codeops.cli import app


def test_cli_imports_and_shows_help():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "run" in result.output
