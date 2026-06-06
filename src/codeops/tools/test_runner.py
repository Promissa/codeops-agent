"""Allowlisted local test runner."""

from pathlib import Path
import shutil
import subprocess
import time
from typing import Sequence

from codeops.core.models import CheckCommand, TestCommand, TestResult
from codeops.safety.command_policy import CommandPolicy
from codeops.tools.test_output_parser import parse_go_test_json


RunnableCommand = TestCommand | CheckCommand | Sequence[str]


class TestRunner:
    """Run allowlisted commands without a shell."""

    __test__ = False

    def __init__(
        self,
        repo_path: Path,
        policy: CommandPolicy | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.repo_path = repo_path.resolve()
        self.policy = policy or CommandPolicy()
        self.timeout_seconds = timeout_seconds

    def run(self, command: RunnableCommand) -> TestResult:
        argv, cwd, timeout_seconds, env = self._prepare(command)
        argv = self.policy.enforce(argv)
        display_command = " ".join(argv)
        argv = _resolve_executable(argv)
        started = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
                env=env or None,
            )
            duration = time.monotonic() - started
            passed = result.returncode == 0
            if isinstance(command, TestCommand | CheckCommand) and command.parse_format == "go_json":
                passed = passed and parse_go_test_json(result.stdout).passed
            return TestResult(
                command=display_command,
                passed=passed,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            return TestResult(
                command=display_command,
                passed=False,
                exit_code=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "command timed out",
                duration_seconds=duration,
            )
        except OSError as exc:
            duration = time.monotonic() - started
            return TestResult(
                command=display_command,
                passed=False,
                exit_code=127,
                stdout="",
                stderr=str(exc),
                duration_seconds=duration,
            )

    def run_many(self, commands: Sequence[RunnableCommand]) -> list[TestResult]:
        return [self.run(command) for command in commands]

    def _prepare(
        self, command: RunnableCommand
    ) -> tuple[list[str], Path, float, dict[str, str]]:
        if isinstance(command, TestCommand | CheckCommand):
            cwd = command.cwd.resolve()
            if not _is_relative_to(cwd, self.repo_path):
                cwd = self.repo_path
            return (
                list(command.command),
                cwd,
                float(command.timeout_seconds),
                command.env,
            )
        return list(command), self.repo_path, self.timeout_seconds, {}


def _resolve_executable(argv: list[str]) -> list[str]:
    found = shutil.which(argv[0])
    if found is None:
        return argv
    return [str(Path(found).resolve()), *argv[1:]]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
