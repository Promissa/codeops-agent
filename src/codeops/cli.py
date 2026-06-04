"""Command-line interface for CodeOps Agent."""

from pathlib import Path
from typing import Annotated

import typer

from codeops.core.models import TaskRequest

app = typer.Typer(
    name="codeops",
    help="Evidence-aware CodeOps Agent.",
    no_args_is_help=True,
)


@app.callback()
def root() -> None:
    """Evidence-aware CodeOps Agent."""


@app.command()
def run(
    repo: Annotated[
        Path,
        typer.Option(
            "--repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Path to the target repository.",
        ),
    ],
    issue: Annotated[
        Path,
        typer.Option(
            "--issue",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Path to the issue or failing test log.",
        ),
    ],
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="Path where a future workflow run will write artifacts.",
        ),
    ],
) -> None:
    """Validate Phase 0 inputs and echo the task request."""
    request = TaskRequest(repo_path=repo, issue_path=issue, out_path=out)
    typer.echo(request.model_dump_json(indent=2))


def main() -> None:
    """Run the CodeOps CLI."""
    app()


if __name__ == "__main__":
    main()
