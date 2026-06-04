"""Safe git command wrapper."""

from pathlib import Path
import subprocess

from pydantic import BaseModel


class GitCommandResult(BaseModel):
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


class GitTool:
    """Run a small set of deterministic git operations."""

    def __init__(self, repo_path: Path | str, timeout_seconds: float = 30.0) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.timeout_seconds = timeout_seconds

    def current_commit(self) -> str | None:
        result = self._run(["rev-parse", "HEAD"])
        if not result.passed:
            return None
        return result.stdout.strip()

    def changed_files(self) -> list[str]:
        result = self._run(["status", "--porcelain"])
        if not result.passed:
            return []
        files: list[str] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.rsplit(" -> ", maxsplit=1)[-1]
            files.append(path)
        return files

    def diff(self, paths: list[str] | None = None) -> str:
        result = self._run(["diff", "--", *(paths or [])])
        return result.stdout

    def apply_check(self, patch_diff: str) -> GitCommandResult:
        return self._run(["apply", "--check", "-"], input_text=patch_diff)

    def apply(self, patch_diff: str) -> GitCommandResult:
        return self._run(["apply", "-"], input_text=patch_diff)

    def _run(
        self,
        args: list[str],
        input_text: str | None = None,
    ) -> GitCommandResult:
        command = ["git", *args]
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_path,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return GitCommandResult(
                args=command,
                returncode=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "git command timed out",
            )
        return GitCommandResult(
            args=command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
