"""Command-line interface for CodeOps Agent."""

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
import sys
from typing import Annotated, Literal, TypeVar

import typer
from pydantic import ValidationError

from codeops.agents.llm_provider import (
    build_llm_config_file,
    llm_config_from_request,
    resolve_llm_request_overrides,
    save_llm_config_file,
)
from codeops.agents.reviewer import ReviewResult, Reviewer
from codeops.core.artifacts import ArtifactWriter
from codeops.core.errors import ToolError
from codeops.core.models import TaskRequest, TaskState
from codeops.core.paths import RunPaths
from codeops.evals.runner import EvalRunner
from codeops.evolution.critic import ReviewForkCritic
from codeops.evolution.curator import EvolutionCurator
from codeops.evolution.evaluator import EvolutionEvaluator
from codeops.evolution.models import FeedbackKind, Proposal
from codeops.evolution.store import EvolutionStore
from codeops.gate.runner import EvidenceGateRunner
from codeops.reporting.trace_events import latest_event_detail, load_trace_events
from codeops.reporting.trace_html import render_trace_report
from codeops.runtime.lifecycle import RunTerminated, TerminationReason
from codeops.safety.secret_filter import SecretFilter
from codeops.trust.escalation import EscalationRequest
from codeops.trust.preflight import PreFlightPlanner, TaskTrustContract, TrustPolicyBuilder
from codeops.workflow.orchestrator import WorkflowOrchestrator

app = typer.Typer(
    name="codeops",
    help="Evidence-aware CodeOps Agent.",
    no_args_is_help=True,
)

evolve = typer.Typer(help="Review and curate repository evolution.", no_args_is_help=True)
app.add_typer(evolve, name="evolve")

RepoOption = Annotated[
    Path, typer.Option("--repo", exists=True, file_okay=False, resolve_path=True),
]
IssueOption = Annotated[
    Path, typer.Option("--issue", exists=True, dir_okay=False),
]


class SandboxOption(StrEnum):
    NONE = "none"
    BWRAP = "bwrap"


@app.callback()
def root() -> None:
    """Evidence-aware CodeOps Agent."""


@app.command()
def run(
    repo: RepoOption,
    issue: IssueOption,
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
    no_network: Annotated[
        bool,
        typer.Option(
            "--no-network",
            help="Record that the run should avoid network-backed operations.",
        ),
    ] = False,
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help=(
                "LLM provider override: none, kimi, moonshotai-cn, moonshotai, "
                "kimi-code, or openai-compatible. Defaults to .codeops/llm.yaml "
                "or CODEOPS_LLM_PROVIDER, else none."
            ),
        ),
    ] = None,
    llm_model: Annotated[
        str | None,
        typer.Option(
            "--llm-model",
            help="LLM model id. Defaults to kimi-k2.6 for Kimi Platform providers.",
        ),
    ] = None,
    llm_base_url: Annotated[
        str | None,
        typer.Option(
            "--llm-base-url",
            help="OpenAI-compatible base URL override.",
        ),
    ] = None,
    llm_api_key_env: Annotated[
        str | None,
        typer.Option(
            "--llm-api-key-env",
            help="Environment variable containing the LLM API key.",
        ),
    ] = None,
    llm_max_completion_tokens: Annotated[
        int | None,
        typer.Option(
            "--llm-max-completion-tokens",
            help="Maximum completion tokens for LLM fallback patch generation.",
        ),
    ] = None,
    sandbox: Annotated[
        SandboxOption, typer.Option("--sandbox", help="Execution sandbox backend."),
    ] = SandboxOption.NONE,
    trust_policy: Annotated[
        Path | None, typer.Option("--trust-policy", help="Path to the trust policy."),
    ] = None,
    approve_trust: Annotated[
        bool, typer.Option("--approve-trust", help="Confirm the pre-flight policy for this run."),
    ] = False,
) -> None:
    """Create initial run artifacts for a task."""
    try:
        llm_overrides = resolve_llm_request_overrides(
            repo,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            llm_api_key_env=llm_api_key_env,
            llm_max_completion_tokens=llm_max_completion_tokens,
        )
    except ToolError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    request = TaskRequest(
        repo_path=repo,
        issue_path=issue.absolute(),
        out_path=out,
        retrieval_mode=retrieval,
        no_network=no_network,
        sandbox_mode="bwrap" if sandbox is SandboxOption.BWRAP else "none",
        trust_policy_path=trust_policy,
        trust_approved=approve_trust,
        **llm_overrides,
    )
    interactive = sys.stdin.isatty()
    try:
        state = WorkflowOrchestrator().run(
            request,
            approve=_confirm_trust if interactive else None,
            approve_escalation=_confirm_escalation if interactive else None,
        )
    except RunTerminated as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if state.termination_reason not in {None, TerminationReason.COMPLETED}:
        typer.echo(f"{state.task_id} finished with {state.termination_reason.value}", err=True)
        raise typer.Exit(1)
    typer.echo(f"completed {state.task_id} with status {state.status}")


