"""Evaluation task loading."""

import json
from pathlib import Path

from pydantic import BaseModel


class EvalTask(BaseModel):
    task_id: str
    repo_path: Path
    issue_path: Path
    expected_tests: list[str] = []
    expected_language: str | None = None
    expected_test_commands: list[list[str]] = []
    changed_files: list[str] = []
    graph_tests: list[str] = []
    expected_affected_test_commands: list[list[str]] = []
    expected_rejected_files: list[str] = []


def load_eval_tasks(path: Path) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        tasks.append(EvalTask.model_validate(json.loads(stripped)))
    return tasks
