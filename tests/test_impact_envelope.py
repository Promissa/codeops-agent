import json
from pathlib import Path

from typer.testing import CliRunner

from codeops.cli import app
from codeops.core.models import CrossCheckResult
from codeops.retrieval.graph_reliability import GraphReliabilityLayer
from codeops.retrieval.repo_sketch import RepoSketchBuilder
from codeops.tools.codegraph_gateway import GraphFile, GraphStatus


def test_repo_sketch_builder_describes_fixture_repo():
    repo = Path("examples/fixtures/mini_data_pipeline")

    sketch = RepoSketchBuilder().build(repo, commit="abc123")
    markdown = RepoSketchBuilder().render_markdown(sketch)

    assert sketch.commit == "abc123"
    assert "Python" in sketch.languages
    assert "pytest" in sketch.frameworks
    assert "src/mini_data_pipeline/parser.py" in sketch.test_map
    assert sketch.test_map["src/mini_data_pipeline/parser.py"] == [
        "tests/test_parser.py"
    ]
    assert "| parser | `src/mini_data_pipeline/parser.py` |" in markdown


def test_graph_reliability_thresholds(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "parser.py").write_text("def parse_csv(): pass\n")
    (tmp_path / "tests" / "test_parser.py").write_text("def test_parser(): pass\n")

    layer = GraphReliabilityLayer()
    unavailable = layer.evaluate(
        repo_path=tmp_path,
        status=GraphStatus(
            available=False,
            repo_path=tmp_path,
            message="CodeGraph executable not found",
        ),
        graph_files=[],
    )
    healthy = layer.evaluate(
        repo_path=tmp_path,
        status=GraphStatus(
            available=True,
            repo_path=tmp_path,
            index_fresh=True,
        ),
        graph_files=[
            GraphFile(path="src/parser.py"),
            GraphFile(path="tests/test_parser.py"),
        ],
        cross_checks=[
            CrossCheckResult(
                source="ripgrep",
                status="pass",
                detail="parse_csv found in src/parser.py",
            )
        ],
    )

    assert unavailable.graph_confidence < 0.60
    assert unavailable.indexed_file_coverage is None
    assert healthy.graph_confidence >= 0.80
    assert healthy.indexed_file_coverage == 1.0
    assert healthy.cross_check_status == "agreement"


def test_cli_writes_phase_4_artifacts(tmp_path):
    repo = tmp_path / "repo"
    source_dir = repo / "src" / "demo"
    tests_dir = repo / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "__init__.py").write_text("")
    (source_dir / "parser.py").write_text("def parse_csv(text):\n    return []\n")
    (tests_dir / "test_parser.py").write_text("def test_parse_csv():\n    assert True\n")
    issue = tmp_path / "issue.md"
    issue.write_text("Bug: parser issue\nExpected: works\nActual: fails\n")
    out = tmp_path / ".runs" / "phase4_demo"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--issue",
            str(issue),
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out / "task.json").exists()
    assert (out / "graph_evidence.json").exists()
    assert (out / "repo_sketch.md").exists()
    assert (repo / ".codeops" / "repo_sketch.json").exists()
    assert (repo / ".codeops" / "repo_sketch.md").exists()
    assert "RepoSketch" in (out / "repo_sketch.md").read_text()
    evidence = json.loads((out / "graph_evidence.json").read_text())
    assert "graph_confidence" in evidence
