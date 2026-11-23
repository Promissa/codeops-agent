"""Hold bounded per-task tmpfs mounts in a private Linux mount namespace."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import select
import signal
import subprocess
import tempfile

from codeops.runtime.lifecycle import RunTerminated, TerminationReason


_PREPARE_MOUNTS = """
/usr/bin/mount -t tmpfs -o "size=$1,mode=1777,nosuid,nodev" tmpfs "$3"
/usr/bin/mount -t tmpfs -o "size=$2,mode=0700,nosuid,nodev" tmpfs "$4"
/usr/bin/mkdir -m 0700 -- "$3/.codeops-home" "$3/.codeops-home/.cache" "$3/.codeops-home/.run"
/usr/bin/cp -a -- "$5/." "$4"
printf 'R'
exec /usr/bin/cat > /dev/null
"""
RUNTIME_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}


@dataclass(frozen=True)
class PrivateMounts:
    pid: int
    tmp: Path
    workspace: Path

    def namespace_prefix(self) -> list[str]:
        return [
            "/usr/bin/nsenter", "--target", str(self.pid), "--user",
            "--preserve-credentials", "--mount", "--",
        ]

    def workspace_view(self) -> Path:
        return Path(f"/proc/{self.pid}/root") / self.workspace.relative_to("/")

    def is_full(self) -> bool:
        root = Path(f"/proc/{self.pid}/root")
        for mount in (self.tmp, self.workspace):
            usage = os.statvfs(root / mount.relative_to("/"))
            if usage.f_bavail == 0 or usage.f_favail == 0:
                return True
        return False


@contextmanager
def private_mounts(
    run_dir: Path, snapshot: Path, *, tmp_bytes: int, workspace_bytes: int, timeout: float,
) -> Iterator[PrivateMounts]:
    tmp = run_dir / "tmp"
    workspace = run_dir / "work"
    tmp.mkdir()
    workspace.mkdir()
    command = [
        "/usr/bin/unshare", "--user", "--map-root-user", "--mount",
        "--propagation", "private", "--", "/bin/sh", "-ec", _PREPARE_MOUNTS,
        "codeops-mounts", str(tmp_bytes), str(workspace_bytes), str(tmp),
        str(workspace), str(snapshot),
    ]
    with tempfile.TemporaryFile(dir=run_dir) as errors:
        try:
            keeper = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=errors, start_new_session=True, env=RUNTIME_ENV,
            )
        except OSError as exc:
            raise RunTerminated(TerminationReason.RUNNER_ERROR, str(exc)) from exc
        assert keeper.stdin is not None and keeper.stdout is not None
        try:
            if not select.select([keeper.stdout], [], [], timeout)[0]:
                raise RunTerminated(TerminationReason.TIMED_OUT, "Sandbox mount setup timed out.")
            if keeper.stdout.read(1) != b"R":
                errors.seek(0)
                detail = errors.read().decode("utf-8", errors="replace").strip()
                reason = TerminationReason.RUNNER_ERROR
                if "No space left on device" in detail:
                    reason = TerminationReason.RESOURCE_EXHAUSTED
                raise RunTerminated(reason, detail or "Sandbox mount setup failed.")
            # EOF on the stdin pipe releases both mounts if the runner dies.
            yield PrivateMounts(keeper.pid, tmp, workspace)
        finally:
            keeper.stdin.close()
            keeper.stdout.close()
            try:
                keeper.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(keeper.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                keeper.wait()
