"""Shared contracts for language profiles."""

from pathlib import Path
from typing import Iterator, Protocol

from pydantic import BaseModel, Field


IGNORED_DIRS = {
    ".codeops",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
}


class DetectionResult(BaseModel):
    profile_name: str
    primary_language: str
    confidence: float = Field(ge=0.0, le=1.0)
    secondary_languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    build_systems: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    monorepo: bool = False
    workspace_roots: list[Path] = Field(default_factory=list)
    detected_from: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LanguageProfile(Protocol):
    name: str
    file_extensions: set[str]
    manifest_files: set[str]
    lockfiles: set[str]
    source_globs: list[str]
    test_globs: list[str]
    generated_globs: list[str]
    high_risk_globs: list[str]

    def detect(self, repo_path: Path) -> DetectionResult | None:
        """Return detection metadata when this profile matches the repository."""


def iter_repo_files(repo_path: Path) -> Iterator[Path]:
    """Yield ordinary repository files, skipping common generated trees."""
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_path)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        yield path
