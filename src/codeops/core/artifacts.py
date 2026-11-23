"""Artifact writing helpers."""

import json
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import BaseModel

from codeops.core.paths import RunPaths


class ArtifactWriter:
    """Write run artifacts under `.runs/<task_id>/`."""

    def __init__(self, paths: RunPaths, *, atomic: bool = False) -> None:
        self.paths = paths
        self.atomic = atomic

    def write_json(self, artifact_name: str, data: Any) -> Path:
        return self._write(
            artifact_name, json.dumps(_to_jsonable(data), indent=2, sort_keys=True) + "\n"
        )

    def write_yaml(self, artifact_name: str, data: Any) -> Path:
        return self._write(artifact_name, _to_yaml(_to_jsonable(data)))

    def write_markdown(self, artifact_name: str, content: str) -> Path:
        return self._write(artifact_name, content)

    def write_html(self, artifact_name: str, content: str) -> Path:
        return self._write(artifact_name, content)

    def _write(self, artifact_name: str, content: str) -> Path:
        path = self.paths.artifact_path(artifact_name)
        self.paths.ensure()
        if self.atomic:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
                pending = Path(output.name)
                try:
                    output.write(content)
                    output.close()
                    pending.replace(path)
                finally:
                    pending.unlink(missing_ok=True)
        else:
            path.write_text(content)
        return path


def _to_jsonable(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if isinstance(data, Path):
        return str(data)
    if isinstance(data, datetime | date):
        return data.isoformat()
    if isinstance(data, dict):
        return {str(key): _to_jsonable(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_to_jsonable(value) for value in data]
    return data


def _to_yaml(data: Any) -> str:
    rendered = _render_yaml(data)
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _render_yaml(data: Any, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(data, dict):
        if not data:
            return prefix + "{}"
        lines = []
        for key, value in data.items():
            if _is_scalar(value):
                lines.append(f"{prefix}{key}: {_format_scalar(value)}")
            else:
                lines.append(f"{prefix}{key}:")
                lines.append(_render_yaml(value, indent + 2))
        return "\n".join(lines)
    if isinstance(data, list):
        if not data:
            return prefix + "[]"
        lines = []
        for value in data:
            if _is_scalar(value):
                lines.append(f"{prefix}- {_format_scalar(value)}")
            else:
                lines.append(f"{prefix}-")
                lines.append(_render_yaml(value, indent + 2))
        return "\n".join(lines)
    return prefix + _format_scalar(data)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value)
