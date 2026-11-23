"""Acceptance tests for M1 pre-flight and Linux runtime isolation."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from codeops.cli import app
from codeops.core.artifacts import ArtifactWriter
from codeops.core.errors import CommandRejected
from codeops.core.models import TaskRequest, TaskState
from codeops.runtime.lifecycle import RunLifecycleState, RunTerminated, TerminationReason
from codeops.runtime.registry import RunRegistry, cleanup_stale_runs
from codeops.runtime.sandbox import BwrapSandboxBackend
from codeops.runtime.workspace import MIB, SandboxWorkspace
from codeops.tools.test_runner import TestRunner
from codeops.trust.escalation import EscalationGate, EscalationRequest
from codeops.trust.policy import POLICY_PATH, SandboxConfig, TrustPolicy, WriteBudget
from codeops.trust.preflight import PreFlightPlanner, TrustPolicyBuilder
from codeops.workflow.orchestrator import WorkflowOrchestrator


ROOT = Path(__file__).resolve().parents[1]
ISSUE = ROOT / "examples/issues/csv_trailing_empty_column.md"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return shutil.copytree(ROOT / "examples/fixtures/mini_data_pipeline", tmp_path / "repo")


def _files(path: Path) -> dict[str, bytes]:
    return {
        file.relative_to(path).as_posix(): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file() and not file.is_symlink()
    }


def test_policy_draft_deterministically_discovers_profile_commands(repo: Path) -> None:
    (repo / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    (repo / "test.js").write_text("// fixture")
    (repo / "go.mod").write_text("module example.com/probe\n")
    (repo / "probe_test.go").write_text("package probe\n")
    builder = TrustPolicyBuilder()
    draft = builder.build(repo)
    assert draft.allowed_test_commands == [
        ["go", "test", "-json", "./..."], ["npm", "run", "test"], ["pytest", "-q"],
    ]
    assert draft == builder.build(repo)
    assert not (repo / POLICY_PATH).exists()
    path = builder.write_draft(repo)
    assert path == repo / POLICY_PATH
    assert TrustPolicy.load(path) == draft
    assert not draft.confirmed
    with pytest.raises(RunTerminated, match="already exists"):
        builder.write_draft(repo)


def test_policy_boundaries_and_preflight_preview(repo: Path) -> None:
    policy = TrustPolicy(
        read_allow_globs=["src/**", "logs/**"],
        read_deny_globs=["**/internal"],
        outbound_allow_globs=["**"],
        write_budget=WriteBudget(max_files_changed=1),
        allowed_test_commands=[["pytest", "-q"]],
    )
    planner = PreFlightPlanner()
    contract = planner.plan(repo, ISSUE, policy)
    assert "pytest -q" in contract.summary()
    assert "1 files" in contract.summary()
    assert "Other repositories" in contract.summary()
    assert "network disabled" in contract.summary()
    assert policy.permits_read("src/pkg/module.py")
    assert not policy.permits_read("src/internal/module.py")
    for path in [".env", "src/.env.local", "src/id_rsa.pub", "src/client.key", "src/cert.pem", "../other.py", "/tmp/outside"]:
        assert not policy.permits_read(path)
        assert not policy.permits_outbound(path)
    default_outbound = policy.model_copy(update={"read_deny_globs": []})
    assert default_outbound.permits_read("logs/events.log")
    assert not default_outbound.permits_outbound("logs/events.log")
    assert not default_outbound.permits_outbound("config/local.toml")
    assert default_outbound.permits_outbound("tests/test_parser.py")
    assert not default_outbound.permits_read("tests/test_parser.py")
    with pytest.raises(RunTerminated) as denied:
        planner.approve(contract)
    assert denied.value.reason == TerminationReason.POLICY_DENIED
    assert planner.approve(contract, preapproved=True).approved
    assert planner.approve(planner.plan(repo, ISSUE, TrustPolicy(confirmed=True))).approved
    assert not (repo / POLICY_PATH).exists()


def test_escalations_require_opt_in_and_spend_budget() -> None:
    read = EscalationRequest(boundary="local-read", path="issue.md", reason="Read issue.")
    outbound = EscalationRequest(boundary="outbound", path="src/parser.py", reason="Send context.")
    cross_repo = EscalationRequest(boundary="cross-repo", path="../shared", reason="Inspect client.")
    assert EscalationGate(2).decide(read).reason == "escalation blocked in non-interactive mode"

    prompted = []

    def approve(request: EscalationRequest) -> bool:
        prompted.append(request)
        return request == read

    gate = EscalationGate(2, approve)
    assert gate.decide(read).approved
    denied = gate.decide(outbound)
    assert not denied.approved and denied.remaining_budget == 0
    assert gate.decide(cross_repo).reason == "escalation budget exhausted"
    assert prompted == [read, outbound]


def test_lifecycle_terminal_reasons_and_registry_recovery(tmp_path: Path) -> None:
    assert [phase.value for phase in RunLifecycleState] == [
        "preparing", "executing", "verifying", "pending_export", "finished",
    ]
    assert {reason.value for reason in TerminationReason} == {
        "completed", "policy_denied", "cancelled", "timed_out", "resource_exhausted", "runner_error",
    }
    registry = RunRegistry(tmp_path / "state")
    (registry.state_dir / "run-abandoned").mkdir()
    with registry.register() as live:
        live.record(123)
        assert json.loads((live.path / "active.json").read_text())["command_pid"] == 123
        assert cleanup_stale_runs(registry.state_dir) == 1
        assert live.path.exists()
    assert list(registry.state_dir.glob("run-*")) == []


def test_snapshot_omits_denied_paths_links_and_other_repos(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside marker")
    (repo / "alias.py").symlink_to(outside)
    os.link(outside, repo / "hardalias.py")
    (repo / ".env").write_bytes(b"denied marker")
    (repo / "nested").mkdir()
    (repo / "nested/.git").write_text("gitdir: ../other")
    (repo / "nested/file.py").write_text("nested marker")
    (repo / "cert.pem").write_bytes(b"private key marker")
    os.mkfifo(repo / "fifo")

    registry = RunRegistry(tmp_path / "state")
    with registry.register() as run:
        workspace = SandboxWorkspace(repo, run.path, 1)
        snapshot = workspace.prepare(TrustPolicy(read_allow_globs=["**"]))
        assert (snapshot / "src/mini_data_pipeline/parser.py").is_file()
        assert not {"alias.py", "hardalias.py", ".env", "nested", "cert.pem", "fifo"}.intersection(
            entry.name for entry in snapshot.iterdir()
        )
    assert (repo / ".env").read_bytes() == b"denied marker"
    assert not run.path.exists()


def test_trust_cli_draft_preview_and_noninteractive_denial(repo: Path, tmp_path: Path) -> None:
    cli = CliRunner()
    init = cli.invoke(app, ["trust-init", "--repo", str(repo)])
    assert init.exit_code == 0, init.output
    before = _files(repo)
    preview = cli.invoke(app, ["trust-plan", "--repo", str(repo), "--issue", str(ISSUE)])
    assert preview.exit_code == 0, preview.output
    assert "pytest -q" in preview.output and "Outbound allow — none" in preview.output
    out = tmp_path / "out"
    denied = cli.invoke(app, _cli_args(repo, out))
    assert denied.exit_code == 1 and "policy_denied" in denied.output
    assert (out / "trust_contract.yaml").exists()
    assert not (out / "repo_sketch.md").exists()
    assert _files(repo) == before


def test_trust_write_budget_rejects_patch_and_preserves_source(repo: Path, tmp_path: Path) -> None:
    policy = TrustPolicyBuilder().build(repo)
    policy.confirmed = True
    policy.sandbox.backend = "none"
    policy.write_budget.max_files_changed = 1
    policy.save(repo / POLICY_PATH)
    before = _files(repo)
    out = tmp_path / "out"
    state = WorkflowOrchestrator().run(TaskRequest(repo_path=repo, issue_path=ISSUE, out_path=out))
    assert state.termination_reason == TerminationReason.POLICY_DENIED
    assert state.lifecycle_state == RunLifecycleState.FINISHED
    assert "changed file count exceeds envelope" in (out / "final_report.md").read_text()
    assert not (out / "patch.diff").exists()
    assert _files(repo) == before


def test_snapshot_quota_is_a_terminal_resource_reason(repo: Path, tmp_path: Path) -> None:
    (repo / "large.dat").write_bytes(b"0" * (MIB + 1))
    policy = TrustPolicy(confirmed=True, sandbox=SandboxConfig(workspace_quota_mb=1))
    policy.save(repo / POLICY_PATH)
    out = tmp_path / "out"
    state = WorkflowOrchestrator().run(TaskRequest(repo_path=repo, issue_path=ISSUE, out_path=out))
    assert state.lifecycle_state == RunLifecycleState.FINISHED
    assert state.termination_reason == TerminationReason.RESOURCE_EXHAUSTED
    assert not (out / "repo_sketch.md").exists()


def test_outbound_fallback_is_blocked_without_noninteractive_grant(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        def generate_patch(self, *args: object) -> None:
            pytest.fail("Outbound provider received unapproved context")

    monkeypatch.setattr("codeops.workflow.orchestrator._llm_patch_provider", lambda request: Provider())
    issue = tmp_path / "issue.md"
    issue.write_text("Bug: parser breaks line handling\nExpected: retain line endings\n")
    policy = TrustPolicy(confirmed=True, sandbox=SandboxConfig(backend="none", network_enabled=True))
    policy.save(repo / POLICY_PATH)
    out = tmp_path / "out"
    state = WorkflowOrchestrator().run(TaskRequest(repo_path=repo, issue_path=issue, out_path=out))
    assert state.termination_reason == TerminationReason.POLICY_DENIED
    decisions = json.loads((out / "trust_escalations.json").read_text())
    assert decisions[0]["request"]["boundary"] == "outbound"
    assert not decisions[0]["approved"]


@contextmanager
def _backend(
    repo: Path, registry: RunRegistry, config: SandboxConfig,
) -> Iterator[BwrapSandboxBackend]:
    with registry.register() as run:
        workspace = SandboxWorkspace(repo, run.path, config.workspace_quota_mb)
        workspace.prepare(TrustPolicy())
        yield BwrapSandboxBackend(workspace, run, config)


def _outer_namespace_restriction(exc: RunTerminated) -> bool:
    message = str(exc).lower()
    return exc.reason == TerminationReason.RUNNER_ERROR and (
        "operation not permitted" in message or "permission denied" in message
    ) and any(marker in message for marker in ("namespace", "netlink_route", "unshare:", "mount: "))


@pytest.fixture(scope="module")
def bwrap_network_enabled(tmp_path_factory: pytest.TempPathFactory) -> bool:
    """Prefer network isolation; probe mounts/PID separately if net setup is blocked."""
    root = tmp_path_factory.mktemp("bubblewrap")
    registry = RunRegistry(root / "state")
    repo = root / "repo"
    repo.mkdir()
    for network_enabled in (False, True):
        try:
            with _backend(repo, registry, SandboxConfig(network_enabled=network_enabled)) as backend:
                backend.probe()
        except RunTerminated as exc:
            if not _outer_namespace_restriction(exc):
                raise
            if network_enabled:
                pytest.skip(f"Outer sandbox restricts Bubblewrap namespaces: {exc}")
        else:
            return network_enabled
    raise AssertionError("Unreachable Bubblewrap probe state")


def _python(
    backend: BwrapSandboxBackend, script: str, *args: str, timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    return backend.run(
        ["python", "-c", script, *args],
        cwd=backend.workspace.path, timeout_seconds=timeout,
    )


def test_bwrap_adversarial_read_isolation(
    repo: Path, tmp_path: Path, bwrap_network_enabled: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside-marker"
    home = tmp_path / "home"
    home.mkdir()
    private = home / "home-marker"
    outside.write_text("outside secret marker")
    private.write_text("home secret marker")
    (repo / ".env").write_text("workspace secret marker")
    (repo / ".git").mkdir()
    (repo / ".git/config").write_text("credential marker")
    (repo / "escape.py").symlink_to(private)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEOPS_TEST_MARKER", "inherited secret marker")
    before = _files(repo)
    config = SandboxConfig(network_enabled=bwrap_network_enabled)

    with _backend(repo, RunRegistry(tmp_path / "state"), config) as backend:
        snapshot = backend.workspace.path
        assert ".env" not in _files(snapshot)
        assert not (snapshot / ".git").exists()
        assert not (snapshot / "escape.py").is_symlink()
        with pytest.raises(CommandRejected):
            TestRunner(snapshot, sandbox_backend=backend).run(["python", "-c", "pass"])
        result = _python(backend, """
