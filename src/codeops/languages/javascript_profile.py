"""JavaScript and TypeScript language profile."""

import json
from pathlib import Path
from typing import Any

from codeops.languages.base import DetectionResult, iter_repo_files


class JavaScriptProfile:
    name = "javascript"
    file_extensions = {".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}
    manifest_files = {
        "package.json",
        "tsconfig.json",
        "vite.config.js",
        "vite.config.ts",
        "next.config.js",
        "next.config.ts",
    }
    lockfiles = {"bun.lockb", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
    source_globs = ["**/*.js", "**/*.jsx", "**/*.ts", "**/*.tsx"]
    test_globs = [
        "**/*.test.js",
        "**/*.test.ts",
        "**/*.spec.js",
        "**/*.spec.ts",
        "tests/**/*.js",
        "tests/**/*.ts",
    ]
    generated_globs = ["node_modules/**", "dist/**", "build/**"]
    high_risk_globs = ["package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"]

    def detect(self, repo_path: Path) -> DetectionResult | None:
        detected_from = _existing(repo_path, self.manifest_files | self.lockfiles)
        source_files = [
            path
            for path in iter_repo_files(repo_path)
            if path.suffix in self.file_extensions
        ]
        if source_files:
            detected_from.append("**/*.{js,jsx,ts,tsx,mjs,cjs}")
        if not detected_from:
            return None

        package_json, package_warning = _read_package_json(repo_path / "package.json")
        scripts = package_json.get("scripts", {}) if package_json else {}
        deps = _package_names(package_json)
        is_typescript = (
            (repo_path / "tsconfig.json").exists()
            or any(path.suffix in {".ts", ".tsx"} for path in source_files)
        )

        return DetectionResult(
            profile_name=self.name,
            primary_language="TypeScript" if is_typescript else "JavaScript",
            secondary_languages=["JavaScript"] if is_typescript else [],
            confidence=_confidence(repo_path, source_files),
            frameworks=_frameworks(repo_path, deps),
            build_systems=_build_systems(repo_path, is_typescript),
            package_managers=_package_managers(repo_path),
            test_frameworks=_test_frameworks(scripts),
            detected_from=sorted(set(detected_from)),
            warnings=[package_warning] if package_warning else [],
        )


def _existing(repo_path: Path, names: set[str]) -> list[str]:
    return sorted(name for name in names if (repo_path / name).exists())


def _read_package_json(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return {}, f"package.json could not be parsed: {exc.msg}"


def _package_names(package_json: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for field in ["dependencies", "devDependencies", "peerDependencies"]:
        value = package_json.get(field, {})
        if isinstance(value, dict):
            names.update(str(name) for name in value)
    return names


def _confidence(repo_path: Path, source_files: list[Path]) -> float:
    if (repo_path / "package.json").exists() and source_files:
        return 0.95
    if (repo_path / "package.json").exists() or (repo_path / "tsconfig.json").exists():
        return 0.85
    return 0.60


def _frameworks(repo_path: Path, deps: set[str]) -> list[str]:
    frameworks: list[str] = []
    if "vite" in deps or any(repo_path.glob("vite.config.*")):
        frameworks.append("vite")
    if "next" in deps or any(repo_path.glob("next.config.*")):
        frameworks.append("next")
    if "express" in deps:
        frameworks.append("express")
    return frameworks


def _build_systems(repo_path: Path, is_typescript: bool) -> list[str]:
    systems = ["package.json"] if (repo_path / "package.json").exists() else []
    if is_typescript:
        systems.append("tsconfig")
    return systems


def _package_managers(repo_path: Path) -> list[str]:
    managers: list[str] = []
    lockfile_managers = [
        ("package-lock.json", "npm"),
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
    ]
    for lockfile, manager in lockfile_managers:
        if (repo_path / lockfile).exists():
            managers.append(manager)
    if not managers and (repo_path / "package.json").exists():
        managers.append("npm")
    return managers


def _test_frameworks(scripts: dict[str, Any]) -> list[str]:
    test_script = str(scripts.get("test", ""))
    if "vitest" in test_script:
        return ["vitest"]
    if "jest" in test_script:
        return ["jest"]
    if "node --test" in test_script:
        return ["node:test"]
    if test_script:
        return ["npm test"]
    return []
