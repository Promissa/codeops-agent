"""Run the evidence gate on a third-party patch without generating code."""

from collections.abc import Callable
from pathlib import Path
import time

from codeops.agents.requirement_parser import RequirementParser
from codeops.core.artifacts import ArtifactWriter
from codeops.core.models import AcceptanceContract, ImpactEnvelope, TaskState, VerificationPlan
from codeops.core.paths import RunPaths
from codeops.evolution.run import RunEvolution
from codeops.gate.evidence_gate import GateEvidence
from codeops.gate.verification import (
    open_workspace, record_patch_policy, validate_submitted_patch, verify_applied_patch,
)
from codeops.languages.detector import ProjectDetector
from codeops.reporting.run_artifacts import checkpoint, finalize_run, record_issue, record_verification
from codeops.reporting.summary import BLOCKING_TERMINATIONS
from codeops.reporting.trace_events import TraceRecorder
from codeops.retrieval.graph_reliability import GraphReliabilityLayer
from codeops.runtime.lifecycle import RunLifecycleState, RunTerminated, TerminationReason, termination_reason
from codeops.runtime.registry import DEFAULT_RUN_STATE_DIR
from codeops.runtime.workspace import open_regular
from codeops.tools.codegraph_gateway import CodeGraphGateway
from codeops.tools.patch_tool import PatchTool
from codeops.trust.policy import SandboxMode
from codeops.trust.preflight import PreFlightPlanner, TaskTrustContract, TrustPolicyBuilder, approved_commands


