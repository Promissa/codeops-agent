"""Command-line interface for CodeOps Agent."""

from pathlib import Path
from typing import Annotated

import typer

from codeops.core.models import TaskRequest
from codeops.evals.runner import EvalRunner
from codeops.workflow.orchestrator import WorkflowOrchestrator

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
    retrieval: Annotated[
        str,
        typer.Option(
            "--retrieval",
            help="Retrieval mode.",
        ),
    ] = "codegraph",
) -> None:
    """Create initial run artifacts for a task."""
    request = TaskRequest(
        repo_path=repo,
        issue_path=issue,
        out_path=out,
        retrieval_mode=retrieval,
    )
    state = WorkflowOrchestrator().run(request)
    typer.echo(f"completed {state.task_id} with status {state.status}")


@app.command("eval")
def eval_command(
    tasks: Annotated[
        Path,
        typer.Option(
            "--tasks",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Path to eval task JSONL.",
        ),
    ],
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="Eval mode.",
        ),
    ] = "repo_sketch_codegraph",
    out_root: Annotated[
        Path,
        typer.Option(
            "--out-root",
            help="Directory where eval run artifacts are written.",
        ),
    ] = Path(".runs"),
) -> None:
    """Run evaluation tasks and write an eval report."""
    report_path = EvalRunner().run(tasks_path=tasks, mode=mode, out_root=out_root)
    typer.echo(f"wrote {report_path}")


def main() -> None:
    """Run the CodeOps CLI."""
    app()


if __name__ == "__main__":
    main()
