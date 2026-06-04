"""Safe filesystem access within a repository."""

from pathlib import Path

from codeops.core.errors import FileTooLarge, PathOutsideRepo, ToolError


class FilesystemTool:
    """Read repository files with traversal and size checks."""

    def __init__(self, repo_path: Path, max_file_bytes: int = 1_000_000) -> None:
        self.repo_path = repo_path.resolve()
        self.max_file_bytes = max_file_bytes

    def resolve(self, path: str | Path) -> Path:
        candidate = (self.repo_path / path).resolve()
        try:
            candidate.relative_to(self.repo_path)
        except ValueError as exc:
            raise PathOutsideRepo(f"path escapes repo: {path}") from exc
        return candidate

    def read_text(self, path: str | Path, encoding: str = "utf-8") -> str:
        resolved = self.resolve(path)
        if not resolved.is_file():
            raise ToolError(f"path is not a file: {path}")

        size = resolved.stat().st_size
        if size > self.max_file_bytes:
            raise FileTooLarge(
                f"file {path} is {size} bytes, limit is {self.max_file_bytes}"
            )
        return resolved.read_text(encoding=encoding)
