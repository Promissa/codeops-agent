"""Issue-to-component routing."""

from pathlib import Path
import re
from typing import Protocol

from pydantic import BaseModel, Field

from codeops.core.models import ProjectProfile, RepoSketch, SymbolRef
from codeops.tools.codegraph_gateway import GraphFile


SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".py",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".kts",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
}
PATH_PATTERN = re.compile(
    r"src[\\/][A-Za-z0-9_./\\-]+\.(?:cpp|hpp|h|cc|cxx|py)"
)
URL_PATTERN = re.compile(r"https?://\S+")
SKIP_PARTS = {
    ".git",
    ".runs",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
DOWNWEIGHT_PARTS = {"docs", "thirdparty", "tests", "test"}
STOP_TERMS = {
    "issue",
    "description",
    "expected",
    "actual",
    "relevant",
    "output",
    "traceback",
    "runtimeerror",
    "exception",
    "openvino",
    "version",
    "windows",
    "model",
    "models",
    "github",
    "https",
    "user",
    "attachments",
    "activate",
    "alt",
    "and",
    "assets",
    "been",
    "can",
    "cloned",
    "com",
    "config",
    "configuration",
    "downloaded",
    "drivers",
    "followed",
    "for",
    "from",
    "git",
    "has",
    "height",
    "image",
    "img",
    "install",
    "instructions",
    "long",
    "main",
    "more",
    "not",
    "org",
    "pip",
    "resolved",
    "run",
    "runtime",
    "runtime_config",
    "running",
    "scripts",
    "src",
    "step",
    "still",
    "than",
    "that",
    "the",
    "time",
    "tried",
    "venv",
    "very",
    "width",
    "will",
    "with",
    "year",
}
MIN_TEST_SCORE = 60.0


class RoutingCandidate(BaseModel):
    path: str
    score: float
    reasons: list[str] = Field(default_factory=list)


class IssueRoutingResult(BaseModel):
    source: str
    components: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    query_terms: list[str] = Field(default_factory=list)
    candidates: list[RoutingCandidate] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    warnings: list[str] = Field(default_factory=list)
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_calls: int = 0


class RoutingProvider(Protocol):
    def enrich_routing(
        self,
        *,
        repo_path: Path,
        issue_text: str,
        project_profile: ProjectProfile,
        repo_sketch: RepoSketch,
        deterministic_result: IssueRoutingResult,
    ) -> IssueRoutingResult:
        """Return an LLM-refined routing result."""


class IssueRouter:
    """Find likely components before patch planning."""

    def route(
        self,
        *,
        repo_path: Path,
        issue_text: str,
        project_profile: ProjectProfile,
        repo_sketch: RepoSketch,
        graph_files: list[GraphFile],
        provider: RoutingProvider | None = None,
    ) -> IssueRoutingResult:
        terms = _query_terms(issue_text)
        candidates = _rank_candidates(repo_path, graph_files, terms, issue_text)
        deterministic = IssueRoutingResult(
            source="deterministic",
            components=_components(candidates[:8]),
            files=_top_source_files(candidates),
            tests=_top_test_files(candidates),
            query_terms=terms,
            candidates=candidates[:80],
            confidence=_confidence(candidates),
            rationale="Ranked repository files from CodeGraph/file inventory against issue and log terms.",
        )
        if provider is None:
            return deterministic

        try:
            enriched = provider.enrich_routing(
                repo_path=repo_path,
                issue_text=issue_text,
                project_profile=project_profile,
                repo_sketch=repo_sketch,
                deterministic_result=deterministic,
            )
        except Exception as exc:  # noqa: BLE001 - provider failures must degrade.
            return deterministic.model_copy(
                update={
                    "warnings": [
                        *deterministic.warnings,
                        f"LLM routing failed: {exc}",
                    ]
                }
            )

        allowed_paths = {candidate.path for candidate in deterministic.candidates}
        files = [
            path
            for path in enriched.files
            if path in allowed_paths or _safe_existing_file(repo_path, path)
        ]
        tests = [
            path
            for path in enriched.tests
            if path in allowed_paths or _safe_existing_file(repo_path, path)
        ]
        if not files:
            files = deterministic.files
        return enriched.model_copy(
            update={
                "source": "llm",
                "files": _dedupe(files)[:3],
                "tests": _dedupe(tests)[:3],
                "query_terms": _dedupe([*enriched.query_terms, *terms]),
                "candidates": deterministic.candidates,
                "confidence": max(enriched.confidence, deterministic.confidence),
            }
        )


def routing_symbols(result: IssueRoutingResult) -> list[SymbolRef]:
    return [
        SymbolRef(symbol=Path(path).stem, path=path, kind="file")
        for path in result.files
    ]


def _query_terms(issue_text: str) -> list[str]:
    searchable_text = URL_PATTERN.sub(" ", issue_text)
    searchable_text = PATH_PATTERN.sub(" ", searchable_text)
    raw_terms = [
        term.lower()
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", searchable_text)
    ]
    terms = [
        term
        for term in raw_terms
        if term not in STOP_TERMS and not term.startswith("http")
    ]
    terms.extend(_paths_from_logs(issue_text))
    if any(term in terms for term in ["clenqueuewritebuffer", "clwaitforevents"]):
        terms.extend(["ocl", "opencl", "intel_gpu"])
    if "gpu" in terms:
        terms.extend(["intel_gpu", "ocl"])
    if "hetero" in terms:
        terms.extend(["hetero", "compiled_model"])
    if "pipeline_parallel" in terms:
        terms.extend(["pipeline", "compiled_model"])
    return _dedupe(terms)[:80]


def _paths_from_logs(issue_text: str) -> list[str]:
    paths = []
    for match in PATH_PATTERN.findall(issue_text):
        paths.append(match.replace("\\", "/").lower())
    return paths


def _rank_candidates(
    repo_path: Path,
    graph_files: list[GraphFile],
    terms: list[str],
    issue_text: str,
) -> list[RoutingCandidate]:
    paths = (
        [graph_file.path for graph_file in graph_files if graph_file.path]
        if graph_files
        else _repo_files(repo_path)
    )
    direct_paths = set(_paths_from_logs(issue_text))
    candidates = []
    for path in paths:
        score, reasons = _score_path(path, terms, direct_paths)
        if score <= 0:
            continue
        candidates.append(RoutingCandidate(path=path, score=score, reasons=reasons))
    return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.path))


