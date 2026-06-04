"""Patch validation and application helpers."""

from pathlib import Path

from pydantic import BaseModel

from codeops.tools.git_tool import GitCommandResult, GitTool


class PatchSummary(BaseModel):
    changed_files: list[str]
    changed_loc: int


class PatchTool:
    """Validate, apply, and summarize unified diffs."""

    def __init__(self, repo_path: Path, timeout_seconds: float = 30.0) -> None:
        self.git = GitTool(repo_path, timeout_seconds=timeout_seconds)

    def check(self, patch_diff: str) -> GitCommandResult:
        return self.git.apply_check(patch_diff)

    def apply(self, patch_diff: str) -> GitCommandResult:
        return self.git.apply(patch_diff)

    def summarize(self, patch_diff: str) -> PatchSummary:
        changed_files: list[str] = []
        changed_loc = 0

        for line in patch_diff.splitlines():
            if line.startswith("+++ "):
                path = line[4:]
                if path != "/dev/null":
                    changed_files.append(_strip_diff_prefix(path))
            elif (
                (line.startswith("+") or line.startswith("-"))
                and not line.startswith("+++")
                and not line.startswith("---")
            ):
                changed_loc += 1

        deduped_files = list(dict.fromkeys(changed_files))
        return PatchSummary(changed_files=deduped_files, changed_loc=changed_loc)


def _strip_diff_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path
