"""Linux Bubblewrap runtime for subprocesses operating on a filtered snapshot."""

from collections.abc import Sequence
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
from typing import Protocol

from codeops.runtime.lifecycle import RunTerminated, TerminationReason
from codeops.runtime.mounts import RUNTIME_ENV, private_mounts
from codeops.runtime.registry import ActiveRun
from codeops.runtime.workspace import MIB, SandboxWorkspace
from codeops.trust.policy import SandboxConfig


_SYSTEM_PATHS = (
    "/usr", "/bin", "/lib", "/lib64", "/etc/alternatives", "/etc/ssl",
    "/etc/ld.so.cache", "/etc/localtime", "/etc/passwd", "/etc/group",
)
_OUTPUT_LIMIT = 8 * MIB


class SandboxBackend(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        input_text: str | None = None,
        readonly: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


class BwrapSandboxBackend:
    """Run commands in a new mount and PID namespace, without host environment."""

    executable = "/usr/bin/bwrap"

    def __init__(
        self, workspace: SandboxWorkspace, run: ActiveRun, config: SandboxConfig,
    ) -> None:
        self.workspace = workspace
        self.active_run = run
        self.config = config
        self.pytest_probe: Path | None = None
        self._deadline = time.monotonic() + config.timeout_seconds
        if not Path(self.executable).is_file():
            raise RunTerminated(TerminationReason.RUNNER_ERROR, f"Bubblewrap unavailable: {self.executable}")
        self._mounts = run.resources.enter_context(private_mounts(
            run.path, workspace.path, tmp_bytes=config.tmpfs_size_mb * MIB,
            workspace_bytes=workspace.quota_bytes, timeout=config.timeout_seconds,
        ))
        run.mount_pid = self._mounts.pid
        run.record()

    def probe(self) -> None:
        """Check that the configured namespace and mounts can actually start."""
        self.run(["/usr/bin/true"], cwd=self.workspace.path, timeout_seconds=5, readonly=True)

    @property
    def workspace_view(self) -> Path:
        return self._mounts.workspace_view()

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        input_text: str | None = None,
        readonly: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        deadline = min(self._deadline, time.monotonic() + timeout_seconds)
        if time.monotonic() >= deadline:
            raise RunTerminated(TerminationReason.TIMED_OUT, "Sandbox timeout expired.")
        with (
            tempfile.TemporaryFile(dir=self.active_run.path) as stdout,
            tempfile.TemporaryFile(dir=self.active_run.path) as stderr,
            tempfile.TemporaryFile(dir=self.active_run.path) as status,
        ):
            command = self._command(argv, cwd, status.fileno(), readonly)
            try:
                process = subprocess.Popen(
                    command, start_new_session=True, pass_fds=(status.fileno(),),
                    stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, text=True, encoding="utf-8",
                    env=RUNTIME_ENV,
                )
            except OSError as exc:
                raise RunTerminated(TerminationReason.RUNNER_ERROR, str(exc)) from exc
            try:
                self.active_run.record(process.pid)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RunTerminated(TerminationReason.TIMED_OUT, "Sandbox command timed out.")
                    try:
                        process.communicate(input_text, timeout=min(remaining, 0.05))
                    except subprocess.TimeoutExpired:
                        input_text = None
                    if any(os.fstat(stream.fileno()).st_size > _OUTPUT_LIMIT for stream in (stdout, stderr)):
                        raise RunTerminated(TerminationReason.RESOURCE_EXHAUSTED, "Sandbox output limit exceeded.")
                    if process.returncode is not None:
                        break
            finally:
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                self.active_run.record()

            stdout.seek(0)
            stderr.seek(0)
            status.seek(0)
            output = stdout.read().decode("utf-8", errors="replace")
            errors = stderr.read().decode("utf-8", errors="replace")
            if not any("exit-code" in json.loads(line) for line in status):
                raise RunTerminated(
                    TerminationReason.RUNNER_ERROR, errors.strip() or "Bubblewrap setup failed.",
                )
            if process.returncode and (
                process.returncode == 128 + signal.SIGXFSZ or self._mounts.is_full()
            ):
                raise RunTerminated(TerminationReason.RESOURCE_EXHAUSTED, "Sandbox storage limit exceeded.")
            return subprocess.CompletedProcess(list(argv), process.returncode, output, errors)

    def _command(
        self, argv: Sequence[str], cwd: Path, status_fd: int, readonly: bool,
    ) -> list[str]:
        try:
            relative_cwd = cwd.resolve().relative_to(self.workspace.path.resolve())
        except ValueError as exc:
            raise RunTerminated(TerminationReason.POLICY_DENIED, "Sandbox cwd is outside the snapshot.") from exc

        args = [
            self.executable, "--die-with-parent", "--unshare-user", "--unshare-pid",
            "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try", "--cap-drop", "ALL", "--clearenv",
            "--json-status-fd", str(status_fd), "--proc", "/proc", "--dev", "/dev",
        ]
        if not self.config.network_enabled:
            args.append("--unshare-net")
        paths = _SYSTEM_PATHS
        if self.config.network_enabled:
            paths += ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf")
        for name in paths:
            if Path(name).exists():
                self._bind_runtime(args, Path(name), name)
        probe = self.pytest_probe if "_codeops_pytest_probe" in argv else None
        python = self._bind_python(args, probe)

        args.extend([
            "--bind", str(self._mounts.tmp), "/tmp",
            "--symlink", "/tmp/.codeops-home", "/sandbox_home",
            "--symlink", "/tmp", "/var/tmp",
            "--ro-bind" if readonly else "--bind", str(self._mounts.workspace), "/workspace",
            "--remount-ro", "/",
            "--chdir", str(Path("/workspace") / relative_cwd),
        ])
        for key, value in {
            "PATH": f"{Path(python).parent}:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": "/sandbox_home",
            "TMPDIR": "/tmp",
            "XDG_CACHE_HOME": "/sandbox_home/.cache",
            "XDG_RUNTIME_DIR": "/sandbox_home/.run",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }.items():
            args.extend(["--setenv", key, value])

        resolved = list(argv)
        if argv[0] == "pytest":
            resolved = [python, "-m", "pytest", *argv[1:]]
        elif argv[0] in {"python", "python3"}:
            resolved[0] = python
        return [
            *self._mounts.namespace_prefix(), *args, "--", "/usr/bin/prlimit",
            f"--fsize={self.workspace.quota_bytes}", "--", *resolved,
        ]

    def _bind_python(self, args: list[str], probe: Path | None = None) -> str:
        base = Path(sys.base_prefix).resolve()
        executable = Path(sys._base_executable).resolve()
        if executable.is_relative_to("/usr"):
            python = str(executable)
        else:
            destination = Path("/runtime/python")
            self._bind_runtime(args, base, str(destination))
            python = str(destination / executable.relative_to(base))
        site_paths = []
        if probe is not None:
            destination = "/runtime/evidence"
            self._bind_runtime(args, probe, f"{destination}/{probe.name}")
            site_paths.append(destination)
        if sys.prefix != sys.base_prefix:
            roots = dict.fromkeys(sysconfig.get_path(name) for name in ("purelib", "platlib"))
            for index, root in enumerate(roots):
                destination = f"/runtime/site-{index}"
                self._bind_runtime(args, Path(root), destination)
                site_paths.append(destination)
        if site_paths:
            args.extend(["--setenv", "PYTHONPATH", ":".join(site_paths)])
        return python

    def _bind_runtime(self, args: list[str], source: Path, target: str) -> None:
        real = source.resolve()
        protected = (Path.home().resolve(), self.workspace.repo_path, Path("/tmp"))
        if any(path.is_relative_to(real) for path in protected):
            raise RunTerminated(TerminationReason.POLICY_DENIED, f"Unsafe runtime mount: {source}")
        args.extend(["--ro-bind", str(source), target])
