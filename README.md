# Evidence-aware CodeOps Agent (Trust Harness)

**Evidence-aware CodeOps Agent** is a local-first **Trust Harness** for coding agents. It improves human trust in agent-authored engineering work through four integrated layers:

1. **Upfront Trust Contracting & Quiet Linux Sandbox** — Concentrate permission approval before execution, then run the agent and its test subprocesses inside an isolated Bubblewrap (`bwrap`) workspace with private `/tmp`, disabled networking by default, and zero mutation of the source repository.
2. **Agent-Agnostic Evidence Gate** — Verify any unified diff (from built-in patching or external agents such as Claude Code, Codex, OpenCode, or Aider) using actual net workspace diffs, baseline-vs-candidate differential testing, and CodeGraph + runtime caller-exposure analysis.
3. **Offline Visual Trace & Honest Summary** — Emit a secret-filtered `trace_events.jsonl` stream and a zero-dependency, self-contained `trace_report.html` showing the verdict (`verified` / `regressed` / `inconclusive` / `blocked`), interactive execution timeline, impact/coverage graph, data boundary flow, and node-level feedback hooks.
4. **Evidence-Guided Self-Evolution** — Turn human feedback on runs or trace nodes into staged procedural skills (`SKILL.md`), bounded memory (`MEMORY.md` / `USER.md`), and policy proposals guarded by prompt-injection/secret checks, ownership and read-before-write locks, offline eval gates, and outcome-driven curation.

---

## Core Design Principles

- **Pre-flight consent, minimal runtime friction:** Generate `.codeops/trust_policy.yaml` deterministically from repository structure (`ProjectDetector` + language profiles), preview the exact `TaskTrustContract` before execution, and run quietly inside the approved boundary.
- **Orthogonal boundaries:** Separate *local read visibility*, *remote outbound disclosure*, *workspace write/export budget*, and *ephemeral temp storage* so tools can read manifests to run tests without leaking secrets or mutating protected files.
- **Measure actual effects, not self-reported diffs:** Compute the net diff between a frozen baseline and the post-execution candidate workspace, catching hidden side-effect writes made by tests or subprocesses.
- **Truthful verdicts:** `inconclusive` and `blocked` are never upgraded to `verified`. Already-passing ("fake-green") tests, test-only edits, or unexercised affected callers produce `inconclusive` with concrete next steps.
- **Evidence-attributed evolution:** Skills and memories never self-modify active rules silently. Proposals stage in `.codeops/evolution/pending/`, require offline gate checks and approval, and are automatically demoted by the curator if they correlate with regressions or human rejections.

---

## Architecture Overview

```text
Issue / External Diff
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│ 1. Trust Policy & Pre-Flight (src/codeops/trust/)        │
│    • .codeops/trust_policy.yaml (read/outbound/write/cmd)│
│    • TaskTrustContract preview + escalation budget       │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│ 2. Isolated Linux Runtime (src/codeops/runtime/)         │
│    • Filtered workspace snapshot (omits denied/secret)   │
│    • Bubblewrap (bwrap): private /tmp tmpfs, quota,      │
│      --unshare-net, process-group timeout & cleanup      │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│ 3. Evidence Gate & Workflow (src/codeops/gate/, workflow/)│
│    • Actual net workspace diff + PatchPolicy             │
│    • CodeGraph + Ripgrep + AST EffectMap & caller probe  │
│    • Baseline vs. Candidate DifferentialVerifier         │
│    • Honest verdict: verified|regressed|inconclusive|    │
│                      blocked                             │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│ 4. Visual Trace & Reporting (src/codeops/reporting/)     │
│    • Secret-filtered trace_events.jsonl                  │
│    • Self-contained offline trace_report.html (SVG/JS)   │
│    • evidence_matrix.md, intent_manifest.md, summary     │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│ 5. Controlled Self-Evolution (src/codeops/evolution/)    │
│    • ReviewForkCritic -> pending/ proposals + corpus     │
│    • Safety/injection/ownership/read-before-write guards │
│    • EvolutionEvaluator + outcome-driven EvolutionCurator│
└──────────────────────────────────────────────────────────┘
```

