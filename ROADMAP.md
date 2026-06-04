# Evidence-aware CodeOps Agent — Codex Execution Roadmap

> This file is intended to be copied into the repository as `ROADMAP.md` or `PLANS.md` and used as the main implementation plan for Codex.
>
> Project codename: **Evidence-aware CodeOps Agent**
>
> Core thesis: **do not build another AI PR generator. Build an evidence-producing, impact-bounded, cost-aware code maintenance agent.**

---

## 0. How Codex should use this document

### 0.1 Execution mode

Codex should treat this document as an implementation contract, not as background reading.

When asked to implement this project, Codex must:

1. Read this roadmap before editing code.
2. Implement the phases in order unless the user explicitly requests a later phase.
3. Keep each change small and reviewable.
4. After each phase, run the relevant tests and report:
   - files changed;
   - commands run;
   - tests passed / failed;
   - unresolved issues;
   - next recommended task.
5. Never skip safety gates just to make a demo pass.
6. Avoid broad refactors unless the current phase explicitly requires them.

### 0.2 Recommended Codex prompt

Use this prompt when starting a fresh Codex session:

```text
Read ROADMAP.md completely. Implement the next incomplete phase only. Keep the diff small. Do not implement future phases unless necessary for the current phase. After editing, run the tests listed in the phase's Acceptance Criteria and summarize changed files, test results, and remaining work.
```

For phase-specific work:

```text
Read ROADMAP.md. Implement Phase N only. Follow the file layout, data contracts, and acceptance criteria exactly. Do not introduce unrelated abstractions. If a dependency or command is unavailable, add a clear TODO and implement a safe fallback.
```

### 0.3 Suggested companion `AGENTS.md`

Add this to repository root as `AGENTS.md`:

```markdown
# Agent instructions

This repository implements Evidence-aware CodeOps Agent.

Before making changes, read `ROADMAP.md` and follow the current phase's acceptance criteria.

Rules:
- Keep diffs small and phase-scoped.
- Do not implement broad refactors unless explicitly requested.
- Do not bypass safety gates.
- Do not let the agent directly execute arbitrary shell commands; use tool wrappers.
- Prefer deterministic tools before LLM calls.
- Always run the relevant tests before reporting completion.
- When uncertain about CodeGraph output, use fallback validation: ripgrep, LSP/static checks, tests, and coverage.
```

---

## 1. Project summary

### 1.1 Problem

AI-generated PRs are increasingly cheap to produce but expensive to review. The failure mode is not only bad code. The deeper problem is that maintainers cannot quickly answer:

- What is the AI trying to change?
- Why is this file or symbol involved?
- What requirement does each diff hunk satisfy?
- What tests prove the change?
- What behavior is intentionally unchanged?
- What is the impact radius?
- How much did this cost?

### 1.2 Goal

Build a local-first code maintenance agent that turns an issue or failing test log into a **small, evidence-backed, impact-bounded patch candidate**.

The system must produce not only a diff, but also:

- `AcceptanceContract`: explicit interpretation of the requirement;
- `GraphEvidence`: CodeGraph-backed context and impact analysis;
- `ImpactEnvelope`: allowed files, symbols, and behavior boundaries;
- `VerificationPlan`: tests and static checks required before PR draft;
- `EvidenceMatrix`: requirement-to-diff-to-test mapping;
- `IntentManifest`: human-readable PR explanation;
- `CostTrace`: token, tool-call, test-runtime, and iteration cost.

### 1.3 Non-goals for MVP

Do not implement these in the first working version:

- automatic merge;
- automatic push to protected branches;
- multi-language support beyond Python-first;
- enterprise authentication and RBAC;
- full SWE-bench execution;
- arbitrary shell execution by the agent;
- autonomous dependency upgrades;
- large architectural refactors.

### 1.4 Core principles

```text
No contract, no patch.
No impact envelope, no edit.
No evidence, no verified status.
No latest dependencies in production.
CodeGraph gives candidates, not truth.
Tests and contracts decide correctness.
Local small models retrieve; strong models edit only when necessary.
```

---

## 2. Target MVP

### 2.1 Supported input

MVP supports:

1. A local Python repository.
2. A natural-language issue or failing pytest log.
3. Optional reproduction snippet.
4. Optional user-provided acceptance criteria.

Example input:

```text
Bug: CSV parser crashes when a row contains trailing empty columns.

Reproduction:
parse_csv("a,b,c\n1,2,\n")

Expected:
Should return ["1", "2", None] for the second row.

Actual:
IndexError: list index out of range.
```

### 2.2 Expected output

For each task, produce a run directory:

```text
.runs/<task_id>/
  task.json
  acceptance_contract.yaml
  graph_evidence.json
  impact_envelope.yaml
  patch_plan.yaml
  verification_plan.yaml
  patch.diff
  test_results.json
  evidence_matrix.md
  intent_manifest.md
  cost_trace.json
  final_report.md
```

