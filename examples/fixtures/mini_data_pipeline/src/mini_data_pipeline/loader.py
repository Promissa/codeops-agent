"""Data loading helpers for the fixture repository."""

from pathlib import Path

from mini_data_pipeline.parser import parse_csv


def load_csv(path: Path) -> list[list[str | None]]:
    return parse_csv(path.read_text())