### Package Layout (`src/codeops/`)

| Package | Responsibility |
|---|---|
| `trust/` | `TrustPolicy`, `TrustPolicyBuilder`, `PreFlightPlanner`, `TaskTrustContract`, `EscalationRequest` |
| `runtime/` | `BwrapSandboxBackend`, `SandboxWorkspace`, `RunLifecycleState`, quota mounts, stale-run registry & cleanup |
| `gate/` | `WorkspaceDiff`, `EffectMap`, `DifferentialVerifier`, `EvidenceGate`, `EvidenceGateRunner`, Python runtime caller probe |
| `reporting/` | `TraceRecorder` (`trace_events.jsonl`), honest verdict resolver, offline `trace_report.html` renderer |
| `evolution/` | `EvolutionStore`, `EvolutionLoader`, `EvolutionSafetyGuard`, `ReviewForkCritic`, `EvolutionEvaluator`, `EvolutionCurator` |
| `workflow/` | `WorkflowOrchestrator`, language-aware `VerificationPlanBuilder` |
| `agents/` | `RequirementParser`, `IssueRouter`, `PatchPlanner`, `PatchGenerator`, `Reviewer`, `ManifestWriter`, LLM providers |
| `retrieval/` | `RepoSketchBuilder`, `GraphReliabilityLayer`, `ImpactEnvelopeBuilder`, `LocalEmbeddingRetriever`, RRF fusion |
| `safety/` | `CommandPolicy` (argv allowlist, no shell), `PatchPolicy`, `SecretFilter` |
| `languages/` | `ProjectDetector` & adapters for Python, JavaScript/TypeScript, Go, Rust, Java/Kotlin |
| `tools/` | `CodeGraphGateway`, `GitTool`, `PatchTool`, `TestRunner`, `RipgrepTool`, `FilesystemTool` |
| `evals/` | Fixture & multi-language evaluation runner (`EvalRunner`) |

---

## Requirements & Installation

- **Python:** `>= 3.13`
- **Core dependencies:** `pydantic`, `typer` (zero extra third-party runtime dependencies required)
- **Linux sandbox (recommended):** `bwrap` (Bubblewrap) for `--sandbox bwrap`
- **Optional:** `codegraph` CLI for call-graph/impact enrichment (falls back deterministically to AST + `RipgrepTool` when unavailable)

```bash
# Install with uv
uv sync

# Run the test suite
uv run pytest -q
```

---

## Quick Start

### 1. Initialize & Preview Trust Policy (Upfront Consent)

Generate a repository trust policy draft deterministically from detected language profiles and test commands:

```bash
uv run codeops trust-init --repo /path/to/repo
```

Preview the pre-flight scope (visible workspace, hard-denied paths, outbound rules, write budget, and allowlisted commands) for a specific issue:

```bash
uv run codeops trust-plan \
  --repo /path/to/repo \
  --issue /path/to/issue.md
```

Hard-denied patterns (such as `**/.env*`, `**/id_rsa*`, `**/id_ed25519*`, `**/*.pem`, `**/*.key`, `**/.ssh`, `**/.git`, `**/.codeops/evolution`, `**/.codeops/trust_policy.yaml`) are physically excluded from sandboxed workspace snapshots and cannot be removed by evolution proposals.

### 2. Run in a Quiet Linux Sandbox

```bash
uv run codeops run \
  --repo /path/to/repo \
  --issue examples/issues/csv_trailing_empty_column.md \
  --out /tmp/codeops-run-01 \
  --sandbox bwrap \
  --approve-trust
```

> Note: Managed sandbox runs require `--out` to be outside the target repository so artifacts never pollute the source tree.

### 3. Verify Any External Diff (Agent-Agnostic Evidence Gate)

Validate a patch produced by any coding agent or human developer without invoking the built-in patch generator:

```bash
uv run codeops verify \
  --repo /path/to/repo \
  --diff /path/to/candidate.diff \
  --issue /path/to/issue.md \
  --out /tmp/codeops-verify-01 \
  --sandbox bwrap \
  --approve-trust
```

