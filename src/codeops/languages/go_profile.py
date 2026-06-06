"""Go language profile."""

from pathlib import Path

from codeops.core.models import CheckCommand, TestCommand
from codeops.languages.base import DetectionResult, iter_repo_files


class GoProfile:
    name = "go"
    file_extensions = {".go"}
    manifest_files = {"go.mod", "go.work"}
    lockfiles = {"go.sum"}
    source_globs = ["**/*.go"]
    test_globs = ["**/*_test.go"]
    generated_globs = ["vendor/**"]
    high_risk_globs = ["go.mod", "go.sum"]

    def detect(self, repo_path: Path) -> DetectionResult | None:
        detected_from = _existing(repo_path, self.manifest_files | self.lockfiles)
        go_files = [path for path in iter_repo_files(repo_path) if path.suffix == ".go"]
        if go_files:
            detected_from.append("**/*.go")
        if not detected_from:
            return None

        return DetectionResult(
            profile_name=self.name,
            primary_language="Go",
            confidence=0.95 if (repo_path / "go.mod").exists() else 0.65,
            build_systems=_build_systems(repo_path),
            package_managers=["go"] if detected_from else [],
            test_frameworks=["go test"] if any(path.name.endswith("_test.go") for path in go_files) else [],
            monorepo=(repo_path / "go.work").exists(),
            workspace_roots=[repo_path] if (repo_path / "go.work").exists() else [],
            detected_from=sorted(set(detected_from)),
        )

    def discover_test_commands(self, repo_path: Path) -> list[TestCommand]:
        return []

    def select_tests(
        self,
        repo_path: Path,
        changed_files: list[str],
        graph_tests: list[str],
    ) -> list[TestCommand]:
        return []

    def static_checks(
        self, repo_path: Path, changed_files: list[str]
    ) -> list[CheckCommand]:
        return []


def _existing(repo_path: Path, names: set[str]) -> list[str]:
    return sorted(name for name in names if (repo_path / name).exists())


def _build_systems(repo_path: Path) -> list[str]:
    systems: list[str] = []
    if (repo_path / "go.mod").exists():
        systems.append("go modules")
    if (repo_path / "go.work").exists():
        systems.append("go workspace")
    return systems
