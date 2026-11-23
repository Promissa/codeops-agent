"""Workflow orchestrator."""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from glob import escape
import os
from pathlib import Path
import time
from tempfile import mkdtemp

from codeops.agents.llm_provider import (
    OpenAICompatibleRoutingProvider,
    OpenAICompatiblePatchProvider,
    llm_config_from_request,
)
from codeops.agents.issue_router import IssueRouter, routing_symbols
from codeops.agents.patch_generator import PatchGenerator
from codeops.agents.patch_planner import PatchPlanner
from codeops.agents.requirement_parser import RequirementParser
from codeops.core.artifacts import ArtifactWriter
from codeops.core.errors import ToolError
from codeops.core.models import (
    CrossCheckResult,
    ModuleCapsule,
    SymbolRef,
    TaskRequest,
    TaskState,
    TestCommand,
    VerificationPlan,
)
from codeops.core.paths import ARTIFACT_FILENAMES, RunPaths
from codeops.evolution.run import RunEvolution
from codeops.gate.effect_map import EffectMap
from codeops.gate.evidence_gate import GateEvidence
from codeops.gate.verification import (
    VerificationWorkspace, bound_envelope, open_workspace, record_patch_policy,
    validate_submitted_patch, verify_applied_patch,
)
from codeops.languages.detector import ProjectDetector
from codeops.reporting.run_artifacts import checkpoint, finalize_run, record_issue, record_verification
from codeops.reporting.summary import BLOCKING_TERMINATIONS
from codeops.reporting.trace_events import TraceRecorder, latest_event_detail
from codeops.retrieval.graph_reliability import GraphReliabilityLayer
from codeops.retrieval.impact_envelope import ImpactEnvelopeBuilder
from codeops.retrieval.local_embeddings import LocalEmbeddingRetriever
from codeops.retrieval.repo_sketch import RepoSketchBuilder
from codeops.runtime.lifecycle import RunLifecycleState, RunTerminated, TerminationReason, termination_reason
from codeops.runtime.registry import DEFAULT_RUN_STATE_DIR, RunRegistry
from codeops.runtime.sandbox import BwrapSandboxBackend
from codeops.runtime.workspace import SandboxWorkspace
from codeops.tools.codegraph_gateway import CodeGraphCommandResult, CodeGraphGateway
from codeops.tools.codegraph_version import CODEGRAPH_VERSION
from codeops.tools.git_tool import GitTool
from codeops.tools.patch_tool import PatchTool
from codeops.trust.escalation import EscalationDecision, EscalationGate, EscalationRequest
from codeops.trust.policy import POLICY_PATH, SandboxConfig, TrustPolicy
from codeops.trust.preflight import (
    PreFlightPlanner, TaskTrustContract, TrustPolicyBuilder, approved_commands,
)
from codeops.workflow.nodes import VerificationPlanBuilder


@dataclass
class _ManagedRun:
    policy: TrustPolicy
    contract: TaskTrustContract
    workspace: SandboxWorkspace
    backend: BwrapSandboxBackend | None
    commands: list[TestCommand]
    escalations: EscalationGate
    decisions: list[EscalationDecision]


