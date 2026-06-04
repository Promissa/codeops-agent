import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from codeops.core.artifacts import ArtifactWriter
from codeops.core.errors import InvalidTaskStateTransition
from codeops.core.models import (
    AcceptanceContract,
    AcceptanceExample,
    GraphEvidence,
    ImpactEnvelope,
    ModuleCapsule,
    PatchPlan,
    RepoSketch,
    SymbolRef,
    TaskRequest,
    TaskState,
    TestResult as CodeOpsTestResult,
    VerificationPlan,
)
from codeops.core.paths import RunPaths
from codeops.core.state import create_task_state, transition_task_state


def test_task_request_serializes_paths():
    request = TaskRequest(
        repo_path=Path("examples/fixtures/mini_data_pipeline"),
        issue_path=Path("examples/issues/csv_trailing_empty_column.md"),
        out_path=Path(".runs/demo_csv_bug"),
    )

    payload = request.model_dump(mode="json")

    assert payload == {
        "repo_path": "examples/fixtures/mini_data_pipeline",
        "issue_path": "examples/issues/csv_trailing_empty_column.md",
        "out_path": ".runs/demo_csv_bug",
    }
    assert TaskRequest.model_validate(payload) == request


def test_task_state_serialization_round_trip():
    state = _task_state()

    payload = state.model_dump(mode="json")
    restored = TaskState.model_validate(payload)

    assert restored == state
    assert payload["acceptance_contract"]["examples"][0]["expected"] == (
        '[["a", "b", "c"], ["1", "2", None]]'
    )
    assert payload["impact_envelope"]["allowed_files"] == [
        "src/mini_data_pipeline/parser.py",
        "tests/test_parser.py",
    ]


def test_repo_sketch_serialization_round_trip():
    generated_at = datetime(2026, 6, 5, tzinfo=timezone.utc)
    sketch = RepoSketch(
        repo_root=Path("examples/fixtures/mini_data_pipeline"),
        commit=None,
        languages=["Python"],
        frameworks=[],
        entrypoints=["src/mini_data_pipeline/loader.py"],
        core_modules=[
            ModuleCapsule(
                name="parser",
                path="src/mini_data_pipeline/parser.py",
                responsibility="Parse CSV text into rows.",
                public_symbols=["parse_csv", "parse_row"],
            )
        ],
        test_commands=["pytest -q"],
        test_map={"src/mini_data_pipeline/parser.py": ["tests/test_parser.py"]},
        high_risk_paths=[],
        generated_at=generated_at,
    )

    payload = sketch.model_dump(mode="json")

    assert payload["generated_at"] == "2026-06-05T00:00:00Z"
    assert RepoSketch.model_validate(payload) == sketch


def test_task_state_lifecycle_transitions():
    state = create_task_state(
        task_id="demo_csv_bug",
        repo_path=Path("examples/fixtures/mini_data_pipeline"),
        issue_path=Path("examples/issues/csv_trailing_empty_column.md"),
        issue_text="Bug: CSV parser crashes.",
        run_dir=Path(".runs/demo_csv_bug"),
    )

    parsed = transition_task_state(state, "parsed")
    done = transition_task_state(parsed, "done")

    assert state.status == "created"
    assert parsed.status == "parsed"
    assert done.status == "done"

    with pytest.raises(InvalidTaskStateTransition):
        transition_task_state(done, "tested")


def test_artifact_writer_writes_under_runs_task_id(tmp_path):
    paths = RunPaths(task_id="demo_csv_bug", root=tmp_path / ".runs")
    writer = ArtifactWriter(paths)
    state = _task_state(run_dir=paths.run_dir)
    contract = state.acceptance_contract
    assert contract is not None

    task_path = writer.write_json("task", state)
    contract_path = writer.write_yaml("acceptance_contract", contract)
    matrix_path = writer.write_markdown(
        "evidence_matrix",
        "| Requirement | Status |\n|---|---|\n| R1 | Pending |\n",
    )

    assert task_path == tmp_path / ".runs" / "demo_csv_bug" / "task.json"
    assert contract_path == (
        tmp_path / ".runs" / "demo_csv_bug" / "acceptance_contract.yaml"
    )
    assert matrix_path == (
        tmp_path / ".runs" / "demo_csv_bug" / "evidence_matrix.md"
    )
    assert json.loads(task_path.read_text())["task_id"] == "demo_csv_bug"
    assert 'requirement_id: "R1"' in contract_path.read_text()
    assert matrix_path.read_text().startswith("| Requirement | Status |")


def _task_state(run_dir: Path = Path(".runs/demo_csv_bug")) -> TaskState:
    symbol = SymbolRef(
        symbol="mini_data_pipeline.parser.parse_csv",
        path="src/mini_data_pipeline/parser.py",
        kind="function",
        line=4,
    )

    return TaskState(
        task_id="demo_csv_bug",
        repo_path=Path("examples/fixtures/mini_data_pipeline"),
        issue_path=Path("examples/issues/csv_trailing_empty_column.md"),
        issue_text="Bug: CSV parser crashes when a row has trailing columns.",
        run_dir=run_dir,
        status="tested",
        acceptance_contract=AcceptanceContract(
            requirement_id="R1",
            summary="Preserve trailing empty CSV fields as None.",
            user_visible_before="Trailing empty fields were dropped.",
            user_visible_after="Trailing empty fields are explicit None values.",
            examples=[
                AcceptanceExample(
                    input='parse_csv("a,b,c\\n1,2,\\n")',
                    expected='[["a", "b", "c"], ["1", "2", None]]',
                )
            ],
            invariants=["Existing NULL handling remains unchanged."],
            non_goals=["Quoted CSV support."],
            ambiguity_questions=[],
            status="ready",
        ),
        graph_evidence=GraphEvidence(
            codegraph_version=None,
            repo_commit=None,
            index_fresh=False,
            indexed_file_coverage=None,
            target_symbols=[symbol],
            direct_callers=[],
            callees=[],
            affected_tests=["tests/test_parser.py"],
            heuristic_edges=[],
            cross_checks=[],
            graph_confidence=0.0,
            warnings=["CodeGraph not implemented in Phase 1."],
        ),
        impact_envelope=ImpactEnvelope(
            target_symbols=[symbol],
            allowed_files=[
                "src/mini_data_pipeline/parser.py",
                "tests/test_parser.py",
            ],
            affected_files=["src/mini_data_pipeline/parser.py"],
            affected_tests=["tests/test_parser.py"],
            forbidden_changes=["dependency change", "public API signature change"],
            risk_level="low",
            requires_human_approval=False,
        ),
        patch_plan=PatchPlan(
            hypothesis="Trailing empty fields are removed before normalization.",
            edit_strategy="Preserve trailing fields and normalize them to None.",
            files_to_edit=["src/mini_data_pipeline/parser.py"],
            tests_to_add_or_update=["tests/test_parser.py"],
            expected_behavior_change="Trailing empty columns become None.",
            risk_notes=[],
        ),
        verification_plan=VerificationPlan(
            acceptance_tests=["tests/test_parser.py::test_trailing_empty_column"],
            affected_tests=["tests/test_parser.py"],
            module_tests=["tests/test_parser.py"],
            full_tests=["pytest -q"],
            static_checks=[],
            behavior_diff_required=True,
            coverage_required=False,
        ),
        test_results=[
            CodeOpsTestResult(
                command="pytest tests/test_parser.py -q",
                passed=True,
                exit_code=0,
                stdout="1 passed",
            )
        ],
    )
