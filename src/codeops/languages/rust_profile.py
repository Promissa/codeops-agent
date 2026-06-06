"""Rust language profile."""

from pathlib import Path

from codeops.core.models import CheckCommand, TestCommand
from codeops.languages.base import DetectionResult, iter_repo_files


class RustProfile:
    name = "rust"
    file_extensions = {".rs"}
    manifest_files = {"Cargo.toml"}
    lockfiles = {"Cargo.lock"}
    source_globs = ["src/**/*.rs", "**/*.rs"]
    test_globs = ["tests/**/*.rs", "**/*_test.rs"]
    generated_globs = ["target/**"]
    high_risk_globs = ["Cargo.toml", "Cargo.lock"]

    def detect(self, repo_path: Path) -> DetectionResult | None:
        detected_from = _existing(repo_path, self.manifest_files | self.lockfiles)
        rust_files = [
            path for path in iter_repo_files(repo_path) if path.suffix == ".rs"
        ]
        if rust_files:
            detected_from.append("**/*.rs")
        if not detected_from:
            return None

        return DetectionResult(
            profile_name=self.name,
            primary_language="Rust",
            confidence=0.95 if (repo_path / "Cargo.toml").exists() else 0.65,
            build_systems=["cargo"] if (repo_path / "Cargo.toml").exists() else [],
            package_managers=["cargo"] if (repo_path / "Cargo.toml").exists() else [],
            test_frameworks=["cargo test"] if (repo_path / "Cargo.toml").exists() else [],
            detected_from=sorted(set(detected_from)),
        )

    def discover_test_commands(self, repo_path: Path) -> list[TestCommand]:
        if not (repo_path / "Cargo.toml").exists():
            return []
        return [
            TestCommand(
                id="cargo_test",
                command=["cargo", "test"],
                cwd=repo_path.resolve(),
                scope="full",
                language="Rust",
                parse_format="cargo",
            )
        ]

    def select_tests(
        self,
        repo_path: Path,
        changed_files: list[str],
        graph_tests: list[str],
    ) -> list[TestCommand]:
        if not (repo_path / "Cargo.toml").exists():
            return []
        if not any(Path(path).suffix == ".rs" for path in changed_files) and not graph_tests:
            return []
        return [
            TestCommand(
                id="cargo_test_affected",
                command=["cargo", "test"],
                cwd=repo_path.resolve(),
                scope="affected",
                language="Rust",
                parse_format="cargo",
            )
        ]

    def static_checks(
        self, repo_path: Path, changed_files: list[str]
    ) -> list[CheckCommand]:
        if not (repo_path / "Cargo.toml").exists():
            return []
        return [
            CheckCommand(
                id="cargo_check",
                command=["cargo", "check"],
                cwd=repo_path.resolve(),
                language="Rust",
                parse_format="cargo",
            )
        ]


def _existing(repo_path: Path, names: set[str]) -> list[str]:
    return sorted(name for name in names if (repo_path / name).exists())
