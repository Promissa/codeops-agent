import difflib
import json
from pathlib import Path
import shutil

import pytest
from typer.testing import CliRunner

from codeops.agents.patch_generator import PatchGenerator
from codeops.cli import app
from codeops.core.models import ImpactEnvelope
from codeops.gate.verification import open_workspace
from codeops.gate.workspace_diff import workspace_diff
from codeops.runtime.lifecycle import RunTerminated
from codeops.tools.codegraph_gateway import CodeGraphCommandResult, CodeGraphGateway
from codeops.tools.patch_tool import PatchTool
from codeops.trust.policy import POLICY_PATH, SandboxConfig, TrustPolicy
from codeops.workflow.nodes import VerificationPlanBuilder


def _json(out: Path, name: str) -> dict:
    return json.loads((out / name).read_text())


def _write(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _patch(repo: Path, path: str, updated: str) -> str:
    return "".join(difflib.unified_diff(
        (repo / path).read_text().splitlines(keepends=True),
        updated.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


def _verify(
    repo: Path, tmp_path: Path, patch: str, expected: str, *, sandbox: str = "none",
) -> Path:
    diff = tmp_path / "external.diff"
    diff.write_text(patch)
    out = tmp_path / "evidence"
    before = {item.relative_to(repo): item.read_bytes() for item in repo.rglob("*") if item.is_file()}
    result = CliRunner().invoke(app, [
        "verify", "--repo", str(repo), "--diff", str(diff), "--out", str(out),
        "--sandbox", sandbox, "--approve-trust",
    ])
    gate = _json(out, "evidence_gate.json") if (out / "evidence_gate.json").exists() else {}
    expected_exit = {"verified": 0, "regressed": 1, "inconclusive": 2}[expected]
    assert (gate.get("verdict"), result.exit_code) == (expected, expected_exit), (result.output, result.exception, gate)
    after = {item.relative_to(repo): item.read_bytes() for item in repo.rglob("*") if item.is_file()}
    assert before == after
    return out


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def reject(*args, **kwargs):
        pytest.fail("Standalone verification invoked PatchGenerator")

    monkeypatch.setattr(PatchGenerator, "generate", reject)
    repo = tmp_path / "repo"
    _write(repo, "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["."]\n')
    _write(repo, "logic.py", "def value():\n    return 1\n")
    _write(repo, "tests/test_value.py", "from logic import value\n\ndef test_value():\n    assert value() == 2\n")
    return repo


def test_catches_test_subprocess_side_effect(repo: Path, tmp_path: Path) -> None:
    _write(repo, "settings.txt", "original\n")
    _write(
        repo, "tests/test_value.py",
        "from pathlib import Path\nfrom logic import value\n\n"
        "def test_value():\n    assert value() == 2\n"
        "    Path('settings.txt').write_text('side effect\\n')\n"
        "    Path('generated.txt').write_text('unexpected\\n')\n",
    )
    out = _verify(repo, tmp_path, _patch(repo, "logic.py", "def value():\n    return 2\n"), "regressed")
    actual = _json(out, "workspace_diff.json")
    assert actual["unexpected_files"] == ["generated.txt", "settings.txt"]
    assert actual["added_files"] == ["generated.txt"]
    assert "+side effect" in actual["unified_diff"]
    gate = _json(out, "evidence_gate.json")
    assert gate["actual_diff_status"] == "rejected"
    assert any("outside impact envelope" in item and "settings.txt" in item for item in gate["violations"])


def test_already_passing_tests_are_inconclusive(repo: Path, tmp_path: Path) -> None:
    _write(repo, "tests/test_value.py", "from logic import value\n\ndef test_value():\n    assert value() > 0\n")
    out = _verify(repo, tmp_path, _patch(repo, "logic.py", "def value():\n    return 2\n"), "inconclusive")
    gate = _json(out, "evidence_gate.json")
    assert gate["transitions"]["still_passing"]
    assert gate["transitions"]["fixed"] == 0
    assert any("reproduces the reported behavior on baseline" in step for step in gate["next_steps"])


def test_cross_file_regression_detected_even_when_both_suites_fail(repo: Path, tmp_path: Path) -> None:
    _write(repo, "dependent.py", "from logic import value\n\ndef expect_original():\n    return value() == 1\n")
    _write(repo, "tests/test_dependent.py", "from dependent import expect_original\n\ndef test_dependent():\n    assert expect_original()\n")
    out = _verify(repo, tmp_path, _patch(repo, "logic.py", "def value():\n    return 2\n"), "regressed")
    evidence = _json(out, "differential_results.json")["commands"][0]
    assert evidence["baseline"]["exit_code"] == evidence["candidate"]["exit_code"] == 1
    assert {case["outcome"] for case in evidence["cases"]} == {"fixed", "regressed"}
    assert "Caller reached by a failing test" in (out / "trace_report.html").read_text()


def test_importing_caller_does_not_count_as_exercising_it(repo: Path, tmp_path: Path) -> None:
    _write(repo, "dependent.py", "from logic import value as current\n\ndef observed():\n    return current()\n\ndef unseen():\n    return current()\n")
    _write(repo, "tests/test_value.py", "from dependent import observed, unseen\n\ndef test_value():\n    assert observed() == 2\n")
    _write(repo, "tests/test_unseen.py", "from dependent import unseen\n\ndef test_unseen():\n    assert unseen() == 2\n")
    TrustPolicy(
        confirmed=True, sandbox=SandboxConfig(backend="none"),
        allowed_test_commands=[["pytest", "tests/test_value.py", "-q"]],
    ).save(repo / POLICY_PATH)
    out = _verify(repo, tmp_path, _patch(repo, "logic.py", "def value():\n    return 2\n"), "inconclusive")
    effect = _json(out, "effect_map.json")
    assert "dependent.py" in effect["observed_files"]
    assert [caller["symbol"] for caller in effect["unobserved_callers"]] == ["dependent.unseen"]
    assert "tests/test_unseen.py" in effect["affected_tests"]
    assert {"ast", "ripgrep"} <= set(effect["sources"])
    assert any("dependent.unseen" in step for step in _json(out, "evidence_gate.json")["next_steps"])
    html = (out / "trace_report.html").read_text()
    assert "dependent.unseen" in html and "Unobserved caller" in html and "Actual net diff policy" in html


def test_genuine_fix_writes_standalone_artifacts(repo: Path, tmp_path: Path) -> None:
    out = _verify(repo, tmp_path, _patch(repo, "logic.py", "def value():\n    return 2\n"), "verified")
    assert _json(out, "evidence_gate.json")["transitions"]["fixed"] > 0
    assert _json(out, "effect_map.json")["unobserved_callers"] == []
    for name in (
        "workspace_diff.json", "effect_map.json", "differential_results.json", "evidence_gate.json",
        "trace_events.jsonl", "trace_report.html", "evidence_matrix.md", "intent_manifest.md", "final_report.md",
    ):
        assert (out / name).stat().st_size > 0


def test_python_module_constant_has_runtime_reference_evidence(repo: Path, tmp_path: Path) -> None:
    _write(repo, "logic.py", "VALUE = 1\n")
    _write(repo, "tests/test_value.py", "from logic import VALUE\n\ndef test_value():\n    assert VALUE == 2\n")
    out = _verify(repo, tmp_path, _patch(repo, "logic.py", "VALUE = 2\n"), "verified")
    edited = _json(out, "effect_map.json")["edited_symbols"]
    assert [(item["symbol"], item["kind"]) for item in edited] == [("logic.VALUE", "variable")]


def test_test_only_change_cannot_turn_a_failure_into_a_verified_fix(repo: Path, tmp_path: Path) -> None:
    test = "tests/test_value.py"
    updated = (repo / test).read_text().replace("== 2", "== 1")
    out = _verify(repo, tmp_path, _patch(repo, test, updated), "inconclusive")
    assert not any(
        case["relevant"]
        for command in _json(out, "differential_results.json")["commands"] for case in command["cases"]
    )


def test_added_regression_test_replays_against_baseline_source(repo: Path, tmp_path: Path) -> None:
    test = "tests/test_value.py"
    existing = "from logic import value\n\ndef test_existing():\n    assert value() > 0\n"
    _write(repo, test, existing)
    source = _patch(repo, "logic.py", "def value():\n    return 2\n")
    tests = _patch(repo, test, existing + "\ndef test_new_expectation():\n    assert value() == 2\n")
    out = _verify(repo, tmp_path, source + tests, "verified")
    command = _json(out, "differential_results.json")["commands"][0]
    assert command["baseline"]["exit_code"] == 0
    assert command["replay"]["exit_code"] == 1
    assert command["candidate"]["exit_code"] == 0
    assert any(case["case"].endswith("test_new_expectation") and case["relevant"] for case in command["cases"])


def test_skipped_tests_produce_unobserved_transitions(repo: Path, tmp_path: Path) -> None:
    _write(repo, "tests/test_value.py", "import pytest\n\n@pytest.mark.skip\ndef test_value():\n    assert False\n")
    out = _verify(repo, tmp_path, _patch(repo, "logic.py", "def value():\n    return 2\n"), "inconclusive")
    gate = _json(out, "evidence_gate.json")
    assert gate["transitions"]["unobserved"] > 0
    assert gate["transitions"]["still_passing"] == 0


def test_standalone_uses_real_bubblewrap_workspaces(repo: Path, tmp_path: Path) -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("Bubblewrap is unavailable")
    for network_enabled in (False, True):
        policy = TrustPolicy(
            confirmed=True, sandbox=SandboxConfig(network_enabled=network_enabled),
            allowed_test_commands=[["pytest", "-q"]],
        )
        try:
            with open_workspace(repo, policy, execute=True):
                break
        except RunTerminated as exc:
            message = str(exc).lower()
            if ("permission denied" not in message and "operation not permitted" not in message
                    or not any(word in message for word in ("unshare", "namespace", "netlink_route", "mount"))):
                raise
            if network_enabled:
                pytest.skip(f"Outer sandbox blocks Bubblewrap namespaces: {exc}")
    policy.save(repo / POLICY_PATH)
    _write(
        repo, "tests/test_value.py",
        "from pathlib import Path\nfrom logic import value\n\n"
        "def test_value():\n    assert Path.cwd() == Path('/workspace')\n    assert value() == 2\n",
    )
    out = _verify(repo, tmp_path, _patch(repo, "logic.py", "def value():\n    return 2\n"), "verified", sandbox="bwrap")
    actual = _json(out, "workspace_diff.json")
    assert actual["changed_files"] == ["logic.py"]
    assert _json(out, "task.json")["sandbox_mode"] == "bwrap"


@pytest.mark.parametrize(("project", "path", "program"), [
    ("go_http_project", "handler.go", "go"),
    ("js_vitest_project", "src/normalize.js", "npm"),
    ("rust_cli_project", "src/lib.rs", "cargo"),
    ("java_maven_project", "src/main/java/com/example/service/NameService.java", "mvn"),
])
def test_verification_plan_uses_language_adapters(project: str, path: str, program: str) -> None:
    root = Path("examples/fixtures") / project
    envelope = ImpactEnvelope(allowed_files=[path], risk_level="high", requires_human_approval=False)
    builder = VerificationPlanBuilder()
    plan = builder.build(envelope, repo_path=root)
    commands = builder.commands(plan, risk_level=envelope.risk_level)
    assert commands and plan.full_tests and plan.affected_tests
    assert {command.command[0] for command in commands} == {program}


def test_workspace_diff_includes_untracked_deleted_and_no_newline_files(tmp_path: Path) -> None:
    baseline, candidate = tmp_path / "base", tmp_path / "head"
    _write(baseline, "old file.txt", "gone\n")
    _write(candidate, "new file.txt", "new")
    _write(candidate, "nested/target/ignored.txt", "build output")
    actual = workspace_diff(baseline, candidate, ["old file.txt"])
    assert actual.added_files == ["new file.txt"]
    assert actual.deleted_files == ["old file.txt"]
    assert actual.unexpected_files == ["new file.txt"]
    assert (actual.added_loc, actual.removed_loc) == (1, 1)
    assert PatchTool(baseline).check(actual.unified_diff).passed


def test_codegraph_gateway_understands_installed_cli_json_shape(tmp_path: Path) -> None:
    node = {"name": "caller", "filePath": "src/main.py", "startLine": 7, "kind": "function"}

    def run(repo, args, text):
        if args[0] == "search" or ("--depth" in args and args[0] == "callers"):
            return CodeGraphCommandResult(args=args, returncode=1)
        payload = {
            "query": [{"node": node, "score": 0.9}],
            "callers": {"callers": [node]},
            "impact": {"affected": [node]},
            "affected": {"affectedTests": ["tests/test_main.py"]},
        }[args[0]]
        return CodeGraphCommandResult(args=args, returncode=0, stdout=json.dumps(payload))

    gateway = CodeGraphGateway(runner=run)
    assert gateway.search(tmp_path, "caller")[0].line == 7
    assert gateway.callers(tmp_path, "caller")[0].path == "src/main.py"
    assert gateway.impact(tmp_path, "caller").affected_files == ["src/main.py"]
    assert gateway.affected_tests(tmp_path, ["src/main.py"]) == ["tests/test_main.py"]
