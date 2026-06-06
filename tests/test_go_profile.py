from pathlib import Path
import shutil

import pytest

from codeops.core.models import CheckCommand, ImpactEnvelope, TestCommand
from codeops.languages.go_profile import GoProfile
from codeops.safety.command_policy import CommandPolicy
from codeops.safety.patch_policy import PatchPolicy
from codeops.tools.test_output_parser import parse_go_test_json
from codeops.tools.test_runner import TestRunner


def test_go_profile_discovers_go_test_and_vet_commands():
    repo = Path("examples/fixtures/go_http_project")

    tests = GoProfile().discover_test_commands(repo)
    checks = GoProfile().static_checks(repo, [])

    assert tests == [
        TestCommand(
            id="go_test_all",
            command=["go", "test", "-json", "./..."],
            cwd=repo.resolve(),
            scope="full",
            language="Go",
            parse_format="go_json",
        )
    ]
    assert checks == [
        CheckCommand(
            id="go_vet_all",
            command=["go", "vet", "./..."],
            cwd=repo.resolve(),
            language="Go",
        )
    ]


def test_go_profile_selects_package_scoped_tests():
    repo = Path("examples/fixtures/go_http_project")

    tests = GoProfile().select_tests(repo, ["handler.go"], [])

    assert tests[0].command == ["go", "test", "-json", "."]
    assert tests[0].scope == "affected"


def test_command_policy_allows_go_test_and_rejects_go_get():
    policy = CommandPolicy()

    assert policy.validate(["go", "test", "./..."]).allowed
    assert policy.validate(["go", "test", "-json", "./..."]).allowed
    assert policy.validate(["go", "test", "-json", "."]).allowed
    assert policy.validate(["go", "vet", "./..."]).allowed
    assert not policy.validate(["go", "get", "example.com/pkg"]).allowed


def test_parse_go_test_json_detects_pass_and_fail():
    passed = parse_go_test_json(
        '{"Action":"run","Package":"example.com/app","Test":"TestOK"}\n'
        '{"Action":"pass","Package":"example.com/app","Test":"TestOK"}\n'
        '{"Action":"pass","Package":"example.com/app"}\n'
    )
    failed = parse_go_test_json(
        '{"Action":"run","Package":"example.com/app","Test":"TestBad"}\n'
        '{"Action":"fail","Package":"example.com/app","Test":"TestBad"}\n'
        '{"Action":"fail","Package":"example.com/app"}\n'
    )

    assert passed.passed
    assert failed.passed is False
    assert "example.com/app::TestBad" in failed.failed_tests


@pytest.mark.skipif(shutil.which("go") is None, reason="go binary is unavailable")
def test_go_profile_runs_fixture_go_tests():
    repo = Path("examples/fixtures/go_http_project")
    command = GoProfile().discover_test_commands(repo)[0]

    result = TestRunner(repo).run(command)

    assert result.passed, result.stderr


def test_patch_policy_rejects_go_module_files_by_default():
    for path in ["go.mod", "go.sum"]:
        patch = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 module example.com/app
+require example.com/pkg v1.0.0
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
