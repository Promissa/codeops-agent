import json
import shutil

from typer.testing import CliRunner

from codeops.cli import app
from codeops.retrieval.fusion import reciprocal_rank_fusion
from codeops.retrieval.local_embeddings import LocalEmbeddingRetriever


def test_local_embedding_retriever_indexes_capsules_without_provider(tmp_path):
    repo = tmp_path / "mini_data_pipeline"
    shutil.copytree("examples/fixtures/mini_data_pipeline", repo)
    store_path = repo / ".codeops" / "local_embeddings.sqlite"

    capsules, embedded = LocalEmbeddingRetriever().index(repo, store_path)

    assert not embedded
    assert store_path.exists()
    assert any(
        capsule.symbol_id == "src/mini_data_pipeline/parser.py::parse_csv"
        for capsule in capsules
    )


def test_reciprocal_rank_fusion_combines_rankings():
    fused = reciprocal_rank_fusion(
        [
            ["parser.py", "loader.py"],
            ["tests/test_parser.py", "parser.py"],
        ]
    )

    assert fused[0][0] == "parser.py"
    assert dict(fused)["parser.py"] > dict(fused)["loader.py"]


def test_hybrid_local_retrieval_falls_back_gracefully(tmp_path):
    repo = tmp_path / "mini_data_pipeline"
    shutil.copytree("examples/fixtures/mini_data_pipeline", repo)
    out = tmp_path / ".runs" / "hybrid_demo"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--issue",
            "examples/issues/csv_trailing_empty_column.md",
            "--out",
            str(out),
            "--retrieval",
            "hybrid_local",
        ],
    )

    assert result.exit_code == 0, result.output
    evidence = json.loads((out / "graph_evidence.json").read_text())
    assert any("embedding provider unavailable" in warning for warning in evidence["warnings"])
    assert (repo / ".codeops" / "local_embeddings.sqlite").exists()
