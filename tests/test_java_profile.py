from pathlib import Path
import shutil

import pytest

from codeops.core.models import ImpactEnvelope, TestCommand
from codeops.languages.detector import ProjectDetector
from codeops.languages.java_profile import JavaProfile
from codeops.safety.command_policy import CommandPolicy
from codeops.safety.patch_policy import PatchPolicy
from codeops.tools.test_runner import TestRunner


def test_java_profile_detects_maven_fixture():
    repo = Path("examples/fixtures/java_maven_project")

    profile = ProjectDetector().detect(repo)

    assert profile.primary_language == "Java"
    assert "java" in profile.language_profiles
    assert "maven" in profile.build_systems
    assert "mvn test" in profile.test_frameworks


def test_java_profile_discovers_maven_test_command():
    repo = Path("examples/fixtures/java_maven_project")

    commands = JavaProfile().discover_test_commands(repo)

    assert commands == [
        TestCommand(
            id="mvn_test",
            command=["mvn", "test"],
            cwd=repo.resolve(),
            scope="full",
            language="Java",
        )
    ]


def test_java_profile_selects_maven_focused_test():
    repo = Path("examples/fixtures/java_maven_project")

    commands = JavaProfile().select_tests(repo, [], ["NameServiceTest.java"])

    assert commands[0].command == ["mvn", "-Dtest=NameServiceTest", "test"]
    assert commands[0].scope == "affected"


def test_java_profile_uses_gradle_wrapper_only_when_present(tmp_path: Path):
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
    (tmp_path / "src" / "test").mkdir(parents=True)

    assert JavaProfile().discover_test_commands(tmp_path) == []

    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    commands = JavaProfile().discover_test_commands(tmp_path)

    assert commands[0].command == ["./gradlew", "test"]


def test_command_policy_allows_maven_and_gradle_wrapper_tests_only():
    policy = CommandPolicy()

    assert policy.validate(["mvn", "test"]).allowed
    assert policy.validate(["mvn", "-Dtest=NameServiceTest", "test"]).allowed
    assert policy.validate(["./gradlew", "test"]).allowed
    assert policy.validate(["./gradlew", ":service:test"]).allowed
    assert not policy.validate(["mvn", "package"]).allowed
    assert not policy.validate(["gradle", "test"]).allowed


@pytest.mark.skipif(shutil.which("mvn") is None, reason="mvn binary is unavailable")
def test_java_profile_runs_fixture_maven_tests():
    repo = Path("examples/fixtures/java_maven_project")
    command = JavaProfile().discover_test_commands(repo)[0]

    result = TestRunner(repo).run(command)

    assert result.passed, result.stderr


def test_patch_policy_rejects_java_build_files_by_default():
    for path in ["pom.xml", "build.gradle", "build.gradle.kts"]:
        patch = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 build
+dependency
"""

        result = PatchPolicy().validate(patch, _envelope(allowed_files=[path]))

        assert not result.allowed
        assert f"dependency change is not allowed: {path}" in result.violations


def _envelope(allowed_files: list[str]) -> ImpactEnvelope:
    return ImpactEnvelope(
        target_symbols=[],
        allowed_files=allowed_files,
        affected_files=allowed_files,
        affected_tests=[],
        forbidden_changes=["dependency change", "public API signature change"],
        risk_level="low",
        requires_human_approval=False,
    )
