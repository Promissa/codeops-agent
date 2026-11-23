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
        result = self._run(repo_path, ["status", "--json"])
        if not _json_object(result.stdout) and result.returncode != 127:
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

        fresh = bool(parsed.get("index_fresh", parsed.get("fresh", parsed.get("ok", False))))
        if parsed.get("initialized"):
            pending = parsed.get("pendingChanges") or {}
            index = parsed.get("index") or {}
            fresh = (
                not any(pending.values()) and not parsed.get("worktreeMismatch")
                and not index.get("reindexRecommended") and index.get("state", "complete") == "complete"
            )
        return GraphStatus(
            available=True,
            repo_path=repo_path,
            index_fresh=fresh,
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
            result = self._run(repo_path, ["query", query, "--limit", str(limit), "--json"])
        return [_symbol_hit(item) for item in _json_items(result.stdout, "results")] if result.passed else []

    def callers(
        self, repo_path: Path, symbol: str, depth: int = 1
    ) -> list[SymbolRef]:
        result = self._run(
            repo_path,
            ["callers", symbol, "--depth", str(depth), "--json"],
        )
        if not result.passed and result.returncode != 127:
            result = self._run(repo_path, ["callers", symbol, "--json"])
        return _relationship_refs(result.stdout, "callers") if result.passed else []

    def callees(
        self, repo_path: Path, symbol: str, depth: int = 1
    ) -> list[SymbolRef]:
        result = self._run(
            repo_path,
            ["callees", symbol, "--depth", str(depth), "--json"],
        )
        if not result.passed and result.returncode != 127:
            result = self._run(repo_path, ["callees", symbol, "--json"])
        return _relationship_refs(result.stdout, "callees") if result.passed else []

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
            for item in _as_list(parsed.get("affected_symbols", parsed.get("affected", [])))
        ]
        return ImpactReport(
            symbol=str(parsed.get("symbol", symbol)),
            affected_symbols=affected_symbols,
            affected_files=list(dict.fromkeys(
                str(path) for path in _as_list(parsed.get("affected_files", [ref.path for ref in affected_symbols]))
                if path
            )),
            affected_tests=[str(path) for path in _as_list(parsed.get("affected_tests", []))],
            warnings=[str(warning) for warning in _as_list(parsed.get("warnings", []))],
        )

    def affected_tests(self, repo_path: Path, changed_files: list[str]) -> list[str]:
        result = self._run(
            repo_path,
            ["affected", "--stdin"],
            input_text="\n".join(changed_files),
        )
        items = _json_items(result.stdout, "tests")
        if not items and result.returncode != 127:
            result = self._run(repo_path, ["affected", "--stdin", "--json"], input_text="\n".join(changed_files))
            items = _json_items(result.stdout, "affectedTests") or _json_items(result.stdout, "tests")
        if not result.passed:
            return []
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
                stdout=_output_text(exc.stdout),
                stderr=_output_text(exc.stderr) or "codegraph command timed out",
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
        path=_node_path(item),
        language=item.get("language"),
        indexed=bool(item.get("indexed", True)),
    )


def _symbol_hit(item: Any) -> SymbolHit:
    if isinstance(item, str):
        return SymbolHit(symbol=item, path="")
    return SymbolHit(**_symbol_ref(item).model_dump(), score=item.get("score"))


def _symbol_ref(item: Any) -> SymbolRef:
    if isinstance(item, str):
        return SymbolRef(symbol=item, path="")
    item = item.get("node", item)
    return SymbolRef(
        symbol=str(item.get("symbol", item.get("name", item.get("id", "")))),
        path=_node_path(item),
        kind=item.get("kind", item.get("type")),
        line=item.get("line", item.get("startLine")),
    )


def _node_path(item: dict[str, Any]) -> str:
    return str(item.get("path", item.get("file", item.get("filePath", ""))))


def _relationship_refs(output: str, key: str) -> list[SymbolRef]:
    return [
        _symbol_ref(item) for item in _json_items(output, key)
        if not isinstance(item, dict) or set(item.get("relationships", [])) != {"imports"}
    ]


def _output_text(output: bytes | str | None) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else output or ""
