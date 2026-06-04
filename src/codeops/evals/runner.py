"""Evaluation runner."""

from datetime import datetime, timezone
from pathlib import Path
import shutil

from codeops.core.models import TaskRequest, TaskState
from codeops.evals.metrics import EvalTaskResult, summarize
from codeops.evals.tasks import EvalTask, load_eval_tasks
from codeops.workflow.orchestrator import WorkflowOrchestrator


SUPPORTED_MODES = {
    "no_graph",
    "codegraph_only",
    "repo_sketch_codegraph",
    "repo_sketch_codegraph_affected_tests",
}


class EvalRunner:
    """Run fixture tasks and write an eval report."""

    def run(
        self,
        *,
        tasks_path: Path,
        mode: str,
        out_root: Path = Path(".runs"),
    ) -> Path:
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"unsupported eval mode: {mode}")

        tasks = load_eval_tasks(tasks_path)
        eval_dir = out_root / f"eval_{_timestamp()}"
        repos_dir = eval_dir / "repos"
        task_runs_dir = eval_dir / "tasks"
        repos_dir.mkdir(parents=True, exist_ok=True)
        task_runs_dir.mkdir(parents=True, exist_ok=True)

        results = []
        for task in tasks:
            copied_repo = _copy_repo(task, repos_dir)
            run_dir = task_runs_dir / task.task_id
            state = WorkflowOrchestrator().run(
                TaskRequest(
                    repo_path=copied_repo,
                    issue_path=task.issue_path,
                    out_path=run_dir,
                )
            )
            results.append(_task_result(task, state))

        report_path = eval_dir / "eval_report.md"
        report_path.write_text(_render_report(mode, results))
        return report_path


def _copy_repo(task: EvalTask, repos_dir: Path) -> Path:
    destination = repos_dir / task.task_id
    shutil.copytree(
        task.repo_path,
        destination,
        ignore=shutil.ignore_patterns(".git", ".venv", ".codeops", "__pycache__"),
    )
    return destination


def _task_result(task: EvalTask, state: TaskState) -> EvalTaskResult:
    tests_passed = bool(state.test_results) and all(
        result.passed for result in state.test_results
    )
    expected_tests_ran = all(
        expected in {result.command for result in state.test_results}
        for expected in task.expected_tests
    )
    cost = state.cost_trace
    return EvalTaskResult(
        task_id=task.task_id,
        solved=state.status != "failed" and tests_passed and expected_tests_ran,
        graph_confidence=(
            state.graph_evidence.graph_confidence if state.graph_evidence else None
        ),
        raw_file_reads=cost.raw_file_reads,
        tool_calls=cost.tool_calls,
        codegraph_calls=cost.codegraph_calls,
        test_runs=cost.test_runs,
        estimated_cost_usd=cost.estimated_cost_usd,
        test_runtime_seconds=sum(
            result.duration_seconds for result in state.test_results
        ),
    )


def _render_report(mode: str, results: list[EvalTaskResult]) -> str:
    summary = summarize(mode, results)
    lines = [
        "# Eval Report",
        "",
        f"- Mode: `{mode}`",
        f"- Tasks: `{summary.task_count}`",
        f"- Solve rate: `{summary.solve_rate}`",
        "",
        "| Mode | Solve Rate | Avg Graph Confidence | Tool Calls | Raw Reads | Test Runs | Cost / Solved |",
        "|---|---:|---:|---:|---:|---:|---:|",
        (
            f"| {mode} | {summary.solve_rate:.4f} | "
            f"{_fmt(summary.avg_graph_confidence)} | "
            f"{summary.avg_tool_calls:.2f} | "
            f"{summary.avg_raw_file_reads:.2f} | "
            f"{summary.avg_test_runs:.2f} | "
            f"{_cost_per_solved(summary.estimated_cost_usd, results)} |"
        ),
        "",
        "## Tasks",
        "",
        "| Task | Solved | Graph Confidence | Test Runs |",
        "|---|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.task_id} | {result.solved} | "
            f"{_fmt(result.graph_confidence)} | {result.test_runs} |"
        )
    lines.append("")
    return "\n".join(lines)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _cost_per_solved(
    estimated_cost_usd: float | None, results: list[EvalTaskResult]
) -> str:
    solved = sum(1 for result in results if result.solved)
    if estimated_cost_usd is None or solved == 0:
        return "n/a"
    return f"{estimated_cost_usd / solved:.4f}"
