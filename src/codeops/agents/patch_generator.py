"""Patch generation."""

from collections.abc import Sequence
import difflib
from pathlib import Path
from typing import Protocol

from codeops.core.errors import ToolError
from codeops.core.models import AcceptanceContract, PatchPlan


class PatchProvider(Protocol):
    """Future provider interface for model-backed patch generation."""

    def generate_patch(
        self,
        repo_path: Path,
        patch_plan: PatchPlan,
        acceptance_contract: AcceptanceContract,
    ) -> str:
        """Return a unified diff."""


class PatchGenerator:
    """Generate unified diffs without mutating the repository."""

    def generate(
        self,
        repo_path: Path,
        patch_plan: PatchPlan,
        acceptance_contract: AcceptanceContract,
    ) -> str:
        if _is_csv_trailing_empty_contract(acceptance_contract):
            return self._fixture_csv_patch(repo_path, patch_plan)
        raise ToolError("no Phase 7 rule-based patch is available for this task")

    def _fixture_csv_patch(self, repo_path: Path, patch_plan: PatchPlan) -> str:
        parser_path = _find_path(
            patch_plan.files_to_edit,
            "src/mini_data_pipeline/parser.py",
        )
        test_path = _find_path(
            patch_plan.tests_to_add_or_update,
            "tests/test_parser.py",
        )
        parser_file = repo_path / parser_path
        test_file = repo_path / test_path

        parser_original = parser_file.read_text()
        parser_updated = _update_parser(parser_original)
        test_original = test_file.read_text()
        test_updated = _update_parser_tests(test_original)

        return "".join(
            [
                _unified_diff(parser_path, parser_original, parser_updated),
                _unified_diff(test_path, test_original, test_updated),
            ]
        )


def _is_csv_trailing_empty_contract(contract: AcceptanceContract) -> bool:
    text = " ".join(
        [
            contract.summary,
            contract.user_visible_before or "",
            contract.user_visible_after or "",
        ]
    ).lower()
    return "csv" in text and "trailing" in text and "empty" in text


def _find_path(paths: Sequence[str], expected: str) -> str:
    for path in paths:
        if path == expected:
            return path
    raise ToolError(f"patch plan does not include required file: {expected}")


def _update_parser(source: str) -> str:
    updated = source.replace(
        '    # Intentional bug: drops trailing empty column and may create downstream mismatch.\n'
        '    if parts and parts[-1] == "":\n'
        "        parts = parts[:-1]\n"
        "    return [normalize_field(part) for part in parts]\n",
        "    return [normalize_field(part) for part in parts]\n",
    )
    updated = updated.replace(
        'def normalize_field(value: str) -> str | None:\n'
        '    if value == "NULL":\n',
        'def normalize_field(value: str) -> str | None:\n'
        '    if value == "":\n'
        "        return None\n"
        '    if value == "NULL":\n',
    )
    return updated


def _update_parser_tests(source: str) -> str:
    if "test_trailing_empty_column_returns_none" in source:
        return source
    addition = (
        "\n\n"
        "def test_trailing_empty_column_returns_none():\n"
        '    assert parse_csv("a,b,c\\n1,2,\\n") == '
        '[["a", "b", "c"], ["1", "2", None]]\n'
    )
    return source.rstrip() + addition


def _unified_diff(path: str, original: str, updated: str) -> str:
    if original == updated:
        return ""
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