import os
from pathlib import Path
import sys

assert Path.cwd() == Path('/workspace')
assert os.environ['HOME'] == '/sandbox_home'
assert os.environ['XDG_CACHE_HOME'] == '/sandbox_home/.cache'
assert Path(os.environ['XDG_RUNTIME_DIR']).stat().st_mode & 0o777 == 0o700
assert 'CODEOPS_TEST_MARKER' not in os.environ
for target in ['/workspace/.env', '/workspace/.git/config', '/workspace/escape.py', *sys.argv[1:]]:
    try:
        Path(target).read_bytes()
    except FileNotFoundError:
        continue
    raise AssertionError(f'Unexpected readable file {target}')
Path('changed-in-snapshot.txt').write_text('sandbox write')
""", str(outside), str(private), str(repo / ".env"))
        assert result.returncode == 0, result.stderr
        persisted = _python(backend, "from pathlib import Path; assert Path('changed-in-snapshot.txt').is_file()")
        assert persisted.returncode == 0, persisted.stderr
    assert _files(repo) == before
    assert (repo / "escape.py").readlink() == private


def test_bwrap_private_tmp_concurrent(
    repo: Path, tmp_path: Path, bwrap_network_enabled: bool,
) -> None:
    host_probe = Path("/tmp/probe")
    before = host_probe.read_bytes() if host_probe.exists() else None
    registry = RunRegistry(tmp_path / "state")
    config = SandboxConfig(network_enabled=bwrap_network_enabled, tmpfs_size_mb=4)
    script = """