### 2.3 MVP user command

Prefer a simple CLI first:

```bash
codeops run \
  --repo ./sandbox_repos/mini_data_pipeline \
  --issue ./examples/issues/csv_trailing_empty_column.md \
  --out .runs/demo_csv_bug
```

Optional later:

```bash
codeops serve
```

---

## 3. High-level architecture

```text
User / Issue / Failing Log
        |
        v
CLI or FastAPI Entry Point
        |
        v
Workflow Orchestrator
        |
        +--> Requirement Parser
        +--> Acceptance Contract Builder
        +--> Graph Reliability Layer
        +--> RepoSketch Router
        +--> Retrieval Service
        |       +--> CodeGraphGateway
        |       +--> ripgrep / lexical search
        |       +--> optional local embeddings / reranker
        +--> Impact Envelope Builder
        +--> Patch Planner
        +--> Patch Generator
        +--> Patch Policy Checker
        +--> Test Selector
        +--> Docker / Local Test Runner
        +--> Reviewer
        +--> Intent Manifest Generator
        +--> Cost Tracker
        |
        v
Run Artifacts + PR Draft
```

### 3.1 Architecture decisions

| Area | MVP choice | Later upgrade |
|---|---|---|
| Interface | CLI | FastAPI + Streamlit / Next.js |
| Workflow | explicit Python state machine | LangGraph |
| Code graph | `colbymchenry/codegraph` via CLI wrapper | sidecar service / MCP wrapper |
| Storage | file-based run artifacts + SQLite optional | Postgres |
| Vector search | disabled initially | FAISS / sqlite-vec / pgvector |
| Test execution | local subprocess with allowlist | Docker sandbox |
| LLM | pluggable provider interface | model routing + local reranker |
| Eval | small fixture tasks | larger benchmark suite |

---

## 4. Repository layout

Create this structure:

```text
codeops-agent/
  README.md
  ROADMAP.md
  AGENTS.md
  pyproject.toml
  .gitignore

  src/codeops/
    __init__.py
    cli.py

    core/
      __init__.py
      models.py
      state.py
      paths.py
      errors.py

    workflow/
      __init__.py
      orchestrator.py
      nodes.py

    tools/
      __init__.py
      codegraph_gateway.py
      ripgrep_tool.py
      filesystem_tool.py
      patch_tool.py
      test_runner.py
      git_tool.py
      coverage_tool.py

    retrieval/
      __init__.py
      repo_sketch.py
      graph_reliability.py
      impact_envelope.py
      local_embeddings.py
      fusion.py

    agents/
      __init__.py
      requirement_parser.py
      patch_planner.py
      patch_generator.py
      reviewer.py
      manifest_writer.py

    safety/
      __init__.py
      patch_policy.py
      command_policy.py
      secret_filter.py

    evals/
      __init__.py
      runner.py
      metrics.py
      tasks.py

  examples/
    issues/
      csv_trailing_empty_column.md
    fixtures/
      mini_data_pipeline/
        pyproject.toml
        src/mini_data_pipeline/
          __init__.py
          parser.py
          loader.py
        tests/
          test_parser.py
          test_loader.py

  tests/
    test_models.py
    test_patch_policy.py
    test_codegraph_gateway.py
    test_impact_envelope.py
    test_workflow_smoke.py
```

---

## 5. Core data contracts

Use Pydantic models for all cross-module data. Keep model fields explicit and serializable.

### 5.1 `TaskState`

```python
class TaskState(BaseModel):
    task_id: str
    repo_path: Path
    issue_path: Path | None = None
    issue_text: str
    run_dir: Path
    status: Literal[
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
    ] = "created"
    acceptance_contract: AcceptanceContract | None = None
    graph_evidence: GraphEvidence | None = None
    impact_envelope: ImpactEnvelope | None = None
    patch_plan: PatchPlan | None = None
    verification_plan: VerificationPlan | None = None
    patch_diff: str | None = None
    test_results: list[TestResult] = []
    evidence_matrix_path: Path | None = None
    intent_manifest_path: Path | None = None
    cost_trace: CostTrace = CostTrace()
```

### 5.2 `AcceptanceContract`

```python
class AcceptanceContract(BaseModel):
    requirement_id: str
    summary: str
    user_visible_before: str | None
    user_visible_after: str | None
    examples: list[AcceptanceExample]
    invariants: list[str]
    non_goals: list[str]
    ambiguity_questions: list[str]
    status: Literal["ready", "needs_clarification"]
```

Rule:

```text
If status == needs_clarification, the workflow must stop before patch generation.
```

### 5.3 `RepoSketch`

```python
class RepoSketch(BaseModel):
    repo_root: Path
    commit: str | None
    languages: list[str]
    frameworks: list[str]
    entrypoints: list[str]
    core_modules: list[ModuleCapsule]
    test_commands: list[str]
    test_map: dict[str, list[str]]
    high_risk_paths: list[str]
    generated_at: datetime
```