class WorkflowOrchestrator:
    """Run the MVP workflow state machine."""

    def run(
        self, request: TaskRequest, *,
        approve: Callable[[TaskTrustContract], bool] | None = None,
        approve_escalation: Callable[[EscalationRequest], bool] | None = None,
    ) -> TaskState:
        default_policy = request.repo_path / POLICY_PATH
        managed = (
            request.sandbox_mode == "bwrap" or request.trust_policy_path is not None
            or default_policy.exists() or default_policy.is_symlink()
        )
        started = time.monotonic()
        repo_path = request.repo_path.resolve()
        out_path = request.out_path.resolve()
        forbidden_output = managed and out_path.is_relative_to(repo_path)
        if forbidden_output:
            # A denial report must also honor the managed repository's write boundary.
            report_root = DEFAULT_RUN_STATE_DIR.parent.resolve()
            if report_root.is_relative_to(repo_path):
                raise RunTerminated(TerminationReason.POLICY_DENIED, "No safe location for managed run output.")
            out_path = Path(mkdtemp(prefix="codeops-blocked-", dir=report_root))
        paths = RunPaths.from_run_dir(out_path)
        writer = ArtifactWriter(paths, atomic=True)
        trace = TraceRecorder(out_path, fresh=True)
        state = TaskState(
            task_id=paths.task_id, repo_path=repo_path, issue_path=request.issue_path,
            issue_text="", run_dir=out_path, sandbox_mode=request.sandbox_mode,
            trust_policy_path=request.trust_policy_path, lifecycle_state=RunLifecycleState.PREPARING,
            trace_events_path=paths.trace_events_path, trace_report_path=paths.trace_report_path,
        )
        reason = TerminationReason.RUNNER_ERROR
        gate = GateEvidence()
        evolution = None
        writer.write_json("task", state)
        try:
            with trace.stage("run", "Execute CodeOps workflow"):
                trace.record("preflight", "sandbox_lifecycle", "ok", "Preparing run")
                if forbidden_output:
                    raise RunTerminated(
                        TerminationReason.POLICY_DENIED, "Managed run output must be outside the repository.",
                    )
                evolution = RunEvolution(repo_path, trace)
                if managed:
                    self._run_managed(request, writer, state, trace, approve, approve_escalation, gate, evolution)
                else:
                    record_issue(state, trace, request.issue_path.read_text())
                    checkpoint(writer, state, trace, RunLifecycleState.EXECUTING)
                    policy = TrustPolicy(sandbox=SandboxConfig(backend="none"))
                    if out_path == repo_path:
                        policy.read_deny_globs.extend(ARTIFACT_FILENAMES.values())
                    elif out_path.is_relative_to(repo_path):
                        policy.read_deny_globs.append(escape(out_path.relative_to(repo_path).as_posix()))
                    with open_workspace(repo_path, policy) as frozen:
                        self._execute(request, writer, state=state, trace=trace, frozen=frozen.path, policy=policy, gate=gate, evolution=evolution)
            reason = TerminationReason.COMPLETED
        except (KeyboardInterrupt, InterruptedError):
            reason = TerminationReason.CANCELLED
            trace.record("run", "error", "blocked", "Task cancelled")
            if not managed:
                raise
        except Exception as exc:
            reason = termination_reason(exc)
            if reason == TerminationReason.POLICY_DENIED and not any(
                event.status == "denied" for event in trace.events
            ):
                trace.record("run", "policy_check", "denied", "Run stopped by policy", details={"reason": str(exc)})
            else:
                trace.record(
                    "run", "error", "blocked" if reason in BLOCKING_TERMINATIONS else "failed",
                    "Execution stopped",
                    details={"type": type(exc).__name__, "message": str(exc)},
                )
            if forbidden_output:
                raise RunTerminated(reason, f"{exc} Report written to {paths.trace_report_path}") from exc
            if not managed:
                raise
        finally:
            recorded_contract = latest_event_detail(trace.events, "trust_decision", "contract")
            contract = TaskTrustContract.model_validate(recorded_contract) if recorded_contract is not None else None
            if reason != TerminationReason.COMPLETED:
                state.status = "failed"
            state.termination_reason = reason
            state.cost_trace.wall_time_seconds = round(time.monotonic() - started, 4)
            checkpoint(writer, state, trace, RunLifecycleState.FINISHED)
            allow_graph_fallback = (
                "ast" in gate.effects.sources and state.graph_evidence is not None
                and "degraded" not in state.graph_evidence.warnings
            )
            finalize_run(
                writer, state, trace, contract, gate if gate.actual_policy else None,
                allow_graph_fallback=allow_graph_fallback, evolution=evolution,
            )
        return state

    def _run_managed(
        self, request: TaskRequest, writer: ArtifactWriter, state: TaskState, trace: TraceRecorder,
        approve: Callable[[TaskTrustContract], bool] | None,
        approve_escalation: Callable[[EscalationRequest], bool] | None, gate: GateEvidence,
        evolution: RunEvolution,
    ) -> None:
        repo_path = state.repo_path
        with trace.stage("preflight", "Resolve trust and prepare sandbox"):
            planner = PreFlightPlanner()
            policy, policy_path = planner.resolve_policy(repo_path, request.trust_policy_path)
            sandbox = policy.sandbox.model_copy(update={
                "backend": "bwrap" if request.sandbox_mode == "bwrap" else policy.sandbox.backend,
                "network_enabled": policy.sandbox.network_enabled and not request.no_network,
            })
            policy = policy.model_copy(update={"sandbox": sandbox})
            state.sandbox_mode = sandbox.backend
            state.trust_policy_path = policy_path
            contract = planner.plan(repo_path, request.issue_path, policy, policy_path=policy_path)
            writer.write_yaml("trust_contract", contract)
            trace.record("preflight", "trust_decision", "ok", "Trust contract prepared", details={"contract": contract})
            # Explicit --sandbox bwrap approves the built-in policy for this run.
            try:
                contract = planner.approve(
                    contract, approve=approve,
                    preapproved=request.trust_approved or policy_path is None,
                )
            except RunTerminated as exc:
                trace.record(
                    "preflight", "trust_decision", "denied", "Trust contract not approved",
                    details={"reason": str(exc)},
                )
                raise
            writer.write_yaml("trust_contract", contract)
            trace.record("preflight", "trust_decision", "allowed", "Trust contract approved", details={"contract": contract})

            escalations = EscalationGate(policy.escalation_budget, approve_escalation)
            decisions: list[EscalationDecision] = []
            issue = Path(os.path.abspath(request.issue_path))
            if issue.is_relative_to(repo_path):
                relative = issue.relative_to(repo_path).as_posix()
                if not policy.denies_read(relative) and not policy.permits_read(relative):
                    decision = escalations.decide(
                        EscalationRequest(boundary="local-read", path=relative, reason="Read the task's issue input.")
                    )
                    decisions.append(decision)
                    writer.write_json("trust_escalations.json", decisions)
                    _record_escalation(trace, "preflight", decision)
                    if not decision.approved:
                        raise RunTerminated(TerminationReason.POLICY_DENIED, decision.reason)
                    policy = policy.model_copy(update={"read_allow_globs": [*policy.read_allow_globs, escape(relative)]})
                    contract = contract.model_copy(update={"read_allow_globs": policy.read_allow_globs})
                    writer.write_yaml("trust_contract", contract)

            record_issue(state, trace, planner.read_issue(contract, policy))
        if DEFAULT_RUN_STATE_DIR.resolve().is_relative_to(repo_path):
            raise RunTerminated(TerminationReason.POLICY_DENIED, "Run registry overlaps the repository.")
        registry = RunRegistry()
        registry.cleanup_stale_runs()
        with registry.register() as run:
            with trace.stage("sandbox", "Prepare filtered workspace"):
                workspace = SandboxWorkspace(repo_path, run.path, sandbox.workspace_quota_mb)
                snapshot = workspace.prepare(policy)
                discovered = TrustPolicyBuilder().discover_commands(snapshot)
                if policy_path is None:
                    contract = contract.model_copy(update={
                        "allowed_test_commands": [command.command for command in discovered],
                    })
                    writer.write_yaml("trust_contract", contract)
                trace.record("sandbox", "trust_decision", "ok", "Effective workspace and command scope",
                             details={"contract": contract})
                commands = approved_commands(contract, discovered, snapshot)
                policy = policy.model_copy(update={"allowed_test_commands": contract.allowed_test_commands})
                backend = None
                if sandbox.backend == "bwrap":
                    backend = BwrapSandboxBackend(workspace, run, sandbox)
                    backend.probe()
                trace.record(
                    "sandbox", "sandbox_lifecycle", "ok", "Filtered workspace ready",
                    details={"backend": sandbox.backend, "network_enabled": sandbox.network_enabled},
                )
                managed = _ManagedRun(
                    policy, contract, workspace, backend, commands, escalations, decisions,
                )
            checkpoint(writer, state, trace, RunLifecycleState.EXECUTING)
            with open_workspace(snapshot, policy) as frozen:
                self._execute(
                    request.model_copy(update={"repo_path": snapshot, "no_network": not sandbox.network_enabled}),
                    writer, state=state, trace=trace, managed=managed,
                    frozen=frozen.path, policy=policy, gate=gate, evolution=evolution,
                )

    def _execute(
        self, request: TaskRequest, writer: ArtifactWriter, *,
        state: TaskState, trace: TraceRecorder, frozen: Path, policy: TrustPolicy,
        gate: GateEvidence, evolution: RunEvolution, managed: _ManagedRun | None = None,
    ) -> None:
        issue_text = state.issue_text
        cost_trace = state.cost_trace
        if request.no_network:
            trace.record("outbound", "outbound_filter", "skipped", "Network-backed workflow operations disabled")
        elif request.llm_provider == "none":
            trace.record("outbound", "outbound_filter", "skipped", "No remote model configured")
        with trace.stage("requirements", "Interpret task intent"):
            project_profile = ProjectDetector().detect(request.repo_path)

            acceptance_contract = RequirementParser().parse(issue_text)
            state.project_profile = project_profile
            state.acceptance_contract = acceptance_contract
            state.status = "contracted" if acceptance_contract.status == "ready" else "needs_clarification"
            writer.write_yaml("acceptance_contract", acceptance_contract)
            trace.record(
                "requirements", "retrieval", "ok" if state.status == "contracted" else "inconclusive",
                "Acceptance contract created", details={"acceptance_contract": acceptance_contract},
                refs=["acceptance_contract.yaml"],
            )

        backend = managed.backend if managed else None
        with trace.stage("retrieval", "Collect context and plan verification"):
            git = GitTool(request.repo_path, sandbox_backend=backend, restricted=managed is not None)
            commit = git.current_commit()
            cost_trace.tool_calls += 1

            sketch_builder = RepoSketchBuilder()
            sketch = sketch_builder.build(request.repo_path, commit=commit)
            sketch_markdown = sketch_builder.render_markdown(sketch)
            _write_repo_sketch_cache(request.repo_path, sketch, sketch_markdown)
            writer.write_markdown("repo_sketch", sketch_markdown)

            gateway = _codegraph_for_run(request.repo_path, backend)
            graph_started = time.monotonic()
            status = gateway.status(request.repo_path)
            graph_files = gateway.files(request.repo_path)
            if managed:
                graph_files = [
                    item for item in graph_files
                    if managed.policy.permits_read(item.path) and (request.repo_path / item.path).is_file()
                ]
            codegraph_version = gateway.version() or CODEGRAPH_VERSION
            cost_trace.codegraph_calls += 3
            trace.record(
                "codegraph", "codegraph", "ok" if status.available else "warning", "Inspect CodeGraph index",
                details={
                    "available": status.available, "index_fresh": status.index_fresh,
                    "version": codegraph_version, "indexed_files_seen": sum(item.indexed for item in graph_files),
                    "message": status.message, "operations": ["status", "files", "version"],
                },
                duration_ms=_elapsed_ms(graph_started),
            )
            reliability = GraphReliabilityLayer()
            issue_router = IssueRouter()
            routing_started = time.monotonic()
            routing_provider = None if managed else _llm_routing_provider(request)
            routing = issue_router.route(
                repo_path=request.repo_path,
                issue_text=issue_text,
                project_profile=project_profile,
                repo_sketch=sketch,
                graph_files=graph_files,
                provider=routing_provider,
            )
            cost_trace.llm_calls += routing.llm_calls
            cost_trace.llm_input_tokens += routing.llm_input_tokens
            cost_trace.llm_output_tokens += routing.llm_output_tokens
            writer.write_json("issue_routing", routing)
            trace.record(
                "routing", "routing", "ok" if routing.files else "inconclusive", "Select candidate files and tests",
                details={"source": routing.source, "confidence": routing.confidence, "files": routing.files,
                         "tests": routing.tests, "rationale": routing.rationale, "warnings": routing.warnings},
                refs=["issue_routing.json"], duration_ms=_elapsed_ms(routing_started),
            )
            if routing_provider is not None:
                _record_outbound(trace, "routing", routing.source == "llm", "; ".join(routing.warnings))

            acceptance_contract = evolution.disclose(state, acceptance_contract, routing.files)
            if acceptance_contract.evolution_guidance:
                state.acceptance_contract = acceptance_contract
                writer.write_yaml("acceptance_contract", acceptance_contract)

            cross_checks = _routing_cross_checks(routing)
            report = reliability.evaluate(
                repo_path=request.repo_path,
                status=status,
                graph_files=graph_files,
                cross_checks=cross_checks,
            )
            target_symbols = routing_symbols(routing) or _target_symbols(
                issue_text,
                sketch.core_modules,
            )
            fallback_tests = _select_tests(issue_text, sketch.test_map)
            affected_tests = (
                routing.tests
                or _tests_for_routed_files(routing.files, sketch.test_map)
                or ([] if routing.files else fallback_tests)
            )
            evidence = reliability.graph_evidence(
                status=status,
                report=report,
                repo_commit=commit,
                codegraph_version=codegraph_version,
                cross_checks=cross_checks,
            ).model_copy(
                update={
                    "target_symbols": target_symbols,
                    "affected_tests": affected_tests,
                }
            )
            if request.retrieval_mode == "hybrid_local":
                capsules, embedded = LocalEmbeddingRetriever().index(
                    request.repo_path,
                    request.repo_path / ".codeops" / "local_embeddings.sqlite",
                )
                warning = (
                    f"hybrid_local indexed {len(capsules)} symbol capsules"
                    if embedded
                    else "hybrid_local embedding provider unavailable; "
                    "fell back to CodeGraph and deterministic retrieval"
                )
                evidence = evidence.model_copy(
                    update={"warnings": [*evidence.warnings, warning]}
                )
            if request.no_network:
                evidence = evidence.model_copy(
                    update={
                        "warnings": [
                            *evidence.warnings,
                            "no-network mode requested; network-backed operations disabled",
                        ]
                    }
                )
            llm_warning = _llm_warning(request) if managed is None else None
            if llm_warning:
                evidence = evidence.model_copy(
                    update={"warnings": [*evidence.warnings, llm_warning]}
                )
            if routing.warnings:
                evidence = evidence.model_copy(
                    update={"warnings": [*evidence.warnings, *routing.warnings]}
                )
            writer.write_json("graph_evidence", evidence)
            state.graph_evidence = evidence
            trace.record(
                "retrieval", "retrieval", "ok" if evidence.graph_confidence >= 0.60 else "inconclusive",
                "Assess graph and fallback evidence",
                details={"mode": request.retrieval_mode, "evidence": evidence}, refs=["graph_evidence.json"],
            )

            impact_envelope = ImpactEnvelopeBuilder().build(
                target_symbols=target_symbols,
                graph_evidence=evidence,
                affected_tests=affected_tests,
            )
            impact_envelope = bound_envelope(impact_envelope, policy)
            writer.write_yaml("impact_envelope", impact_envelope)
            state.impact_envelope = impact_envelope

            verification_builder = VerificationPlanBuilder()
            verification_plan = verification_builder.build(
                impact_envelope,
                repo_path=request.repo_path,
                language=project_profile.primary_language,
            )
            if managed:
                verification_plan = VerificationPlan(
                    full_tests=managed.commands, behavior_diff_required=True, coverage_required=False,
                )
            writer.write_yaml("verification_plan", verification_plan)
            state.verification_plan = verification_plan

        with trace.stage("patch", "Generate and check patch"):
            patch_plan = None
            patch_diff = None
            if acceptance_contract.status == "ready":
                patch_plan = PatchPlanner().plan(acceptance_contract, impact_envelope)
                writer.write_yaml("patch_plan", patch_plan)
                state.patch_plan = patch_plan
                outbound_paths = [
                    *impact_envelope.allowed_files, *impact_envelope.affected_files,
                    *impact_envelope.affected_tests, *(symbol.path for symbol in impact_envelope.target_symbols),
                    *patch_plan.files_to_edit, *patch_plan.tests_to_add_or_update,
                ]
                provider = _llm_patch_provider(request)
                missing_outbound = []
                if managed and provider is not None:
                    issue_scope = PreFlightPlanner().outbound_issue_path(managed.contract)
                    missing_outbound = [
                        path for path in dict.fromkeys([issue_scope, *outbound_paths])
                        if not managed.policy.permits_outbound(path)
                    ]
                patch_generator = PatchGenerator(provider=provider if not missing_outbound else None)
                try:
                    try:
                        patch_diff = patch_generator.generate(
                            request.repo_path, patch_plan, acceptance_contract, impact_envelope,
                        )
                    except ToolError:
                        if not (managed and missing_outbound):
                            raise
                        _approve_outbound(managed, writer, trace, missing_outbound)
                        patch_generator.provider = provider
                        patch_diff = patch_generator.generate(
                            request.repo_path, patch_plan, acceptance_contract, impact_envelope,
                        )
                    if patch_generator.last_llm_result is not None:
                        _record_outbound(trace, "patch", True)
                        cost_trace.llm_calls += 1
                        cost_trace.llm_input_tokens += (
                            patch_generator.last_llm_result.input_tokens
                        )
                        cost_trace.llm_output_tokens += (
                            patch_generator.last_llm_result.output_tokens
                        )
                except RunTerminated:
                    raise
                except ToolError as exc:
                    if patch_generator.provider is not None:
                        _record_outbound(trace, "patch", False, str(exc))
                    trace.record("patch", "patch", "inconclusive", "No patch generated", details={"reason": str(exc)})
            else:
                writer.write_markdown("patch_plan", "")
                trace.record("patch", "patch", "skipped", "Patch generation requires clarification")

            if patch_diff:
                patch_tool = PatchTool(request.repo_path, git=git)
                summary = patch_tool.summarize_checked(patch_diff)
                policy_result = validate_submitted_patch(patch_diff, summary, impact_envelope, policy)
                record_patch_policy(trace, policy_result, "Before patch application")
                if managed and not policy_result.allowed:
                    raise RunTerminated(TerminationReason.POLICY_DENIED, "\n".join(policy_result.violations))
                writer.write_markdown("patch", patch_diff)
                state.patch_diff = patch_diff
                if policy_result.allowed:
                    apply_started = time.monotonic()
                    apply_result = patch_tool.apply(patch_diff)
                    cost_trace.tool_calls += 1
                    trace.record(
                        "patch", "patch", "ok" if apply_result.passed else "failed", "Apply patch",
                        details={"command": apply_result.args, "exit_code": apply_result.returncode,
                                 "stderr": apply_result.stderr, "diff": policy_result.summary},
                        refs=["patch.diff"], duration_ms=_elapsed_ms(apply_started),
                    )
                    if not apply_result.passed:
                        state.status = "failed"
                else:
                    state.status = "failed"
            else:
                writer.write_markdown("patch", "")

        checkpoint(writer, state, trace, RunLifecycleState.VERIFYING)
        if patch_diff and state.status != "failed":
            candidate_workspace = (
                nullcontext(VerificationWorkspace(request.repo_path, managed.workspace, backend))
                if managed else open_workspace(request.repo_path, policy, execute=True)
            )

            def select_commands(effects: EffectMap) -> list[TestCommand]:
                if managed:
                    return managed.commands
                affected = list(dict.fromkeys([*impact_envelope.affected_tests, *effects.affected_tests]))
                plan = verification_builder.build(
                    impact_envelope.model_copy(update={"affected_tests": affected}),
                    repo_path=request.repo_path, language=project_profile.primary_language,
                )
                state.verification_plan = plan
                writer.write_yaml("verification_plan", plan)
                selected = verification_builder.commands(plan, risk_level=impact_envelope.risk_level)
                # The generated command list is the exact trust scope in the legacy local workflow.
                policy.allowed_test_commands = [command.command for command in selected]
                return selected

            with candidate_workspace as candidate:
                verify_applied_patch(
                    frozen, candidate, summary, impact_envelope, policy, gate,
                    command_root=request.repo_path.resolve(), select_commands=select_commands,
                    on_result=lambda role, result: record_verification(state, trace, role, result),
                    writer=writer, trace=trace, graph=gateway if status.available and graph_files else None,
                    graph_root=request.repo_path,
                )
            if gate.actual_policy is not None and not gate.actual_policy.allowed:
                state.status = "failed"
        else:
            trace.record("verification", "test_run", "skipped", "No test commands run")

        if state.status != "failed":
            if any(not result.passed for result in state.test_results):
                state.status = "failed"
            elif acceptance_contract.status == "ready":
                state.status = "reviewed"
        checkpoint(writer, state, trace, RunLifecycleState.PENDING_EXPORT)