import os
from pathlib import Path
import sys
import time

probe = Path('/tmp/probe')
assert not probe.exists()
assert os.statvfs('/tmp').f_frsize * os.statvfs('/tmp').f_blocks == 4 * 1024 * 1024
probe.write_text(sys.argv[1])
time.sleep(0.3)
assert probe.read_text() == sys.argv[1]
assert Path('/var/tmp/probe').read_text() == sys.argv[1]
print(probe.read_text())
"""

    def run(payload: str) -> str:
        with _backend(repo, registry, config) as backend:
            result = _python(backend, script, payload)
            assert result.returncode == 0, result.stderr
            persisted = _python(backend, "from pathlib import Path; print(Path('/tmp/probe').read_text())")
            assert persisted.returncode == 0, persisted.stderr
            assert persisted.stdout.strip() == payload
            return result.stdout.strip()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ["first task", "second task"])) == ["first task", "second task"]
    after = host_probe.read_bytes() if host_probe.exists() else None
    assert after == before
    assert list(registry.state_dir.glob("run-*")) == []


def test_bwrap_private_network(
    repo: Path, tmp_path: Path, bwrap_network_enabled: bool,
) -> None:
    if bwrap_network_enabled:
        pytest.skip("Outer sandbox blocks NETLINK_ROUTE during Bubblewrap network namespace setup")
    host_net = os.readlink("/proc/self/ns/net")
    with _backend(repo, RunRegistry(tmp_path / "state"), SandboxConfig()) as backend:
        result = _python(backend, """
