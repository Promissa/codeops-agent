"""Java and Kotlin language profile."""

from pathlib import Path
import re

from codeops.core.models import CheckCommand, TestCommand
from codeops.languages.base import DetectionResult, iter_repo_files


class JavaProfile:
    name = "java"
    file_extensions = {".java", ".kt", ".kts"}
    manifest_files = {
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
    }
    lockfiles = {"gradle.lockfile"}
    source_globs = [
        "src/main/java/**/*.java",
        "src/main/kotlin/**/*.kt",
        "**/*.java",
        "**/*.kt",
    ]
    test_globs = ["src/test/java/**/*.java", "src/test/kotlin/**/*.kt"]
    generated_globs = ["build/**", "target/**"]
    high_risk_globs = ["pom.xml", "build.gradle", "build.gradle.kts"]

    def detect(self, repo_path: Path) -> DetectionResult | None:
        detected_from = _existing(repo_path, self.manifest_files | self.lockfiles)
        source_files = [
            path
            for path in iter_repo_files(repo_path)
            if path.suffix in {".java", ".kt", ".kts"}
        ]
        if source_files:
            detected_from.append("**/*.{java,kt,kts}")
        if not detected_from:
            return None

        has_kotlin = any(path.suffix in {".kt", ".kts"} for path in source_files)
        return DetectionResult(
            profile_name=self.name,
            primary_language="Kotlin" if has_kotlin else "Java",
            secondary_languages=["Java"] if has_kotlin else [],
            confidence=0.95 if _has_build_manifest(repo_path) else 0.65,
            build_systems=_build_systems(repo_path),
            package_managers=_build_systems(repo_path),
            test_frameworks=_test_frameworks(repo_path),
            detected_from=sorted(set(detected_from)),
        )

    def discover_test_commands(self, repo_path: Path) -> list[TestCommand]:
        repo_root = repo_path.resolve()
        if (repo_path / "pom.xml").exists():
            return [
                TestCommand(
                    id="mvn_test",
                    command=["mvn", "test"],
                    cwd=repo_root,
                    scope="full",
                    language="Java",
                    parse_format="raw",
                )
            ]
        if _gradle_wrapper(repo_path).exists():
            return [
                TestCommand(
                    id="gradle_test",
                    command=["./gradlew", "test"],
                    cwd=repo_root,
                    scope="full",
                    language=_language(repo_path),
                    parse_format="raw",
                )
            ]
        return []

    def select_tests(
        self,
        repo_path: Path,
        changed_files: list[str],
        graph_tests: list[str],
    ) -> list[TestCommand]:
        repo_root = repo_path.resolve()
        if (repo_path / "pom.xml").exists():
            test_classes = _test_classes(changed_files, graph_tests)
            if test_classes:
                return [
                    TestCommand(
                        id=f"mvn_test_{test_classes[0]}",
                        command=["mvn", f"-Dtest={','.join(test_classes)}", "test"],
                        cwd=repo_root,
                        scope="affected",
                        language="Java",
                        parse_format="raw",
                    )
                ]
            return self.discover_test_commands(repo_path)
        if _gradle_wrapper(repo_path).exists():
            return [
                TestCommand(
                    id="gradle_test_affected",
                    command=["./gradlew", "test"],
                    cwd=repo_root,
                    scope="affected",
                    language=_language(repo_path),
                    parse_format="raw",
                )
            ]
        return []

    def static_checks(
        self, repo_path: Path, changed_files: list[str]
    ) -> list[CheckCommand]:
        return []


def _existing(repo_path: Path, names: set[str]) -> list[str]:
    return sorted(name for name in names if (repo_path / name).exists())


def _has_build_manifest(repo_path: Path) -> bool:
    return any((repo_path / name).exists() for name in JavaProfile.manifest_files)


def _build_systems(repo_path: Path) -> list[str]:
    systems: list[str] = []
    if (repo_path / "pom.xml").exists():
        systems.append("maven")
    if any((repo_path / name).exists() for name in ["build.gradle", "build.gradle.kts"]):
        systems.append("gradle")
    return systems


def _test_frameworks(repo_path: Path) -> list[str]:
    frameworks: list[str] = []
    if (repo_path / "pom.xml").exists() and (repo_path / "src" / "test").exists():
        frameworks.append("mvn test")
    if _gradle_wrapper(repo_path).exists() and (repo_path / "src" / "test").exists():
        frameworks.append("gradle test")
    return frameworks


def _gradle_wrapper(repo_path: Path) -> Path:
    return repo_path / "gradlew"


def _language(repo_path: Path) -> str:
    if any(path.suffix in {".kt", ".kts"} for path in iter_repo_files(repo_path)):
        return "Kotlin"
    return "Java"


def _test_classes(changed_files: list[str], graph_tests: list[str]) -> list[str]:
    classes: list[str] = []
    for path in [*graph_tests, *changed_files]:
        name = Path(path).stem
        if not name:
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            classes.append(name)
    return list(dict.fromkeys(classes))
