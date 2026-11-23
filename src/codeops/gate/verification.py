"""Shared post-apply verification used by both workflow entry points."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from pathlib import Path

from codeops.core.artifacts import ArtifactWriter
from codeops.core.models import ImpactEnvelope, TestCommand, TestResult
from codeops.gate.differential import DifferentialVerifier, verifier_runner
from codeops.gate.effect_map import EffectMap, EffectMapBuilder, is_test_file
from codeops.gate.evidence_gate import GateEvidence
from codeops.gate.workspace_diff import TRANSIENT_PATHS, WorkspaceDiffSummary, workspace_diff
from codeops.reporting.trace_events import TraceRecorder, TraceRedactor
from codeops.runtime.lifecycle import RunTerminated, TerminationReason
from codeops.runtime.registry import RunRegistry
from codeops.runtime.sandbox import BwrapSandboxBackend
from codeops.runtime.workspace import SandboxWorkspace
from codeops.safety.patch_policy import PatchPolicy, PatchPolicyResult
from codeops.tools.codegraph_gateway import CodeGraphGateway
from codeops.tools.git_tool import GitTool
from codeops.tools.patch_tool import PatchSummary
from codeops.trust.policy import TrustPolicy


@dataclass
class VerificationWorkspace:
    path: Path
    workspace: SandboxWorkspace
    backend: BwrapSandboxBackend | None

    @property
    def view(self) -> Path:
        return self.backend.workspace_view if self.backend else self.path

    def git(self) -> GitTool:
        return GitTool(self.path, sandbox_backend=self.backend, restricted=True)


@contextmanager
def open_workspace(
    source: Path, policy: TrustPolicy, *, execute: bool = False,
) -> Iterator[VerificationWorkspace]:
    registry = RunRegistry()
    with registry.register() as run:
        workspace = SandboxWorkspace(source, run.path, policy.sandbox.workspace_quota_mb)
        path = workspace.prepare(policy)
        backend = None
        if execute and policy.sandbox.backend == "bwrap":
            backend = BwrapSandboxBackend(workspace, run, policy.sandbox)
            backend.probe()
        yield VerificationWorkspace(path, workspace, backend)


def bound_envelope(envelope: ImpactEnvelope, policy: TrustPolicy) -> ImpactEnvelope:
    budget = policy.write_budget
    return envelope.model_copy(update={
        "allowed_files": [path for path in envelope.allowed_files if policy.permits_read(path)],
        "max_files_changed": min(envelope.max_files_changed, budget.max_files_changed),
        "max_loc_changed": min(envelope.max_loc_changed, budget.max_loc_changed),
        "allow_dependency_change": envelope.allow_dependency_change and budget.allow_dependency_change,
        "allow_public_api_change": envelope.allow_public_api_change and budget.allow_public_api_change,
    })


def validate_submitted_patch(
    patch: str, summary: PatchSummary, envelope: ImpactEnvelope, policy: TrustPolicy,
) -> PatchPolicyResult:
    result = PatchPolicy().validate(patch, bound_envelope(envelope, policy), summary=summary)
    result.violations.extend(
        f"Patch edits a transient path excluded from workspace evidence: {path}"
        for path in summary.changed_files if TRANSIENT_PATHS.intersection(Path(path).parts)
    )
    result.allowed = not result.violations and result.allowed
    return result


def record_patch_policy(trace: TraceRecorder, result: PatchPolicyResult, description: str) -> None:
    trace.record(
        "patch", "policy_check", "allowed" if result.allowed else "denied", description,
        details={"boundary": "patch", "violations": result.violations,
                 "diff": PatchSummary(changed_files=result.summary.changed_files, changed_loc=result.summary.changed_loc)},
    )


def verify_applied_patch(
    frozen: Path, candidate: VerificationWorkspace, submitted: PatchSummary,
    envelope: ImpactEnvelope, policy: TrustPolicy, evidence: GateEvidence, *,
    command_root: Path, select_commands: Callable[[EffectMap], list[TestCommand]],
    on_result: Callable[[str, TestResult], None],
    writer: ArtifactWriter, trace: TraceRecorder,
    graph: CodeGraphGateway | None = None, graph_root: Path | None = None,
) -> None:
    read = policy.permits_read
    staged = workspace_diff(frozen, candidate.view, submitted.changed_files, permits_read=read)
    effects = EffectMapBuilder(frozen, candidate.view, staged)
    evidence.effects = effects.build(graph, graph_root)
    commands = select_commands(evidence.effects)
    changed_tests = {path for path in staged.changed_files if is_test_file(path)}
    if changed_tests.intersection(staged.deleted_files):
        evidence.findings.append("Restore deleted tests or provide evidence that covers their original behavior.")
    with ExitStack() as workspaces:
        replay = None
        before_replay: WorkspaceDiffSummary | None = None
        baseline = None
        try:
            if commands:
                baseline = workspaces.enter_context(open_workspace(frozen, policy, execute=True))
                if changed_tests and set(staged.changed_files) - changed_tests:
                    replay = workspaces.enter_context(open_workspace(frozen, policy, execute=True))
                    overlay = workspace_diff(frozen, candidate.view, paths=changed_tests, permits_read=read)
                    applied = replay.git().apply(overlay.unified_diff)
                    if not applied.passed:
                        raise RunTerminated(TerminationReason.RUNNER_ERROR, "Could not replay the submitted tests on baseline source.")
                    before_replay = workspace_diff(frozen, replay.view, permits_read=read)
                differential = DifferentialVerifier(
                    verifier_runner(baseline.path, baseline.backend),
                    verifier_runner(candidate.path, candidate.backend),
                    policy=policy,
                    replay=verifier_runner(replay.path, replay.backend) if replay else None,
                )
                with trace.stage("verification", "Compare baseline and candidate tests"):
                    evidence.differential = differential.run(
                        commands, command_root=command_root, effects=effects, changed_tests=changed_tests,
                        on_result=on_result,
                    )
                differential_results = evidence.differential
                effects.observe(
                    evidence.effects, differential_results.observed_lines,
                    differential_results.witnessed_lines, differential_results.failed_lines,
                )
            else:
                trace.record("verification", "test_run", "skipped", "No test commands run")
        finally:
            actual = workspace_diff(frozen, candidate.view, submitted.changed_files, permits_read=read)
            policy_result = validate_submitted_patch(actual.unified_diff, actual, envelope, policy)
            policy_result.violations.extend(f"Unexpected workspace side-effect: {path}" for path in actual.unexpected_files)
            policy_result.violations.extend(f"Unverifiable file change: {path}" for path in actual.unsupported_files)
            for path in sorted(set(staged.fingerprints) | set(actual.fingerprints)):
                if staged.fingerprints.get(path) != actual.fingerprints.get(path) and path not in actual.unexpected_files:
                    policy_result.violations.append(f"Commands changed the submitted patch footprint: {path}")
            if baseline:
                mutated = workspace_diff(frozen, baseline.view, permits_read=read).changed_files
                if mutated:
                    evidence.findings.append(f"Baseline tests wrote repository files ({', '.join(mutated)}). Isolate those test writes.")
            if replay and before_replay:
                after_replay = workspace_diff(frozen, replay.view, permits_read=read)
                if before_replay.fingerprints != after_replay.fingerprints:
                    evidence.findings.append("Replayed tests wrote repository files. Isolate those test writes.")
            policy_result.allowed = policy_result.allowed and not policy_result.violations
            evidence.actual_policy = policy_result
            record_patch_policy(trace, policy_result, "After verification (actual workspace net diff)")
            writer.write_json("workspace_diff", TraceRedactor().redact(actual))
            for workspace in (candidate, baseline, replay):
                if workspace and workspace.backend is None:
                    workspace.workspace.check_quota()
