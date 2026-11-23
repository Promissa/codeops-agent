"""Append-only, secret-filtered execution events."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from codeops.core.paths import RunPaths
from codeops.runtime.lifecycle import RunTerminated, TerminationReason
from codeops.safety.secret_filter import SecretFilter


TraceStatus = Literal[
    "ok", "allowed", "denied", "warning", "failed", "skipped", "inconclusive", "blocked",
]
TraceKind = Literal[
    "stage_start", "stage_end", "trust_decision", "sandbox_lifecycle", "retrieval",
    "routing", "codegraph", "patch", "policy_check", "test_run", "outbound_filter",
    "review", "evidence_gate", "evolution_loaded", "evolution_attributed", "error",
]


class TraceEvent(BaseModel):
    event_id: str
    seq: int = Field(ge=1)
    timestamp: datetime
    task_id: str
    stage: str
    kind: TraceKind
    status: TraceStatus
    duration_ms: float | None = Field(default=None, ge=0)
    parent_event_id: str | None = None
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)


class TraceRedactor:
    """Filter nested report data before serialization and count redactions."""

    _credential_fields = {"password", "passwd", "api_key", "access_token", "client_secret"}

    def __init__(self) -> None:
        self._filter = SecretFilter()
        self.count = 0

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            scan = self._filter.scan(value)
            self.count += len(scan.findings)
            return scan.redacted_text
        if isinstance(value, BaseModel):
            return self.redact(value.model_dump(mode="json"))
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                field = str(key)
                if field.lower() in self._credential_fields and item:
                    self.count += 1
                    result[field] = "[REDACTED credential]"
                else:
                    result[self.redact(field)] = self.redact(item)
            return result
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, Path):
            return self.redact(str(value))
        return value


class TraceRecorder:
    """Record a single run's events and link them to their enclosing stage."""

    def __init__(self, run_dir: Path, task_id: str | None = None, *, fresh: bool = False) -> None:
        paths = RunPaths.from_run_dir(run_dir)
        self.path = paths.trace_events_path
        self.task_id = task_id if task_id is not None else paths.task_id
        self._parent_id: str | None = None
        paths.ensure()
        if fresh:
            self.path.unlink(missing_ok=True)
        self.events = load_trace_events(self.path) if self.path.exists() else []
        self._seq = max((event.seq for event in self.events), default=0)

    def record(
        self, stage: str, kind: TraceKind, status: TraceStatus, summary: str, *,
        details: dict[str, Any] | None = None, refs: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> TraceEvent:
        redactor = TraceRedactor()
        payload = TraceEvent(
            event_id=uuid4().hex, seq=self._seq + 1, timestamp=datetime.now(timezone.utc),
            task_id=self.task_id, stage=stage, kind=kind,
            status=status, duration_ms=duration_ms, parent_event_id=self._parent_id,
            summary=summary, details=details or {}, refs=refs or [],
        )
        event = TraceEvent.model_validate(redactor.redact(payload))
        if redactor.count:
            event.details["trace_redactions"] = redactor.count
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
        with os.fdopen(os.open(self.path, flags, 0o600), "a", encoding="utf-8") as output:
            output.write(event.model_dump_json() + "\n")
        self._seq = event.seq
        self.events.append(event)
        return event

    @contextmanager
    def stage(
        self, stage: str, summary: str, *, details: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        started = time.monotonic()
        previous_parent = self._parent_id
        self._parent_id = self.record(stage, "stage_start", "ok", summary, details=details).event_id
        status: TraceStatus = "ok"
        try:
            yield
        except (KeyboardInterrupt, InterruptedError):
            status = "blocked"
            raise
        except RunTerminated as exc:
            status = "failed" if exc.reason == TerminationReason.RUNNER_ERROR else "blocked"
            raise
        except BaseException:
            status = "failed"
            raise
        finally:
            try:
                self.record(
                    stage, "stage_end", status, summary,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
            finally:
                self._parent_id = previous_parent


def load_trace_events(path: Path) -> list[TraceEvent]:
    """Load a UTF-8 JSONL stream in recorded order."""
    with path.open(encoding="utf-8") as source:
        return [TraceEvent.model_validate_json(line) for line in source if line.strip()]


def latest_event_detail(events: list[TraceEvent], kind: TraceKind, key: str) -> Any:
    return next(
        (event.details[key] for event in reversed(events) if event.kind == kind and key in event.details),
        None,
    )