### 5.4 `GraphEvidence`

```python
class GraphEvidence(BaseModel):
    codegraph_version: str | None
    repo_commit: str | None
    index_fresh: bool
    indexed_file_coverage: float | None
    skipped_files: list[str]
    parse_error_files: list[str]
    target_symbols: list[SymbolRef]
    direct_callers: list[SymbolRef]
    callees: list[SymbolRef]
    affected_tests: list[str]
    heuristic_edges: list[GraphEdge]
    cross_checks: list[CrossCheckResult]
    graph_confidence: float
    warnings: list[str]
```

Rule:

```text
If graph_confidence < 0.60, do not auto-generate patch.
If 0.60 <= graph_confidence < 0.80, require fallback validation before patch.
If graph_confidence >= 0.80, CodeGraph can be primary context source.
```

### 5.5 `ImpactEnvelope`

```python
class ImpactEnvelope(BaseModel):
    target_symbols: list[SymbolRef]
    allowed_files: list[str]
    affected_files: list[str]
    affected_tests: list[str]
    forbidden_changes: list[str]
    max_files_changed: int = 3
    max_loc_changed: int = 120
    allow_dependency_change: bool = False
    allow_public_api_change: bool = False
    risk_level: Literal["low", "medium", "high", "critical"]
    requires_human_approval: bool
```

### 5.6 `PatchPlan`

```python
class PatchPlan(BaseModel):
    hypothesis: str
    edit_strategy: str
    files_to_edit: list[str]
    tests_to_add_or_update: list[str]
    expected_behavior_change: str
    risk_notes: list[str]
```

### 5.7 `VerificationPlan`

```python
class VerificationPlan(BaseModel):
    acceptance_tests: list[str]
    affected_tests: list[str]
    module_tests: list[str]
    full_tests: list[str]
    static_checks: list[str]
    behavior_diff_required: bool
    coverage_required: bool
```

### 5.8 `EvidenceMatrix`

Markdown table mapping requirement to evidence:

```markdown
| Requirement | Changed File / Symbol | Why changed | Verification | Status |
|---|---|---|---|---|
| R1 | src/parser.py::parse_row | Handles trailing empty fields | test_trailing_empty_column | Passed |
```

### 5.9 `CostTrace`

```python
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
```

---

## 6. Workflow

### 6.1 Main workflow

```text
load_task
  -> parse_requirement
  -> build_acceptance_contract
  -> stop_if_ambiguous
  -> check_graph_health
  -> build_or_load_repo_sketch
  -> retrieve_context
  -> build_graph_evidence
  -> build_impact_envelope
  -> plan_patch
  -> generate_or_update_acceptance_test
  -> run_acceptance_test_on_original_repo
  -> generate_patch
  -> apply_patch_safely
  -> enforce_patch_policy
  -> select_tests
  -> run_tests
  -> analyze_failures_or_continue
  -> reviewer_check
  -> generate_evidence_matrix
  -> generate_intent_manifest
  -> write_final_report
```

### 6.2 Failure handling

| Failure | Required behavior |
|---|---|
| Ambiguous requirement | Stop and emit `needs_clarification` report |
| CodeGraph unavailable | Use fallback retrieval, mark graph evidence degraded |
| Graph confidence too low | Do not auto-patch unless user explicitly overrides |
| Patch exceeds envelope | Reject patch and ask planner for smaller plan |
| Tests fail | Allow at most 2 repair iterations in MVP |
| Dangerous file touched | Require human approval |
| New dependency added | Reject unless explicitly allowed |
| Secret detected in context | Redact and block external LLM call |

### 6.3 Patch repair loop

```text
run_tests failed
  -> summarize focused failure logs only
  -> update patch plan
  -> generate minimal repair patch
  -> apply patch
  -> rerun affected tests
```

Hard limits:

```text
max_repair_iterations = 2
max_changed_files = ImpactEnvelope.max_files_changed
max_loc_changed = ImpactEnvelope.max_loc_changed
```

---

## 7. CodeGraph-first context design

### 7.1 Role of CodeGraph

Use `colbymchenry/codegraph` as a low-cost structural context layer.

Allowed uses:

- find candidate symbols;
- inspect callers and callees;
- estimate impact radius;
- find affected tests;
- build `RepoSketch`;
- reduce raw file reads and grep loops.

Forbidden assumption:

```text
Do not treat CodeGraph output as final correctness proof.
```

### 7.2 CodeGraphGateway

All access to CodeGraph must go through `CodeGraphGateway`.

Do not let agents directly call arbitrary `codegraph` CLI commands.

Required methods:

