"""Project-level language detection."""

from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

from codeops.core.models import ProjectProfile
from codeops.languages.base import DetectionResult
from codeops.languages.registry import LanguageProfileRegistry


T = TypeVar("T")


class ProjectDetector:
    """Build a language-agnostic profile for a repository."""

    def __init__(self, registry: LanguageProfileRegistry | None = None) -> None:
        self.registry = registry or LanguageProfileRegistry()

    def detect(self, repo_path: Path) -> ProjectProfile:
        repo_root = repo_path.resolve()
        results = [
            result
            for result in (
                profile.detect(repo_root) for profile in self.registry.profiles()
            )
            if result is not None and result.confidence > 0
        ]
        if not results:
            return ProjectProfile(
                repo_path=repo_root,
                primary_language="unknown",
                warnings=["No language profile detected."],
            )

        results.sort(key=lambda result: (-result.confidence, result.primary_language))
        primary = results[0].primary_language
        languages = _unique(
            language
            for result in results
            for language in [result.primary_language, *result.secondary_languages]
        )
        secondary = [language for language in languages if language != primary]

        return ProjectProfile(
            repo_path=repo_root,
            primary_language=primary,
            secondary_languages=secondary,
            frameworks=_collect(results, "frameworks"),
            build_systems=_collect(results, "build_systems"),
            package_managers=_collect(results, "package_managers"),
            test_frameworks=_collect(results, "test_frameworks"),
            language_profiles=_unique(result.profile_name for result in results),
            monorepo=any(result.monorepo for result in results),
            workspace_roots=_unique(
                root for result in results for root in result.workspace_roots
            ),
            detected_from=_collect(results, "detected_from"),
            warnings=_collect(results, "warnings"),
        )


def _collect(results: Iterable[DetectionResult], field_name: str) -> list[str]:
    return _unique(
        item
        for result in results
        for item in getattr(result, field_name)
    )


def _unique(items: Iterable[T]) -> list[T]:
    seen: set[T] = set()
    ordered: list[T] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered
