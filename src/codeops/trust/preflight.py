"""Build and preview a task's scope before routing or running commands."""

from collections.abc import Callable
import errno
import os
from pathlib import Path

from pydantic import BaseModel, Field

from codeops.core.models import TestCommand
from codeops.languages.detector import ProjectDetector
from codeops.languages.registry import LanguageProfileRegistry
from codeops.runtime.lifecycle import RunTerminated, TerminationReason
from codeops.runtime.registry import DEFAULT_RUN_STATE_DIR, RunRegistry
from codeops.runtime.workspace import SandboxWorkspace, open_regular
from codeops.safety.command_policy import CommandPolicy
from codeops.trust.policy import POLICY_PATH, SandboxConfig, TrustPolicy, WriteBudget


class TaskTrustContract(BaseModel):
    repo_path: Path
    issue_path: Path | None = None
    policy_path: Path | None = None
    approved: bool = False
    read_allow_globs: list[str]
    read_deny_globs: list[str]
    excluded_scopes: list[str] = Field(default_factory=lambda: [
        "Host HOME outside the selected repository",
        "Other repositories, symlinks, hard links, and special files",
    ])
    outbound_allow_globs: list[str]
    outbound_deny_globs: list[str]
    write_budget: WriteBudget
    sandbox: SandboxConfig
    allowed_test_commands: list[list[str]]
    escalation_budget: int

    def summary(self) -> str:
        budget = self.write_budget
        commands = [" ".join(command) for command in self.allowed_test_commands]
        network = "enabled" if self.sandbox.network_enabled else "disabled"
        if self.sandbox.backend == "none":
            network = "uses host namespace"
        return "\n".join([
            f"Workspace — {self.repo_path}",
            f"Issue input — {self.issue_path}",
            f"Approval — {'confirmed' if self.approved else 'draft'}",
            f"Visible paths — {', '.join(self.read_allow_globs) or 'none'}",
            f"Excluded — {', '.join([*self.excluded_scopes, *self.read_deny_globs])}",
            f"Outbound allow — {', '.join(self.outbound_allow_globs) or 'none'}",
            f"Outbound deny — {', '.join(self.outbound_deny_globs)}",
            f"Writes — {budget.max_files_changed} files, {budget.max_loc_changed} changed lines, "
            f"dependencies {'allowed' if budget.allow_dependency_change else 'denied'}, "
            f"public API changes {'allowed' if budget.allow_public_api_change else 'denied'}",
            f"Commands — {', '.join(commands) or 'none'}",
            f"Runtime — {self.sandbox.backend}, network "
            f"{network}, "
            f"{self.sandbox.timeout_seconds}s timeout, {self.sandbox.tmpfs_size_mb} MiB tmpfs, "
            f"{self.sandbox.workspace_quota_mb} MiB workspace quota",
            f"Escalations — {self.escalation_budget}",
        ])