```python
class CodeGraphGateway:
    def status(self, repo_path: Path) -> GraphStatus: ...
    def version(self) -> str | None: ...
    def files(self, repo_path: Path) -> list[GraphFile]: ...
    def search(self, repo_path: Path, query: str, limit: int = 20) -> list[SymbolHit]: ...
    def callers(self, repo_path: Path, symbol: str, depth: int = 1) -> list[SymbolRef]: ...
    def callees(self, repo_path: Path, symbol: str, depth: int = 1) -> list[SymbolRef]: ...
    def impact(self, repo_path: Path, symbol: str, depth: int = 2) -> ImpactReport: ...
    def affected_tests(self, repo_path: Path, changed_files: list[str]) -> list[str]: ...
```

### 7.3 Safe command policy

Allow only:

```text
codegraph status
codegraph files --json
codegraph query/search with --json if supported
codegraph callers with --json if supported
codegraph callees with --json if supported
codegraph impact with --json if supported
codegraph affected --stdin
codegraph sync
```

Disallow agent-triggered:

```text
codegraph install
codegraph uninstall
codegraph uninit
npm install latest
npx package@latest
arbitrary shell
```

### 7.4 Graph Reliability Layer

Implement `GraphReliabilityLayer`:

Inputs:

- CodeGraph status;
- file list;
- target language support;
- skipped files;
- parse errors if available;
- cross-check results from `rg` and tests.

Output:

```python
GraphReliabilityReport(
    index_fresh=True,
    indexed_file_coverage=0.96,
    parse_error_files=[],
    skipped_files=[],
    dynamic_risk="medium",
    cross_check_status="partial_agreement",
    graph_confidence=0.82,
    warnings=[]
)
```

### 7.5 Cross-check strategy

For every target symbol, check at least two of:

```text
CodeGraph result
ripgrep result
LSP/static analyzer result
stack trace result
test coverage result
file naming convention
```

If CodeGraph and another signal conflict:

```text
Expand impact envelope and downgrade confidence.
```

### 7.6 ImpactEnvelope rule

Agent may edit only inside `ImpactEnvelope.allowed_files`.

Patch policy must reject:

- files outside envelope;
- public API signature changes unless allowed;
- new dependencies unless allowed;
- formatting-only churn in unrelated files;
- broad refactors;
- changes to secrets, lockfiles, auth, billing, migrations, or infra without approval.

### 7.7 CodeGraph versioning

Do not use `latest` in reproducible demo or CI.

Use:

```json
"@colbymchenry/codegraph": "<pinned-version>"
```

Maintain two channels:

```text
stable: pinned, used by demo and tests
canary: latest, used only for compatibility testing
```

Upgrade flow:

```text
install candidate version
-> run CodeGraph fixture contract tests
-> run workflow smoke tests
-> compare retrieval recall and cost
-> update pinned version only if all pass
```

---

## 8. RepoSketch design

`RepoSketch` is the project thumbnail used to reduce cost.

### 8.1 Purpose

Instead of sending full repository context to the LLM, build a compact sketch:

```text
project structure
entrypoints
core modules
public symbols
high fan-in symbols
risky paths
test commands
test map
CodeGraph health summary
```

### 8.2 Storage

```text
.codeops/
  repo_sketch.json
  repo_sketch.md
  module_capsules/
    parser.md
    api.md
    database.md
  symbol_capsules.jsonl
  test_map.json
  risk_hotspots.json
```

### 8.3 `repo_sketch.md` format

```markdown
# RepoSketch

## Project
- Name:
- Commit:
- Languages:
- Frameworks:

## Entrypoints
- `src/main.py`: CLI entrypoint

## Core modules
| Module | Path | Responsibility | Public symbols | Risk |
|---|---|---|---|---|

## Test commands
- `pytest -q`

## Test map
| Source file | Likely tests |
|---|---|

## Risk hotspots
- `src/auth/`: requires human approval
- `src/db/migrations/`: no autonomous edits
```

### 8.4 Context pyramid

Use context escalation:

```text
L0 RepoSketch
L1 Module Capsule
L2 Symbol Context
L3 Raw File
```

Rules:

```text
Start with L0.
Use L1 when routing is uncertain.
Use L2 for patch planning.
Use L3 only when generating or validating patch.
```

---

## 9. Optional local small-model retrieval

This is not required for the first MVP. Add after the CodeGraph baseline works.

### 9.1 Goal

Use a local embedding/reranker model to improve natural-language issue to symbol/module retrieval without sending large context to a remote model.

### 9.2 Model role

Local small models should:

- embed symbol capsules;
- embed module capsules;
- retrieve semantically related code areas;
- rerank candidates from CodeGraph and lexical search;
- summarize stable module capsules if safe.

They should not:

- directly generate high-risk patches;
- bypass tests;
- override the impact envelope;
- decide final correctness.

### 9.3 Retrieval architecture

```text
Issue text
  -> query parser
  -> parallel retrieval
       + CodeGraph structural search
       + ripgrep lexical search
       + local embedding search
       + test-name search
  -> RRF fusion
  -> local reranker
  -> top-k symbol context
  -> patch planner
```

