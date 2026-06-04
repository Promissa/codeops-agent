"""Deterministic requirement parsing."""

from codeops.core.models import AcceptanceContract, AcceptanceExample


SECTION_LABELS = {
    "bug": "bug",
    "summary": "bug",
    "reproduction": "reproduction",
    "repro": "reproduction",
    "expected": "expected",
    "actual": "actual",
    "invariants": "invariants",
    "invariant": "invariants",
    "non-goals": "non_goals",
    "non-goal": "non_goals",
    "non_goals": "non_goals",
}


class RequirementParser:
    """Parse issue text into an AcceptanceContract."""

    def parse(
        self,
        issue_text: str,
        requirement_id: str = "R1",
    ) -> AcceptanceContract:
        sections = _sections(issue_text)
        expected = sections.get("expected", "").strip()
        reproduction = sections.get("reproduction", "").strip()

        ambiguity_questions = []
        if not expected:
            ambiguity_questions.append(
                "What user-visible behavior should this change produce?"
            )

        return AcceptanceContract(
            requirement_id=requirement_id,
            summary=_summary(issue_text, sections),
            user_visible_before=sections.get("actual") or None,
            user_visible_after=expected or None,
            examples=_examples(reproduction, expected),
            invariants=_list_section(sections.get("invariants", "")),
            non_goals=_list_section(sections.get("non_goals", "")),
            ambiguity_questions=ambiguity_questions,
            status="ready" if expected else "needs_clarification",
        )


def _sections(issue_text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = "body"

    for line in issue_text.splitlines():
        label, value = _split_label(line)
        if label is not None:
            current = label
            sections.setdefault(current, [])
            if value:
                sections[current].append(value)
            continue
        sections.setdefault(current, []).append(line)

    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _split_label(line: str) -> tuple[str | None, str]:
    if ":" not in line:
        return None, ""
    possible_label, value = line.split(":", maxsplit=1)
    normalized = possible_label.strip().lower()
    label = SECTION_LABELS.get(normalized)
    if label is None:
        return None, ""
    return label, value.strip()


def _summary(issue_text: str, sections: dict[str, str]) -> str:
    bug = sections.get("bug", "").strip()
    if bug:
        return _first_content_line(bug)
    return _first_content_line(issue_text)


def _first_content_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip(" `")
        if stripped:
            return stripped
    return "Unspecified requirement"


def _examples(reproduction: str, expected: str) -> list[AcceptanceExample]:
    if not reproduction or not expected:
        return []
    return [AcceptanceExample(input=reproduction, expected=expected)]


def _list_section(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        items.append(stripped.removeprefix("-").strip())
    return items