class EvidenceGateRunner:
    """Verify an external diff in disposable baseline and candidate workspaces."""

    def run(
        self, repo: Path, diff: Path, out: Path, *, issue: Path | None = None,
        sandbox_mode: SandboxMode = "bwrap", approve_trust: bool = False,
        approve: Callable[[TaskTrustContract], bool] | None = None,
    ) -> TaskState:
        repo, out = repo.resolve(), out.resolve()
        if out.is_relative_to(repo) or DEFAULT_RUN_STATE_DIR.resolve().is_relative_to(repo):
            raise RunTerminated(
                TerminationReason.POLICY_DENIED,
                "Verification output and workspace registry must be outside the source repository.",
            )
        paths = RunPaths.from_run_dir(out)
        writer = ArtifactWriter(paths, atomic=True)
        trace = TraceRecorder(out, fresh=True)
        state = TaskState(
            task_id=paths.task_id, repo_path=repo, issue_path=issue, issue_text="", run_dir=out,
            sandbox_mode=sandbox_mode, lifecycle_state=RunLifecycleState.PREPARING,
            trace_events_path=paths.trace_events_path, trace_report_path=paths.trace_report_path,
        )
        gate = GateEvidence()
        evolution = None
        contract = None
        started = time.monotonic()
        reason = TerminationReason.COMPLETED
        writer.write_json("task", state)
        try:
            with trace.stage("run", "Verify external patch"):
                evolution = RunEvolution(repo, trace)
                planner = PreFlightPlanner()
                policy, path = planner.resolve_policy(repo)
                if path is None or sandbox_mode == "bwrap":
                    policy.sandbox.backend = sandbox_mode
                state.sandbox_mode = policy.sandbox.backend
                state.trust_policy_path = path
                contract = planner.plan(repo, issue, policy, policy_path=path)
                writer.write_yaml("trust_contract", contract)
                trace.record("preflight", "trust_decision", "ok", "Trust contract prepared", details={"contract": contract})
                contract = planner.approve(contract, approve=approve, preapproved=approve_trust or path is None)
                record_issue(state, trace, planner.read_issue(contract, policy))
                state.acceptance_contract = (
                    RequirementParser().parse(state.issue_text) if issue else AcceptanceContract(
                        requirement_id="R1", summary="Verify the submitted patch using differential test evidence",
                        user_visible_before=None, user_visible_after=None, status="ready",
                    )
                )
                writer.write_yaml("acceptance_contract", state.acceptance_contract)
                with open_workspace(repo, policy) as frozen:
                    state.project_profile = ProjectDetector().detect(frozen.path)
                    discovered = TrustPolicyBuilder().discover_commands(frozen.path)
                    if path is None:
                        policy.allowed_test_commands = [command.command for command in discovered]
                        contract = contract.model_copy(update={"allowed_test_commands": policy.allowed_test_commands})
                    writer.write_yaml("trust_contract", contract)
                    trace.record(
                        "preflight", "trust_decision", "allowed", "Trust contract approved",
                        details={"contract": contract},
                    )
                    commands = approved_commands(contract, discovered, frozen.path)
                    state.verification_plan = VerificationPlan(
                        full_tests=commands, behavior_diff_required=True, coverage_required=False,
                    )
                    writer.write_yaml("verification_plan", state.verification_plan)
                    checkpoint(writer, state, trace, RunLifecycleState.EXECUTING)
                    patch_path = diff.absolute()
                    if patch_path.is_relative_to(repo):
                        permitted = policy.permits_read(patch_path.relative_to(repo).as_posix())
                    else:
                        permitted = not policy.denies_read(patch_path.relative_to(patch_path.anchor).as_posix())
                    if not permitted:
                        raise RunTerminated(TerminationReason.POLICY_DENIED, "Patch input is outside local read scope.")
                    with open_regular(Path(patch_path.anchor), patch_path.relative_to(patch_path.anchor)) as source:
                        patch = source.read().decode("utf-8")
                    summary = PatchTool(frozen.path).summarize_checked(patch)
                    state.acceptance_contract = evolution.disclose(
                        state, state.acceptance_contract, summary.changed_files,
                    )
                    if state.acceptance_contract.evolution_guidance:
                        writer.write_yaml("acceptance_contract", state.acceptance_contract)
                    with open_workspace(frozen.path, policy, execute=True) as candidate:
                        tool = PatchTool(candidate.path, git=candidate.git())
                        budget = contract.write_budget
                        envelope = ImpactEnvelope(
                            allowed_files=summary.changed_files, risk_level="low",
                            max_files_changed=budget.max_files_changed, max_loc_changed=budget.max_loc_changed,
                            allow_dependency_change=budget.allow_dependency_change,
                            allow_public_api_change=budget.allow_public_api_change,
                            requires_human_approval=False,
                        )
                        state.impact_envelope = envelope
                        writer.write_yaml("impact_envelope", envelope)
                        result = validate_submitted_patch(patch, summary, envelope, policy)
                        record_patch_policy(trace, result, "Before patch application")
                        if not result.allowed:
                            gate.rejected = True
                            gate.findings.extend(result.violations)
                        else:
                            state.patch_diff = patch
                            writer.write_markdown("patch", patch)
                            applied = tool.apply(patch)
                            trace.record(
                                "patch", "patch", "ok" if applied.passed else "failed", "Apply patch",
                                details={"command": applied.args, "exit_code": applied.returncode,
                                         "stderr": applied.stderr, "diff": summary},
                                refs=["patch.diff"],
                            )
                            gate.rejected = not applied.passed

                        if not gate.rejected:
                            gateway = CodeGraphGateway()
                            status = gateway.status(frozen.path)
                            graph_files = gateway.files(frozen.path) if status.available else []
                            reliability = GraphReliabilityLayer()
                            report = reliability.evaluate(repo_path=frozen.path, status=status, graph_files=graph_files)
                            state.graph_evidence = reliability.graph_evidence(status=status, report=report)
                            writer.write_json("graph_evidence", state.graph_evidence)
                            checkpoint(writer, state, trace, RunLifecycleState.VERIFYING)
                            verify_applied_patch(
                                frozen.path, candidate, summary, envelope, policy, gate,
                                command_root=frozen.path, select_commands=lambda _: commands,
                                on_result=lambda role, result: record_verification(state, trace, role, result),
                                writer=writer, trace=trace, graph=gateway if graph_files else None,
                                graph_root=frozen.path,
                            )
                            checkpoint(writer, state, trace, RunLifecycleState.PENDING_EXPORT)
            state.status = (
                "failed" if gate.rejected or (gate.actual_policy is not None and not gate.actual_policy.allowed)
                or any(not result.passed for result in state.test_results) else "reviewed"
            )
        except (KeyboardInterrupt, InterruptedError):
            reason = TerminationReason.CANCELLED
            trace.record("run", "error", "blocked", "Verification cancelled")
        except Exception as exc:
            reason = termination_reason(exc)
            trace.record(
                "run", "error", "blocked" if reason in BLOCKING_TERMINATIONS else "failed",
                "Verification stopped", details={"reason": str(exc)},
            )
        finally:
            if reason != TerminationReason.COMPLETED:
                state.status = "failed"
            state.termination_reason = reason
            state.cost_trace.wall_time_seconds = round(time.monotonic() - started, 4)
            checkpoint(writer, state, trace, RunLifecycleState.FINISHED)
            finalize_run(writer, state, trace, contract, gate, allow_graph_fallback=True, evolution=evolution)
        return state
