"""Optional local embedding retrieval."""

import ast
import hashlib
import json
from pathlib import Path
from typing import Protocol
import sqlite3

from pydantic import BaseModel, Field


class SymbolCapsule(BaseModel):
    symbol_id: str
    type: str
    path: str
    summary: str
    calls: list[str] = Field(default_factory=list)
    called_by: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    source_hash: str


class EmbeddingProvider(Protocol):
    @property
    def available(self) -> bool:
        """Whether this provider can embed text in the current environment."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed text into vectors."""


class MissingEmbeddingProvider:
    """Provider used when optional embedding dependencies are absent."""

    @property
    def available(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("local embedding provider is unavailable")


class LocalEmbeddingStore:
    """SQLite-backed capsule and embedding store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def upsert_capsules(
        self,
        capsules: list[SymbolCapsule],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        embeddings = embeddings or [[] for _ in capsules]
        with sqlite3.connect(self.path) as connection:
            for capsule, embedding in zip(capsules, embeddings, strict=True):
                connection.execute(
                    """
                    insert into symbol_capsules
                      (symbol_id, payload_json, embedding_json)
                    values (?, ?, ?)
                    on conflict(symbol_id) do update set
                      payload_json = excluded.payload_json,
                      embedding_json = excluded.embedding_json
                    """,
                    (
                        capsule.symbol_id,
                        capsule.model_dump_json(),
                        json.dumps(embedding),
                    ),
                )

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                create table if not exists symbol_capsules (
                  symbol_id text primary key,
                  payload_json text not null,
                  embedding_json text not null
                )
                """
            )


class LocalEmbeddingRetriever:
    """Build capsules and optionally store local embeddings."""

    def __init__(self, provider: EmbeddingProvider | None = None) -> None:
        self.provider = provider or MissingEmbeddingProvider()

    def build_capsules(self, repo_path: Path) -> list[SymbolCapsule]:
        repo_root = repo_path.resolve()
        capsules: list[SymbolCapsule] = []
        for path in sorted(repo_root.rglob("*.py")):
            if ".git" in path.parts or ".venv" in path.parts:
                continue
            capsules.extend(_capsules_for_file(repo_root, path))
        return capsules

    def index(self, repo_path: Path, store_path: Path) -> tuple[list[SymbolCapsule], bool]:
        capsules = self.build_capsules(repo_path)
        store = LocalEmbeddingStore(store_path)
        if not self.provider.available:
            store.upsert_capsules(capsules)
            return capsules, False

        texts = [f"{capsule.symbol_id}\n{capsule.summary}" for capsule in capsules]
        store.upsert_capsules(capsules, self.provider.embed(texts))
        return capsules, True


def _capsules_for_file(repo_root: Path, path: Path) -> list[SymbolCapsule]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    rel_path = str(path.relative_to(repo_root))
    capsules = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            symbol_type = "class" if isinstance(node, ast.ClassDef) else "function"
            capsules.append(
                SymbolCapsule(
                    symbol_id=f"{rel_path}::{node.name}",
                    type=symbol_type,
                    path=rel_path,
                    summary=ast.get_docstring(node) or f"{symbol_type} `{node.name}`.",
                    calls=_calls(node),
                    tests=_tests_for(rel_path),
                    source_hash=_hash_source(ast.get_source_segment(source, node) or ""),
                )
            )
    return capsules


def _calls(node: ast.AST) -> list[str]:
    calls = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                calls.append(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                calls.append(child.func.attr)
    return list(dict.fromkeys(calls))


def _tests_for(path: str) -> list[str]:
    stem = Path(path).stem
    return [f"tests/test_{stem}.py"]


def _hash_source(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