@app.command()
def verify(
    repo: RepoOption,
    diff: Annotated[Path, typer.Option("--diff", exists=True, dir_okay=False)],
    out: Annotated[Path, typer.Option("--out")],
    issue: Annotated[Path | None, typer.Option("--issue", exists=True, dir_okay=False)] = None,
    sandbox: Annotated[SandboxOption, typer.Option("--sandbox")] = SandboxOption.BWRAP,
    approve_trust: Annotated[bool, typer.Option("--approve-trust")] = False,
) -> None:
    """Verify a submitted unified diff against the source repository."""
    try:
        state = EvidenceGateRunner().run(
            repo, diff, out, issue=issue, sandbox_mode=sandbox.value, approve_trust=approve_trust,
            approve=_confirm_trust if sys.stdin.isatty() else None,
        )
    except (OSError, RunTerminated) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{state.verdict} · {state.trace_report_path}")
    if state.verdict != "verified":
        raise typer.Exit(2 if state.verdict == "inconclusive" else 1)


@app.command()
def report(
    run_dir: Annotated[Path, typer.Option("--run", exists=True, file_okay=False, resolve_path=True)],
) -> None:
    """Regenerate the offline trace report from saved run artifacts."""
    paths = RunPaths.from_run_dir(run_dir)
    try:
        state = TaskState.model_validate_json(paths.artifact_path("task").read_bytes())
        state.run_dir = paths.run_dir
        if not paths.artifact_path("evidence_matrix").is_file():
            state.evidence_matrix_path = None
        if not paths.artifact_path("intent_manifest").is_file():
            state.intent_manifest_path = None
        events = load_trace_events(paths.trace_events_path) if paths.trace_events_path.exists() else []
        recorded_review = latest_event_detail(events, "review", "review")
        review = ReviewResult.model_validate(recorded_review) if recorded_review is not None else Reviewer().review(state)
        recorded_contract = latest_event_detail(events, "trust_decision", "contract")
        contract = TaskTrustContract.model_validate(recorded_contract) if recorded_contract is not None else None
        html = render_trace_report(state, review, contract, events)
        path = ArtifactWriter(paths, atomic=True).write_html("trace_report", html)
    except (OSError, ValidationError) as exc:
        typer.echo(SecretFilter().scan(str(exc)).redacted_text, err=True)
        raise typer.Exit(1) from exc
    typer.echo(path)


@app.command()
def feedback(
    run: Annotated[Path, typer.Option("--run", exists=True, file_okay=False)],
    comment: Annotated[str, typer.Option("--comment")],
    node: Annotated[str | None, typer.Option("--node")] = None,
    kind: Annotated[FeedbackKind, typer.Option("--kind")] = FeedbackKind.AUTO,
    repo: Annotated[Path | None, typer.Option("--repo", exists=True, file_okay=False)] = None,
) -> None:
    """Stage learning from a completed run or trace node."""
    proposal = _evolution_call(lambda: ReviewForkCritic.feedback(run, comment, node=node, kind=kind, repo=repo))
    if proposal is None:
        typer.echo("Feedback recorded without a proposal.")
    else:
        _print_proposal(proposal)


ProposalOption = Annotated[str, typer.Option("--proposal")]
SkillOption = Annotated[str, typer.Option("--skill")]
NetworkOption = Annotated[bool, typer.Option("--allow-network", help="Explicitly opt in to enabling network.")]
_T = TypeVar("_T")


@evolve.command("list")
def evolve_list(repo: RepoOption) -> None:
    """List skill revisions and proposal status."""
    store = EvolutionStore(repo)
    for skill in _evolution_call(lambda: store.index() + store.index(archived=True)):
        typer.echo(f"{skill.name}  {skill.status}  {skill.owner}  {skill.revision_hash}  {skill.status_reason}")
    for proposal in _evolution_call(lambda: tuple(store.proposals())):
        _print_proposal(proposal)


@evolve.command("eval")
def evolve_eval(repo: RepoOption, proposal: ProposalOption, allow_network: NetworkOption = False) -> None:
    """Run the offline evolution gates and save their report."""
    result = _evolution_call(lambda: EvolutionEvaluator(EvolutionStore(repo)).evaluate(proposal, allow_network=allow_network))
    _print_proposal(result)
    if result.eval_report is None or not result.eval_report.passed:
        raise typer.Exit(1)


@evolve.command("approve")
def evolve_approve(repo: RepoOption, proposal: ProposalOption, allow_network: NetworkOption = False) -> None:
    """Recheck and apply a pending proposal."""
    result = _evolution_call(lambda: EvolutionEvaluator(EvolutionStore(repo)).approve(proposal, allow_network=allow_network))
    _print_proposal(result)


@evolve.command("reject")
def evolve_reject(repo: RepoOption, proposal: ProposalOption) -> None:
    """Close a pending proposal."""
    _print_proposal(_evolution_call(lambda: EvolutionEvaluator(EvolutionStore(repo)).reject(proposal)))


