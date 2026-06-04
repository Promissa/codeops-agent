"""Workflow orchestrator."""

from pathlib import Path
import time

from codeops.agents.manifest_writer import ManifestWriter
from codeops.agents.patch_generator import PatchGenerator
from codeops.agents.patch_planner import PatchPlanner
from codeops.agents.requirement_parser import RequirementParser
from codeops.agents.reviewer import Reviewer
from codeops.core.artifacts import ArtifactWriter
from codeops.core.errors import ToolError
from codeops.core.models import (
    CostTrace,
    CrossCheckResult,
    ModuleCapsule,
    SymbolRef,
    TaskRequest,
    TaskState,
)
from codeops.core.paths import RunPaths
from codeops.core.state import create_task_state
from codeops.retrieval.graph_reliability import GraphReliabilityLayer
from codeops.retrieval.impact_envelope import ImpactEnvelopeBuilder
from codeops.retrieval.repo_sketch import RepoSketchBuilder
from codeops.safety.patch_policy import PatchPolicy
from codeops.tools.codegraph_gateway import CodeGraphGateway
from codeops.tools.git_tool import GitTool
from codeops.tools.patch_tool import PatchTool
from codeops.tools.test_runner import TestRunner
from codeops.workflow.nodes import VerificationPlanBuilder


class WorkflowOrchestrator:
    """Run the MVP workflow state machine."""

    def run(self, request: TaskRequest) -> TaskState:
        started = time.monotonic()
        paths = RunPaths.from_run_dir(request.out_path)
        writer = ArtifactWriter(paths)
        issue_text = request.issue_path.read_text()
        cost_trace = CostTrace()

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
        writer.write_yaml("acceptance_contract", acceptance_contract)

        git = GitTool(request.repo_path)
        commit = git.current_commit()
        cost_trace.tool_calls += 1

        sketch_builder = RepoSketchBuilder()
        sketch = sketch_builder.build(request.repo_path, commit=commit)
        sketch_markdown = sketch_builder.render_markdown(sketch)
        _write_repo_sketch_cache(request.repo_path, sketch, sketch_markdown)
        writer.write_markdown("repo_sketch", sketch_markdown)

        gateway = CodeGraphGateway()
        status = gateway.status(request.repo_path)
        graph_files = gateway.files(request.repo_path)
        codegraph_version = gateway.version()
        cost_trace.codegraph_calls += 3
        reliability = GraphReliabilityLayer()
        report = reliability.evaluate(
            repo_path=request.repo_path,
            status=status,
            graph_files=graph_files,
            cross_checks=_fallback_cross_checks(issue_text, sketch.core_modules),
        )
        target_symbols = _target_symbols(issue_text, sketch.core_modules)
        affected_tests = _select_tests(issue_text, sketch.test_map)
        evidence = reliability.graph_evidence(
            status=status,
            report=report,
            repo_commit=commit,
            codegraph_version=codegraph_version,
            cross_checks=_fallback_cross_checks(issue_text, sketch.core_modules),
        ).model_copy(
            update={
                "target_symbols": target_symbols,
                "affected_tests": affected_tests,
            }
        )
        writer.write_json("graph_evidence", evidence)

        impact_envelope = ImpactEnvelopeBuilder().build(
            target_symbols=target_symbols,
            graph_evidence=evidence,
            affected_tests=affected_tests,
        )
        writer.write_yaml("impact_envelope", impact_envelope)

        verification_builder = VerificationPlanBuilder()
        verification_plan = verification_builder.build(impact_envelope)
        writer.write_yaml("verification_plan", verification_plan)

        patch_plan = None
        patch_diff = None
        if acceptance_contract.status == "ready":
            patch_plan = PatchPlanner().plan(acceptance_contract, impact_envelope)
            writer.write_yaml("patch_plan", patch_plan)
            try:
                patch_diff = PatchGenerator().generate(
                    request.repo_path,
                    patch_plan,
                    acceptance_contract,
                )
            except ToolError:
                patch_diff = None

        if patch_diff:
            writer.write_markdown("patch", patch_diff)
            policy_result = PatchPolicy().validate(patch_diff, impact_envelope)
            if policy_result.allowed:
                apply_result = PatchTool(request.repo_path).apply(patch_diff)
                cost_trace.tool_calls += 1
                policy_after_apply = PatchPolicy().validate(
                    patch_diff, impact_envelope
                )
                if not apply_result.passed or not policy_after_apply.allowed:
                    state = state.model_copy(update={"status": "failed"})
            else:
                state = state.model_copy(update={"status": "failed"})
        else:
            writer.write_markdown("patch", "")

        commands = verification_builder.commands(
            verification_plan,
            risk_level=impact_envelope.risk_level,
        )
        test_results = TestRunner(request.repo_path).run_many(commands)
        cost_trace.test_runs += len(test_results)
        cost_trace.tool_calls += len(test_results)
        writer.write_json("test_results", test_results)

        if state.status != "failed":
            if any(not result.passed for result in test_results):
                state = state.model_copy(update={"status": "failed"})
            elif acceptance_contract.status == "ready":
                state = state.model_copy(update={"status": "tested"})

        state = state.model_copy(
            update={
                "graph_evidence": evidence,
                "impact_envelope": impact_envelope,
                "patch_plan": patch_plan,
                "verification_plan": verification_plan,
                "patch_diff": patch_diff,
                "test_results": test_results,
            }
        )

        review = Reviewer().review(state)
        manifest_writer = ManifestWriter()
        evidence_matrix_path = writer.write_markdown(
            "evidence_matrix",
            manifest_writer.evidence_matrix(state),
        )
        intent_manifest_path = writer.write_markdown(
            "intent_manifest",
            manifest_writer.intent_manifest(state, review),
        )
        writer.write_markdown("final_report", manifest_writer.final_report(state, review))

        if state.status not in {"failed", "needs_clarification"}:
            state = state.model_copy(update={"status": "reviewed"})
        state = state.model_copy(
            update={
                "evidence_matrix_path": evidence_matrix_path,
                "intent_manifest_path": intent_manifest_path,
            }
        )
        cost_trace.wall_time_seconds = round(time.monotonic() - started, 4)
        state = state.model_copy(update={"cost_trace": cost_trace})
        writer.write_json("cost_trace", cost_trace)
        writer.write_json("task", state)
        return state


