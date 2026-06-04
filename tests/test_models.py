from pathlib import Path

from codeops.core.models import TaskRequest


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
