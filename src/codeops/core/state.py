"""TaskState lifecycle helpers."""

from pathlib import Path

from codeops.core.errors import InvalidTaskStateTransition
from codeops.core.models import TaskState, TaskStatus


TERMINAL_STATUSES = {"done", "failed", "needs_clarification"}


def create_task_state(
    *,
    task_id: str,
    repo_path: Path,
    issue_text: str,
    run_dir: Path,
    issue_path: Path | None = None,
) -> TaskState:
    """Create the initial task state for a run."""
    return TaskState(
        task_id=task_id,
        repo_path=repo_path,
        issue_path=issue_path,
        issue_text=issue_text,
        run_dir=run_dir,
    )


def transition_task_state(state: TaskState, status: TaskStatus) -> TaskState:
    """Return a copy of state with an updated lifecycle status."""
    if state.status in TERMINAL_STATUSES and status != state.status:
        raise InvalidTaskStateTransition(
            f"cannot transition task {state.task_id!r} from terminal "
            f"status {state.status!r} to {status!r}"
        )

    data = state.model_dump()
    data["status"] = status
    return TaskState.model_validate(data)
