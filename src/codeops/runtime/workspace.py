"""Copy a bounded, link-free repository snapshot before inspection."""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import BinaryIO, Protocol

from codeops.runtime.lifecycle import RunTerminated, TerminationReason


class SnapshotPolicy(Protocol):
    def denies_read(self, path: str) -> bool: ...
    def permits_read(self, path: str) -> bool: ...


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
MIB = 1024 * 1024


@contextmanager
def open_regular(root: Path | int, relative: Path) -> Iterator[BinaryIO]:
    """Open a single-link regular file without following any path components."""
    fd = os.dup(root) if isinstance(root, int) else os.open(root, _DIR)
    try:
        for directory in relative.parts[:-1]:
            child = os.open(directory, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        with _open_file(fd, relative.name) as source:
            yield source
    finally:
        os.close(fd)


@contextmanager
def _open_file(parent: int, name: str) -> Iterator[BinaryIO]:
    with os.fdopen(os.open(name, _FILE, dir_fd=parent), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RunTerminated(TerminationReason.POLICY_DENIED, "Unsafe input file type.")
        yield source


class SandboxWorkspace:
    """Materialize only the locally readable files under a private run directory."""

    def __init__(self, repo_path: Path, run_dir: Path, quota_mb: int) -> None:
        self.repo_path = repo_path.resolve()
        self.path = run_dir / "workspace"
        self.quota_bytes = quota_mb * MIB
        self._copied_bytes = 0

    def prepare(self, policy: SnapshotPolicy) -> Path:
        if self.path.resolve().is_relative_to(self.repo_path):
            raise RunTerminated(TerminationReason.POLICY_DENIED, "Run registry overlaps the repository.")
        home = Path.home().resolve()
        if self.repo_path == home or home.is_relative_to(self.repo_path):
            raise RunTerminated(TerminationReason.POLICY_DENIED, "Repository includes host HOME.")
        if self.repo_path.is_relative_to(home):
            relative = self.repo_path.relative_to(home)
            if relative.parts[0].startswith(".") or policy.denies_read(relative.as_posix()):
                raise RunTerminated(TerminationReason.POLICY_DENIED, "Repository is in private HOME storage.")
        if policy.denies_read(self.repo_path.name):
            raise RunTerminated(TerminationReason.POLICY_DENIED, "Repository name is excluded by read policy.")

        root = os.open(self.repo_path, _DIR)
        try:
            self.path.mkdir(mode=0o700)
            self._copy_tree(root, Path(), policy, os.fstat(root).st_dev)
        finally:
            os.close(root)
        return self.path

    def _copy_tree(
        self, source: int, relative: Path, policy: SnapshotPolicy, device: int,
    ) -> None:
        for name in sorted(os.listdir(source)):
            child = relative / name
            path = child.as_posix()
            if policy.denies_read(path):
                continue
            info = os.stat(name, dir_fd=source, follow_symlinks=False)
            if info.st_dev != device:
                continue
            if stat.S_ISDIR(info.st_mode):
                fd = os.open(name, _DIR, dir_fd=source)
                try:
                    # Nested Git roots are separate repositories.
                    if os.fstat(fd).st_dev == device and ".git" not in os.listdir(fd):
                        self._copy_tree(fd, child, policy, device)
                finally:
                    os.close(fd)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and policy.permits_read(path):
                self._copy_file(source, name, self.path / child, device)

    def _copy_file(self, parent: int, name: str, target: Path, device: int) -> None:
        with _open_file(parent, name) as source:
            info = os.fstat(source.fileno())
            if info.st_dev != device:
                return
            self._check_usage(self._copied_bytes + info.st_size)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                while chunk := source.read(MIB):
                    self._copied_bytes += len(chunk)
                    self._check_usage(self._copied_bytes)
                    output.write(chunk)
            target.chmod(0o700 if info.st_mode & 0o111 else 0o600)

    def check_quota(self) -> None:
        size = 0
        for _, _, files, fd in os.fwalk(self.path, follow_symlinks=False):
            for name in files:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    size += max(info.st_size, info.st_blocks * 512)
                    self._check_usage(size)

    def _check_usage(self, size: int) -> None:
        if size > self.quota_bytes:
            raise RunTerminated(TerminationReason.RESOURCE_EXHAUSTED, "Workspace quota exceeded.")