### 9.4 Symbol capsule format

```json
{
  "symbol_id": "src/parser.py::parse_csv",
  "type": "function",
  "path": "src/parser.py",
  "summary": "Parse CSV input into normalized rows.",
  "calls": ["parse_row", "normalize_field"],
  "called_by": ["load_dataset"],
  "tests": ["tests/test_parser.py"],
  "source_hash": "sha256..."
}
```

### 9.5 Fusion algorithm

Use Reciprocal Rank Fusion first:

```python
def reciprocal_rank_fusion(rankings, k=60):
    scores = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

---

## 10. Safety and quality gates

### 10.1 Patch budget

Default limits:

```yaml
max_changed_files: 3
max_changed_lines: 120
new_dependencies_allowed: false
public_api_change_allowed: false
format_unrelated_files_allowed: false
```

### 10.2 High-risk paths

Require human approval before editing:

```text
**/auth/**
**/billing/**
**/payment/**
**/security/**
**/crypto/**
**/migrations/**
**/infra/**
**/.github/workflows/**
**/Dockerfile
**/docker-compose*.yml
**/pyproject.toml
**/package.json
**/package-lock.json
**/requirements*.txt
```

### 10.3 Secret filtering

Before sending any context to an LLM or writing run artifacts, redact:

```text
API keys
private keys
JWTs
.env values
cloud credentials
database URLs
passwords
customer data samples
```

Implement `secret_filter.py` with regex-based first pass and allow future integration with tools such as `gitleaks`.

### 10.4 Command policy

Agent-generated commands must go through `CommandPolicy`.

Allow MVP commands:

```text
pytest ...
python -m pytest ...
ruff check .
mypy src
git diff
git status
git apply --check
git apply
codegraph status/query/files/callers/callees/impact/affected/sync through gateway only
```

Reject:

```text
rm -rf
curl | sh
wget | sh
sudo
chmod -R
npm install without explicit approval
pip install without explicit approval
git push
git reset --hard without explicit approval
network scanners
arbitrary shell generated by LLM
```

### 10.5 Verification levels

| Risk | Required verification |
|---|---|
| Low | acceptance test + affected tests |
| Medium | acceptance test + affected tests + module tests + static checks |
| High | all above + full tests + human approval |
| Critical | do not auto-patch in MVP |

---

## 11. Implementation phases

## Phase 0 — Project scaffold

### Goal

Create a runnable Python package with CLI, tests, and fixture repo.

### Tasks

1. Create repository layout from Section 4.
2. Add `pyproject.toml` with dependencies:
   - `pydantic`;
   - `typer` or `click`;
   - `pytest`;
   - `rich` optional;
   - no heavy LLM dependency yet.
3. Implement `src/codeops/cli.py` with:

```bash
codeops --help
codeops run --repo <path> --issue <path> --out <path>
```

4. Add fixture repo `examples/fixtures/mini_data_pipeline`.
5. Add example issue file.
6. Add smoke test for CLI import and model serialization.

### Acceptance criteria

```bash
pytest -q
codeops --help
```

Both must pass.

---

## Phase 1 — Core models and run artifacts

### Goal

Implement data contracts and artifact writing.

### Tasks

1. Implement `core/models.py`.
2. Implement `core/paths.py` for run directory layout.
3. Implement JSON/YAML/Markdown artifact writer.
4. Implement `TaskState` lifecycle.
5. Add tests for serialization round-trip.

### Acceptance criteria

```bash
pytest tests/test_models.py -q
```

Artifacts must be written to:

```text
.runs/<task_id>/
```

---

## Phase 2 — Deterministic tool layer

### Goal

Build safe wrappers before adding LLM or CodeGraph logic.

### Tasks

Implement:

- `filesystem_tool.py`
  - safe read within repo;
  - path traversal prevention;
  - file size limits.
- `ripgrep_tool.py`
  - call `rg` if available;
  - fallback to Python search.
- `git_tool.py`
  - current commit;
  - changed files;
  - diff;
  - apply patch check.
- `patch_tool.py`
  - `git apply --check`;
  - apply patch;
  - compute changed files and changed LOC.
- `test_runner.py`
  - run allowlisted commands;
  - timeout;
  - capture stdout/stderr.
- `command_policy.py`
  - allow/reject command validation.

### Acceptance criteria

```bash
pytest tests/test_patch_policy.py -q
pytest tests/test_workflow_smoke.py -q
```

No arbitrary shell execution should be possible from workflow code.

---

## Phase 3 — CodeGraphGateway MVP

### Goal

Wrap CodeGraph behind stable internal API.

### Tasks

1. Implement `tools/codegraph_gateway.py`.
2. Use subprocess wrapper with timeout and cwd pinned to repo.
3. Implement:
   - `status`;
   - `version`;
   - `files`;
   - `search` or `query`;
   - `callers`;
   - `callees`;
   - `impact`;
   - `affected_tests`.
4. If CodeGraph is missing, return a degraded status instead of crashing the whole workflow.
5. Add mock provider for tests.
6. Add fixture-based tests that can run even without CodeGraph installed.

### Acceptance criteria

```bash
pytest tests/test_codegraph_gateway.py -q
```

Manual check, if CodeGraph is installed:

```bash
cd examples/fixtures/mini_data_pipeline
codegraph init -i || true
codegraph status
```

The workflow must still work in degraded mode when CodeGraph is unavailable.

---

## Phase 4 — RepoSketch and Graph Reliability Layer

### Goal

Build project thumbnail and graph confidence scoring.

### Tasks

1. Implement `retrieval/repo_sketch.py`.
2. Generate `.codeops/repo_sketch.json` and `.codeops/repo_sketch.md`.
3. Implement `retrieval/graph_reliability.py`.
4. Compute:
   - index freshness;
   - indexed file coverage if possible;
   - skipped files;
   - cross-check agreement;
   - graph confidence.
5. Implement `GraphEvidence` artifact writing.
6. Add tests for confidence thresholds.

### Acceptance criteria

```bash
pytest tests/test_impact_envelope.py -q
```

CLI should be able to write:

```text
.runs/<task_id>/graph_evidence.json
.runs/<task_id>/repo_sketch.md
```

---

## Phase 5 — Requirement parsing and AcceptanceContract

### Goal

Convert natural language issue into an explicit contract.

### Tasks

1. Implement `agents/requirement_parser.py`.
2. MVP can use deterministic parsing plus optional LLM stub.
3. Extract:
   - bug summary;
   - reproduction;
   - expected behavior;
   - actual behavior;
   - invariants;
   - ambiguity questions.
4. If expected behavior is missing, mark as `needs_clarification`.
5. Write `acceptance_contract.yaml`.

### Acceptance criteria

Input issue:

```text
Bug: CSV parser crashes when a row contains trailing empty columns.
Expected: Should return None for trailing empty field.
Actual: IndexError.
```

Must produce:

```yaml
status: ready
summary: ...
```

Missing expected behavior must produce:

```yaml
status: needs_clarification
```

---

## Phase 6 — ImpactEnvelope builder and patch policy

### Goal

Prevent uncontrolled edits.

### Tasks

1. Implement `retrieval/impact_envelope.py`.
2. Build allowed files from:
   - target symbols;
   - CodeGraph impact;
   - tests from affected tests;
   - fallback grep results.
3. Implement `safety/patch_policy.py`.
4. Validate:
   - changed files subset;
   - changed LOC budget;
   - high-risk paths;
   - dependency changes;
   - public API change placeholder.
5. Add tests for policy rejection.

### Acceptance criteria

A patch touching an unrelated file must be rejected.

A patch adding dependency must be rejected unless explicitly allowed.

---

## Phase 7 — Patch planning and generation

### Goal

Generate a minimal patch plan and patch diff.

### Tasks

1. Implement `agents/patch_planner.py`.
2. Implement `agents/patch_generator.py`.
3. MVP approach:
   - allow a simple rule-based demo patch for fixture repo;
   - provide LLM provider interface for future model integration.
4. Patch generator must output unified diff, not directly overwrite files.
5. Apply via `PatchTool` only.
6. Always run `PatchPolicy` after apply.

### Acceptance criteria

For the fixture CSV issue, the system should generate or apply a minimal patch affecting only:

```text
src/mini_data_pipeline/parser.py
tests/test_parser.py
```

Policy must reject overbroad patch.

---

## Phase 8 — Test selection and verification

### Goal

Run focused tests first and build verification evidence.

### Tasks

1. Implement `VerificationPlan` builder.
2. Select tests from:
   - acceptance test;
   - CodeGraph affected tests;
   - naming convention;
   - module test fallback.
3. Implement `test_runner.py` integration.
4. Store structured test results.
5. Add optional coverage collection placeholder.

### Acceptance criteria

For fixture repo:

```bash
pytest tests/test_parser.py -q
```

must run and results must be saved to:

```text
.runs/<task_id>/test_results.json
```

---

## Phase 9 — Reviewer, EvidenceMatrix, IntentManifest

### Goal

Make the PR understandable to humans.

### Tasks

1. Implement `agents/reviewer.py`.
2. Implement `agents/manifest_writer.py`.
3. Generate:
   - `evidence_matrix.md`;
   - `intent_manifest.md`;
   - `final_report.md`.
4. Reviewer checks:
   - patch size;
   - allowed files;
   - tests present;
   - graph confidence;
   - unresolved ambiguity;
   - failed tests.

### Acceptance criteria

`intent_manifest.md` must include:

```markdown
# Intent
# Behavior Change
# Root Cause Hypothesis
# Graph Evidence
# Impact Envelope
# Tests Run
# Risks
# Reviewer Checklist
```

`evidence_matrix.md` must include requirement-to-test mapping.

---

## Phase 10 — Workflow orchestrator

### Goal

Wire phases into one runnable command.

### Tasks

1. Implement `workflow/orchestrator.py`.
2. Implement nodes in `workflow/nodes.py`.
3. Support run status updates.
4. Stop safely on ambiguity, low graph confidence, policy violation, or failed tests.
5. Add smoke integration test.

### Acceptance criteria

This command should complete on fixture repo:

```bash
codeops run \
  --repo examples/fixtures/mini_data_pipeline \
  --issue examples/issues/csv_trailing_empty_column.md \
  --out .runs/demo_csv_bug