class TrustPolicyBuilder:
    """Discover exact test commands using the same language profiles as CodeOps."""

    def __init__(self, registry: LanguageProfileRegistry | None = None) -> None:
        self.registry = registry or LanguageProfileRegistry()

    def discover_commands(self, snapshot: Path) -> list[TestCommand]:
        project = ProjectDetector(self.registry).detect(snapshot)
        commands: dict[tuple[str, ...], TestCommand] = {}
        policy = CommandPolicy()
        for profile in self.registry.profiles():
            if profile.name not in project.language_profiles:
                continue
            for command in profile.discover_test_commands(snapshot):
                if policy.validate(command.command).allowed:
                    commands.setdefault(tuple(command.command), command)
        return [commands[key] for key in sorted(commands)]

    def build(self, repo_path: Path) -> TrustPolicy:
        policy = TrustPolicy()
        if DEFAULT_RUN_STATE_DIR.resolve().is_relative_to(repo_path.resolve()):
            raise RunTerminated(TerminationReason.POLICY_DENIED, "Run registry overlaps the repository.")
        registry = RunRegistry()
        registry.cleanup_stale_runs()
        with registry.register() as run:
            workspace = SandboxWorkspace(repo_path, run.path, policy.sandbox.workspace_quota_mb)
            workspace.prepare(policy)
            commands = self.discover_commands(workspace.path)
        return policy.model_copy(update={"allowed_test_commands": [cmd.command for cmd in commands]})

    def write_draft(self, repo_path: Path) -> Path:
        repo_path = repo_path.resolve()
        path = repo_path / POLICY_PATH
        if path.parent.is_symlink() or path.is_symlink() or path.exists():
            raise RunTerminated(
                TerminationReason.POLICY_DENIED, f"Trust policy already exists or uses a link: {path}",
            )
        policy = self.build(repo_path)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = os.open(repo_path, directory_flags)
        try:
            try:
                os.mkdir(POLICY_PATH.parent, mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            config = os.open(POLICY_PATH.parent, directory_flags, dir_fd=root)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                fd = os.open(POLICY_PATH.name, flags, mode=0o600, dir_fd=config)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(policy.to_yaml())
            finally:
                os.close(config)
        finally:
            os.close(root)
        return path


class PreFlightPlanner:
    def resolve_policy(
        self, repo_path: Path, policy_path: Path | None = None,
    ) -> tuple[TrustPolicy, Path | None]:
        path = Path(os.path.abspath(policy_path or repo_path / POLICY_PATH))
        if policy_path is None and not path.is_symlink() and not path.exists():
            return TrustPolicy(), None
        try:
            with open_regular(Path(path.anchor), path.relative_to(path.anchor)) as source:
                return TrustPolicy.parse(source.read().decode("utf-8")), path
        except (OSError, ValueError) as exc:
            raise RunTerminated(TerminationReason.POLICY_DENIED, f"Cannot load trust policy: {exc}") from exc

    def plan(
        self, repo_path: Path, issue_path: Path | None, policy: TrustPolicy, *,
        policy_path: Path | None = None,
    ) -> TaskTrustContract:
        return TaskTrustContract(
            repo_path=repo_path.resolve(),
            issue_path=issue_path.absolute() if issue_path else None,
            policy_path=policy_path,
            approved=policy.confirmed,
            read_allow_globs=policy.read_allow_globs,
            read_deny_globs=policy.effective_read_denies,
            outbound_allow_globs=policy.outbound_allow_globs,
            outbound_deny_globs=policy.effective_outbound_denies,
            write_budget=policy.write_budget,
            sandbox=policy.sandbox,
            allowed_test_commands=policy.allowed_test_commands,
            escalation_budget=policy.escalation_budget,
        )

    def approve(
        self, contract: TaskTrustContract, *,
        approve: Callable[[TaskTrustContract], bool] | None = None,
        preapproved: bool = False,
    ) -> TaskTrustContract:
        if contract.approved or preapproved or (approve is not None and approve(contract)):
            return contract.model_copy(update={"approved": True})
        raise RunTerminated(TerminationReason.POLICY_DENIED, "Trust policy requires confirmation.")

    def preview(self, repo_path: Path, issue_path: Path) -> TaskTrustContract:
        policy, path = self.resolve_policy(repo_path)
        if path is None:
            policy = TrustPolicyBuilder().build(repo_path)
        return self.plan(repo_path, issue_path, policy, policy_path=path)

    def read_issue(self, contract: TaskTrustContract, policy: TrustPolicy) -> str:
        if contract.issue_path is None:
            return ""
        path = Path(os.path.abspath(contract.issue_path))
        if path.is_relative_to(contract.repo_path):
            relative = path.relative_to(contract.repo_path).as_posix()
            if not policy.permits_read(relative):
                raise RunTerminated(TerminationReason.POLICY_DENIED, "Issue path is outside local read scope.")
        elif policy.denies_read(path.relative_to(path.anchor).as_posix()):
            raise RunTerminated(TerminationReason.POLICY_DENIED, "Issue path is excluded by read policy.")
        try:
            with open_regular(Path(path.anchor), path.relative_to(path.anchor)) as source:
                return source.read().decode("utf-8")
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise RunTerminated(TerminationReason.POLICY_DENIED, "Issue path uses a link.") from exc
            raise

    def outbound_issue_path(self, contract: TaskTrustContract) -> str:
        path = contract.issue_path
        if path is None:
            raise ValueError("The trust contract has no issue input.")
        if path.is_relative_to(contract.repo_path):
            return path.relative_to(contract.repo_path).as_posix()
        return f"@issue/{path.name}"


def approved_commands(
    contract: TaskTrustContract, discovered: list[TestCommand], snapshot: Path,
) -> list[TestCommand]:
    by_argv = {tuple(command.command): command for command in discovered}
    return [
        by_argv.get(tuple(argv)) or TestCommand(
            id=f"trust_test_{index}", command=argv, cwd=snapshot,
            scope="full", language="unknown",
        )
        for index, argv in enumerate(contract.allowed_test_commands)
    ]