@evolve.command("pin")
def evolve_pin(repo: RepoOption, skill: SkillOption) -> None:
    """Keep a skill eligible for loading when its scope matches."""
    _transition(repo, skill, "pin")


@evolve.command("unpin")
def evolve_unpin(repo: RepoOption, skill: SkillOption) -> None:
    """Return a pinned skill to normal curation."""
    _transition(repo, skill, "unpin")


@evolve.command("archive")
def evolve_archive(repo: RepoOption, skill: SkillOption) -> None:
    """Move a skill into the recoverable archive."""
    _transition(repo, skill, "archive")


@evolve.command("restore")
def evolve_restore(repo: RepoOption, skill: SkillOption) -> None:
    """Restore an archived skill."""
    _transition(repo, skill, "restore")


@evolve.command("revert")
def evolve_revert(repo: RepoOption, skill: SkillOption) -> None:
    """Restore the previous skill revision."""
    _transition(repo, skill, "revert")


@evolve.command("curate")
def evolve_curate(repo: RepoOption) -> None:
    """Apply age and outcome rules to stored skills."""
    changes = _evolution_call(lambda: EvolutionCurator(EvolutionStore(repo)).curate())
    for name, status in changes.items():
        typer.echo(f"{name}  {status}")
    if not changes:
        typer.echo("No skill status changes.")


def _transition(
    repo: Path, skill: str, action: Literal["pin", "unpin", "archive", "restore", "revert"],
) -> None:
    updated = _evolution_call(lambda: EvolutionStore(repo).transition(skill, action))
    typer.echo(f"{updated.name}  {updated.status}  {updated.revision_hash}")


def _evolution_call(action: Callable[[], _T]) -> _T:
    try:
        return action()
    except (ValueError, OSError, RunTerminated) as exc:
        message = (
            "Invalid or unreadable evolution input."
            if isinstance(exc, (OSError, ValidationError)) else str(exc)
        )
        typer.echo(SecretFilter().scan(message).redacted_text, err=True)
        raise typer.Exit(1) from exc


def _print_proposal(proposal: Proposal) -> None:
    typer.echo(f"{proposal.proposal_id}  {proposal.kind}  {proposal.status}")
    for finding in proposal.safety_report.findings:
        typer.echo(f"  {finding}")
    if proposal.eval_report is not None:
        for check in proposal.eval_report.checks:
            typer.echo(f"  {'PASS' if check.passed else 'FAIL'} {check.name}  {check.detail}")


@app.command("trust-init")
def trust_init(repo: RepoOption) -> None:
    """Generate a trust policy draft for the repository."""
    try:
        path = TrustPolicyBuilder().write_draft(repo)
    except (RunTerminated, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"wrote {path}")


@app.command("trust-plan")
def trust_plan(repo: RepoOption, issue: IssueOption) -> None:
    """Show the repository, outbound, write, and command scope for a task."""
    try:
        contract = PreFlightPlanner().preview(repo, issue)
    except (RunTerminated, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(contract.summary())


@app.command("llm-init")
def llm_init(
    repo: RepoOption,
    provider: Annotated[
        str,
        typer.Option(
            "--provider",
            help="Provider preset (kimi, moonshotai-cn, moonshotai, kimi-code, openai-compatible).",
        ),
    ] = "moonshotai-cn",
    model: Annotated[str | None, typer.Option("--model", help="Default model id.")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="API base URL override.")] = None,
    api_key_env: Annotated[str | None, typer.Option("--api-key-env", help="API key environment variable name.")] = None,
    max_completion_tokens: Annotated[int, typer.Option("--max-completion-tokens")] = 4096,
) -> None:
    """Write non-secret harness LLM configuration to .codeops/llm.yaml."""
    try:
        config = build_llm_config_file(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            max_completion_tokens=max_completion_tokens,
        )
        path = save_llm_config_file(repo, config)
    except (ToolError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"wrote {path}")


@app.command("llm-show")
def llm_show(repo: RepoOption) -> None:
    """Show the effective LLM provider configuration without printing secrets."""
    try:
        overrides = resolve_llm_request_overrides(repo)
        resolved = llm_config_from_request(
            TaskRequest(
                repo_path=repo,
                issue_path=repo,
                out_path=repo,
                **overrides,
            )
        )
    except ToolError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if resolved is None:
        typer.echo("provider: none")
        return
    typer.echo(f"provider: {resolved.provider}")
    typer.echo(f"model: {resolved.model}")
    typer.echo(f"base_url: {resolved.base_url}")
    typer.echo(f"api_protocol: {resolved.api_protocol}")
    typer.echo(f"api_key_env: {resolved.api_key_env} (set={bool(resolved.api_key)})")
    typer.echo(f"max_completion_tokens: {resolved.max_completion_tokens}")


def _confirm_trust(contract: TaskTrustContract) -> bool:
    typer.echo(contract.summary())
    return typer.confirm("Approve this task's trust policy?")


def _confirm_escalation(request: EscalationRequest) -> bool:
    return typer.confirm(f"Allow {request.boundary} for {request.path}? {request.reason}")


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
