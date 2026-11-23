"""Persisted states and terminal reasons for a managed run."""

from enum import StrEnum
import errno

from codeops.core.errors import CommandRejected, ToolError


class RunLifecycleState(StrEnum):
    PREPARING = "preparing"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    PENDING_EXPORT = "pending_export"
    FINISHED = "finished"


class TerminationReason(StrEnum):
    COMPLETED = "completed"
    POLICY_DENIED = "policy_denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RUNNER_ERROR = "runner_error"


class RunTerminated(ToolError):
    def __init__(self, reason: TerminationReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def termination_reason(exc: Exception) -> TerminationReason:
    if isinstance(exc, RunTerminated):
        return exc.reason
    if isinstance(exc, CommandRejected):
        return TerminationReason.POLICY_DENIED
    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        return TerminationReason.RESOURCE_EXHAUSTED
    return TerminationReason.RUNNER_ERROR
