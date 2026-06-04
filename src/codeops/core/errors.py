"""Shared error types."""


class CodeOpsError(Exception):
    """Base class for CodeOps errors."""


class InvalidTaskStateTransition(CodeOpsError):
    """Raised when a task state transition is not allowed."""
