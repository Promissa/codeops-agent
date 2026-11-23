"""Offline proposal checks, repeated under the store lock at approval time."""

from collections.abc import Callable
from difflib import unified_diff
import re

from pydantic import ValidationError

from codeops.evolution.guards import EvolutionSafetyGuard
from codeops.evolution.models import (
    CorpusRule, EvalCheck, EvolutionEvalReport, FeedbackCase, Proposal, SafetyReport, Skill,
    utc_now,
)
from codeops.evolution.store import EvolutionStore, digest
from codeops.runtime.lifecycle import RunTerminated
from codeops.trust.policy import TrustPolicy

SandboxCheck = Callable[[Proposal], EvalCheck]

# These are checks of the guidance itself. Repository behavior still requires
# differential evidence from an actual run.
_PROCEDURAL_REGRESSIONS = {
    "verification": re.compile(
        r"\b(?:skip|omit|avoid|remove|disable)\b.{0,35}\b(?:tests?|verification|baselines?|replay)\b"
        r"|\b(?:mark|declare|treat)\b.{0,40}\bverified\b.{0,45}\b(?:regardless|without|only)\b"
        r"|\b(?:test pass(?:ing|es)?|exit code 0)\s+(?:alone\s+)?(?:means|proves|is enough)", re.I,
    ),
    "boundary": re.compile(r"\b(?:allow|edit|write|patch)\b.{0,30}\boutside (?:the )?(?:envelope|scope)\b", re.I),
    "routing": re.compile(r"\b(?:trust|accept|assume)\b.{0,30}\bcodegraph\b.{0,30}\b(?:always|alone|unconditionally)\b", re.I),
}
_NEGATION = re.compile(r"\b(?:never|do not|don't|must not|cannot|avoid instructions? to)\s+$", re.I)
_CORPUS_RULES: dict[CorpusRule, re.Pattern[str]] = {
    "baseline_replay": re.compile(r"\bbaseline (?:replay|test(?:ing)?)\b|\bre(?:run|play)\b.{0,40}\bbaseline\b", re.I),
    "caller_coverage": re.compile(r"\b(?:exercise|cover|test|check)\b.{0,40}\b(?:callers|dependents)\b|\bcaller coverage\b", re.I),
    "edit_scope": re.compile(r"\b(?:approved |allowed )?(?:edit|impact) envelope\b|\bapproved edit scope\b", re.I),
    "routing_crosscheck": re.compile(r"\bcross.check\b.{0,40}\b(?:codegraph|routing|candidate files)\b", re.I),
    "deny_network": re.compile(r"\b(?:deny|disable|disallow|no|without)\s+(?:the\s+)?network\b", re.I),
}


def explicit_checks(comment: str) -> tuple[CorpusRule, ...]:
    return tuple(rule for rule, pattern in _CORPUS_RULES.items() if pattern.search(comment))


