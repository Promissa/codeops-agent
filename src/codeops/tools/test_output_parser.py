"""Parsers for structured test command output."""

import json
from typing import Any

from pydantic import BaseModel, Field


class ParsedTestOutput(BaseModel):
    passed: bool
    failed_tests: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def parse_go_test_json(output: str) -> ParsedTestOutput:
    """Parse newline-delimited `go test -json` events."""
    failed_tests: list[str] = []
    packages: list[str] = []
    saw_pass = False
    warnings: list[str] = []

    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            warnings.append("ignored non-json go test output line")
            continue
        if not isinstance(event, dict):
            continue
        package = _string(event.get("Package"))
        test = _string(event.get("Test"))
        action = _string(event.get("Action"))
        if package and package not in packages:
            packages.append(package)
        if action == "pass":
            saw_pass = True
        elif action == "fail":
            failed_tests.append(f"{package}::{test}" if test else package)

    return ParsedTestOutput(
        passed=saw_pass and not failed_tests,
        failed_tests=failed_tests,
        packages=packages,
        warnings=warnings,
    )


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""
