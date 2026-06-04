"""Phase 0 data models."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class TaskRequest(BaseModel):
    """Minimal CLI request payload for the Phase 0 scaffold."""

    model_config = ConfigDict(frozen=True)

    repo_path: Path
    issue_path: Path
    out_path: Path
