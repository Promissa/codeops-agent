"""Shared error types."""


class CodeOpsError(Exception):
    """Base class for CodeOps errors."""


class InvalidTaskStateTransition(CodeOpsError):
    """Raised when a task state transition is not allowed."""


class ToolError(CodeOpsError):
    """Raised when a deterministic tool cannot complete safely."""


class CommandRejected(ToolError):
    """Raised when a command is not allowed by policy."""


class PathOutsideRepo(ToolError):
    """Raised when a requested path escapes the repository root."""


class FileTooLarge(ToolError):
    """Raised when a file exceeds the configured read limit."""
