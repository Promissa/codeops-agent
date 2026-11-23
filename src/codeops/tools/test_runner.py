"""Allowlisted local test runner."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Sequence

from codeops.core.models import CheckCommand, TestCommand, TestResult
from codeops.runtime.sandbox import SandboxBackend
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
        sandbox_backend: SandboxBackend | None = None,
        pytest_probe: Path | None = None,
    ) -> None:
        self.repo_path = repo_path.resolve()
        self.policy = policy or CommandPolicy()
        self.timeout_seconds = timeout_seconds
        self.sandbox_backend = sandbox_backend
        self.pytest_probe = pytest_probe

    def run(self, command: RunnableCommand) -> TestResult:
        argv, cwd, timeout_seconds, env = self._prepare(command)
        argv = self.policy.enforce(argv)
        display_command = " ".join(argv)
        if self.pytest_probe is not None and (
            argv[0] == "pytest" or argv[:3] == ["python", "-m", "pytest"]
        ):
            offset = 1 if argv[0] == "pytest" else 3
            argv[offset:offset] = ["-p", self.pytest_probe.stem]
            if self.sandbox_backend is None:
                env = {**os.environ, **env}
                env["PYTHONPATH"] = os.pathsep.join(filter(None, (
                    str(self.pytest_probe.parent), env.get("PYTHONPATH"),
                )))
        started = time.monotonic()
        try:
            if self.sandbox_backend is not None:
                result = self.sandbox_backend.run(argv, cwd=cwd, timeout_seconds=timeout_seconds)
            else:
                result = subprocess.run(
                    _resolve_executable(argv),
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
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr) or "command timed out",
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
        if argv[0] == "pytest":
            return [sys.executable, "-m", "pytest", *argv[1:]]
        return argv
    return [str(Path(found).resolve()), *argv[1:]]


def _as_text(output: bytes | str | None) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else output or ""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