```

Expected artifacts:

```text
acceptance_contract.yaml
graph_evidence.json
impact_envelope.yaml
patch_plan.yaml
patch.diff
test_results.json
evidence_matrix.md
intent_manifest.md
final_report.md
cost_trace.json
```

---

## Phase 11 — Cost tracing and ablation eval

### Goal

Show that CodeGraph-first context reduces cost without hiding quality regressions.

### Tasks

1. Implement `evals/tasks.py`.
2. Implement `evals/runner.py`.
3. Implement metrics:
   - solve rate;
   - retrieval top-k recall;
   - graph confidence;
   - raw file reads;
   - CodeGraph calls;
   - test runs;
   - estimated cost;
   - cost per solved issue.
4. Add baseline modes:
   - `no_graph`;
   - `codegraph_only`;
   - `repo_sketch_codegraph`;
   - `repo_sketch_codegraph_affected_tests`.
5. Generate `eval_report.md`.

### Acceptance criteria

Run:

```bash
codeops eval --tasks examples/eval_tasks.jsonl --mode repo_sketch_codegraph
```

Output:

```text
.runs/eval_<timestamp>/eval_report.md
```

---

## Phase 12 — Optional local embeddings and reranker

### Goal

Improve semantic retrieval while keeping cost local.

### Tasks

1. Implement `retrieval/local_embeddings.py` with pluggable provider.
2. Implement symbol capsule builder.
3. Store embeddings in local FAISS or SQLite-based store.
4. Implement RRF fusion.
5. Add mode flag:

```bash
codeops run --retrieval hybrid_local
```

6. Do not make embeddings required for MVP.

### Acceptance criteria

If embedding dependencies are missing, workflow must gracefully fall back to CodeGraph + ripgrep.

---

## Phase 13 — Hardening

### Goal

Improve reliability and security before presenting as a serious project.

### Tasks

1. Containerize CodeGraph sidecar.
2. Mount source read-only for CodeGraph.
3. Add no-network option.
4. Add dependency pinning.
5. Add `CODEGRAPH_VERSION` recording.
6. Add fixture contract tests for CodeGraph upgrade.
7. Add secret scanning before context export.
8. Add high-risk path approval gate.

### Acceptance criteria

System must produce a security section in `final_report.md`:

```markdown
## Safety Checks
- Command policy: passed
- Patch policy: passed
- Secret filter: passed
- High-risk paths: none touched
- CodeGraph version: ...
```

---

## 12. Fixture repo design

Create a small repo under `examples/fixtures/mini_data_pipeline`.

### 12.1 Initial buggy parser

```python
# examples/fixtures/mini_data_pipeline/src/mini_data_pipeline/parser.py