Exit codes for `codeops verify`:
- `0` — `verified` (net diff within policy, relevant fail-to-pass differential proof, zero regressions, zero unresolved caller blind spots)
- `2` — `inconclusive` (fake-green / only `still_passing` tests, unobserved affected callers, or insufficient evidence)
- `1` — `regressed` or `blocked` (regression, side-effect edit / policy violation, or trust/sandbox denial)

### 4. Inspect or Regenerate the Offline Visual Trace Report

Every run writes `trace_events.jsonl` and a self-contained `trace_report.html` into `--out`, even on early termination or policy denial. You can regenerate the HTML offline at any time:

```bash
uv run codeops report --run /tmp/codeops-verify-01
```

`trace_report.html` contains five offline sections (zero external CDN calls, secret-filtered, XSS-escaped):
1. **Trust Summary Card** — verdict badge, lifecycle/termination reason, differential transitions (`fixed` / `regressed` / `still_passing` / `still_failing` / `unobserved`), net diff check status, actionable next steps, and cost/latency metrics.
2. **Interactive Execution DAG / Timeline** — clickable stage/event SVG with durations, policy allow/deny outcomes, and structured inspector details.
3. **Impact & Evidence Graph** — edited symbols, callers, tests, and unobserved caller blind spots color-coded by verification outcome.
4. **Data & Boundary Flow** — visible workspace scope, excluded/denied boundaries, outbound policy, and redactions.
5. **Active Evolved Rules & Feedback Panel** — provenance of loaded skills/memory plus a node-anchored feedback command generator for `codeops feedback`.

### 5. Feedback & Controlled Self-Evolution Loop

Give feedback on a completed run or a specific `event_id` from `trace_report.html`:

```bash
uv run codeops feedback \
  --run /tmp/codeops-verify-01 \
  --node evt_0012 \
  --kind workflow \
  --comment "When editing parser modules, always verify downstream loader callers."
```

Review, evaluate against offline gates, and approve or manage evolved skills:

```bash
# Inspect stored skills and staged proposals
uv run codeops evolve list --repo /path/to/repo

# Run offline structural, safety/injection, ownership, and corpus regression gates
uv run codeops evolve eval --repo /path/to/repo --proposal <proposal_id>

# Approve (re-verifies safety, freshness hash, and gates before activation) or reject
uv run codeops evolve approve --repo /path/to/repo --proposal <proposal_id>
uv run codeops evolve reject --repo /path/to/repo --proposal <proposal_id>

# Lifecycle governance (pin, unpin, archive, restore, revert, curate)
uv run codeops evolve curate --repo /path/to/repo
uv run codeops evolve pin --repo /path/to/repo --skill <skill-name>
uv run codeops evolve revert --repo /path/to/repo --skill <skill-name>
```

#### Self-Evolution Guardrails

- **Bounded artifacts:** `SKILL.md` body `<= 15,000` UTF-8 bytes, routing description `<= 120` chars; `MEMORY.md <= 2,200` chars and `USER.md <= 1,375` chars (loaded as a frozen snapshot at run start; overflow fails closed to force curation).
- **Ownership & freshness:** Agent review forks cannot modify `owner: human` skills or edit `trust_policy.yaml` directly; patches must match the fresh on-disk `revision_hash` (read-before-write).
- **Injection & secret defense:** `EvolutionSafetyGuard` blocks prompt-injection, sandbox/policy-bypass text, and raw secrets across feedback, issues, and logs before staging or activation.
- **Outcome-driven Curator:** Unused skills age `active -> stale (30d) -> archived (90d)` (recoverable via `restore`; `pinned` exempt). Active agent-owned skills with `loads >= 3` and `(regressed + human_rejections) / loads >= 0.5` are automatically `demoted`.

---

## Run Artifacts

Each run writes structured artifacts under `--out`:

