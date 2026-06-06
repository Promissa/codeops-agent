from pathlib import Path
import shutil

import pytest

from codeops.core.models import CheckCommand, ImpactEnvelope, TestCommand
from codeops.languages.rust_profile import RustProfile
from codeops.safety.command_policy import CommandPolicy
from codeops.safety.patch_policy import PatchPolicy
from codeops.tools.test_runner import TestRunner


def test_rust_profile_discovers_cargo_test_and_check_commands():
    repo = Path("examples/fixtures/rust_cli_project")

    tests = RustProfile().discover_test_commands(repo)
    checks = RustProfile().static_checks(repo, [])

    assert tests == [
        TestCommand(
            id="cargo_test",
            command=["cargo", "test"],
            cwd=repo.resolve(),
            scope="full",
            language="Rust",
            parse_format="cargo",
        )
    ]
    assert checks == [
        CheckCommand(
            id="cargo_check",
            command=["cargo", "check"],
            cwd=repo.resolve(),
            language="Rust",
            parse_format="cargo",
        )
    ]


def test_rust_profile_selects_cargo_test_for_rust_changes():
    repo = Path("examples/fixtures/rust_cli_project")

    tests = RustProfile().select_tests(repo, ["src/lib.rs"], [])

    assert tests[0].command == ["cargo", "test"]
    assert tests[0].scope == "affected"


def test_command_policy_allows_cargo_test_and_check_only():
    policy = CommandPolicy()

    assert policy.validate(["cargo", "test"]).allowed
    assert policy.validate(["cargo", "check"]).allowed
    assert policy.validate(["cargo", "clippy", "--all-targets", "--all-features"]).allowed
    assert not policy.validate(["cargo", "add", "serde"]).allowed


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo binary is unavailable")
def test_rust_profile_runs_fixture_cargo_tests():
    repo = Path("examples/fixtures/rust_cli_project")
    command = RustProfile().discover_test_commands(repo)[0]

    result = TestRunner(repo).run(command)

    assert result.passed, result.stderr


def test_patch_policy_rejects_cargo_files_by_default():
    for path in ["Cargo.toml", "Cargo.lock"]:
        patch = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 [package]
+serde = "1"
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
