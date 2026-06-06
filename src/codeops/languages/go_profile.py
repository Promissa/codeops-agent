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
        if not (repo_path / "go.mod").exists() and not _go_files(repo_path):
            return []
        return [
            TestCommand(
                id="go_test_all",
                command=["go", "test", "-json", "./..."],
                cwd=repo_path.resolve(),
                scope="full",
                language="Go",
                parse_format="go_json",
            )
        ]

    def select_tests(
        self,
        repo_path: Path,
        changed_files: list[str],
        graph_tests: list[str],
    ) -> list[TestCommand]:
        packages = _affected_packages(repo_path, changed_files)
        if not packages and graph_tests:
            packages = ["./..."]
        return [
            TestCommand(
                id=f"go_test_{package.replace('/', '_').replace('.', 'root')}",
                command=["go", "test", "-json", package],
                cwd=repo_path.resolve(),
                scope="affected",
                language="Go",
                parse_format="go_json",
            )
            for package in packages
        ]

    def static_checks(
        self, repo_path: Path, changed_files: list[str]
    ) -> list[CheckCommand]:
        if not (repo_path / "go.mod").exists() and not _go_files(repo_path):
            return []
        return [
            CheckCommand(
                id="go_vet_all",
                command=["go", "vet", "./..."],
                cwd=repo_path.resolve(),
                language="Go",
                parse_format="raw",
            )
        ]


def _existing(repo_path: Path, names: set[str]) -> list[str]:
    return sorted(name for name in names if (repo_path / name).exists())


def _build_systems(repo_path: Path) -> list[str]:
    systems: list[str] = []
    if (repo_path / "go.mod").exists():
        systems.append("go modules")
    if (repo_path / "go.work").exists():
        systems.append("go workspace")
    return systems


def _go_files(repo_path: Path) -> list[Path]:
    return [path for path in iter_repo_files(repo_path) if path.suffix == ".go"]


def _affected_packages(repo_path: Path, changed_files: list[str]) -> list[str]:
    packages: list[str] = []
    for changed_file in changed_files:
        path = Path(changed_file)
        if path.suffix != ".go":
            continue
        parent = path.parent.as_posix()
        packages.append("." if parent == "." else f"./{parent}")
    return list(dict.fromkeys(packages))