import os
from pathlib import Path
import socket
import sys

assert os.readlink('/proc/self/ns/net') != sys.argv[1]
interfaces = {line.split(':')[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:]}
assert interfaces <= {'lo'}
with socket.socket() as connection:
    connection.settimeout(0.5)
    assert connection.connect_ex(('192.0.2.1', 80)) != 0
""", host_net)
        assert result.returncode == 0, result.stderr


def test_bwrap_workspace_growth_triggers_resource_limit(
    repo: Path, tmp_path: Path, bwrap_network_enabled: bool,
) -> None:
    registry = RunRegistry(tmp_path / "state")
    config = SandboxConfig(network_enabled=bwrap_network_enabled, workspace_quota_mb=1)
    with _backend(repo, registry, config) as backend:
        with pytest.raises(RunTerminated) as caught:
            _python(backend, """
from pathlib import Path
import time

for name in ['first.bin', 'second.bin']:
    Path(name).write_bytes(b'0' * 600_000)
time.sleep(5)
""")
        assert caught.value.reason == TerminationReason.RESOURCE_EXHAUSTED
    assert list(registry.state_dir.glob("run-*")) == []


def _descendants(pid: int) -> set[int]:
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except FileNotFoundError:
        return set()
    return {int(child) for child in children} | {
        descendant for child in children for descendant in _descendants(int(child))
    }


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_bwrap_timeout_kills_child_and_detached_grandchild(
    repo: Path, tmp_path: Path, bwrap_network_enabled: bool,
) -> None:
    registry = RunRegistry(tmp_path / "state")
    config = SandboxConfig(network_enabled=bwrap_network_enabled)
    script = """
import subprocess
import sys
import time

