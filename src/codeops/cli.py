"""Command-line interface for CodeOps Agent."""

from pathlib import Path
from typing import Annotated

import typer

from codeops.core.artifacts import ArtifactWriter
from codeops.core.models import TaskRequest
from codeops.core.paths import RunPaths
from codeops.core.state import create_task_state

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
    """Create the initial run artifact for a task."""
    request = TaskRequest(repo_path=repo, issue_path=issue, out_path=out)
    paths = RunPaths.from_run_dir(request.out_path)
    state = create_task_state(
        task_id=paths.task_id,
        repo_path=request.repo_path,
        issue_path=request.issue_path,
        issue_text=request.issue_path.read_text(),
        run_dir=paths.run_dir,
    )
    task_path = ArtifactWriter(paths).write_json("task", state)
    typer.echo(f"wrote {task_path}")


def main() -> None:
    """Run the CodeOps CLI."""
    app()


if __name__ == "__main__":
    main()
