"""Graph reliability scoring."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from codeops.core.models import CrossCheckResult, GraphEvidence
from codeops.tools.codegraph_gateway import GraphFile, GraphStatus


class GraphReliabilityReport(BaseModel):
    index_fresh: bool
    indexed_file_coverage: float | None
    parse_error_files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    dynamic_risk: Literal["low", "medium", "high"]
    cross_check_status: Literal[
        "agreement", "partial_agreement", "conflict", "unknown"
    ]
    graph_confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)


class GraphReliabilityLayer:
    """Score whether CodeGraph can be trusted as a context source."""

    def evaluate(
        self,
        *,
        repo_path: Path,
        status: GraphStatus,
        graph_files: list[GraphFile],
        cross_checks: list[CrossCheckResult] | None = None,
    ) -> GraphReliabilityReport:
        cross_checks = cross_checks or []
        raw = status.raw or {}
        indexed_file_coverage = _coverage(repo_path, status, graph_files)
        skipped_files = _skipped_files(repo_path, status, graph_files, raw)
        parse_error_files = [
            str(path) for path in raw.get("parse_error_files", [])
        ]
        cross_check_status = _cross_check_status(cross_checks)
        warnings: list[str] = []

        if not status.available:
            warnings.append(status.message or "CodeGraph unavailable")
        if skipped_files:
            warnings.append("Some repository files were not indexed")
        if parse_error_files:
            warnings.append("CodeGraph reported parse errors")
        if cross_check_status == "conflict":
            warnings.append("CodeGraph conflicted with fallback validation")

        confidence = _confidence(
            status=status,
            coverage=indexed_file_coverage,
            cross_check_status=cross_check_status,
            skipped_files=skipped_files,
            parse_error_files=parse_error_files,
        )

        return GraphReliabilityReport(
            index_fresh=status.index_fresh,
            indexed_file_coverage=indexed_file_coverage,
            skipped_files=skipped_files,
            parse_error_files=parse_error_files,
            dynamic_risk=_dynamic_risk(skipped_files, parse_error_files),
            cross_check_status=cross_check_status,
            graph_confidence=confidence,
            warnings=warnings,
        )

    def graph_evidence(
        self,
        *,
        status: GraphStatus,
        report: GraphReliabilityReport,
        repo_commit: str | None = None,
        codegraph_version: str | None = None,
        cross_checks: list[CrossCheckResult] | None = None,
    ) -> GraphEvidence:
        return GraphEvidence(
            codegraph_version=codegraph_version,
            repo_commit=repo_commit,
            index_fresh=report.index_fresh,
            indexed_file_coverage=report.indexed_file_coverage,
            skipped_files=report.skipped_files,
            parse_error_files=report.parse_error_files,
            target_symbols=[],
            direct_callers=[],
            callees=[],
            affected_tests=[],
            heuristic_edges=[],
            cross_checks=cross_checks or [],
            graph_confidence=report.graph_confidence,
            warnings=[*report.warnings, *([] if status.available else ["degraded"])],
        )


def _coverage(
    repo_path: Path, status: GraphStatus, graph_files: list[GraphFile]
) -> float | None:
    if not status.available:
        return None

    repo_files = _repo_python_files(repo_path)
    if not repo_files:
        return 1.0

    indexed = {file.path for file in graph_files if file.indexed}
    if not indexed:
        return 0.0
    return round(len(indexed.intersection(repo_files)) / len(repo_files), 4)


def _skipped_files(
    repo_path: Path,
    status: GraphStatus,
    graph_files: list[GraphFile],
    raw: dict,
) -> list[str]:
    if not status.available:
        return []
    raw_skipped = raw.get("skipped_files")
    if isinstance(raw_skipped, list):
        return [str(path) for path in raw_skipped]

    repo_files = set(_repo_python_files(repo_path))
    indexed = {file.path for file in graph_files if file.indexed}
    if not indexed:
        return sorted(repo_files)
    return sorted(repo_files - indexed)


def _repo_python_files(repo_path: Path) -> list[str]:
    repo_root = repo_path.resolve()
    files = []
    for path in repo_root.rglob("*.py"):
        if ".git" in path.parts or ".venv" in path.parts:
            continue
        files.append(str(path.relative_to(repo_root)))
    return sorted(files)


def _cross_check_status(
    cross_checks: list[CrossCheckResult],
) -> Literal["agreement", "partial_agreement", "conflict", "unknown"]:
    if not cross_checks:
        return "unknown"
    statuses = {check.status for check in cross_checks}
    if "fail" in statuses:
        return "conflict"
    if statuses == {"pass"}:
        return "agreement"
    return "partial_agreement"


def _confidence(
    *,
    status: GraphStatus,
    coverage: float | None,
    cross_check_status: str,
    skipped_files: list[str],
    parse_error_files: list[str],
) -> float:
    if not status.available:
        return 0.0

    score = 0.25
    if status.index_fresh:
        score += 0.25
    score += 0.30 * (coverage or 0.0)
    score += {
        "agreement": 0.20,
        "partial_agreement": 0.10,
        "unknown": 0.05,
        "conflict": -0.20,
    }[cross_check_status]
    score -= min(len(skipped_files), 5) * 0.03
    score -= min(len(parse_error_files), 5) * 0.05
    return round(max(0.0, min(1.0, score)), 4)


def _dynamic_risk(
    skipped_files: list[str], parse_error_files: list[str]
) -> Literal["low", "medium", "high"]:
    if parse_error_files:
        return "high"
    if skipped_files:
        return "medium"
    return "low"
