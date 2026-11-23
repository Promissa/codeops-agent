"""Measure the net change in a workspace, including untracked files."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from difflib import unified_diff
from hashlib import sha256
import os
from pathlib import Path
import stat

from pydantic import Field

from codeops.runtime.workspace import open_regular
from codeops.tools.patch_tool import PatchSummary


TRANSIENT_PATHS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".codeops",
    ".codegraph", "node_modules", "target", "build", ".venv", ".git", ".runs",
    ".gradle", ".tox", ".nox", ".coverage", ".hypothesis", "dist",
})


@dataclass(frozen=True)
class _File:
    mode: str
    digest: str


class WorkspaceDiffSummary(PatchSummary):
    unified_diff: str
    added_files: list[str]
    deleted_files: list[str]
    added_loc: int
    removed_loc: int
    unexpected_files: list[str]
    unsupported_files: list[str]
    fingerprints: dict[str, str] = Field(default_factory=dict)


def workspace_diff(
    baseline: Path, candidate: Path, submitted_files: list[str] | None = None, *,
    paths: set[str] | None = None, permits_read: Callable[[str], bool] | None = None,
) -> WorkspaceDiffSummary:
    old = dict(_files(baseline, paths, permits_read))
    new = dict(_files(candidate, paths, permits_read))
    changed: list[str] = []
    added: list[str] = []
    deleted: list[str] = []
    unsupported: list[str] = []
    patches: list[str] = []
    fingerprints = {}
    added_loc = removed_loc = 0
    for path in sorted(old.keys() | new.keys()):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        changed.append(path)
        if before is None:
            added.append(path)
        if after is None:
            deleted.append(path)
        a, b = _quote("a/" + path), _quote("b/" + path)
        patches.append(f"diff --git {a} {b}\n")
        old_mode = before.mode if before else None
        new_mode = after.mode if after else None
        fingerprints[path] = f"{old_mode}:{before.digest if before else ''}:{new_mode}:{after.digest if after else ''}"
        if permits_read is not None and not permits_read(path):
            unsupported.append(path)
            continue
        if old_mode is None:
            patches.append(f"new file mode {new_mode}\n")
        elif new_mode is None:
            patches.append(f"deleted file mode {old_mode}\n")
        elif old_mode != new_mode:
            patches.extend([f"old mode {old_mode}\n", f"new mode {new_mode}\n"])
        old_lines = _text_lines(baseline, path, before)
        new_lines = _text_lines(candidate, path, after)
        if old_lines is None or new_lines is None:
            unsupported.append(path)
            patches.append(f"Binary files {a if before else '/dev/null'} and {b if after else '/dev/null'} differ\n")
            continue
        lines = unified_diff(
            old_lines, new_lines, fromfile=a if before else "/dev/null", tofile=b if after else "/dev/null",
        )
        for index, line in enumerate(lines):
            added_loc += index >= 2 and line.startswith("+")
            removed_loc += index >= 2 and line.startswith("-")
            patches.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return WorkspaceDiffSummary(
        unified_diff="".join(patches), changed_files=changed, added_files=added, deleted_files=deleted,
        added_loc=added_loc, removed_loc=removed_loc, changed_loc=added_loc + removed_loc,
        unexpected_files=sorted(set(changed) - set(submitted_files)) if submitted_files is not None else [],
        unsupported_files=unsupported, fingerprints=fingerprints,
    )


def _files(
    root: Path, paths: set[str] | None, permits_read: Callable[[str], bool] | None,
) -> Iterator[tuple[str, _File]]:
    def raise_error(exc: OSError) -> None:
        raise exc

    for directory, children, names in os.walk(root, followlinks=False, onerror=raise_error):
        parent = Path(directory)
        children[:] = sorted(set(children) - TRANSIENT_PATHS)
        for name in [*children, *sorted(set(names) - TRANSIENT_PATHS)]:
            path = parent / name
            relative = path.relative_to(root)
            key = relative.as_posix()
            if paths is not None and key not in paths:
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if permits_read is not None and not permits_read(key):
                yield key, _File("excluded", f"{info.st_size}:{info.st_mtime_ns}:{info.st_mode}")
            else:
                yield key, _file_fingerprint(root, relative, info)


def _file_fingerprint(root: Path, relative: Path, info: os.stat_result) -> _File:
    if stat.S_ISLNK(info.st_mode):
        return _File("120000", sha256(os.fsencode(os.readlink(root / relative))).hexdigest())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return _File("unsupported", f"{info.st_mode}:{info.st_ino}:{info.st_mtime_ns}")
    digest = sha256()
    with open_regular(root, relative) as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return _File("100755" if info.st_mode & 0o111 else "100644", digest.hexdigest())


def _text_lines(root: Path, path: str, entry: _File | None) -> list[str] | None:
    if entry is None:
        return []
    if entry.mode not in {"100644", "100755"}:
        return None
    with open_regular(root, Path(path)) as source:
        data = source.read()
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in decoded:
        return None
    parts = decoded.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _quote(path: str) -> str:
    if all(" " < char < "\x7f" and char not in {'"', "\\"} for char in path):
        return path
    # Git uses C-style octal escapes for non-ASCII UTF-8 bytes.
    return '"' + "".join(
        chr(byte) if 32 <= byte < 127 and byte not in (34, 92) else f"\\{byte:03o}"
        for byte in os.fsencode(path)
    ) + '"'
