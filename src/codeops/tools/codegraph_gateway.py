"""CodeGraph CLI gateway."""

from collections.abc import Callable
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from pydantic import BaseModel

from codeops.core.models import SymbolRef


class CodeGraphCommandResult(BaseModel):
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        return self.returncode == 0


class GraphStatus(BaseModel):
    available: bool
    repo_path: Path
    index_fresh: bool = False
    message: str = ""
    raw: dict[str, Any] | None = None


class GraphFile(BaseModel):
    path: str
    language: str | None = None
    indexed: bool = True


class SymbolHit(BaseModel):
    symbol: str
    path: str
    kind: str | None = None
    line: int | None = None
    score: float | None = None


class ImpactReport(BaseModel):
    symbol: str
    affected_symbols: list[SymbolRef] = []
    affected_files: list[str] = []
    affected_tests: list[str] = []
    warnings: list[str] = []


Runner = Callable[[Path, list[str], str | None], CodeGraphCommandResult]


class CodeGraphGateway:
    """Wrap CodeGraph behind a stable internal API."""

    def __init__(
        self,
        executable: str = "codegraph",
        timeout_seconds: float = 15.0,
        runner: Runner | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self._runner = runner

    def status(self, repo_path: Path) -> GraphStatus:
        result = self._run(repo_path, ["status"])
        if result.returncode == 127:
            return GraphStatus(
                available=False,
                repo_path=repo_path,
                message=result.stderr or "CodeGraph executable not found",
            )
        if not result.passed:
            return GraphStatus(
                available=True,
                repo_path=repo_path,
                message=result.stderr or result.stdout,
            )

        parsed = _json_object(result.stdout)
        if parsed is None:
            return GraphStatus(
                available=True,
                repo_path=repo_path,
                index_fresh=True,
                message=result.stdout.strip(),
            )

        return GraphStatus(
            available=True,
            repo_path=repo_path,
            index_fresh=bool(
                parsed.get("index_fresh", parsed.get("fresh", parsed.get("ok", False)))
            ),
            message=str(parsed.get("message", "")),
            raw=parsed,
        )

    def version(self) -> str | None:
        result = self._run(Path.cwd(), ["--version"])
        if not result.passed:
            return None
        return result.stdout.strip() or None

    def files(self, repo_path: Path) -> list[GraphFile]:
        result = self._run(repo_path, ["files", "--json"])
        if not result.passed:
            return []
        return [_graph_file(item) for item in _json_items(result.stdout, "files")]

    def search(
        self, repo_path: Path, query: str, limit: int = 20
    ) -> list[SymbolHit]:
        result = self._run(
            repo_path,
            ["search", query, "--limit", str(limit), "--json"],
        )
        if not result.passed:
            return []
        return [_symbol_hit(item) for item in _json_items(result.stdout, "results")]

    def callers(
        self, repo_path: Path, symbol: str, depth: int = 1
    ) -> list[SymbolRef]:
        result = self._run(
            repo_path,
            ["callers", symbol, "--depth", str(depth), "--json"],
        )
        if not result.passed:
            return []
        return [_symbol_ref(item) for item in _json_items(result.stdout, "callers")]

    def callees(
        self, repo_path: Path, symbol: str, depth: int = 1
    ) -> list[SymbolRef]:
        result = self._run(
            repo_path,
            ["callees", symbol, "--depth", str(depth), "--json"],
        )
        if not result.passed:
            return []
        return [_symbol_ref(item) for item in _json_items(result.stdout, "callees")]

    def impact(
        self, repo_path: Path, symbol: str, depth: int = 2
    ) -> ImpactReport:
        result = self._run(
            repo_path,
            ["impact", symbol, "--depth", str(depth), "--json"],
        )
        if not result.passed:
            return ImpactReport(symbol=symbol, warnings=[result.stderr])

        parsed = _json_object(result.stdout) or {}
        affected_symbols = [
            _symbol_ref(item)
            for item in _as_list(parsed.get("affected_symbols", []))
        ]
        return ImpactReport(
            symbol=str(parsed.get("symbol", symbol)),
            affected_symbols=affected_symbols,
            affected_files=[str(path) for path in _as_list(parsed.get("affected_files", []))],
            affected_tests=[str(path) for path in _as_list(parsed.get("affected_tests", []))],
            warnings=[str(warning) for warning in _as_list(parsed.get("warnings", []))],
        )

    def affected_tests(self, repo_path: Path, changed_files: list[str]) -> list[str]:
        result = self._run(
            repo_path,
            ["affected", "--stdin"],
            input_text="\n".join(changed_files),
        )
        if not result.passed:
            return []
        items = _json_items(result.stdout, "tests")
        return [str(item) for item in items]

    def _run(
        self,
        repo_path: Path,
        args: list[str],
        input_text: str | None = None,
    ) -> CodeGraphCommandResult:
        resolved_repo = repo_path.resolve()
        if self._runner is not None:
            return self._runner(resolved_repo, args, input_text)

        if shutil.which(self.executable) is None:
            return CodeGraphCommandResult(
                args=[self.executable, *args],
                returncode=127,
                stderr=f"{self.executable!r} executable not found",
            )

        command = [self.executable, *args]
        try:
            result = subprocess.run(
                command,
                cwd=resolved_repo,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CodeGraphCommandResult(
                args=command,
                returncode=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "codegraph command timed out",
            )
        except OSError as exc:
            return CodeGraphCommandResult(
                args=command,
                returncode=127,
                stderr=str(exc),
            )
        return CodeGraphCommandResult(
            args=command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_items(text: str, key: str) -> list[Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return _as_list(parsed.get(key, []))
    return []


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _graph_file(item: Any) -> GraphFile:
    if isinstance(item, str):
        return GraphFile(path=item)
    return GraphFile(
        path=str(item.get("path", item.get("file", ""))),
        language=item.get("language"),
        indexed=bool(item.get("indexed", True)),
    )


def _symbol_hit(item: Any) -> SymbolHit:
    if isinstance(item, str):
        return SymbolHit(symbol=item, path="")
    return SymbolHit(
        symbol=str(item.get("symbol", item.get("name", item.get("id", "")))),
        path=str(item.get("path", item.get("file", ""))),
        kind=item.get("kind", item.get("type")),
        line=item.get("line"),
        score=item.get("score"),
    )


def _symbol_ref(item: Any) -> SymbolRef:
    if isinstance(item, str):
        return SymbolRef(symbol=item, path="")
    return SymbolRef(
        symbol=str(item.get("symbol", item.get("name", item.get("id", "")))),
        path=str(item.get("path", item.get("file", ""))),
        kind=item.get("kind", item.get("type")),
        line=item.get("line"),
    )
