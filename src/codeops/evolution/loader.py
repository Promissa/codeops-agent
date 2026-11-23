"""Freeze memory and disclose only the bodies selected by run context."""

from collections.abc import Iterable
from pathlib import Path, PurePosixPath
import re

from codeops.core.models import ProjectProfile
from codeops.evolution.models import MemorySnapshot, Skill, SkillHeader
from codeops.evolution.store import EvolutionStore
from codeops.runtime.lifecycle import RunTerminated

_LANGUAGE_EXTENSIONS = {
    "python": {".py", ".pyi"}, "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx"}, "go": {".go"}, "rust": {".rs"},
    "java": {".java"}, "kotlin": {".kt"}, "csharp": {".cs"}, "dotnet": {".cs", ".fs"},
}
_STOP_WORDS = {
    "use", "the", "and", "for", "when", "with", "before", "check", "after",
    "from", "reusable", "skill", "guidance", "workflow",
}


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def profile_languages(profile: ProjectProfile | None) -> tuple[str, ...]:
    if profile is None:
        return ()
    return tuple(dict.fromkeys(
        language.lower() for language in [profile.primary_language, *profile.secondary_languages]
        if language and language.lower() not in {"unknown", "mixed"}
    ))


def match_skill(
    skill: SkillHeader, *, profile: ProjectProfile | None = None,
    issue: str = "", files: Iterable[str] = (),
) -> bool:
    paths = tuple(PurePosixPath(path) for path in files)
    if skill.scope.globs and not any(
        path.full_match(pattern) or (
            pattern.startswith("**/") and path.full_match(pattern[3:])
        )
        for path in paths for pattern in skill.scope.globs if pattern
    ):
        return False
    languages = set(profile_languages(profile))
    if paths:
        routed = {language for language, extensions in _LANGUAGE_EXTENSIONS.items()
                  if any(path.suffix.lower() in extensions for path in paths)}
        languages = routed or languages
    if skill.scope.languages and not languages.intersection(map(str.lower, skill.scope.languages)):
        return False
    context = words(" ".join([
        issue, *(str(path) for path in paths), *languages,
        *(profile.frameworks + profile.language_profiles if profile else ()),
    ]))
    if skill.scope.tags:
        return any((tokens := words(tag)) and tokens <= context for tag in skill.scope.tags)
    if skill.scope.globs or skill.scope.languages:
        return True
    keywords = words(f"{skill.name} {skill.description}") - _STOP_WORDS
    return bool(context.intersection(word for word in keywords if len(word) > 2))


class EvolutionLoader:
    def __init__(self, store: EvolutionStore) -> None:
        self.store = store
        self.identity = store.identity
        self.memory = MemorySnapshot()
        self.index: tuple[SkillHeader, ...] = ()
        if self.identity is not None:
            with store.transaction(expected=self.identity):
                self.memory = store.memory_snapshot()
                self.index = tuple(
                    skill for skill in store.index(strict=False) if skill.status in {"active", "pinned"}
                )

    @classmethod
    def for_repo(cls, repo: Path) -> "EvolutionLoader":
        return cls(EvolutionStore(repo))

    def load(
        self, *, profile: ProjectProfile | None = None, issue: str = "",
        files: Iterable[str] = (),
    ) -> tuple[Skill, ...]:
        if self.identity is None:
            return ()
        files = tuple(files)
        matched = []
        with self.store.transaction(expected=self.identity):
            for metadata in self.index:
                if match_skill(metadata, profile=profile, issue=issue, files=files):
                    try:
                        skill = self.store.get_skill(metadata.name)
                    except (ValueError, OSError, RunTerminated):
                        continue
                    if (
                        skill is not None and skill.status in {"active", "pinned"}
                        and (
                            metadata.owner == skill.owner == "human" or not metadata.revision_hash
                            or metadata.revision_hash == skill.revision_hash
                        )
                        and match_skill(skill, profile=profile, issue=issue, files=files)
                    ):
                        matched.append(skill)
        return tuple(matched)
