"""Resolve changed symbols and identify dependent code that tests actually reach."""

import ast
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re

from pydantic import BaseModel, Field

from codeops.core.models import SymbolRef
from codeops.gate.workspace_diff import TRANSIENT_PATHS, WorkspaceDiffSummary
from codeops.languages.registry import LanguageProfileRegistry
from codeops.runtime.workspace import open_regular
from codeops.tools.codegraph_gateway import CodeGraphGateway
from codeops.tools.ripgrep_tool import RipgrepTool


class EffectMap(BaseModel):
    edited_symbols: list[SymbolRef] = Field(default_factory=list)
    affected_callers: list[SymbolRef] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    affected_tests: list[str] = Field(default_factory=list)
    observed_files: list[str] = Field(default_factory=list)
    unobserved_callers: list[SymbolRef] = Field(default_factory=list)
    failing_callers: list[SymbolRef] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


_DEFINITION = re.compile(
    r"^\s*(?:(?:export|default|pub(?:\([^)]*\))?|async|public|private|protected|static|final|suspend)\s+)*"
    r"(?:function|func(?:\s*\([^)]*\))?|fn|fun|class|interface|struct|enum|record|const|let|var)\s+(\w+)"
    r"|^\s*(?:(?:public|private|protected|static|final)\s+)+(?:[\w<>\[\],.?]+\s+)+(\w+)\s*\("
)
_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_VARIABLE = re.compile(r"\b(?:const|let|var)\b")
_EXTENSIONS = set().union(*(profile.file_extensions for profile in LanguageProfileRegistry().profiles()))
_MAX_REFERENCES = 200


@dataclass(frozen=True)
class _Scope:
    name: str
    start: int
    end: int
    kind: str
    definition: int


def is_test_file(path: str) -> bool:
    item = Path(path)
    name = item.name
    return (
        "tests" in item.parts or "test" in item.parts[:-1] or "__tests__" in item.parts
        or name == "conftest.py" or name.startswith("test_")
        or item.stem.endswith(("_test", "Test", "Tests", "TestCase"))
        or ".spec." in name or ".test." in name
    )


def _text(root: Path, path: str) -> str:
    try:
        with open_regular(root, Path(path)) as source:
            return source.read().decode("utf-8")
    except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError):
        return ""


def _module(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class _Source:
    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.lines = text.splitlines()
        self.tree = None
        self.scopes: list[_Scope] = []
        if path.endswith(".py"):
            try:
                self.tree = ast.parse(text)
            except SyntaxError:
                pass
        if self.tree is not None:
            self._python_scopes(self.tree, _module(path), variables=True)
        elif not path.endswith(".py"):
            definitions = [
                (index, match[1] or match[2])
                for index, line in enumerate(self.lines, 1)
                if (match := _DEFINITION.match(line))
            ]
            for position, (index, name) in enumerate(definitions):
                end = definitions[position + 1][0] - 1 if position + 1 < len(definitions) else len(self.lines)
                kind = "variable" if _VARIABLE.search(self.lines[index - 1].split(name, 1)[0]) else "symbol"
                self.scopes.append(_Scope(name, index, end, kind, index))

    def _python_scopes(self, node: ast.AST, prefix: str, *, variables: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE):
                name = f"{prefix}.{child.name}" if prefix else child.name
                start = min((decorator.lineno for decorator in child.decorator_list), default=child.lineno)
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                self.scopes.append(_Scope(name, start, child.end_lineno or child.lineno, kind, child.lineno))
                self._python_scopes(child, name, variables=kind == "class")
            elif variables and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        name = f"{prefix}.{target.id}" if prefix else target.id
                        self.scopes.append(_Scope(
                            name, child.lineno, child.end_lineno or child.lineno, "variable", child.lineno,
                        ))
            else:
                self._python_scopes(child, prefix, variables=variables)

    def enclosing(self, line: int) -> _Scope:
        return next(
            (scope for scope in reversed(self.scopes) if scope.start <= line <= scope.end),
            _Scope(f"{_module(self.path)}.<module>", 1, len(self.lines), "module", 1),
        )

    def ref(self, scope: _Scope) -> SymbolRef:
        return SymbolRef(path=self.path, symbol=scope.name, line=scope.definition, kind=scope.kind)

    def references(self, target: SymbolRef) -> Iterable[int]:
        name = target.symbol.rsplit(".", 1)[-1]
        if self.tree is None:
            suffix = r"\b" if target.kind == "variable" else r"\s*\("
            pattern = re.compile(rf"\b{re.escape(name)}{suffix}")
            definitions = {scope.start for scope in self.scopes}
            return (index for index, line in enumerate(self.lines, 1) if index not in definitions and pattern.search(line))
        module = _module(target.path)
        aliases = {name} if self.path == target.path else set()
        module_aliases = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if node.level:
                    package = _module(self.path).split(".")
                    if not self.path.endswith("/__init__.py"):
                        package = package[:-1]
                    imported = ".".join([*package[:len(package) - node.level + 1], *filter(None, [imported])])
                for alias in node.names:
                    if imported == module and alias.name in {name, "*"}:
                        aliases.add(alias.asname or name)
                    if f"{imported}.{alias.name}".lstrip(".") == module:
                        module_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                module_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == module)
        return (
            node.lineno for node in ast.walk(self.tree)
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases)
            or (
                isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr == name
                and (
                    ast.unparse(node.value) in module_aliases
                    or target.symbol.removeprefix(module + ".").count(".") > 0
                )
            )
        )


