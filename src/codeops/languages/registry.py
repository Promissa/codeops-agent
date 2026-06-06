"""Registry for language profile adapters."""

from collections.abc import Iterable

from codeops.languages.base import LanguageProfile
from codeops.languages.go_profile import GoProfile
from codeops.languages.javascript_profile import JavaScriptProfile
from codeops.languages.java_profile import JavaProfile
from codeops.languages.python_profile import PythonProfile
from codeops.languages.rust_profile import RustProfile


class LanguageProfileRegistry:
    """Hold the language adapters available to the workflow."""

    def __init__(self, profiles: Iterable[LanguageProfile] | None = None) -> None:
        self._profiles = list(profiles or default_language_profiles())

    def profiles(self) -> list[LanguageProfile]:
        return list(self._profiles)

    def register(self, profile: LanguageProfile) -> None:
        self._profiles.append(profile)


def default_language_profiles() -> list[LanguageProfile]:
    return [
        PythonProfile(),
        JavaScriptProfile(),
        GoProfile(),
        RustProfile(),
        JavaProfile(),
    ]
