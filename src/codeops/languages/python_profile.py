"""Python language profile."""

from pathlib import Path

from codeops.core.models import CheckCommand, TestCommand
from codeops.languages.base import DetectionResult, iter_repo_files


class PythonProfile:
    name = "python"
    file_extensions = {".py"}
    manifest_files = {"pyproject.toml", "setup.cfg", "setup.py", "requirements.txt"}
    lockfiles = {"uv.lock", "Pipfile.lock", "poetry.lock"}
    source_globs = ["**/*.py"]
    test_globs = ["tests/**/*.py", "**/test_*.py", "**/*_test.py"]
    generated_globs = ["**/__pycache__/**"]
    high_risk_globs = ["pyproject.toml", "requirements*.txt"]

    def detect(self, repo_path: Path) -> DetectionResult | None:
        detected_from = _existing(repo_path, self.manifest_files | self.lockfiles)
        python_files = [
            path for path in iter_repo_files(repo_path) if path.suffix == ".py"
        ]
        if python_files:
            detected_from.append("**/*.py")
        if not detected_from:
            return None

        pyproject = repo_path / "pyproject.toml"
        test_files = [
            path
            for path in python_files
            if path.name.startswith("test_") or "tests" in path.relative_to(repo_path).parts
        ]
        package_managers = []
        if (repo_path / "uv.lock").exists():
            package_managers.append("uv")
        if (repo_path / "requirements.txt").exists() or pyproject.exists():
            package_managers.append("pip")

        return DetectionResult(
            profile_name=self.name,
            primary_language="Python",
            confidence=0.95 if pyproject.exists() or python_files else 0.70,
            frameworks=["pytest"] if _uses_pytest(repo_path, pyproject, test_files) else [],
            build_systems=["pyproject"] if pyproject.exists() else [],
            package_managers=package_managers,
            test_frameworks=["pytest"] if test_files else [],
            detected_from=sorted(set(detected_from)),
        )

    def discover_test_commands(self, repo_path: Path) -> list[TestCommand]:
        if not _test_files(repo_path):
            return []
        return [
            TestCommand(
                id="pytest_full",
                command=["pytest", "-q"],
                cwd=repo_path.resolve(),
                scope="full",
                language="Python",
                parse_format="pytest",
            )
        ]

    def select_tests(
        self,
        repo_path: Path,
        changed_files: list[str],
        graph_tests: list[str],
    ) -> list[TestCommand]:
        tests = list(dict.fromkeys([*graph_tests, *_matching_tests(repo_path, changed_files)]))
        return [
            TestCommand(
                id=f"pytest_affected_{test.replace('/', '_').replace(':', '_')}",
                command=["pytest", test, "-q"],
                cwd=repo_path.resolve(),
                scope="affected",
                language="Python",
                parse_format="pytest",
            )
            for test in tests
        ]

    def static_checks(
        self, repo_path: Path, changed_files: list[str]
    ) -> list[CheckCommand]:
        return []


def _existing(repo_path: Path, names: set[str]) -> list[str]:
    return sorted(name for name in names if (repo_path / name).exists())


def _uses_pytest(repo_path: Path, pyproject: Path, test_files: list[Path]) -> bool:
    if test_files or (repo_path / "pytest.ini").exists():
        return True
    if not pyproject.exists():
        return False
    return "pytest" in pyproject.read_text(encoding="utf-8", errors="ignore")


def _test_files(repo_path: Path) -> list[Path]:
    return [
        path
        for path in iter_repo_files(repo_path)
        if path.suffix == ".py"
        and (path.name.startswith("test_") or "tests" in path.relative_to(repo_path).parts)
    ]


def _matching_tests(repo_path: Path, changed_files: list[str]) -> list[str]:
    tests_by_stem = {
        path.stem.removeprefix("test_"): path.relative_to(repo_path).as_posix()
        for path in _test_files(repo_path)
    }
    selected: list[str] = []
    for changed_file in changed_files:
        stem = Path(changed_file).stem
        if stem in tests_by_stem:
            selected.append(tests_by_stem[stem])
    return selected