grandchild = "from pathlib import Path; import time; Path('grandchild-ready').touch(); time.sleep(30)"
child = "import subprocess, sys; subprocess.run([sys.executable, '-c', sys.argv[1]], start_new_session=True)"
subprocess.Popen([sys.executable, '-c', child, grandchild])
time.sleep(30)
"""
    with _backend(repo, registry, config) as backend, ThreadPoolExecutor(max_workers=1) as pool:
        runtime_dir = backend.active_run.path
        pending = pool.submit(_python, backend, script, timeout=1.5)
        ready = backend._mounts.workspace_view() / "grandchild-ready"
        deadline = time.monotonic() + 1.2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        command_pid = json.loads((runtime_dir / "active.json").read_text())["command_pid"]
        pids = {command_pid, *_descendants(command_pid)}
        assert len(pids) >= 4
        with pytest.raises(RunTerminated) as caught:
            pending.result(timeout=5)
        assert caught.value.reason == TerminationReason.TIMED_OUT
        deadline = time.monotonic() + 2
        while any(map(_alive, pids)) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert all(not _alive(pid) for pid in pids)
    assert not runtime_dir.exists()


def test_bwrap_stale_registry_releases_mount_keeper(
    repo: Path, tmp_path: Path, bwrap_network_enabled: bool,
) -> None:
    state_dir = tmp_path / "state"
    child = subprocess.run(
        [sys.executable, "-c", """
import os
from pathlib import Path
import sys

from codeops.runtime.registry import RunRegistry
from codeops.runtime.sandbox import BwrapSandboxBackend
from codeops.runtime.workspace import SandboxWorkspace
from codeops.trust.policy import SandboxConfig, TrustPolicy

with RunRegistry(Path(sys.argv[2])).register() as run:
    workspace = SandboxWorkspace(Path(sys.argv[1]), run.path, 8)
    workspace.prepare(TrustPolicy())
    backend = BwrapSandboxBackend(workspace, run, SandboxConfig(network_enabled=sys.argv[3] == 'True'))
    backend.probe()
    print(run.path, flush=True)
    os._exit(0)
""", str(repo), str(state_dir), str(bwrap_network_enabled)],
        capture_output=True, text=True, check=True, timeout=10,
    )
    run_dir = Path(child.stdout.strip())
    mount_pid = json.loads((run_dir / "active.json").read_text())["mount_pid"]
    deadline = time.monotonic() + 2
    while _alive(mount_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _alive(mount_pid)
    assert cleanup_stale_runs(state_dir) == 1
    assert not run_dir.exists()


def _cli_args(repo: Path, out: Path) -> list[str]:
    return ["run", "--repo", str(repo), "--issue", str(ISSUE), "--out", str(out)]


def test_bwrap_end_to_end_cli_preserves_repo_and_records_lifecycle(
    repo: Path, tmp_path: Path, bwrap_network_enabled: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo / ".env").write_text("private marker")
    config = SandboxConfig(network_enabled=bwrap_network_enabled)
    policy = TrustPolicyBuilder().build(repo)
    policy.sandbox = config
    policy.save(repo / POLICY_PATH)
    before = _files(repo)
    out = tmp_path / "out"
    checkpoints = []
    write = ArtifactWriter.write_json

    def record(writer: ArtifactWriter, name: str, data: object) -> Path:
        if name == "task" and isinstance(data, TaskState):
            checkpoints.append(data.lifecycle_state)
            if data.lifecycle_state == RunLifecycleState.EXECUTING:
                assert (out / "trust_contract.yaml").is_file()
                assert not (out / "repo_sketch.md").exists()
        return write(writer, name, data)

    monkeypatch.setattr(ArtifactWriter, "write_json", record)
    result = CliRunner().invoke(app, [*_cli_args(repo, out), "--sandbox", "bwrap", "--approve-trust"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert _files(repo) == before
    assert list(dict.fromkeys(checkpoints)) == list(RunLifecycleState)
    assert "test_trailing_empty_column_returns_none" in (out / "patch.diff").read_text()
    tests = json.loads((out / "test_results.json").read_text())
    assert tests and all(test["passed"] for test in tests)
    assert tests[0]["command"] == "pytest -q"
    task = json.loads((out / "task.json").read_text())
    assert task["repo_path"] == str(repo)
    assert task["sandbox_mode"] == "bwrap"
    assert task["lifecycle_state"] == "finished" and task["termination_reason"] == "completed"
    for filename in ["acceptance_contract.yaml", "evidence_matrix.md", "intent_manifest.md", "final_report.md"]:
        assert (out / filename).is_file()
