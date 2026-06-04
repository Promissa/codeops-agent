"""Command-line interface for CodeOps Agent."""

from pathlib import Path
from typing import Annotated

import typer

from codeops.agents.requirement_parser import RequirementParser
from codeops.core.artifacts import ArtifactWriter
from codeops.core.models import TaskRequest
from codeops.core.paths import RunPaths
from codeops.core.state import create_task_state
from codeops.retrieval.graph_reliability import GraphReliabilityLayer
from codeops.retrieval.impact_envelope import ImpactEnvelopeBuilder
from codeops.retrieval.repo_sketch import RepoSketchBuilder
from codeops.tools.codegraph_gateway import CodeGraphGateway
from codeops.tools.git_tool import GitTool
from codeops.tools.test_runner import TestRunner
from codeops.workflow.nodes import VerificationPlanBuilder

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
    """Create initial run artifacts for a task."""
    request = TaskRequest(repo_path=repo, issue_path=issue, out_path=out)
    paths = RunPaths.from_run_dir(request.out_path)
    writer = ArtifactWriter(paths)
    git = GitTool(request.repo_path)
    commit = git.current_commit()
    issue_text = request.issue_path.read_text()
    acceptance_contract = RequirementParser().parse(issue_text)
    state = create_task_state(
        task_id=paths.task_id,
        repo_path=request.repo_path,
        issue_path=request.issue_path,
        issue_text=issue_text,
        run_dir=paths.run_dir,
    ).model_copy(
        update={
            "acceptance_contract": acceptance_contract,
            "status": (
                "contracted"
                if acceptance_contract.status == "ready"
                else "needs_clarification"
            ),
        }
    )
    contract_path = writer.write_yaml("acceptance_contract", acceptance_contract)

    sketch_builder = RepoSketchBuilder()
    sketch = sketch_builder.build(request.repo_path, commit=commit)
    sketch_markdown = sketch_builder.render_markdown(sketch)
    _write_repo_sketch_cache(request.repo_path, sketch, sketch_markdown)
    repo_sketch_path = writer.write_markdown("repo_sketch", sketch_markdown)

    gateway = CodeGraphGateway()
    status = gateway.status(request.repo_path)
    graph_files = gateway.files(request.repo_path)
    reliability = GraphReliabilityLayer()
    report = reliability.evaluate(
        repo_path=request.repo_path,
        status=status,
        graph_files=graph_files,
    )
    evidence = reliability.graph_evidence(
        status=status,
        report=report,
        repo_commit=commit,
        codegraph_version=gateway.version(),
    )
    graph_path = writer.write_json("graph_evidence", evidence)

    affected_tests = _select_tests(issue_text, sketch.test_map)
    impact_envelope = ImpactEnvelopeBuilder().build(
        graph_evidence=evidence,
        affected_tests=affected_tests,
    )
    impact_path = writer.write_yaml("impact_envelope", impact_envelope)

    verification_builder = VerificationPlanBuilder()
    verification_plan = verification_builder.build(impact_envelope)
    verification_path = writer.write_yaml("verification_plan", verification_plan)
    test_commands = verification_builder.commands(
        verification_plan,
        risk_level=impact_envelope.risk_level,
    )
    test_results = TestRunner(request.repo_path).run_many(test_commands)
    test_results_path = writer.write_json("test_results", test_results)

    if test_results:
        state = state.model_copy(
            update={
                "impact_envelope": impact_envelope,
                "verification_plan": verification_plan,
                "test_results": test_results,
                "status": (
                    "tested"
                    if acceptance_contract.status == "ready"
                    else "needs_clarification"
                ),
            }
        )
    task_path = writer.write_json("task", state)

    typer.echo(f"wrote {task_path}")
    typer.echo(f"wrote {contract_path}")
    typer.echo(f"wrote {repo_sketch_path}")
    typer.echo(f"wrote {graph_path}")
    typer.echo(f"wrote {impact_path}")
    typer.echo(f"wrote {verification_path}")
    typer.echo(f"wrote {test_results_path}")


def _write_repo_sketch_cache(
    repo_path: Path, sketch: object, sketch_markdown: str
) -> None:
    cache_dir = repo_path / ".codeops"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "repo_sketch.json").write_text(
        sketch.model_dump_json(indent=2) + "\n"
    )
    (cache_dir / "repo_sketch.md").write_text(sketch_markdown)


def _select_tests(issue_text: str, test_map: dict[str, list[str]]) -> list[str]:
    issue_lower = issue_text.lower()
    selected: list[str] = []
    for source, tests in test_map.items():
        source_lower = source.lower()
        source_name = Path(source).stem.lower()
        if source_name in issue_lower or ("csv" in issue_lower and "parser" in source_lower):
            selected.extend(tests)
    return list(dict.fromkeys(selected))


def main() -> None:
    """Run the CodeOps CLI."""
    app()


if __name__ == "__main__":
    main()
