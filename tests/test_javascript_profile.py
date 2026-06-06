import json
from pathlib import Path

import pytest

from codeops.core.models import TestCommand
from codeops.languages.detector import ProjectDetector
from codeops.languages.javascript_profile import JavaScriptProfile
from codeops.tools.test_runner import TestRunner


def test_javascript_profile_discovers_package_test_script_only():
    repo = Path("examples/fixtures/js_vitest_project")

    commands = JavaScriptProfile().discover_test_commands(repo)

    assert commands == [
        TestCommand(
            id="npm_test",
            command=["npm", "run", "test"],
            cwd=repo.resolve(),
            scope="full",
            language="JavaScript",
            parse_format="npm",
        )
    ]


def test_javascript_profile_runs_package_test_script_fixture():
    repo = Path("examples/fixtures/js_vitest_project")
    command = JavaScriptProfile().discover_test_commands(repo)[0]

    result = TestRunner(repo).run(command)

    assert result.passed, result.stderr
    assert result.command == "npm run test"


@pytest.mark.parametrize(
    ("lockfile", "manager"),
    [
        ("package-lock.json", "npm"),
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
    ],
)
def test_project_detector_detects_javascript_lockfiles(
    tmp_path: Path,
    lockfile: str,
    manager: str,
):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test"}})
    )
    (tmp_path / "src.js").write_text("export const ok = true;\n")
    (tmp_path / lockfile).write_text("{}\n")

    profile = ProjectDetector().detect(tmp_path)

    assert profile.primary_language == "JavaScript"
    assert manager in profile.package_managers
