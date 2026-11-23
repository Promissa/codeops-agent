"""Private run directories with crash-safe ownership tracking."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile


DEFAULT_RUN_STATE_DIR = Path("/tmp") / f"codeops-runs-{os.getuid()}"


@dataclass
class ActiveRun:
    path: Path
    resources: ExitStack
    mount_pid: int | None = None

    def record(self, command_pid: int | None = None) -> None:
        pending = self.path / "active.tmp"
        pending.write_text(json.dumps({
            "owner_pid": os.getpid(), "command_pid": command_pid, "mount_pid": self.mount_pid,
        }))
        pending.replace(self.path / "active.json")


class RunRegistry:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir or DEFAULT_RUN_STATE_DIR
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_dir.lstat()
        if self.state_dir.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError(f"Run registry must be private: {self.state_dir}")

    @contextmanager
    def _registry_lock(self) -> Iterator[None]:
        with (self.state_dir / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @contextmanager
    def register(self) -> Iterator[ActiveRun]:
        with self._registry_lock():
            path = Path(tempfile.mkdtemp(prefix="run-", dir=self.state_dir))
            lock = (path / "owner.lock").open("x")
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with ExitStack() as resources:
                run = ActiveRun(path, resources)
                run.record()
                yield run
        finally:
            with lock, self._registry_lock():
                _remove_run(path)

    def cleanup_stale_runs(self) -> int:
        removed = 0
        with self._registry_lock():
            for path in self.state_dir.glob("run-*"):
                if path.is_symlink() or not path.is_dir():
                    continue
                with (path / "owner.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    # Bubblewrap's parent-death signal terminates the private PID namespace.
                    _remove_run(path)
                    removed += 1
        return removed


def cleanup_stale_runs(state_dir: Path | None = None) -> int:
    return RunRegistry(state_dir).cleanup_stale_runs()


def _remove_run(path: Path) -> None:
    # Sandbox commands own their snapshot and can change directory permissions.
    for directory, children, _files in os.walk(
        path, topdown=True, followlinks=False, onerror=lambda exc: _make_searchable(Path(exc.filename)),
    ):
        for child in children:
            _make_searchable(Path(directory) / child)
    shutil.rmtree(path)


def _make_searchable(directory: Path) -> None:
    if not directory.is_symlink():
        directory.chmod(0o700)
