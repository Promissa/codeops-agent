# Agent instructions

This repository implements **Evidence-aware CodeOps Agent**.

Before making changes, read `ROADMAP.md` and follow the current phase's acceptance criteria.

## Rules

- Keep diffs small and phase-scoped.
- Do not implement future phases unless explicitly requested.
- Do not perform broad refactors unless the roadmap phase requires them.
- Do not bypass safety gates.
- Do not let the agent directly execute arbitrary shell commands; use tool wrappers.
- Prefer deterministic tools before LLM calls.
- Always run the relevant tests before reporting completion.
- Treat CodeGraph as a candidate context source, not a correctness oracle.
- When CodeGraph output is uncertain, validate with fallback signals: ripgrep, static checks, tests, coverage, and patch policy.
- No patch should be generated without an AcceptanceContract and ImpactEnvelope.
- No PR-style result should be marked verified without an EvidenceMatrix, IntentManifest, and test results.

## Default task instruction

When asked to continue the project:

1. Read `ROADMAP.md`.
2. Identify the next incomplete phase.
3. Implement that phase only.
4. Run the phase's acceptance tests.
5. Report changed files, tests run, results, and remaining work.