class EffectMapBuilder:
    """Collect probable callers from both trees and retain the candidate use sites."""

    def __init__(self, baseline: Path, candidate: Path, diff: WorkspaceDiffSummary) -> None:
        self.baseline = baseline
        self.candidate = candidate
        self.diff = diff
        self._sources: dict[tuple[Path, str], _Source] = {}
        self._sites: dict[tuple[str, str], set[int]] = {}
        self._edited_sites: dict[tuple[Path, str], set[int]] = {}
        self._edited_references: dict[tuple[Path, str], set[int]] = {}

    def _source(self, root: Path, path: str) -> _Source:
        key = (root, path)
        if key not in self._sources:
            self._sources[key] = _Source(path, _text(root, path))
        return self._sources[key]

    def build(self, gateway: CodeGraphGateway | None = None, graph_root: Path | None = None) -> EffectMap:
        result = EffectMap()
        edited = {}
        for path in self.diff.changed_files:
            if path in self.diff.unsupported_files:
                continue
            old, new = (self._source(root, path) for root in (self.baseline, self.candidate))
            if old.tree is not None or new.tree is not None:
                result.sources.append("ast")
            for tag, i, j, k, l in SequenceMatcher(None, old.lines, new.lines, autojunk=False).get_opcodes():
                if tag == "equal":
                    continue
                for source, start, end in ((old, i, j), (new, k, l)):
                    for line in range(start + 1, end + 1):
                        scope = source.enclosing(line)
                        ref = source.ref(scope)
                        edited[path, ref.symbol] = ref
                        root = self.baseline if source is old else self.candidate
                        self._edited_sites.setdefault((root, path), set()).update(
                            range(scope.start, scope.end + 1)
                        )
        result.edited_symbols = list(edited.values())
        tests = {path for path in self.diff.changed_files if is_test_file(path)}
        files = set(self.diff.changed_files)
        callers: dict[tuple[str, str], SymbolRef] = {}
        roots = (self.baseline, self.candidate)
        graph_enabled = gateway is not None and graph_root is not None
        pending = deque((ref, 0) for ref in edited.values() if not is_test_file(ref.path))
        visited = set(edited)
        if graph_enabled and gateway is not None and graph_root is not None:
            graph_tests = gateway.affected_tests(graph_root, self.diff.changed_files)
            tests.update(path for path in graph_tests if _local_source(path) and (self.baseline / path).is_file())
            if graph_tests:
                result.sources.append("codegraph")
        while pending:
            target, depth = pending.popleft()
            found: dict[tuple[str, str], SymbolRef] = {}
            name = target.symbol.rsplit(".", 1)[-1]
            if name.isidentifier():
                result.sources.append("ripgrep")
                for root in roots:
                    rg = RipgrepTool(root, preserve_root=True)
                    matches = rg.search(name, max_results=_MAX_REFERENCES)
                    if len(matches) == _MAX_REFERENCES:
                        result.warnings.append(f"Reference search reached its limit for {target.symbol}.")
                    paths = {match.path for match in matches if _local_source(match.path)}
                    for path in sorted(paths):
                        source = self._source(root, path)
                        for line in source.references(target):
                            if depth == 0 and target.kind == "variable":
                                self._edited_references.setdefault((root, path), set()).add(line)
                            ref = source.ref(source.enclosing(line))
                            key = (ref.path, ref.symbol)
                            if key == (target.path, target.symbol):
                                continue
                            found[key] = ref
                            if root == self.candidate:
                                self._sites.setdefault(key, set()).add(line)
                if depth == 0 and graph_enabled and gateway is not None and graph_root is not None:
                    graph_callers = gateway.callers(graph_root, name)
                    callees = gateway.callees(graph_root, name)
                    impact = gateway.impact(graph_root, name)
                    if graph_callers or callees or impact.affected_files or impact.affected_tests:
                        result.sources.append("codegraph")
                    files.update(path for path in [
                        *impact.affected_files, *(ref.path for ref in callees),
                    ] if _local_source(path) and (self.baseline / path).is_file())
                    tests.update(path for path in impact.affected_tests if _local_source(path) and (self.baseline / path).is_file())
                    for ref in graph_callers:
                        if not _local_source(ref.path) or not (self.baseline / ref.path).is_file():
                            continue
                        source = self._source(self.baseline, ref.path)
                        scope = source.enclosing(ref.line or 1)
                        canonical = source.ref(scope) if ref.line is not None else ref
                        found.setdefault((canonical.path, canonical.symbol), canonical)
            for key, ref in found.items():
                if is_test_file(ref.path):
                    tests.add(ref.path)
                    continue
                if key in edited:
                    continue
                callers[key] = ref
                files.add(ref.path)
                if key not in visited and depth < 2:
                    pending.append((ref, depth + 1))
                    visited.add(key)
        result.affected_callers = list(callers.values())
        result.unobserved_callers = result.affected_callers.copy()
        result.affected_tests = sorted(tests)
        result.affected_files = sorted(files | tests)
        result.sources = sorted(set(result.sources))
        result.warnings = sorted(set(result.warnings))
        return result

    def observes_edit(self, lines: dict[str, list[int]], *, candidate: bool) -> bool:
        root = self.candidate if candidate else self.baseline
        return any(
            bool(self._edited_references.get((root, path), set()).intersection(numbers))
            or (not is_test_file(path) and bool(self._edited_sites.get((root, path), set()).intersection(numbers)))
            for path, numbers in lines.items()
        )

    def observe(
        self, effect_map: EffectMap, lines: dict[str, set[int]],
        witnessed: dict[str, set[int]], failed: dict[str, set[int]],
    ) -> None:
        effect_map.observed_files = sorted(lines)
        effect_map.unobserved_callers = [
            ref for ref in effect_map.affected_callers
            if not self._sites.get((ref.path, ref.symbol), set()).intersection(witnessed.get(ref.path, set()))
        ]
        effect_map.failing_callers = [
            ref for ref in effect_map.affected_callers
            if self._sites.get((ref.path, ref.symbol), set()).intersection(failed.get(ref.path, set()))
        ]


def _local_source(path: str) -> bool:
    if not path:
        return False
    item = Path(path)
    return not item.is_absolute() and ".." not in item.parts and not (
        TRANSIENT_PATHS.intersection(item.parts)
    ) and item.suffix in _EXTENSIONS