def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)


def _record_escalation(trace: TraceRecorder, stage: str, decision: EscalationDecision) -> None:
    trace.record(
        stage, "trust_decision", "allowed" if decision.approved else "denied",
        f"Escalation for {decision.request.boundary}", details={"decision": decision},
        refs=["trust_escalations.json"],
    )


def _record_outbound(trace: TraceRecorder, stage: str, completed: bool, reason: str = "") -> None:
    denied = "secret detected in LLM " in reason
    trace.record(
        stage, "outbound_filter", "denied" if denied else "allowed" if completed else "warning",
        "LLM request completed" if completed else "LLM request did not produce a result",
        details={"destination": "configured LLM", "reason": reason},
    )


def _approve_outbound(
    managed: _ManagedRun, writer: ArtifactWriter, trace: TraceRecorder, paths: list[str],
) -> None:
    if any(managed.policy.denies_outbound(path) for path in paths):
        trace.record("patch", "outbound_filter", "denied", "Patch context includes an outbound-denied path")
        raise RunTerminated(TerminationReason.POLICY_DENIED, "Patch context includes an outbound-denied path.")
    for path in paths:
        decision = managed.escalations.decide(
            EscalationRequest(boundary="outbound", path=path, reason="Send patch context to the configured LLM.")
        )
        managed.decisions.append(decision)
        writer.write_json("trust_escalations.json", managed.decisions)
        _record_escalation(trace, "patch", decision)
        if not decision.approved:
            raise RunTerminated(TerminationReason.POLICY_DENIED, decision.reason)
    managed.contract = managed.contract.model_copy(update={
        "outbound_allow_globs": [*managed.contract.outbound_allow_globs, *(escape(path) for path in paths)],
    })
    writer.write_yaml("trust_contract", managed.contract)
    trace.record(
        "patch", "trust_decision", "ok", "Effective outbound scope", details={"contract": managed.contract},
    )


