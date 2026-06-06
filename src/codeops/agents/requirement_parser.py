"""Deterministic requirement parsing."""

from codeops.core.models import AcceptanceContract, AcceptanceExample


SECTION_LABELS = {
    "bug": "bug",
    "summary": "bug",
    "issue description": "bug",
    "reproduction": "reproduction",
    "repro": "reproduction",
    "step-by-step reproduction": "reproduction",
    "steps to reproduce": "reproduction",
    "expected": "expected",
    "expected behavior": "expected",
    "actual": "actual",
    "actual behavior": "actual",
    "relevant log output": "actual",
    "logs": "actual",
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
        expected = _expected_behavior(sections)
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
    heading = line.strip().lstrip("#").strip()
    normalized_heading = heading.lower()
    if normalized_heading in SECTION_LABELS:
        return SECTION_LABELS[normalized_heading], ""

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
        if stripped.lower() in {"hello.", "hello"}:
            continue
        if stripped.lower().startswith(("i will re-post", "i will repost")):
            continue
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


def _expected_behavior(sections: dict[str, str]) -> str:
    expected = sections.get("expected", "").strip()
    if expected:
        return expected
    if sections.get("reproduction", "").strip() and sections.get("actual", "").strip():
        return "The documented reproduction should complete without the logged failure."
    return ""
