"""On-disk evolution records and immutable run inputs."""

from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

SKILL_BODY_BYTES = 15_000
MEMORY_LIMITS = {"MEMORY.md": 2200, "USER.md": 1375}
MemoryName = Literal["MEMORY.md", "USER.md"]
SkillStatus = Literal["active", "stale", "archived", "pinned", "demoted"]
ProposalKind = Literal["skill_create", "skill_patch", "memory_update", "policy_proposal"]
Focus = Literal["verification", "boundary", "routing", "workflow", "memory", "policy"]
Digest = str
CorpusRule = Literal["baseline_replay", "caller_coverage", "edit_scope", "routing_crosscheck", "deny_network"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def check_skill_name(name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) or len(name) > 80:
        raise ValueError("Use a lowercase, hyphenated skill name of at most 80 characters.")
    if re.search(r"(?:^|-)(?:pr|issue|session|run|ticket)-?\d+(?:-|$)|(?:^|-)(?:today|yesterday)(?:-|$)", name):
        raise ValueError("Use a reusable skill name without a session, issue, PR, or date reference.")
    return name


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SkillScope(Record):
    globs: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


class EvidenceStats(Record):
    loads: int = Field(default=0, ge=0)
    verified: int = Field(default=0, ge=0)
    regressed: int = Field(default=0, ge=0)
    inconclusive: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    human_rejections: int = Field(default=0, ge=0)


class SkillDraft(Record):
    name: str
    description: str = Field(min_length=1, max_length=120)
    scope: SkillScope
    body: str = Field(min_length=1)

    _check_name = field_validator("name")(check_skill_name)

    @field_validator("body")
    @classmethod
    def check_body(cls, body: str) -> str:
        if not body.strip() or len(body.encode("utf-8")) > SKILL_BODY_BYTES:
            raise ValueError("Skill body must contain guidance and fit within 15,000 UTF-8 bytes.")
        return body


class SkillHeader(Record):
    name: str
    description: str = Field(min_length=1, max_length=120)
    scope: SkillScope
    owner: Literal["human", "agent"]
    status: SkillStatus = "active"
    source_runs: tuple[str, ...] = ()
    revision_hash: Digest = ""
    created_at: AwareDatetime = Field(default_factory=utc_now)
    last_used_at: AwareDatetime | None = None
    restored_at: AwareDatetime | None = None
    evidence_stats: EvidenceStats = Field(default_factory=EvidenceStats)
    status_reason: str = ""
    outcome_runs: tuple[Digest, ...] = Field(default=(), repr=False)
    rejection_runs: tuple[Digest, ...] = Field(default=(), repr=False)

    _check_name = field_validator("name")(check_skill_name)

    def provenance(self) -> dict:
        return self.model_dump(mode="json", exclude={"outcome_runs", "rejection_runs"})


class Skill(SkillHeader):
    body: str

    _check_body = field_validator("body")(SkillDraft.check_body)

    def draft(self) -> SkillDraft:
        return SkillDraft.model_validate(self.model_dump(include={"name", "description", "scope", "body"}))


class MemorySnapshot(Record):
    model_config = ConfigDict(frozen=True)

    memory: str = ""
    user: str = ""


class SafetyReport(Record):
    findings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.findings


class EvalCheck(Record):
    name: str
    passed: bool
    detail: str


class EvolutionEvalReport(Record):
    checks: tuple[EvalCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


class Proposal(Record):
    proposal_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    kind: ProposalKind
    rationale: str
    diff_preview: str = ""
    source_run_id: str
    source_event_id: str | None = None
    base_revision_hash: Digest | None = None
    skill: SkillDraft | None = None
    memory_target: MemoryName | None = None
    replacement: str | None = None
    safety_report: SafetyReport = Field(default_factory=SafetyReport)
    eval_report: EvolutionEvalReport | None = None
    approval_receipt: Digest | None = None
    status: Literal["pending", "approved", "rejected"] = "pending"


class FeedbackKind(StrEnum):
    AUTO = "auto"
    WORKFLOW = "workflow"
    POLICY = "policy"
    MEMORY = "memory"
    EPHEMERAL = "ephemeral"


class FeedbackCase(Record):
    case_id: Digest
    kind: FeedbackKind
    focus: Focus
    verdict: Literal["verified", "regressed", "inconclusive", "blocked"]
    skill_keys: tuple[Digest, ...] = ()
    safety_blocked: bool = False
    rejected_guidance: bool = False
    rejected_revisions: tuple[Digest, ...] = ()
    required_checks: tuple[CorpusRule, ...] = ()
