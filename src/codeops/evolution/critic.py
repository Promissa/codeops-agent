"""Read completed run evidence and synthesize reviewable, offline proposals."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from codeops.core.models import AcceptanceContract, ProjectProfile, TrustVerdict
from codeops.evolution.evaluator import EvolutionEvaluator, explicit_checks, guidance_key
from codeops.evolution.loader import match_skill, profile_languages, words
from codeops.evolution.models import (
    FeedbackCase, FeedbackKind, Focus, MemoryName, MemorySnapshot, Proposal, ProposalKind,
    SafetyReport, SkillDraft, SkillHeader, SkillScope,
)
from codeops.evolution.store import EvolutionStore, digest
from codeops.reporting.trace_events import TraceEvent
from codeops.runtime.lifecycle import RunLifecycleState, RunTerminated
from codeops.runtime.workspace import open_regular

_TOPICS = (
    "csv", "parser", "serialization", "migration", "concurrency", "cache", "routing",
    "verification", "dependency", "configuration", "boundary",
)
_DISSATISFACTION = re.compile(
    r"\b(?:reject(?:ed)?|bad|wrong|harmful|regress(?:ed|ion)?|dissatisfied|misleading|unhelpful|incorrect|failed|"
    r"not (?:helpful|useful)|didn.t work|stop using)\b", re.I,
)
_ONE_OFF = re.compile(
    r"\b(?:for|in|on) (?:(?:this|that) (?:run|pr|issue|session)\b|(?:pr|issue) #?\d+\b)"
    r"|^(?:(?:retry|rerun|redo) (?:this|that|the same)\b|(?:fix|update) (?:the )?(?:typo|wording)\b)", re.I,
)
_REUSABLE = re.compile(r"\b(?:always|every time|going forward|future|reusable|across runs)\b", re.I)
_MEMORY_PREFIX = re.compile(r"^(USER|MEMORY)\.md\s*:\s*", re.I)
_RECOMMENDATIONS = {
    "verification": "Reproduce the behavior on the baseline and confirm that the focused test reaches the changed behavior on the candidate.",
    "boundary": "Check the proposed paths against the approved edit envelope and resolve policy denials before rerunning.",
    "routing": "Cross-check candidate files with deterministic search, then select tests that exercise the affected behavior.",
    "workflow": "Capture the expected behavior and reproduce it with focused tests before making the bounded change.",
}


class ReviewInput(BaseModel):
    model_config = ConfigDict(from_attributes=True, hide_input_in_errors=True)

    task_id: str
    repo_path: Path
    lifecycle_state: RunLifecycleState
    verdict: TrustVerdict
    issue_text: str = ""
    acceptance_contract: AcceptanceContract | None = None
    project_profile: ProjectProfile | None = None


@dataclass(frozen=True)
class LoadedRevision:
    name: str
    revision_hash: str
    guidance_key: str


@dataclass(frozen=True)
class CriticContext:
    verdict: TrustVerdict
    focus: Focus
    comment: str
    scope: SkillScope
    memory: MemorySnapshot
    existing: SkillDraft | None


class CriticProvider(Protocol):
    def propose_skill(self, context: CriticContext) -> SkillDraft | None: ...


class ReviewForkCritic:
    """The critic proposes changes. Approval and outcome accounting are host operations."""

    def __init__(self, store: EvolutionStore, provider: CriticProvider | None = None) -> None:
        self.store = store
        self.provider = provider
        self.evaluator = EvolutionEvaluator(store)

    @classmethod
    def feedback(
        cls, run: Path, comment: str, *, node: str | None = None,
        kind: FeedbackKind = FeedbackKind.AUTO, repo: Path | None = None,
    ) -> Proposal | None:
        run = run.resolve()
        task, events, logs = _read_run(run)
        target = task.repo_path.resolve()
        if repo is not None and target != repo.resolve():
            raise ValueError("Feedback repository must match the completed run's target.")
        if not comment.strip():
            raise ValueError("Feedback comment is empty.")
        return cls(EvolutionStore(target)).review(
            task, events, comment=comment, kind=kind, node=node, logs=logs, create=True,
        )

    def review(
        self, task: ReviewInput, events: list[TraceEvent], *, comment: str = "",
        kind: FeedbackKind = FeedbackKind.AUTO, node: str | None = None, logs: tuple[str, ...] = (),
        create: bool = False, expected: tuple[int, int] | None = None,
    ) -> Proposal | None:
        review = next((event for event in reversed(events) if event.kind == "review"), None)
        if (
            task.lifecycle_state != RunLifecycleState.FINISHED or review is None
            or task.verdict != review.details.get("verdict")
            or any(event.task_id != task.task_id for event in events)
            or task.repo_path.resolve() != self.store.repo
        ):
            raise ValueError("Feedback requires completed run evidence for this repository.")
        selected = next((event for event in events if event.event_id == node), None)
        if node is not None and selected is None:
            raise ValueError("The feedback node does not exist in this run.")
        event = selected or review
        verdict = task.verdict
        learned = _loaded(events)
        finding = self.store.guard.scan(
            comment, task.task_id, event.event_id, task.issue_text,
            task.acceptance_contract, logs,
            *(item.model_dump(mode="json") for item in events),
        )
        if kind == FeedbackKind.AUTO:
            if _MEMORY_PREFIX.match(comment.strip()):
                kind = FeedbackKind.MEMORY
            elif _ONE_OFF.search(comment.strip()) and not _REUSABLE.search(comment):
                kind = FeedbackKind.EPHEMERAL
        focus = _focus(kind, selected, verdict, comment)
        rejection = bool(_DISSATISFACTION.search(comment))
        names = [item.name for item in learned]
        selected_names = [
            name for name in names
            if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", comment, re.I)
        ]
        if rejection and not selected_names and len(names) == 1:
            selected_names = names
        case_id = digest(f"{review.event_id}\0{event.event_id}\0{kind}\0{comment}")
        proposal_kind: ProposalKind = (
            "policy_proposal" if kind == FeedbackKind.POLICY
            else "memory_update" if kind == FeedbackKind.MEMORY else "skill_create"
        )
        base = Proposal(
            proposal_id=case_id[:32], kind=proposal_kind,
            rationale=comment.strip() or f"Evidence Gate recorded a {verdict} outcome. Review the {focus} procedure.",
            source_run_id=task.task_id, source_event_id=node or review.event_id,
        )
        with self.store.transaction(create=create, expected=expected):
            cases = self.store.cases()
            proposal = self.store.get_proposal(base.proposal_id)
            if any(case.case_id == case_id for case in cases):
                return proposal

            rejected_revisions = ()
            if rejection and not finding.findings:
                rejected_revisions = tuple(
                    item.guidance_key for item in learned if item.name in selected_names and item.guidance_key
                )
                for item in learned:
                    if item.name in selected_names:
                        self.store.attribute(
                            item.name, item.revision_hash, case_id, verdict=None, now=review.timestamp,
                        )
            if proposal is None and (kind != FeedbackKind.EPHEMERAL or finding.findings):
                if finding.findings:
                    proposal = self.evaluator.quarantine(base, finding)
                else:
                    try:
                        draft = self._draft(base, task, events, focus, verdict, comment, selected_names)
                        proposal = self.evaluator.admit(draft) if draft else None
                    except (ValueError, OSError, RunTerminated) as exc:
                        # Validation errors can contain submitted text. Keep only a fixed reason.
                        reason = (
                            "Invalid candidate structure, budget, or policy schema."
                            if isinstance(exc, ValidationError) else "Could not safely synthesize a proposal from fresh store inputs."
                        )
                        proposal = self.evaluator.quarantine(base, SafetyReport(findings=(reason,)))
            candidate_names = [*names, proposal.skill.name] if proposal and proposal.skill else names
            self.store._append_case(FeedbackCase(
                case_id=case_id, kind=kind, focus=focus, verdict=verdict,
                skill_keys=tuple(digest(name) for name in dict.fromkeys(candidate_names)),
                safety_blocked=bool(proposal and not proposal.safety_report.passed),
                rejected_guidance=rejection, rejected_revisions=rejected_revisions,
                required_checks=(
                    explicit_checks(comment) if not finding.findings and kind != FeedbackKind.EPHEMERAL
                    and not (proposal and not proposal.safety_report.passed) else ()
                ),
            ), cases)
        return proposal

    def _draft(
        self, base: Proposal, task: ReviewInput, events: list[TraceEvent], focus: Focus, verdict: TrustVerdict,
        comment: str, selected_names: list[str],
    ) -> Proposal | None:
        if base.kind == "memory_update":
            prefix = _MEMORY_PREFIX.match(comment.strip())
            target: MemoryName = "USER.md" if prefix and prefix[1].upper() == "USER" else "MEMORY.md"
            return base.model_copy(update={
                "memory_target": target,
                "base_revision_hash": self.store.memory_revision(target),
                "replacement": _MEMORY_PREFIX.sub("", comment.strip(), count=1),
            })
        if base.kind == "policy_proposal":
            current, revision = self.store.policy_snapshot()
            if comment.strip().startswith("{") or re.match(r"^\w+\s*:", comment):
                proposed = comment.strip()
            elif re.search(r"\b(?:deny|disable|restrict|disallow|no|without)\s+(?:the\s+)?network\b", comment, re.I):
                current.sandbox.network_enabled = False
                proposed = current.to_yaml()
            else:
                raise ValueError("Policy feedback requires replacement policy YAML or JSON.")
            return base.model_copy(update={"replacement": proposed, "base_revision_hash": revision})

        profile = task.project_profile
        languages = profile_languages(profile)
        issue = task.issue_text
        routing = next((event.details.get("files", []) for event in reversed(events) if event.kind == "routing"), [])
        topics = words(f"{issue} {comment} {' '.join(routing)}")
        topic = next((item for item in _TOPICS if item in topics), None)
        scope = SkillScope(languages=languages[:1], tags=(topic,) if topic else ())
        candidates = self.store.index(strict=False)
        existing_header = next((item for item in candidates if item.name in selected_names), None)
        if existing_header is None:
            existing_header = next((
                item for item in candidates
                if item.status != "archived"
                and item.scope.tags == scope.tags
                and match_skill(item, profile=profile, issue=f"{issue} {comment}", files=routing)
            ), None)
        action = next((word for word in ("verification", "boundary", "routing") if focus == word), "workflow")
        name = "-".join(filter(None, (topic or (languages[0] if languages else None), action)))
        if name == "verification-verification":
            name = "verification-procedure"
        existing = self.store.get_skill(existing_header.name if existing_header else name)
        memory = self.store.memory_snapshot()
        draft = self.provider.propose_skill(CriticContext(
            verdict=verdict, focus=focus, comment=comment, scope=scope, memory=memory,
            existing=existing.draft() if existing else None,
        )) if self.provider else None
        if draft is None:
            title = f"Reusable {topic + ' ' if topic else ''}{action} procedure"
            guidance = _RECOMMENDATIONS[action]
            # Treat a corrective review as evidence, and use its actionable guidance.
            # A one-off complaint alone is never elevated to a procedural instruction.
            actionable = comment.strip() if comment.strip() and (not _DISSATISFACTION.search(comment) or
                re.search(r"\b(?:should|instead|always|add|check|run|verify|use)\b", comment, re.I)) else ""
            instruction = f"{guidance}\n" + (f"\nReviewer guidance: {actionable}\n" if actionable else "")
            body = f"# {title}\n\n{instruction}"
            if existing and not (selected_names and _DISSATISFACTION.search(comment)):
                additions = [
                    part for part in (guidance, f"Reviewer guidance: {actionable}" if actionable else "")
                    if part and part not in existing.body
                ]
                if not additions:
                    return None
                body = f"{existing.body.rstrip()}\n\n" + "\n".join(additions) + "\n"
            if existing and guidance_key(existing.body) == guidance_key(body):
                return None
            draft = SkillDraft(
                name=existing.name if existing else name,
                description=existing.description if existing else title,
                scope=existing.scope if existing else scope, body=body,
            )
        if existing and draft.name != existing.name:
            raise ValueError("An existing scoped skill must be patched under its current name.")
        return base.model_copy(update={
            "kind": "skill_patch" if existing else "skill_create",
            "skill": draft, "base_revision_hash": existing.revision_hash if existing else None,
        })


def _loaded(events: list[TraceEvent]) -> list[LoadedRevision]:
    skills: dict[str, LoadedRevision] = {}
    for event in events:
        if event.kind == "evolution_loaded":
            for item in event.details.get("skills", []):
                if isinstance(item, dict) and isinstance(item.get("guidance_key", ""), str):
                    core = {key: value for key, value in item.items() if key in SkillHeader.model_fields}
                    try:
                        header = SkillHeader.model_validate(core)
                    except ValueError:
                        continue
                    if re.fullmatch(r"[a-f0-9]{64}", header.revision_hash):
                        key = item.get("guidance_key", "")
                        skills[header.name] = LoadedRevision(
                            name=header.name, revision_hash=header.revision_hash,
                            guidance_key=key if re.fullmatch(r"[a-f0-9]{64}", key) else "",
                        )
    return list(skills.values())


def _focus(kind: FeedbackKind, event: TraceEvent | None, verdict: TrustVerdict, comment: str) -> Focus:
    if kind == FeedbackKind.POLICY:
        return "policy"
    if kind == FeedbackKind.MEMORY:
        return "memory"
    tokens = words(comment)
    if event and event.stage in {"routing", "retrieval", "codegraph"}:
        return "routing"
    if (event and event.kind in {"policy_check", "trust_decision"}) or verdict == "blocked":
        return "boundary"
    if (event and event.stage in {"verification", "evidence_gate", "review"}) or verdict == "regressed" or tokens.intersection({"test", "tests", "replay", "baseline", "verification"}):
        return "verification"
    return "workflow"


def _read_run(run: Path) -> tuple[ReviewInput, list[TraceEvent], tuple[str, ...]]:
    with open_regular(run, Path("task.json")) as source:
        task = ReviewInput.model_validate_json(source.read())
    with open_regular(run, Path("trace_events.jsonl")) as source:
        events = [TraceEvent.model_validate_json(line) for line in source if line.strip()]
    return task, events, _run_logs(run)


def _run_logs(run: Path) -> tuple[str, ...]:
    try:
        with open_regular(run, Path("test_results.json")) as source:
            results = json.load(source)
    except FileNotFoundError:
        return ()
    if not isinstance(results, list) or any(not isinstance(result, dict) for result in results):
        raise ValueError("Invalid test results artifact.")
    return tuple(
        value for result in results for field in ("stdout", "stderr")
        if isinstance(value := result.get(field), str) and value
    )
