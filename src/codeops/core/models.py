"""Core data contracts for CodeOps Agent."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


TaskStatus = Literal[
    "created",
    "parsed",
    "contracted",
    "graph_checked",
    "retrieved",
    "planned",
    "patched",
    "tested",
    "reviewed",
    "done",
    "failed",
    "needs_clarification",
]


class TaskRequest(BaseModel):
    """CLI request payload."""

    model_config = ConfigDict(frozen=True)

    repo_path: Path
    issue_path: Path
    out_path: Path
    retrieval_mode: str = "codegraph"
    no_network: bool = False


class AcceptanceExample(BaseModel):
    input: str
    expected: str
    notes: str | None = None


class AcceptanceContract(BaseModel):
    requirement_id: str
    summary: str
    user_visible_before: str | None
    user_visible_after: str | None
    examples: list[AcceptanceExample] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    ambiguity_questions: list[str] = Field(default_factory=list)
    status: Literal["ready", "needs_clarification"]


class ModuleCapsule(BaseModel):
    name: str
    path: str
    responsibility: str
    public_symbols: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high", "critical"] = "low"


class RepoSketch(BaseModel):
    repo_root: Path
    commit: str | None
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    entrypoints: list[str] = Field(default_factory=list)
    core_modules: list[ModuleCapsule] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    test_map: dict[str, list[str]] = Field(default_factory=dict)
    high_risk_paths: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class SymbolRef(BaseModel):
    symbol: str
    path: str
    kind: str | None = None
    line: int | None = None


class GraphEdge(BaseModel):
    source: SymbolRef
    target: SymbolRef
    relationship: str
    confidence: float = Field(ge=0.0, le=1.0)


class CrossCheckResult(BaseModel):
    source: str
    status: Literal["pass", "fail", "unknown"]
    detail: str


class GraphEvidence(BaseModel):
    codegraph_version: str | None
    repo_commit: str | None
    index_fresh: bool
    indexed_file_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    skipped_files: list[str] = Field(default_factory=list)
    parse_error_files: list[str] = Field(default_factory=list)
    target_symbols: list[SymbolRef] = Field(default_factory=list)
    direct_callers: list[SymbolRef] = Field(default_factory=list)
    callees: list[SymbolRef] = Field(default_factory=list)
    affected_tests: list[str] = Field(default_factory=list)
    heuristic_edges: list[GraphEdge] = Field(default_factory=list)
    cross_checks: list[CrossCheckResult] = Field(default_factory=list)
    graph_confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)


class ImpactEnvelope(BaseModel):
    target_symbols: list[SymbolRef] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    affected_tests: list[str] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)
    max_files_changed: int = 3
    max_loc_changed: int = 120
    allow_dependency_change: bool = False
    allow_public_api_change: bool = False
    risk_level: Literal["low", "medium", "high", "critical"]
    requires_human_approval: bool


class PatchPlan(BaseModel):
    hypothesis: str
    edit_strategy: str
    files_to_edit: list[str] = Field(default_factory=list)
    tests_to_add_or_update: list[str] = Field(default_factory=list)
    expected_behavior_change: str
    risk_notes: list[str] = Field(default_factory=list)


class VerificationPlan(BaseModel):
    acceptance_tests: list[str] = Field(default_factory=list)
    affected_tests: list[str] = Field(default_factory=list)
    module_tests: list[str] = Field(default_factory=list)
    full_tests: list[str] = Field(default_factory=list)
    static_checks: list[str] = Field(default_factory=list)
    behavior_diff_required: bool
    coverage_required: bool


class TestResult(BaseModel):
    command: str
    passed: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


class CostTrace(BaseModel):
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    codegraph_calls: int = 0
    raw_file_reads: int = 0
    test_runs: int = 0
    wall_time_seconds: float = 0.0
    estimated_cost_usd: float | None = None


class TaskState(BaseModel):
    task_id: str
    repo_path: Path
    issue_path: Path | None = None
    issue_text: str
    run_dir: Path
    status: TaskStatus = "created"
    acceptance_contract: AcceptanceContract | None = None
    graph_evidence: GraphEvidence | None = None
    impact_envelope: ImpactEnvelope | None = None
    patch_plan: PatchPlan | None = None
    verification_plan: VerificationPlan | None = None
    patch_diff: str | None = None
    test_results: list[TestResult] = Field(default_factory=list)
    evidence_matrix_path: Path | None = None
    intent_manifest_path: Path | None = None
    cost_trace: CostTrace = Field(default_factory=CostTrace)
