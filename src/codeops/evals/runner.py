"""Evaluation runner."""

from datetime import datetime, timezone
from pathlib import Path
import shutil

from pydantic import BaseModel, Field

from codeops.core.models import ImpactEnvelope, TaskRequest, TaskState
from codeops.evals.metrics import EvalTaskResult, summarize
from codeops.evals.tasks import EvalTask, load_eval_tasks
from codeops.languages.detector import ProjectDetector
from codeops.languages.registry import LanguageProfileRegistry
from codeops.safety.patch_policy import PatchPolicy
from codeops.workflow.orchestrator import WorkflowOrchestrator


SUPPORTED_MODES = {
    "no_graph",
    "codegraph_only",
    "repo_sketch_codegraph",
    "repo_sketch_codegraph_affected_tests",
    "multilang_detection",
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
        if mode == "multilang_detection":
            return _run_multilang_detection(tasks_path, out_root)

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


class MultiLangEvalTaskResult(BaseModel):
    task_id: str
    language: str
    expected_language: str | None = None
    detected: bool
    test_commands_found: bool
    affected_expected: list[list[str]] = Field(default_factory=list)
    affected_selected: list[list[str]] = Field(default_factory=list)
    unsafe_rejections: int = 0
    unsafe_checks: int = 0


def _run_multilang_detection(tasks_path: Path, out_root: Path) -> Path:
    tasks = load_eval_tasks(tasks_path)
    eval_dir = out_root / f"eval_{_timestamp()}"
    eval_dir.mkdir(parents=True, exist_ok=True)
    results = [_multilang_task_result(task) for task in tasks]
    report_path = eval_dir / "eval_report.md"
    report_path.write_text(_render_multilang_report(results))
    return report_path


def _multilang_task_result(task: EvalTask) -> MultiLangEvalTaskResult:
    profile = ProjectDetector().detect(task.repo_path)
    adapter = _profile_adapter(profile.language_profiles[0] if profile.language_profiles else "")
    test_commands = adapter.discover_test_commands(task.repo_path) if adapter else []
    affected_commands = (
        adapter.select_tests(task.repo_path, task.changed_files, task.graph_tests)
        if adapter
        else []
    )
    unsafe_rejections, unsafe_checks = _unsafe_rejection_counts(task)
    expected_commands = {tuple(command) for command in task.expected_test_commands}
    discovered_commands = {tuple(command.command) for command in test_commands}

    return MultiLangEvalTaskResult(
        task_id=task.task_id,
        language=profile.primary_language,
        expected_language=task.expected_language,
        detected=(
            task.expected_language is None
            or profile.primary_language == task.expected_language
        ),
        test_commands_found=expected_commands.issubset(discovered_commands),
        affected_expected=task.expected_affected_test_commands,
        affected_selected=[command.command for command in affected_commands],
        unsafe_rejections=unsafe_rejections,
        unsafe_checks=unsafe_checks,
    )


def _profile_adapter(profile_name: str):
    for profile in LanguageProfileRegistry().profiles():
        if profile.name == profile_name:
            return profile
    return None


def _unsafe_rejection_counts(task: EvalTask) -> tuple[int, int]:
    rejections = 0
    for path in task.expected_rejected_files:
        patch = _synthetic_patch(path)
        result = PatchPolicy().validate(
            patch,
            ImpactEnvelope(
                target_symbols=[],
                allowed_files=[path],
                affected_files=[path],
                affected_tests=[],
                forbidden_changes=["dependency change"],
                risk_level="low",
                requires_human_approval=False,
            ),
        )
        if not result.allowed:
            rejections += 1
    return rejections, len(task.expected_rejected_files)


def _synthetic_patch(path: str) -> str:
    return f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 original
+changed
"""


def _render_multilang_report(results: list[MultiLangEvalTaskResult]) -> str:
    detection_accuracy = _rate(result.detected for result in results)
    command_accuracy = _rate(result.test_commands_found for result in results)
    unsafe_rejection_rate = _unsafe_rejection_rate(results)
    affected_precision, affected_recall = _affected_precision_recall(results)
    languages = sorted({result.expected_language or result.language for result in results})
    lines = [
        "# Eval Report",
        "",
        "- Mode: `multilang_detection`",
        f"- Project detection accuracy: `{_fmt_rate(detection_accuracy)}`",
        f"- Test-command discovery accuracy: `{_fmt_rate(command_accuracy)}`",
        f"- Affected-test precision: `{_fmt_rate(affected_precision)}`",
        f"- Affected-test recall: `{_fmt_rate(affected_recall)}`",
        f"- Unsafe edit rejection rate: `{_fmt_rate(unsafe_rejection_rate)}`",
        "",
        "## Solve Rate By Language",
        "",
        "| Language | Solve Rate | Notes |",
        "|---|---:|---|",
    ]
    for language in languages:
        lines.append(
            f"| {language} | n/a | Not measured by detection-only multilang eval. |"
        )
    lines.extend(
        [
            "",
            "## Tasks",
            "",
            "| Task | Expected Language | Detected Language | Detection | Commands | Unsafe Rejections |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for result in results:
        lines.append(
            f"| {result.task_id} | {result.expected_language or 'n/a'} | "
            f"{result.language} | {result.detected} | "
            f"{result.test_commands_found} | "
            f"{result.unsafe_rejections}/{result.unsafe_checks} |"
        )
    lines.append("")
    return "\n".join(lines)


def _rate(values) -> float | None:
    values = list(values)
    if not values:
        return None
    return round(sum(1 for value in values if value) / len(values), 4)


def _unsafe_rejection_rate(results: list[MultiLangEvalTaskResult]) -> float | None:
    checks = sum(result.unsafe_checks for result in results)
    if checks == 0:
        return None
    rejections = sum(result.unsafe_rejections for result in results)
    return round(rejections / checks, 4)


def _affected_precision_recall(
    results: list[MultiLangEvalTaskResult],
) -> tuple[float | None, float | None]:
    true_positive = 0
    predicted = 0
    expected = 0
    for result in results:
        selected = {tuple(command) for command in result.affected_selected}
        wanted = {tuple(command) for command in result.affected_expected}
        true_positive += len(selected.intersection(wanted))
        predicted += len(selected)
        expected += len(wanted)
    precision = round(true_positive / predicted, 4) if predicted else None
    recall = round(true_positive / expected, 4) if expected else None
    return precision, recall


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"