def _write_repo_sketch_cache(
    repo_path: Path, sketch: object, sketch_markdown: str
) -> None:
    cache_dir = repo_path / ".codeops"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "repo_sketch.json").write_text(
        sketch.model_dump_json(indent=2) + "\n"
    )
    (cache_dir / "repo_sketch.md").write_text(sketch_markdown)


def _target_symbols(issue_text: str, modules: list[ModuleCapsule]) -> list[SymbolRef]:
    issue_lower = issue_text.lower()
    symbols: list[SymbolRef] = []
    for module in modules:
        module_match = module.name.lower() in issue_lower
        csv_parser_match = "csv" in issue_lower and "parser" in module.path.lower()
        if not module_match and not csv_parser_match:
            continue
        for public_symbol in module.public_symbols:
            symbols.append(
                SymbolRef(
                    symbol=public_symbol,
                    path=module.path,
                    kind="function",
                )
            )
    return symbols


def _select_tests(issue_text: str, test_map: dict[str, list[str]]) -> list[str]:
    issue_lower = issue_text.lower()
    selected: list[str] = []
    for source, tests in test_map.items():
        source_lower = source.lower()
        source_name = Path(source).stem.lower()
        if source_name in issue_lower or ("csv" in issue_lower and "parser" in source_lower):
            selected.extend(tests)
    return list(dict.fromkeys(selected))


def _fallback_cross_checks(
    issue_text: str, modules: list[ModuleCapsule]
) -> list[CrossCheckResult]:
    issue_lower = issue_text.lower()
    if any(module.name.lower() in issue_lower for module in modules):
        return [
            CrossCheckResult(
                source="repo_sketch",
                status="pass",
                detail="Issue text matched RepoSketch module names.",
            )
        ]
    if "csv" in issue_lower and any("parser" in module.path for module in modules):
        return [
            CrossCheckResult(
                source="repo_sketch",
                status="pass",
                detail="CSV issue matched parser module by naming convention.",
            )
        ]
    return [
        CrossCheckResult(
            source="repo_sketch",
            status="unknown",
            detail="No deterministic module match found.",
        )
    ]