def parse_csv(text: str) -> list[list[str | None]]:
    rows = []
    for line in text.strip().splitlines():
        rows.append(parse_row(line))
    return rows


def parse_row(line: str) -> list[str | None]:
    parts = line.split(",")
    # Intentional bug: drops trailing empty column and may create downstream mismatch.
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return [normalize_field(part) for part in parts]


def normalize_field(value: str) -> str | None:
    if value == "NULL":
        return None
    return value
```

### 12.2 Expected fix

Trailing empty fields should become `None`, while quoted-empty handling can be left out of MVP unless implemented.

Expected added test:

```python
def test_trailing_empty_column_returns_none():
    assert parse_csv("a,b,c\n1,2,\n") == [["a", "b", "c"], ["1", "2", None]]
```

### 12.3 Existing tests

Include tests for:

- normal rows;
- `NULL` value;
- loader using parser.

---

## 13. PR output template

`intent_manifest.md` should follow this structure:

```markdown
# Intent

Fix CSV parser behavior for trailing empty columns.

# Behavior Change

Before:
`parse_csv("a,b,c\n1,2,\n")` dropped the final column or caused downstream mismatch.

After:
`parse_csv("a,b,c\n1,2,\n")` returns `[["a", "b", "c"], ["1", "2", None]]`.

# Root Cause Hypothesis

`parse_row` treated trailing empty fields as removable instead of preserving them as explicit empty fields.

# Graph Evidence

