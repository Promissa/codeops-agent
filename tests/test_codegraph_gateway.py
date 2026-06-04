import json
from pathlib import Path

from codeops.tools.codegraph_gateway import (
    CodeGraphCommandResult,
    CodeGraphGateway,
)


def test_gateway_degrades_when_codegraph_is_missing(tmp_path):
    gateway = CodeGraphGateway(executable="definitely-not-codegraph")

    status = gateway.status(tmp_path)

    assert not status.available
    assert not status.index_fresh
    assert "not found" in status.message
    assert gateway.files(tmp_path) == []
    assert gateway.search(tmp_path, "parse_csv") == []
    assert gateway.callers(tmp_path, "parse_csv") == []
    assert gateway.callees(tmp_path, "parse_csv") == []
    assert gateway.affected_tests(tmp_path, ["src/parser.py"]) == []


def test_gateway_parses_status_and_version(tmp_path):
    gateway = CodeGraphGateway(runner=_fake_runner)

    status = gateway.status(tmp_path)
    version = gateway.version()

    assert status.available
    assert status.index_fresh
    assert status.raw == {"index_fresh": True, "message": "ready"}
    assert version == "codegraph 1.2.3"


def test_gateway_parses_files_and_search_results(tmp_path):
    gateway = CodeGraphGateway(runner=_fake_runner)

    files = gateway.files(tmp_path)
    hits = gateway.search(tmp_path, "parse_csv", limit=3)

    assert files[0].path == "src/mini_data_pipeline/parser.py"
    assert files[0].language == "Python"
    assert hits[0].symbol == "parse_csv"
    assert hits[0].path == "src/mini_data_pipeline/parser.py"
    assert hits[0].score == 0.95


def test_gateway_parses_callers_callees_impact_and_affected_tests(tmp_path):
    gateway = CodeGraphGateway(runner=_fake_runner)

    callers = gateway.callers(tmp_path, "parse_row")
    callees = gateway.callees(tmp_path, "parse_csv")
    impact = gateway.impact(tmp_path, "parse_csv")
    affected = gateway.affected_tests(
        tmp_path, ["src/mini_data_pipeline/parser.py"]
    )

    assert callers[0].symbol == "parse_csv"
    assert callees[0].symbol == "parse_row"
    assert impact.symbol == "parse_csv"
    assert impact.affected_symbols[0].symbol == "parse_row"
    assert impact.affected_files == ["src/mini_data_pipeline/parser.py"]
    assert impact.affected_tests == ["tests/test_parser.py"]
    assert affected == ["tests/test_parser.py"]


def _fake_runner(
    repo_path: Path, args: list[str], input_text: str | None
) -> CodeGraphCommandResult:
    assert repo_path.is_absolute()

    if args == ["status"]:
        return _result(args, {"index_fresh": True, "message": "ready"})
    if args == ["--version"]:
        return CodeGraphCommandResult(args=args, returncode=0, stdout="codegraph 1.2.3\n")
    if args == ["files", "--json"]:
        return _result(
            args,
            {
                "files": [
                    {
                        "path": "src/mini_data_pipeline/parser.py",
                        "language": "Python",
                    }
                ]
            },
        )
    if args[:2] == ["search", "parse_csv"]:
        return _result(
            args,
            {
                "results": [
                    {
                        "symbol": "parse_csv",
                        "path": "src/mini_data_pipeline/parser.py",
                        "kind": "function",
                        "line": 4,
                        "score": 0.95,
                    }
                ]
            },
        )
    if args[:2] == ["callers", "parse_row"]:
        return _result(
            args,
            {
                "callers": [
                    {
                        "symbol": "parse_csv",
                        "path": "src/mini_data_pipeline/parser.py",
                    }
                ]
            },
        )
    if args[:2] == ["callees", "parse_csv"]:
        return _result(
            args,
            {
                "callees": [
                    {
                        "symbol": "parse_row",
                        "path": "src/mini_data_pipeline/parser.py",
                    }
                ]
            },
        )
    if args[:2] == ["impact", "parse_csv"]:
        return _result(
            args,
            {
                "symbol": "parse_csv",
                "affected_symbols": [
                    {
                        "symbol": "parse_row",
                        "path": "src/mini_data_pipeline/parser.py",
                    }
                ],
                "affected_files": ["src/mini_data_pipeline/parser.py"],
                "affected_tests": ["tests/test_parser.py"],
            },
        )
    if args == ["affected", "--stdin"]:
        assert input_text == "src/mini_data_pipeline/parser.py"
        return _result(args, {"tests": ["tests/test_parser.py"]})

    return CodeGraphCommandResult(args=args, returncode=2, stderr="unexpected")


def _result(args: list[str], payload: object) -> CodeGraphCommandResult:
    return CodeGraphCommandResult(
        args=args,
        returncode=0,
        stdout=json.dumps(payload),
    )
