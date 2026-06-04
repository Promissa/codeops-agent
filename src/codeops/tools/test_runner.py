"""Allowlisted local test runner."""

from pathlib import Path
import subprocess
import time
from typing import Sequence

from codeops.core.models import TestResult
from codeops.safety.command_policy import CommandPolicy


class TestRunner:
    """Run allowlisted commands without a shell."""

    def __init__(
        self,
        repo_path: Path,
        policy: CommandPolicy | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.repo_path = repo_path.resolve()
        self.policy = policy or CommandPolicy()
        self.timeout_seconds = timeout_seconds

    def run(self, command: str | Sequence[str]) -> TestResult:
        argv = self.policy.enforce(command)
        started = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
            duration = time.monotonic() - started
            return TestResult(
                command=" ".join(argv),
                passed=result.returncode == 0,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            return TestResult(
                command=" ".join(argv),
                passed=False,
                exit_code=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "command timed out",
                duration_seconds=duration,
            )
        except OSError as exc:
            duration = time.monotonic() - started
            return TestResult(
                command=" ".join(argv),
                passed=False,
                exit_code=127,
                stdout="",
                stderr=str(exc),
                duration_seconds=duration,
            )
