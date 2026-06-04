"""Allowlist policy for agent-generated commands."""

import shlex
from dataclasses import dataclass
from typing import Sequence

from codeops.core.errors import CommandRejected


_SHELL_TOKENS = {"|", "&&", "||", ";", ">", ">>", "<", "$(", "`"}
_CODEGRAPH_SUBCOMMANDS = {
    "status",
    "query",
    "search",
    "files",
    "callers",
    "callees",
    "impact",
    "affected",
    "sync",
}


@dataclass(frozen=True)
class CommandPolicyResult:
    allowed: bool
    argv: list[str]
    reason: str


class CommandPolicy:
    """Validate deterministic commands before execution."""

    def validate(self, command: str | Sequence[str]) -> CommandPolicyResult:
        argv = _normalize_command(command)
        if not argv:
            return CommandPolicyResult(False, [], "empty command")

        joined = " ".join(argv)
        if any(token in joined for token in _SHELL_TOKENS):
            return CommandPolicyResult(False, argv, "shell control operators are not allowed")

        program = argv[0]
        if program == "pytest":
            return CommandPolicyResult(True, argv, "pytest is allowed")

        if program == "python" and argv[1:3] == ["-m", "pytest"]:
            return CommandPolicyResult(True, argv, "python -m pytest is allowed")

        if program == "ruff" and argv[1:3] == ["check", "."]:
            return CommandPolicyResult(True, argv, "ruff check . is allowed")

        if program == "mypy" and argv[1:] == ["src"]:
            return CommandPolicyResult(True, argv, "mypy src is allowed")

        if program == "git":
            return _validate_git(argv)

        if program == "codegraph":
            return _validate_codegraph(argv)

        return CommandPolicyResult(False, argv, f"{program!r} is not allowlisted")

    def enforce(self, command: str | Sequence[str]) -> list[str]:
        result = self.validate(command)
        if not result.allowed:
            raise CommandRejected(result.reason)
        return result.argv


def _normalize_command(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


def _validate_git(argv: list[str]) -> CommandPolicyResult:
    if argv[1:] == ["diff"] or argv[1:] == ["status"]:
        return CommandPolicyResult(True, argv, f"{' '.join(argv)} is allowed")

    if len(argv) >= 3 and argv[1:3] == ["apply", "--check"]:
        return CommandPolicyResult(True, argv, "git apply --check is allowed")

    if len(argv) >= 2 and argv[1] == "apply" and "--check" not in argv[2:]:
        return CommandPolicyResult(True, argv, "git apply is allowed")

    return CommandPolicyResult(False, argv, "git command is not allowlisted")


def _validate_codegraph(argv: list[str]) -> CommandPolicyResult:
    if len(argv) >= 2 and argv[1] in _CODEGRAPH_SUBCOMMANDS:
        return CommandPolicyResult(True, argv, "codegraph subcommand is allowed")
    return CommandPolicyResult(False, argv, "codegraph command is not allowlisted")