def _codegraph_for_run(
    snapshot: Path, backend: BwrapSandboxBackend | None,
) -> CodeGraphGateway:
    if backend is None:
        return CodeGraphGateway()

    def run(_cwd: Path, args: list[str], input_text: str | None) -> CodeGraphCommandResult:
        command = ["codegraph", *args]
        result = backend.run(
            command, cwd=snapshot, input_text=input_text, timeout_seconds=15, readonly=True,
        )
        return CodeGraphCommandResult(
            args=command, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
        )

    return CodeGraphGateway(runner=run)


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


def _tests_for_routed_files(
    routed_files: list[str], test_map: dict[str, list[str]]
) -> list[str]:
    routed = set(routed_files)
    selected = []
    for source, tests in test_map.items():
        if source in routed:
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


def _routing_cross_checks(routing: object) -> list[CrossCheckResult]:
    if getattr(routing, "files", None):
        return [
            CrossCheckResult(
                source="issue_router",
                status="pass",
                detail=(
                    "IssueRouter selected candidate files: "
                    + ", ".join(getattr(routing, "files")[:3])
                ),
            )
        ]
    return [
        CrossCheckResult(
            source="issue_router",
            status="unknown",
            detail="IssueRouter found no candidate files.",
        )
    ]


def _llm_patch_provider(request: TaskRequest) -> OpenAICompatiblePatchProvider | None:
    if request.no_network:
        return None
    try:
        config = llm_config_from_request(request)
    except ToolError:
        return None
    if config is None or config.api_key is None:
        return None
    return OpenAICompatiblePatchProvider(config)


def _llm_routing_provider(request: TaskRequest) -> OpenAICompatibleRoutingProvider | None:
    if request.no_network:
        return None
    try:
        config = llm_config_from_request(request)
    except ToolError:
        return None
    if config is None or config.api_key is None:
        return None
    return OpenAICompatibleRoutingProvider(config)


def _llm_warning(request: TaskRequest) -> str | None:
    try:
        config = llm_config_from_request(request)
    except ToolError as exc:
        return f"LLM provider configuration invalid: {exc}"
    if config is None:
        return None
    if request.no_network:
        return "LLM provider requested but disabled by no-network mode."
    if config.api_key is None:
        return (
            "LLM provider requested but API key env var is not set: "
            f"{config.api_key_envs_display}; LLM fallback disabled."
        )
    return f"LLM fallback enabled for provider {config.provider}."