| Artifact | Purpose |
|---|---|
| `trust_contract.yaml` | Approved pre-flight trust contract and boundaries |
| `acceptance_contract.yaml` | Parsed user-visible intent, examples, and invariants |
| `impact_envelope.yaml` | Allowed files, edit budgets, and risk level |
| `workspace_diff.json` / `patch.diff` | Actual net workspace diff (and candidate diff) |
| `effect_map.json` | Modified symbols, callers, observed vs. unobserved blind spots |
| `differential_results.json` | Baseline vs. candidate test outcome transitions |
| `evidence_gate.json` | Final adjudicated verdict, violations, and next steps |
| `trace_events.jsonl` | Secret-filtered structured event stream |
| `trace_report.html` | Self-contained offline visual trace & trust report |
| `evidence_matrix.md` / `intent_manifest.md` / `final_report.md` | Reviewer-facing Markdown summaries |
| `cost_trace.json` / `task.json` | Tool/LLM/test counts, latency, and serialized `TaskState` |

---

## Optional LLM Provider Support

LLM calls are disabled by default (`--llm-provider none`) unless configured in `.codeops/llm.yaml` or `CODEOPS_LLM_PROVIDER`. Deterministic parsing, routing, sandboxed verification, visual reporting, and self-evolution all run offline without API keys.

### Persisting Kimi (OpenCode-aligned)

Initialize a non-secret `.codeops/llm.yaml` (never stores raw keys; excluded from sandbox reads by `HARD_READ_DENIES`):

```bash
# Kimi K2.8 Preview via Kimi Code (matches OpenCode's `kimi-code-plan-cn` -> model ID `kimi-for-coding`, KIMI_API_KEY)
uv run codeops llm-init --repo /path/to/repo --provider kimi-code-plan-cn --model kimi-2.8

# Kimi Platform China (matches OpenCode's `moonshotai-cn`: https://api.moonshot.cn/v1, MOONSHOT_API_KEY)
uv run codeops llm-init --repo /path/to/repo --provider moonshotai-cn --model kimi-k2.6

# Kimi Platform Global (matches OpenCode's `moonshotai`: https://api.moonshot.ai/v1, MOONSHOT_API_KEY)
uv run codeops llm-init --repo /path/to/repo --provider moonshotai --model kimi-k2.6

# Inspect effective provider config (shows whether api_key_env is set, never prints secrets)
uv run codeops llm-show --repo /path/to/repo
```

> Note: Kimi K2.8 Preview is served on **Kimi Code** (`kimi-code-plan-cn` / `kimi-code-plan-global`) under the API model ID `kimi-for-coding` (`--model kimi-2.8` / `kimi-k2.8` automatically normalizes to `kimi-for-coding`). Kimi Open Platform (`moonshotai-cn` / `moonshotai`) exposes `kimi-k2.6`, `kimi-k3`, `kimi-k2.7-code`, and `kimi-k2.7-code-highspeed`.

To override per run (or disable with `--llm-provider none`):

```bash
# Kimi Platform (MOONSHOT_API_KEY, default model kimi-k2.6)
export MOONSHOT_API_KEY=sk-...
uv run codeops run \
  --repo /path/to/repo \
  --issue /path/to/issue.md \
  --out /tmp/kimi_demo \
  --llm-provider moonshotai-cn

# Kimi Code (KIMI_API_KEY, Anthropic-compatible endpoint https://api.kimi.com/coding/)
export KIMI_API_KEY=sk-...
uv run codeops run \
  --repo /path/to/repo \
  --issue /path/to/issue.md \
  --out /tmp/kimi_code_demo \
  --llm-provider kimi-code
```

---

## Evaluation & Verification Suite

Run the built-in multi-language or fixture evaluation suites:

```bash
uv run codeops eval --tasks examples/eval_tasks.jsonl --mode repo_sketch_codegraph
uv run codeops eval --tasks examples/multilang_eval_tasks.jsonl --mode multilang_detection
```

Run the full automated test suite (157 passing tests covering real Bubblewrap isolation, secret/XSS-safe offline trace reports, differential & caller-exposure evidence gating, and controlled self-evolution):

```bash
uv run pytest -q
```
