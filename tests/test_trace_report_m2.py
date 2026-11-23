"""Acceptance tests for M2 event streams, honest verdicts, and offline rendering."""

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import socket
from typing import Any

import pytest
from typer.testing import CliRunner

from codeops.agents.reviewer import ReviewResult, Reviewer
from codeops.cli import app
from codeops.core.models import TaskRequest, TaskState
from codeops.reporting.trace_events import TraceRecorder, load_trace_events
from codeops.reporting.trace_html import render_trace_report
from codeops.runtime.lifecycle import RunTerminated, TerminationReason
from codeops.runtime.workspace import SandboxWorkspace
from codeops.safety.patch_policy import PatchPolicy, PatchPolicyResult
from codeops.tools.codegraph_gateway import CodeGraphGateway, GraphFile, GraphStatus
from codeops.tools.git_tool import GitCommandResult, GitTool
from codeops.trust.policy import POLICY_PATH, SandboxConfig, TrustPolicy, WriteBudget
from codeops.trust.preflight import TrustPolicyBuilder
from codeops.workflow.orchestrator import WorkflowOrchestrator
from test_trust_sandbox_m1 import bwrap_network_enabled


ROOT = Path(__file__).resolve().parents[1]
ISSUE = ROOT / "examples/issues/csv_trailing_empty_column.md"
XSS = "</script><img src=x onerror=alert(1)>"
SECRETS = (
    "sk-synthetic-M2-1234567890",
    "eyJtMiI6InRlc3QifQ.eyJpZCI6MX0.syntheticSignature",
    "postgresql://synthetic_user:m2_database_credential@localhost/test",
    "m2 quoted password with spaces",
    "m2 opaque api key with spaces",
)
SECRET_TEXT = " ".join([*SECRETS[:3], f'password="{SECRETS[3]}"', f'api_key="{SECRETS[4]}"'])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return shutil.copytree(ROOT / "examples/fixtures/mini_data_pipeline", tmp_path / "repo")


