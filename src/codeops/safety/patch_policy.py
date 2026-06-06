"""Patch policy enforcement."""

from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from codeops.core.models import ImpactEnvelope
from codeops.tools.patch_tool import PatchSummary, PatchTool


DEPENDENCY_PATTERNS = (
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "requirements*.txt",
)
HIGH_RISK_PATTERNS = (
    "*/auth/*",
    "*/billing/*",
    "*/payment/*",
    "*/security/*",
    "*/crypto/*",
    "*/migrations/*",
    "*/infra/*",
    ".github/workflows/*",
    "Dockerfile",
    "docker-compose*.yml",
)


class PatchPolicyResult(BaseModel):
    allowed: bool
    violations: list[str] = Field(default_factory=list)
    summary: PatchSummary


class PatchPolicy:
    """Reject patches outside the current ImpactEnvelope."""

    def validate(
        self,
        patch_diff: str,
        impact_envelope: ImpactEnvelope,
    ) -> PatchPolicyResult:
        summary = PatchTool(repo_path=Path(".")).summarize(patch_diff)
        violations: list[str] = []

        allowed_files = set(impact_envelope.allowed_files)
        for path in summary.changed_files:
            if path not in allowed_files:
                violations.append(f"changed file outside impact envelope: {path}")
            if _is_dependency_file(path) and not impact_envelope.allow_dependency_change:
                violations.append(f"dependency change is not allowed: {path}")
            if _is_high_risk_file(path) and not impact_envelope.requires_human_approval:
                violations.append(f"high-risk path requires human approval: {path}")

        if len(summary.changed_files) > impact_envelope.max_files_changed:
            violations.append(
                "changed file count exceeds envelope: "
                f"{len(summary.changed_files)} > {impact_envelope.max_files_changed}"
            )

        if summary.changed_loc > impact_envelope.max_loc_changed:
            violations.append(
                "changed LOC exceeds envelope: "
                f"{summary.changed_loc} > {impact_envelope.max_loc_changed}"
            )

        if (
            _looks_like_public_api_change(patch_diff)
            and not impact_envelope.allow_public_api_change
        ):
            violations.append("public API signature change is not allowed")

        return PatchPolicyResult(
            allowed=not violations,
            violations=violations,
            summary=summary,
        )


def _is_dependency_file(path: str) -> bool:
    return any(fnmatch(path, pattern) for pattern in DEPENDENCY_PATTERNS)


def _is_high_risk_file(path: str) -> bool:
    normalized = path.strip("/")
    return any(fnmatch(normalized, pattern) for pattern in HIGH_RISK_PATTERNS)


def _looks_like_public_api_change(patch_diff: str) -> bool:
    removed_defs = set()
    added_defs = set()
    for line in patch_diff.splitlines():
        if line.startswith("-def ") or line.startswith("-class "):
            removed_defs.add(_signature_name(line[1:]))
        elif line.startswith("+def ") or line.startswith("+class "):
            added_defs.add(_signature_name(line[1:]))
    return bool(removed_defs.intersection(added_defs))


def _signature_name(line: str) -> str:
    return line.split(" ", maxsplit=1)[1].split("(", maxsplit=1)[0]