class EvolutionEvaluator:
    def __init__(self, store: EvolutionStore, *, sandbox_check: SandboxCheck | None = None) -> None:
        self.store = store
        self.sandbox_check = sandbox_check

    def candidate(self, proposal: Proposal) -> tuple[str, str, Skill | TrustPolicy | None]:
        """Validate the structure against a fresh read and return both sides of the change."""
        skill = proposal.skill
        if proposal.kind in {"skill_create", "skill_patch"}:
            if skill is None or proposal.memory_target is not None or proposal.replacement is not None:
                raise ValueError("A skill proposal must supply only a skill draft.")
            old = self.store.get_skill(skill.name)
            if proposal.kind == "skill_create":
                if old or self.store.get_skill(skill.name, archived=True) or proposal.base_revision_hash is not None:
                    raise ValueError("Skill already exists or creation has a base revision.")
            else:
                if old is None:
                    raise ValueError("Skill patch requires an existing skill.")
                if old.owner == "human":
                    raise ValueError("Agent proposals cannot edit a human-owned skill.")
                self._require_revision(proposal, old.revision_hash)
                if old.draft() == skill:
                    raise ValueError("Skill patch contains no change.")
            return old.body if old else "", skill.body, old

        if skill is not None or proposal.replacement is None:
            raise ValueError("A replacement proposal must supply replacement text.")
        if proposal.kind == "memory_update":
            if proposal.memory_target is None:
                raise ValueError("Memory proposal requires MEMORY.md or USER.md.")
            self.store.check_memory(proposal.memory_target, proposal.replacement)
            self._require_revision(proposal, self.store.memory_revision(proposal.memory_target))
            try:
                original = self.store.memory_text(proposal.memory_target)
            except ValueError:
                original = "[Invalid memory content withheld]\n"
            return original, proposal.replacement, None
        if proposal.memory_target is not None:
            raise ValueError("Policy proposals cannot set a memory target.")
        current, revision = self.store.policy_snapshot()
        self._require_revision(proposal, revision)
        candidate = TrustPolicy.parse(proposal.replacement)
        return current.to_yaml(), candidate.to_yaml(), candidate

    @staticmethod
    def _require_revision(proposal: Proposal, revision: str) -> None:
        if proposal.base_revision_hash != revision:
            raise ValueError("Base revision is stale. Read the current revision and stage a fresh proposal.")

    def admit(self, proposal: Proposal) -> Proposal:
        """Stage after checking ownership, freshness, budgets and safety."""
        report = self.store.guard.scan(proposal)
        if not report.passed:
            return self.quarantine(proposal, report)
        try:
            before, after, parsed = self.candidate(proposal)
            if isinstance(parsed, TrustPolicy):
                current, _ = self.store.policy_snapshot()
                self.store.guard.policy(parsed, current, allow_network=True)
        except (ValueError, OSError, RunTerminated) as exc:
            return self.quarantine(proposal, SafetyReport(findings=(_error(exc),)))
        label = proposal.skill.name if proposal.skill else proposal.memory_target or "trust_policy.yaml"
        preview = _preview(before, after, label)
        if proposal.skill is not None:
            old_metadata = parsed.draft().model_dump_json(exclude={"body"}, indent=2) if isinstance(parsed, Skill) else ""
            new_metadata = proposal.skill.model_dump_json(exclude={"body"}, indent=2)
            preview = _preview(old_metadata, new_metadata, f"{label} (routing)") + preview
        proposal = proposal.model_copy(update={"diff_preview": preview})
        report = self.store.guard.scan(proposal)
        if not report.passed:
            return self.quarantine(proposal, report)
        self.store._save_proposal(proposal)
        return proposal

    def quarantine(self, proposal: Proposal, report: SafetyReport) -> Proposal:
        self.store.guard.require_safe(report)
        safe_ids = self.store.guard.scan(proposal.source_run_id, proposal.source_event_id).passed
        rejected = Proposal(
            proposal_id=proposal.proposal_id, kind=proposal.kind,
            rationale="Quarantined by evolution admission checks.",
            source_run_id=proposal.source_run_id if safe_ids else "[withheld]",
            source_event_id=proposal.source_event_id if safe_ids else None,
            safety_report=report, status="rejected",
        )
        self.store._save_proposal(rejected)
        return rejected

    def evaluate(self, proposal_id: str, *, allow_network: bool = False) -> Proposal:
        with self.store.transaction():
            original = self._get(proposal_id)
            if original.status == "approved":
                return original
            if recovered := self._recover_approval(original):
                return recovered
            proposal, _, _ = self._evaluate(original, allow_network=allow_network)
            return proposal

    def approve(self, proposal_id: str, *, allow_network: bool = False) -> Proposal:
        with self.store.transaction():
            original = self._get(proposal_id)
            if original.status == "approved":
                return original
            if original.status != "pending":
                raise ValueError("Only a pending proposal can be approved.")
            if recovered := self._recover_approval(original):
                return recovered
            proposal, after, parsed = self._evaluate(original, allow_network=allow_network)
            if proposal.eval_report is None or not proposal.eval_report.passed:
                raise ValueError("Proposal failed evolution evaluation. Inspect its evaluation report.")
            proposal = proposal.model_copy(update={"approval_receipt": _receipt(proposal)})
            self.store._save_proposal(proposal)
            if proposal.skill is not None:
                self.store.install(
                    proposal.skill, proposal.source_run_id,
                    parsed if isinstance(parsed, Skill) else None, now=utc_now(),
                )
            elif proposal.memory_target is not None:
                self.store._save_memory(proposal.memory_target, after)
            elif isinstance(parsed, TrustPolicy):
                self.store._save_policy(parsed)
            return self._mark_approved(proposal)

    def _mark_approved(self, proposal: Proposal) -> Proposal:
        approved = proposal.model_copy(update={"status": "approved"})
        self.store._save_proposal(approved)
        return approved

    def _recover_approval(self, proposal: Proposal) -> Proposal | None:
        # The receipt is written only after explicit approval and a fresh pass.
        # An interrupted final status write can be recovered from the installed content.
        if (
            proposal.status != "pending" or proposal.approval_receipt != _receipt(proposal)
            or proposal.eval_report is None or not proposal.eval_report.passed
            or not proposal.safety_report.passed or not self.store.guard.scan(proposal).passed
        ):
            return None
        if proposal.skill is not None:
            current = self.store.get_skill(proposal.skill.name)
            applied = (
                current is not None and current.owner == "agent" and current.draft() == proposal.skill
                and proposal.source_run_id in current.source_runs
            )
        elif proposal.memory_target is not None:
            applied = self.store.memory_revision(proposal.memory_target) == digest(proposal.replacement or "")
        else:
            _, revision = self.store.policy_snapshot()
            applied = revision == digest(TrustPolicy.parse(proposal.replacement or "").to_yaml())
        return self._mark_approved(proposal) if applied else None

    def reject(self, proposal_id: str) -> Proposal:
        with self.store.transaction():
            proposal = self._get(proposal_id)
            if proposal.status == "approved" or self._recover_approval(proposal) is not None:
                raise ValueError("An applied proposal cannot be rejected.")
            report = self.store.guard.scan(proposal)
            if not report.passed:
                return self.quarantine(proposal, report)
            rejected = proposal.model_copy(update={"status": "rejected"})
            self.store._save_proposal(rejected)
            return rejected

    def _get(self, proposal_id: str) -> Proposal:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError("Unknown proposal ID.")
        return proposal

    def _evaluate(
        self, proposal: Proposal, *, allow_network: bool,
    ) -> tuple[Proposal, str, Skill | TrustPolicy | None]:
        after = ""
        parsed = None
        safety = self.store.guard.scan(proposal)
        if not safety.passed:
            proposal = self.quarantine(proposal, safety)
        early_failure = proposal.status != "pending" or not proposal.safety_report.passed
        checks = [EvalCheck(
            name="safety", passed=not early_failure,
            detail="Secret and injection checks passed." if not early_failure else
            "; ".join(proposal.safety_report.findings) or "Proposal is closed.",
        )]
        if not early_failure:
            try:
                _, after, parsed = self.candidate(proposal)
                checks.append(EvalCheck(name="structure_and_revision", passed=True, detail="Structure, budgets, owner and base revision checked."))
            except (ValueError, OSError, RunTerminated) as exc:
                checks.append(EvalCheck(name="structure_and_revision", passed=False, detail=_error(exc)))
            else:
                if isinstance(parsed, TrustPolicy):
                    current, _ = self.store.policy_snapshot()
                    try:
                        self.store.guard.policy(parsed, current, allow_network=allow_network)
                    except ValueError as exc:
                        checks.append(EvalCheck(name="hard_policy_baseline", passed=False, detail=str(exc)))
                    else:
                        checks.append(EvalCheck(name="hard_policy_baseline", passed=True, detail="Hard denies, sandbox and network opt-in checked."))
                checks.append(self._corpus_check(proposal, after, parsed))
                if self.sandbox_check is not None and all(check.passed for check in checks):
                    checks.append(self.sandbox_check(proposal))
        result = proposal.model_copy(update={"eval_report": EvolutionEvalReport(checks=tuple(checks))})
        self.store._save_proposal(result)
        return result, after, parsed

    def _corpus_check(
        self, proposal: Proposal, candidate: str, parsed: Skill | TrustPolicy | None,
    ) -> EvalCheck:
        try:
            cases = self.store.cases()
        except (ValueError, OSError, RunTerminated):
            return EvalCheck(name="feedback_corpus", passed=False, detail="Feedback corpus is unreadable or unsafe.")
        relevant = [
            case for case in cases
            if not case.safety_blocked and case.kind.value != "ephemeral" and _relevant(case, proposal)
        ]
        expected = {rule for case in relevant for rule in case.required_checks}
        if isinstance(parsed, TrustPolicy):
            missing = ["deny_network"] if "deny_network" in expected and parsed.sandbox.network_enabled else []
            return EvalCheck(
                name="feedback_corpus", passed=not missing,
                detail=f"Checked {len(relevant)} relevant cases of {len(cases)}."
                + (" A feedback case requires network to stay disabled." if missing else ""),
            )
        revision = guidance_key(candidate)
        rejected = any(revision in case.rejected_revisions for case in relevant)
        missing = sorted(rule for rule in expected if not _CORPUS_RULES[rule].search(candidate))
        contradicted = [
            focus for focus, pattern in _PROCEDURAL_REGRESSIONS.items()
            if any(not _NEGATION.search(candidate[max(0, match.start() - 32):match.start()])
                   for match in pattern.finditer(candidate))
        ]
        passed = not (rejected or missing or contradicted)
        detail = f"Checked {len(relevant)} relevant cases of {len(cases)} and {len(_PROCEDURAL_REGRESSIONS)} procedural rules."
        if missing:
            detail += f" Drops feedback requirements for {', '.join(rule.replace('_', ' ') for rule in missing)}."
        if rejected:
            detail += " This guidance reproduces a rejected revision."
        if contradicted:
            detail += f" Conflicts with {', '.join(contradicted)} safeguards."
        return EvalCheck(name="feedback_corpus", passed=passed, detail=detail)


def _receipt(proposal: Proposal) -> str:
    return digest(proposal.model_dump_json(exclude={"status", "approval_receipt", "eval_report"}))


def guidance_key(body: str) -> str:
    return digest(" ".join(body.lower().split()))


def _relevant(case: FeedbackCase, proposal: Proposal) -> bool:
    if proposal.skill:
        if digest(proposal.skill.name) in case.skill_keys:
            return True
        tags = " ".join(proposal.skill.scope.tags).lower().split()
        return case.focus in tags
    return case.kind.value == ("memory" if proposal.kind == "memory_update" else "policy")


def _preview(before: str, after: str, label: str) -> str:
    return "".join(unified_diff(
        [line + "\n" for line in before.splitlines()],
        [line + "\n" for line in after.splitlines()],
        fromfile=f"a/{label}", tofile=f"b/{label}",
    ))


def _error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "Invalid proposal structure, field budget, or trust policy schema."
    if isinstance(exc, OSError):
        return "Evolution input could not be safely read."
    if isinstance(exc, RunTerminated):
        return "Unsafe evolution input file."
    message = str(exc)
    return message if EvolutionSafetyGuard().scan(message).passed else "Evolution validation failed."