@pytest.fixture
def indexed_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply deterministic index metadata while exercising real patch and test tools."""
    monkeypatch.setattr(
        CodeGraphGateway, "status",
        lambda self, path: GraphStatus(repo_path=path, available=True, index_fresh=True),
    )
    monkeypatch.setattr(
        CodeGraphGateway, "files",
        lambda self, path: [GraphFile(path=file.relative_to(path).as_posix()) for file in path.rglob("*.py")],
    )
    monkeypatch.setattr(CodeGraphGateway, "version", lambda self: "test-index")


class ParsedReport(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self._in_trace_data = False
        self.trace_data: dict[str, Any] = {}
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        self._in_trace_data = tag == "script" and attributes.get("id") == "trace-data"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_trace_data = False

    def handle_data(self, data: str) -> None:
        if self._in_trace_data:
            self.trace_data = json.loads(data)


def _assert_artifacts(out: Path, verdict: str) -> tuple[TaskState, str]:
    state = TaskState.model_validate_json((out / "task.json").read_bytes())
    assert state.verdict == verdict
    assert state.lifecycle_state == "finished"
    assert state.trace_events_path == out / "trace_events.jsonl"
    assert state.trace_report_path == out / "trace_report.html"
    events = load_trace_events(state.trace_events_path)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    known = set()
    for event in events:
        assert event.task_id == state.task_id
        assert event.timestamp.tzinfo is not None
        assert event.parent_event_id is None or event.parent_event_id in known
        assert event.event_id not in known
        known.add(event.event_id)
    assert any(event.kind == "stage_end" and event.duration_ms is not None for event in events)
    assert events[-1].kind == "review" and events[-1].details["verdict"] == verdict
    assert events[-1].details["review"]["verified"] == (verdict == "verified")
    html = state.trace_report_path.read_text()
    report = ParsedReport(html)
    assert [attrs["data-verdict"] for _, attrs in report.tags if "data-verdict" in attrs] == [verdict]
    assert {event["event_id"] for event in report.trace_data["events"]} == known
    for tag, attribute in (("g", "data-event-id"), ("option", "value")):
        values = {attrs[attribute] for name, attrs in report.tags if name == tag and attribute in attrs}
        assert known <= values
    for filename in ("final_report.md", "intent_manifest.md"):
        content = (out / filename).read_text()
        assert f"Trust verdict: `{verdict}`" in content
        assert "Lifecycle: `finished`" in content
        assert f"Termination: `{state.termination_reason}`" in content
        assert "trace_report.html" in content and "trace_events.jsonl" in content
    if verdict != "verified":
        assert "Reviewer verified: `False`" in (out / "final_report.md").read_text()
    return state, html


@pytest.mark.parametrize("verdict", ["verified", "regressed", "inconclusive", "blocked"])
def test_run_writes_honest_verdict_and_trace(
    repo: Path, tmp_path: Path, verdict: str, indexed_graph: None,
) -> None:
    issue = ISSUE
    if verdict == "regressed":
        with (repo / "tests/test_parser.py").open("a") as output:
            output.write("\n\ndef test_deliberate_regression():\n    assert False\n")
    elif verdict == "inconclusive":
        issue = tmp_path / "ambiguous.md"
        issue.write_text("Bug: parser returns the wrong output\nActual: unexpected result\n")
    elif verdict == "blocked":
        TrustPolicy(sandbox=SandboxConfig(backend="none")).save(repo / POLICY_PATH)
    out = tmp_path / "out"
    returned = WorkflowOrchestrator().run(TaskRequest(repo_path=repo, issue_path=issue, out_path=out))
    state, html = _assert_artifacts(out, verdict)
    assert returned.verdict == state.verdict
    if verdict == "verified":
        assert state.test_results and all(result.passed for result in state.test_results)
        assert "Applied · 2 files" in html
        assert "Test command passed" in html
        assert "parse_row · Python hunk / definition" in html
    elif verdict == "regressed":
        assert any(not result.passed for result in state.test_results)
        assert "Test command failed" in html
    elif verdict == "blocked":
        assert state.termination_reason == TerminationReason.POLICY_DENIED
        assert "Unapproved" in html
        assert not state.test_results
    else:
        assert state.status == "needs_clarification"
        assert not state.test_results


@pytest.mark.parametrize(
    "failure",
    ["apply", "patch_policy", "trust_policy", "no_patch", "no_tests", "collection_only", "low_evidence", "review_warning"],
)
def test_patch_and_verification_failures(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, indexed_graph: None, failure: str,
) -> None:
    issue = ISSUE
    if failure == "no_patch":
        issue = tmp_path / "issue.md"
        issue.write_text("Bug: parser breaks line handling\nExpected: retain line endings\n")
    elif failure == "apply":
        monkeypatch.setattr(GitTool, "apply", lambda self, diff: GitCommandResult(
            args=["git", "apply", "-"], returncode=1, stdout="", stderr="patch did not apply",
        ))
    elif failure == "patch_policy":
        validate = PatchPolicy.validate

        def reject(self: PatchPolicy, diff: str, envelope: Any, **kwargs: Any) -> PatchPolicyResult:
            result = validate(self, diff, envelope, **kwargs)
            return result.model_copy(update={"allowed": False, "violations": ["diff violates write policy"]})

        monkeypatch.setattr(PatchPolicy, "validate", reject)
    elif failure in {"trust_policy", "no_tests", "collection_only"}:
        TrustPolicy(
            confirmed=True, sandbox=SandboxConfig(backend="none"),
            write_budget=WriteBudget(max_files_changed=1 if failure == "trust_policy" else 3),
            allowed_test_commands=[["pytest", "--collect-only", "-q"]] if failure == "collection_only" else [],
        ).save(repo / POLICY_PATH)
    elif failure == "review_warning":
        monkeypatch.setattr(
            Reviewer, "review", lambda self, state: ReviewResult(verified=True, warnings=["Unresolved evidence warning"]),
        )
    else:
        monkeypatch.setattr(
            CodeGraphGateway, "status", lambda self, path: GraphStatus(repo_path=path, available=False),
        )

    out = tmp_path / "out"
    WorkflowOrchestrator().run(TaskRequest(repo_path=repo, issue_path=issue, out_path=out))
    expected = {
        "apply": "regressed", "patch_policy": "regressed", "trust_policy": "blocked",
        "no_patch": "inconclusive", "no_tests": "inconclusive", "collection_only": "inconclusive",
        "low_evidence": "inconclusive", "review_warning": "inconclusive",
    }
    state, html = _assert_artifacts(out, expected[failure])
    if failure == "trust_policy":
        assert not (out / "patch.diff").exists()
        assert "Rejected" in html
    elif failure in {"low_evidence", "collection_only", "review_warning"}:
        assert state.test_results and all(result.passed for result in state.test_results)
        if failure == "collection_only":
            assert "Test execution unconfirmed" in html
    else:
        assert not state.test_results


def test_managed_output_denial_reports_outside_repository(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("codeops.workflow.orchestrator.DEFAULT_RUN_STATE_DIR", tmp_path / "registry")
    TrustPolicy(confirmed=True).save(repo / POLICY_PATH)
    forbidden = repo / "out"
    with pytest.raises(RunTerminated) as caught:
        WorkflowOrchestrator().run(TaskRequest(repo_path=repo, issue_path=ISSUE, out_path=forbidden))
    assert caught.value.reason == TerminationReason.POLICY_DENIED
    assert not forbidden.exists()
    [out] = tmp_path.glob("codeops-blocked-*")
    assert str(out / "trace_report.html") in str(caught.value)
    _assert_artifacts(out, "blocked")


@pytest.mark.parametrize(
    ("reason", "verdict"),
    [(TerminationReason.TIMED_OUT, "blocked"), (TerminationReason.CANCELLED, "blocked"),
     (TerminationReason.RESOURCE_EXHAUSTED, "blocked"), (TerminationReason.RUNNER_ERROR, "inconclusive")],
)
def test_sandbox_termination_keeps_filtered_issue_and_error(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: TerminationReason, verdict: str,
) -> None:
    issue = tmp_path / "issue.md"
    issue.write_text(f"Bug: {XSS} {SECRET_TEXT}\nExpected: preserve valid output\n")
    TrustPolicy(confirmed=True).save(repo / POLICY_PATH)

    def fail_snapshot(self: SandboxWorkspace, policy: TrustPolicy) -> Path:
        raise RunTerminated(reason, f"synthetic runtime error {XSS} {SECRET_TEXT}")

    monkeypatch.setattr(SandboxWorkspace, "prepare", fail_snapshot)
    out = tmp_path / "out"
    WorkflowOrchestrator().run(TaskRequest(repo_path=repo, issue_path=issue, out_path=out))
    state, html = _assert_artifacts(out, verdict)
    assert state.termination_reason == reason
    trace = (out / "trace_events.jsonl").read_text()
    for secret in SECRETS:
        assert secret not in trace and secret not in html
    assert "[REDACTED password_assignment]" in trace
    assert "&lt;/script&gt;&lt;img" in html
    report = ParsedReport(html)
    assert XSS in next(event["details"]["message"] for event in report.trace_data["events"] if event["kind"] == "error")
    assert all(tag != "img" for tag, _ in report.tags)


def test_trace_filters_nested_details_and_report_stays_offline(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path)
    recorded = recorder.record(
        "setup", "evolution_loaded", "ok", f"Loaded review skill {XSS} {SECRET_TEXT}",
        details={"nested": [{"trace": SECRET_TEXT.encode(), "api_key": "synthetic opaque key"}]},
        refs=[SECRET_TEXT],
    )
    # Direct rendering also has to filter inputs even when they bypass the recorder.
    unfiltered = recorded.model_copy(update={
        "summary": f"Skill {XSS}", "details": {"nested": [{"malicious": XSS, "text": SECRET_TEXT}]},
    })
    state = TaskState(task_id=tmp_path.name, repo_path=tmp_path, run_dir=tmp_path, issue_text=f"{XSS} {SECRET_TEXT}")
    html = render_trace_report(state, Reviewer().review(state), None, [unfiltered])
    for value in (*SECRETS, "synthetic opaque key"):
        assert value not in recorder.path.read_text() and value not in html
    assert "[REDACTED credential]" in recorder.path.read_text()
    assert "&lt;/script&gt;&lt;img" in html and XSS not in html
    report = ParsedReport(html)
    sections = {attrs.get("id") for tag, attrs in report.tags if tag == "section"}
    assert {"trust-summary", "execution", "impact", "data-boundary-flow", "feedback", "evolved-rules"} <= sections
    assert {attrs.get("id") for tag, attrs in report.tags if tag == "svg"} == {
        "execution-dag", "impact-evidence-graph",
    }
    assert {attrs.get("id") for tag, attrs in report.tags if tag == "button"} == {"export-feedback", "copy-feedback"}
    assert sum(tag == "script" for tag, _ in report.tags) == 2
    assert all("src" not in attrs for tag, attrs in report.tags if tag == "script")
    assert not any(tag in {"link", "img"} for tag, _ in report.tags)
    assert report.trace_data["events"][0]["details"]["nested"][0]["malicious"] == XSS
    assert "codeops feedback --run" in html
    assert "Skill &lt;/script&gt;" in html


def _regenerate_without_network(out: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = out / "trace_report.html"
    original = path.read_bytes()
    path.unlink()

    def reject_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Report regeneration attempted network I/O.")

    with monkeypatch.context() as no_network:
        no_network.setattr(socket.socket, "connect", reject_network)
        no_network.setattr(socket.socket, "connect_ex", reject_network)
        result = CliRunner().invoke(app, ["report", "--run", str(out)])
    assert result.exit_code == 0, (result.output, result.exception)
    assert Path(result.output.strip()) == path
    assert path.read_bytes() == original


def test_cli_regenerates_with_only_persisted_artifacts(
    repo: Path, tmp_path: Path, indexed_graph: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "out"
    WorkflowOrchestrator().run(TaskRequest(repo_path=repo, issue_path=ISSUE, out_path=out))
    repo.rename(tmp_path / "repo-unavailable")
    _regenerate_without_network(out, monkeypatch)
    _assert_artifacts(out, "verified")


def test_cli_regenerates_after_real_bwrap_run(
    repo: Path, tmp_path: Path, bwrap_network_enabled: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = TrustPolicyBuilder().build(repo)
    policy.sandbox = SandboxConfig(network_enabled=bwrap_network_enabled)
    policy.save(repo / POLICY_PATH)
    out = tmp_path / "out"
    result = CliRunner().invoke(app, [
        "run", "--repo", str(repo), "--issue", str(ISSUE), "--out", str(out), "--sandbox", "bwrap", "--approve-trust",
    ])
    assert result.exit_code == 0, (result.output, result.exception)
    task = TaskState.model_validate_json((out / "task.json").read_bytes())
    assert task.termination_reason == TerminationReason.COMPLETED and task.sandbox_mode == "bwrap"
    assert task.test_results and all(test.passed for test in task.test_results)
    assert any(
        event.kind == "sandbox_lifecycle" and event.details.get("backend") == "bwrap"
        for event in load_trace_events(out / "trace_events.jsonl")
    )
    repo.rename(tmp_path / "repo-unavailable")
    _regenerate_without_network(out, monkeypatch)
    assert task.verdict in {"verified", "inconclusive"}
    _assert_artifacts(out, task.verdict)
