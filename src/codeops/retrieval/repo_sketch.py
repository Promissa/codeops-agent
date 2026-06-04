"""RepoSketch generation."""

from datetime import datetime, timezone
from pathlib import Path

from codeops.core.models import ModuleCapsule, RepoSketch


HIGH_RISK_PARTS = {
    "auth",
    "billing",
    "payment",
    "security",
    "crypto",
    "migrations",
    "infra",
}


class RepoSketchBuilder:
    """Build a compact, deterministic project thumbnail."""

    def build(self, repo_path: Path, commit: str | None = None) -> RepoSketch:
        repo_root = repo_path.resolve()
        python_files = _python_files(repo_root)
        test_files = _test_files(repo_root)

        return RepoSketch(
            repo_root=repo_root,
            commit=commit,
            languages=["Python"] if python_files else [],
            frameworks=_detect_frameworks(repo_root),
            entrypoints=_entrypoints(repo_root, python_files),
            core_modules=_core_modules(repo_root, python_files),
            test_commands=["pytest -q"] if test_files else [],
            test_map=_test_map(python_files, test_files, repo_root),
            high_risk_paths=_high_risk_paths(repo_root),
            generated_at=datetime.now(timezone.utc),
        )

    def render_markdown(self, sketch: RepoSketch) -> str:
        lines = [
            "# RepoSketch",
            "",
            "## Project",
            f"- Name: {sketch.repo_root.name}",
            f"- Commit: {sketch.commit or 'unknown'}",
            f"- Languages: {', '.join(sketch.languages) or 'unknown'}",
            f"- Frameworks: {', '.join(sketch.frameworks) or 'none'}",
            "",
            "## Entrypoints",
            *(_bullet_list(sketch.entrypoints) or ["- none detected"]),
            "",
            "## Core modules",
            "| Module | Path | Responsibility | Public symbols | Risk |",
            "|---|---|---|---|---|",
        ]
        for module in sketch.core_modules:
            symbols = ", ".join(module.public_symbols) or "none"
            lines.append(
                f"| {module.name} | `{module.path}` | "
                f"{module.responsibility} | {symbols} | {module.risk} |"
            )

        lines.extend(
            [
                "",
                "## Test commands",
                *(_bullet_list(sketch.test_commands) or ["- none detected"]),
                "",
                "## Test map",
                "| Source file | Likely tests |",
                "|---|---|",
            ]
        )
        for source, tests in sketch.test_map.items():
            lines.append(f"| `{source}` | {', '.join(tests) or 'none'} |")

        lines.extend(["", "## Risk hotspots"])
        lines.extend(_bullet_list(sketch.high_risk_paths) or ["- none detected"])
        lines.append("")
        return "\n".join(lines)


def _python_files(repo_root: Path) -> list[Path]:
    return sorted(
        path
        for path in repo_root.rglob("*.py")
        if ".git" not in path.parts and ".venv" not in path.parts
    )


def _test_files(repo_root: Path) -> list[Path]:
    return [
        path
        for path in _python_files(repo_root)
        if path.name.startswith("test_") or "/tests/" in path.as_posix()
    ]


def _detect_frameworks(repo_root: Path) -> list[str]:
    frameworks: list[str] = []
    if (repo_root / "tests").exists():
        frameworks.append("pytest")
    return frameworks


def _entrypoints(repo_root: Path, python_files: list[Path]) -> list[str]:
    candidates = [
        path
        for path in python_files
        if path.name in {"cli.py", "__main__.py", "main.py"}
    ]
    return [str(path.relative_to(repo_root)) for path in candidates]


def _core_modules(repo_root: Path, python_files: list[Path]) -> list[ModuleCapsule]:
    modules: list[ModuleCapsule] = []
    for path in python_files:
        rel = path.relative_to(repo_root)
        if "tests" in rel.parts or path.name == "__init__.py":
            continue
        modules.append(
            ModuleCapsule(
                name=path.stem,
                path=str(rel),
                responsibility=f"Module `{path.stem}` in the project source tree.",
                public_symbols=_public_symbols(path),
                risk="high" if _is_high_risk(rel) else "low",
            )
        )
    return modules


def _public_symbols(path: Path) -> list[str]:
    symbols: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("class "):
            name = stripped.split(" ", maxsplit=1)[1].split("(", maxsplit=1)[0]
            if not name.startswith("_"):
                symbols.append(name)
    return symbols


def _test_map(
    python_files: list[Path], test_files: list[Path], repo_root: Path
) -> dict[str, list[str]]:
    tests_by_stem = {path.stem.removeprefix("test_"): path for path in test_files}
    mapping: dict[str, list[str]] = {}
    for source in python_files:
        rel = source.relative_to(repo_root)
        if "tests" in rel.parts or source.name == "__init__.py":
            continue
        test = tests_by_stem.get(source.stem)
        mapping[str(rel)] = [str(test.relative_to(repo_root))] if test else []
    return mapping


def _high_risk_paths(repo_root: Path) -> list[str]:
    hotspots = []
    for path in repo_root.rglob("*"):
        if not path.is_dir():
            continue
        rel = path.relative_to(repo_root)
        if _is_high_risk(rel):
            hotspots.append(str(rel))
    return sorted(hotspots)


def _is_high_risk(path: Path) -> bool:
    return any(part in HIGH_RISK_PARTS for part in path.parts)


def _bullet_list(items: list[str]) -> list[str]:
    return [f"- `{item}`" for item in items]