| Symbol | File | Evidence |
|---|---|---|
| `parse_row` | `src/mini_data_pipeline/parser.py` | matched parser behavior and direct caller `parse_csv` |
| `parse_csv` | `src/mini_data_pipeline/parser.py` | public parser entrypoint |

# Impact Envelope

Allowed files:
- `src/mini_data_pipeline/parser.py`
- `tests/test_parser.py`

Forbidden changes:
- public API signature change
- dependency change
- unrelated formatting

# Tests Run

- `pytest tests/test_parser.py -q`

# Evidence Matrix

See `evidence_matrix.md`.

# Risks

Low to medium. This touches empty-field semantics.

# Reviewer Checklist

- [ ] Confirm trailing empty field should map to `None`.
- [ ] Confirm existing parser behavior remains unchanged.
- [ ] Confirm no unrelated files were modified.
```

---

## 14. Evaluation plan

### 14.1 Metrics

| Metric | Definition |
|---|---|
| Solve rate | Percentage of tasks where required tests pass |
| Retrieval top-k recall | Whether correct file/symbol appears in top-k candidates |
| Graph confidence | Computed reliability score |
| Raw file reads | Count of full file reads |
| Tool calls | Total deterministic tool calls |
| CodeGraph calls | CodeGraph-specific calls |
| Test runs | Number of test commands run |
| Cost per task | Estimated model/tool cost |
| Cost per solved issue | Total cost divided by solved tasks |
| Patch size | Changed files and changed LOC |
| Reviewability | Whether IntentManifest and EvidenceMatrix generated successfully |

### 14.2 Ablation modes

```text
Mode A: no_graph
  Use ripgrep + raw file reads only.

Mode B: codegraph_only
  Use CodeGraph for structural retrieval, no RepoSketch.

Mode C: repo_sketch_codegraph
  Use cached RepoSketch first, then CodeGraph.

Mode D: repo_sketch_codegraph_affected_tests
  Add affected test selection.

Mode E: hybrid_local_optional
  Add local embedding/reranker if available.
```

### 14.3 Eval report table

```markdown
| Mode | Solve Rate | Avg Tokens | Tool Calls | Raw Reads | Test Runtime | Cost / Solved |
|---|---:|---:|---:|---:|---:|---:|
| no_graph | TBD | TBD | TBD | TBD | TBD | TBD |
| codegraph_only | TBD | TBD | TBD | TBD | TBD | TBD |
| repo_sketch_codegraph | TBD | TBD | TBD | TBD | TBD | TBD |
```

Do not fabricate numbers. Generate them from actual runs.

---

## 15. README positioning

The README should describe the project as:

```text
Evidence-aware CodeOps Agent is a local-first agent that converts issues into small, tested patch candidates. Unlike ordinary AI PR tools, it produces explicit acceptance contracts, graph-backed impact analysis, patch boundaries, verification evidence, and cost traces.
```

### 15.1 Interview explanation

Use this explanation:

```text
The main problem I am solving is not just code generation. AI-generated PRs are cheap, but review attention is expensive. My system makes every AI patch explainable and bounded: first it builds an Acceptance Contract from the issue, then uses CodeGraph and fallback validation to build an Impact Envelope, then generates a minimal patch, runs affected tests, and produces an Evidence Matrix and Intent Manifest for the reviewer.

I use CodeGraph to reduce context cost, but I do not trust it as a correctness oracle. Its output is cross-checked with ripgrep, tests, coverage, and patch policy. The final status is not 'AI says fixed'; it is 'verified against explicit acceptance criteria'.
```

---

## 16. Known limitations

Be explicit about limitations:

- CodeGraph is static analysis and can miss dynamic behavior.
- Python dynamic imports, reflection, monkey patching, and configuration-driven calls may degrade graph confidence.
- Passing tests does not prove absolute correctness.
- The MVP is Python-first and fixture-focused.
- Local small models improve retrieval, not final correctness.
- Full security requires containerized execution and stricter permission controls.
- Human approval is still required for high-risk changes.

---

## 17. Immediate next task for Codex

Start with Phase 0.

Use this exact instruction:

```text
Implement Phase 0 from ROADMAP.md. Create the Python package scaffold, CLI entrypoint, fixture repo, example issue, and smoke tests. Keep the implementation minimal and do not implement CodeGraph or LLM integration yet. Run pytest -q and show the changed files and test result.
```

After Phase 0 passes, continue phase by phase.

---

## 18. External references for human maintainer

These are context references; implementation must not depend on them being available at runtime.

- CodeGraph repository: https://github.com/colbymchenry/codegraph
- CodeGraph docs: https://colbymchenry.github.io/codegraph/
- OpenAI Codex `AGENTS.md` guide: https://developers.openai.com/codex/guides/agents-md
- OpenAI Codex best practices: https://developers.openai.com/codex/learn/best-practices
- OpenAI Codex `PLANS.md` article: https://developers.openai.com/cookbook/articles/codex_exec_plans

