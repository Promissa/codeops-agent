"""Ripgrep-backed lexical search with a Python fallback."""

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from codeops.tools.filesystem_tool import FilesystemTool


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line: int
    column: int
    text: str


class RipgrepTool:
    """Search repository text using `rg` when available."""

    def __init__(self, repo_path: Path, timeout_seconds: float = 5.0) -> None:
        self.repo_path = repo_path.resolve()
        self.timeout_seconds = timeout_seconds
        self.filesystem = FilesystemTool(self.repo_path)

    def search(
        self,
        pattern: str,
        paths: Iterable[str | Path] | None = None,
        max_results: int = 50,
    ) -> list[SearchMatch]:
        path_args = [str(path) for path in paths or []]
        if shutil.which("rg"):
            matches = self._search_with_rg(pattern, path_args, max_results)
            if matches is not None:
                return matches
        return self._search_with_python(pattern, path_args, max_results)

    def _search_with_rg(
        self, pattern: str, path_args: list[str], max_results: int
    ) -> list[SearchMatch] | None:
        command = [
            "rg",
            "--line-number",
            "--column",
            "--no-heading",
            "--color",
            "never",
            pattern,
            *path_args,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        if result.returncode == 1:
            return []
        if result.returncode != 0:
            return None

        return [
            match
            for line in result.stdout.splitlines()[:max_results]
            if (match := _parse_rg_line(line)) is not None
        ]

    def _search_with_python(
        self, pattern: str, path_args: list[str], max_results: int
    ) -> list[SearchMatch]:
        roots = path_args or ["."]
        matches: list[SearchMatch] = []
        for root in roots:
            resolved = self.filesystem.resolve(root)
            files = [resolved] if resolved.is_file() else resolved.rglob("*")
            for path in files:
                if len(matches) >= max_results:
                    return matches
                if not path.is_file() or ".git" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                rel_path = str(path.relative_to(self.repo_path))
                for line_no, line in enumerate(text.splitlines(), start=1):
                    column = line.find(pattern)
                    if column >= 0:
                        matches.append(
                            SearchMatch(
                                path=rel_path,
                                line=line_no,
                                column=column + 1,
                                text=line,
                            )
                        )
                        if len(matches) >= max_results:
                            return matches
        return matches


def _parse_rg_line(line: str) -> SearchMatch | None:
    parts = line.split(":", 3)
    if len(parts) != 4:
        return None
    path, line_no, column, text = parts
    try:
        return SearchMatch(
            path=path,
            line=int(line_no),
            column=int(column),
            text=text,
        )
    except ValueError:
        return None
