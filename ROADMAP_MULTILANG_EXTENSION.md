# ROADMAP Addendum — Multi-language Support

> Purpose: extend Evidence-aware CodeOps Agent from Python-first to non-Python repositories without weakening evidence, impact-boundary, and safety guarantees.

## 1. Design principle

Do not special-case every language inside the workflow. Keep the orchestrator language-agnostic and move ecosystem-specific behavior into `LanguageProfile` adapters.

```text
Workflow Orchestrator
  -> ProjectDetector
  -> LanguageProfileRegistry
  -> RetrievalService
  -> ImpactEnvelopeBuilder
  -> PatchPolicyChecker
  -> TestRunner
  -> EvidenceWriters
```

The orchestrator should never assume `pytest`, `pyproject.toml`, `src/`, `tests/`, Python import rules, Python coverage, or Python public API semantics.

## 2. New abstractions

### 2.1 ProjectProfile

```python
class ProjectProfile(BaseModel):
    repo_path: Path
    primary_language: str
    secondary_languages: list[str] = []
    frameworks: list[str] = []
    build_systems: list[str] = []
    package_managers: list[str] = []
    test_frameworks: list[str] = []
    language_profiles: list[str] = []
    monorepo: bool = False
    workspace_roots: list[Path] = []
    detected_from: list[str] = []
    warnings: list[str] = []
```

### 2.2 LanguageProfile

```python
class LanguageProfile(Protocol):
    name: str
    file_extensions: set[str]
    manifest_files: set[str]
    lockfiles: set[str]
    source_globs: list[str]
    test_globs: list[str]
    generated_globs: list[str]
    high_risk_globs: list[str]

    def detect(self, repo_path: Path) -> DetectionResult: ...
    def discover_test_commands(self, repo_path: Path) -> list[TestCommand]: ...
    def select_tests(self, repo_path: Path, changed_files: list[str], graph_tests: list[str]) -> list[TestCommand]: ...
    def static_checks(self, repo_path: Path, changed_files: list[str]) -> list[CheckCommand]: ...
    def coverage_command(self, repo_path: Path, tests: list[TestCommand]) -> CoverageCommand | None: ...
    def public_api_diff(self, repo_path: Path, patch_diff: str) -> ApiDiffResult: ...
    def dependency_diff(self, repo_path: Path, patch_diff: str) -> DependencyDiffResult: ...
    def normalize_symbol(self, raw_symbol: str, path: str) -> SymbolRef: ...
```

### 2.3 Command model

```python
class TestCommand(BaseModel):
    id: str
    command: list[str]
    cwd: Path
    scope: Literal["acceptance", "affected", "module", "full"]
    language: str
    timeout_seconds: int = 120
    parse_format: Literal["pytest", "junit_xml", "go_json", "cargo", "npm", "dotnet", "raw"] = "raw"
    env: dict[str, str] = {}
```

All command execution must go through `CommandPolicy`.

## 3. First-class language targets

Implement adapters in this order:

```text
Tier 1:
  JavaScript / TypeScript
  Go
  Rust

Tier 2:
  Java / Kotlin
  C#

Tier 3:
  C / C++
  Ruby
  PHP
```

Rationale: Tier 1 has relatively standardized project metadata and test commands; Tier 2 is common but has more build-system branching; Tier 3 usually requires more project-specific configuration.

## 4. Repository layout changes

Add:

```text
src/codeops/languages/
  __init__.py
  base.py
  registry.py
  detector.py
  python_profile.py
  javascript_profile.py
  go_profile.py
  rust_profile.py
  java_profile.py
  dotnet_profile.py
  cpp_profile.py

src/codeops/tools/
  junit_parser.py
  test_output_parser.py
  api_diff.py

examples/fixtures/
  js_vitest_project/
  ts_express_project/
  go_http_project/
  rust_cli_project/
  java_maven_project/
```

## 5. Detection rules

### 5.1 JavaScript / TypeScript

Detect from:

```text
package.json
package-lock.json / pnpm-lock.yaml / yarn.lock / bun.lockb
tsconfig.json
vite.config.*
next.config.*
```

Default commands:

```text
npm test -- --runInBand        # only if package.json has scripts.test
npm run test                   # fallback if no extra args allowed
npm run typecheck              # if script exists
npm run lint                   # if script exists
npx tsc --noEmit               # only if TypeScript project and allowed
```

Do not run install commands unless explicitly approved.

### 5.2 Go

Detect from:

```text
go.mod
go.work
**/*.go
```

Default commands:

```text
go test ./...
go test ./path/to/pkg
 go test -json ./path/to/pkg
 go test -cover ./path/to/pkg
 go vet ./...
```

Use package-level affected tests rather than file-level tests when possible.

### 5.3 Rust

Detect from:

```text
Cargo.toml
Cargo.lock
src/lib.rs
src/main.rs
```

Default commands:

```text
cargo test
cargo test -p <package>
cargo test <name_filter>
cargo check
cargo clippy --all-targets --all-features
```

Reject autonomous changes to `Cargo.toml` / `Cargo.lock` unless dependency changes are explicitly allowed.

### 5.4 Java / Kotlin

Detect from:

```text
pom.xml
build.gradle
build.gradle.kts
settings.gradle
settings.gradle.kts
src/main/java
src/test/java
src/main/kotlin
src/test/kotlin
```

Default commands:

```text
mvn test
mvn -Dtest=ClassName test
./gradlew test
./gradlew :module:test
```

Prefer wrapper scripts when present, but run them only through `CommandPolicy`.

