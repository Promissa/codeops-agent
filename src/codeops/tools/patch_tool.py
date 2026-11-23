"""Patch validation and application helpers."""

from pathlib import Path

from pydantic import BaseModel

from codeops.runtime.lifecycle import RunTerminated, TerminationReason
from codeops.tools.git_tool import GitCommandResult, GitTool


class PatchSummary(BaseModel):
    changed_files: list[str]
    changed_loc: int


class PatchTool:
    """Validate, apply, and summarize unified diffs."""

    def __init__(
        self, repo_path: Path, timeout_seconds: float = 30.0, *, git: GitTool | None = None,
    ) -> None:
        self.git = git or GitTool(repo_path, timeout_seconds=timeout_seconds)

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

    def summarize_checked(self, patch_diff: str) -> PatchSummary:
        """Use Git's parsed destinations and reject operations that can hide paths."""
        for line in patch_diff.splitlines():
            if line.startswith(("old mode ", "new mode ", "rename ", "copy ", "GIT binary patch")):
                raise RunTerminated(TerminationReason.POLICY_DENIED, "Unsupported patch operation.")
            if line.startswith(("new file mode ", "deleted file mode ")) and line.split()[-1] not in {
                "100644", "100755",
            }:
                raise RunTerminated(TerminationReason.POLICY_DENIED, "Patch uses a non-regular file.")
        result = self.git.patch_numstat(patch_diff)
        paths: list[str] = []
        changed_loc = 0
        if result.passed and result.stdout.endswith("\0"):
            for entry in result.stdout[:-1].split("\0"):
                fields = entry.split("\t", maxsplit=2)
                if len(fields) != 3 or not all(count.isascii() and count.isdigit() for count in fields[:2]):
                    break
                added, deleted, path = fields
                if not path or "\ufffd" in path:
                    break
                paths.append(path)
                changed_loc += int(added) + int(deleted)
            else:
                return PatchSummary(changed_files=list(dict.fromkeys(paths)), changed_loc=changed_loc)
        raise RunTerminated(TerminationReason.POLICY_DENIED, "Patch has no valid textual change summary.")


def _strip_diff_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path