def _repo_files(repo_path: Path) -> list[str]:
    files = []
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_path)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        if path.suffix not in SOURCE_SUFFIXES and path.name != "CMakeLists.txt":
            continue
        files.append(rel.as_posix())
    return files


def _score_path(
    path: str, terms: list[str], direct_paths: set[str]
) -> tuple[float, list[str]]:
    path_lower = path.lower()
    path_parts = set(path_lower.replace(".", "/").replace("-", "_").split("/"))
    filename = Path(path_lower).name
    score = 0.0
    reasons = []

    for direct_path in direct_paths:
        if direct_path and direct_path in path_lower:
            score += 100.0
            reasons.append(f"log path matched {direct_path}")

    for term in terms:
        normalized = term.replace("\\", "/")
        if normalized in path_lower:
            boost = 8.0 if normalized in filename else 4.0
            if normalized in path_parts:
                boost += 2.0
            score += boost
            reasons.append(f"path matched {term}")

    if any(part in path_parts for part in DOWNWEIGHT_PARTS):
        score -= 8.0
        reasons.append("downweighted generated/docs/test/thirdparty path")
    if "src/plugins" in path_lower:
        score += 3.0
        reasons.append("source plugin path")
    return score, reasons


def _top_source_files(candidates: list[RoutingCandidate]) -> list[str]:
    source_candidates = [
        candidate for candidate in candidates if not _is_test_file(candidate.path)
    ]
    threshold = (
        60.0 if any(candidate.score >= 80.0 for candidate in candidates) else 0.0
    )
    files = [
        candidate.path
        for candidate in source_candidates
        if candidate.score >= threshold
    ]
    return _dedupe(files)[:3]


def _top_test_files(candidates: list[RoutingCandidate]) -> list[str]:
    return _dedupe(
        candidate.path
        for candidate in candidates
        if _is_test_file(candidate.path) and candidate.score >= MIN_TEST_SCORE
    )[:3]


def _components(candidates: list[RoutingCandidate]) -> list[str]:
    components = []
    for candidate in candidates:
        parts = Path(candidate.path).parts
        if len(parts) >= 3 and parts[0] == "src" and parts[1] == "plugins":
            components.append("/".join(parts[:3]))
        elif len(parts) >= 2:
            components.append("/".join(parts[:2]))
        else:
            components.append(candidate.path)
    return _dedupe(components)[:8]


def _confidence(candidates: list[RoutingCandidate]) -> float:
    if not candidates:
        return 0.0
    top = candidates[0].score
    if top >= 80:
        return 0.75
    if top >= 20:
        return 0.55
    if top >= 8:
        return 0.35
    return 0.15


def _safe_existing_file(repo_path: Path, rel_path: str) -> bool:
    path = (repo_path / rel_path).resolve()
    try:
        path.relative_to(repo_path.resolve())
    except ValueError:
        return False
    return path.is_file()


def _is_test_file(path: str) -> bool:
    lowered = path.lower()
    return (
        "/test/" in f"/{lowered}"
        or "/tests/" in f"/{lowered}"
        or "test_" in Path(lowered).name
    )


def _dedupe(items) -> list:
    return list(dict.fromkeys(items))