### 5.5 C# / .NET

Detect from:

```text
*.sln
*.csproj
Directory.Build.props
global.json
```

Default commands:

```text
dotnet test
dotnet test path/to/TestProject.csproj
dotnet build --no-restore
```

Reject edits to `.csproj` / `.sln` dependency sections unless explicitly approved.

### 5.6 C / C++

Detect from:

```text
CMakeLists.txt
Makefile
meson.build
configure.ac
*.c / *.cc / *.cpp / *.h / *.hpp
```

Default commands must be project-specific. The adapter may discover but should not invent a build command if none is obvious.

Possible safe commands:

```text
cmake --build build
ctest --test-dir build --output-on-failure
make test
```

For C/C++, require user-provided build instructions unless a build directory and test command are already discoverable.

## 6. ImpactEnvelope changes

Extend `ImpactEnvelope`:

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
    allow_build_config_change: bool = False
    language: str | None = None
    build_system: str | None = None
    risk_level: Literal["low", "medium", "high", "critical"]
    requires_human_approval: bool
```

Add default forbidden files:

```text
package.json
package-lock.json
pnpm-lock.yaml
yarn.lock
bun.lockb
go.mod
go.sum
Cargo.toml
Cargo.lock
pom.xml
build.gradle
build.gradle.kts
*.csproj
*.sln
CMakeLists.txt
Makefile
Dockerfile
.github/workflows/**
```

These files may be read, but edits require approval.

## 7. VerificationPlan changes

`VerificationPlan` should no longer store raw strings only. Store typed commands:

```python
class VerificationPlan(BaseModel):
    acceptance_tests: list[TestCommand]
    affected_tests: list[TestCommand]
    module_tests: list[TestCommand]
    full_tests: list[TestCommand]
    static_checks: list[CheckCommand]
    behavior_diff_required: bool
    coverage_required: bool
```

## 8. Test selection strategy

Use four signals:

```text
1. CodeGraph affected tests
2. LanguageProfile naming convention
3. manifest/build-system test discovery
4. previous coverage map, if available
```

Never rely only on one signal for non-Python projects.

## 9. Patch generation constraints

For the first multi-language version:

```text
Allowed:
  small bug fixes
  test additions
  pure source-level fixes
  no dependency changes
  no generated file edits
  no formatter-only repo-wide changes

Disallowed:
  package manager changes
  build-system rewrites
  framework migrations
  database migrations
  CI workflow changes
  large refactors
```

## 10. Multi-language acceptance tests

Add fixture smoke tests:

```text
1. JS/Vitest: function handles null input, npm test passes.
2. TS/Express: route handler validates missing field, typecheck passes.
3. Go: handler returns 400 for bad query parameter, go test ./... passes.
4. Rust: parser returns Err for invalid token, cargo test passes.
5. Java/Maven: service handles empty string, mvn test passes.
```

Each fixture should include:

```text
- one failing issue file;
- one small source bug;
- one local test command;
- no external services;
- no dependency install during test;
- a reference expected final behavior.
```

## 11. Codex implementation sequence

### Phase M0 — Introduce ProjectProfile and LanguageProfile

Goal: decouple workflow from Python assumptions.

Acceptance:

```text
- ProjectDetector detects Python, JS/TS, Go, Rust from fixture files.
- Existing Python tests still pass.
- Workflow stores ProjectProfile in TaskState.
```

### Phase M1 — Typed command model

Goal: replace string-only test commands with `TestCommand` / `CheckCommand`.

Acceptance:

```text
- Python fixture still runs pytest through TestCommand.
- CommandPolicy validates command arrays, not shell strings.
- No arbitrary shell command execution is introduced.
```

### Phase M2 — JS/TS adapter

Goal: support package.json projects without dependency installation.

Acceptance:

```text
- Detects npm/pnpm/yarn/bun lockfiles.
- Uses package.json scripts only.
- Runs test script on fixture.
- Rejects package.json edits by default.
```

### Phase M3 — Go adapter

Goal: support Go modules.

Acceptance:

```text
- Detects go.mod.
- Runs go test ./... or package-scoped go test.
- Parses go test -json enough to determine pass/fail.
- Rejects go.mod/go.sum edits by default.
```

### Phase M4 — Rust adapter

Goal: support Cargo projects.

Acceptance:

```text
- Detects Cargo.toml.
- Runs cargo test and cargo check.
- Rejects Cargo.toml/Cargo.lock edits by default.
```

### Phase M5 — Java adapter

Goal: support Maven first, Gradle second.

Acceptance:

```text
- Detects pom.xml and build.gradle.
- Runs mvn test for Maven fixture.
- Runs ./gradlew test only if wrapper exists and CommandPolicy allows it.
- Rejects pom.xml/build.gradle edits by default.
```

### Phase M6 — Multi-language evaluation

Goal: compare Python-only baseline against multi-language support.

Metrics:

```text
- project detection accuracy
- test-command discovery accuracy
- affected-test precision/recall
- solve rate by language
- cost per solved issue by language
- unsafe edit rejection rate
```

## 12. Recommended Codex prompt

```text
Read ROADMAP.md and this multi-language addendum. Implement Phase M0 only. Do not change patch generation logic yet. Add ProjectProfile, LanguageProfile, ProjectDetector, and registry support. Keep Python behavior unchanged. Add fixture-level tests for language detection for Python, JavaScript/TypeScript, Go, and Rust. Run the existing test suite and the new detection tests. Report changed files, commands run, failures, and the next phase.
```
