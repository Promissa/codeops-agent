"""CSV parsing helpers for the fixture repository."""


def parse_csv(text: str) -> list[list[str | None]]:
    rows = []
    for line in text.strip().splitlines():
        rows.append(parse_row(line))
    return rows


def parse_row(line: str) -> list[str | None]:
    parts = line.split(",")
    # Intentional bug: drops trailing empty column and may create downstream mismatch.
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return [normalize_field(part) for part in parts]


def normalize_field(value: str) -> str | None:
    if value == "NULL":
        return None
    return value
