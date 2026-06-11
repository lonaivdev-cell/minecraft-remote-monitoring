"""Transport layer: run commands & move files on the server.

SshTransport wraps the system OpenSSH client with ControlMaster multiplexing:
one authenticated connection, every subsequent call rides the control socket
(~10ms instead of a full handshake). Remote payloads are always piped to
`bash -s` on stdin, so the remote login shell (fish on CarborioLand) never
parses a single byte of our scripts — no quoting roulette, no fish-isms.

LocalTransport runs the same bash snippets locally; it powers the test suite
and `transport = "local"` dev mode against a sandbox directory.
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
from base64 import b64encode
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import util
from .config import Config

log = util.get_logger("transport")


class TransportError(RuntimeError):
    pass


@dataclass(slots=True)
class RunResult:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def q(s: str) -> str:
    """POSIX-quote a value for embedding in a bash -s payload."""
    return shlex.quote(s)


class BaseTransport:
    """Interface; concrete methods raise TransportError on connection-level failure."""

    def run(self, script: str, *, timeout: float = 30, check: bool = False) -> RunResult:
        raise NotImplementedError

    def stream(self, script: str) -> Iterator[str]:
        raise NotImplementedError

    def read_text(self, path: str, *, check: bool = True) -> str:
        r = self.run(f"cat {q(path)}", timeout=30, check=check)
        return r.out

    def get_bytes(self, path: str) -> bytes:
        import base64
        r = self.run(f"base64 < {q(path)}", timeout=120, check=True)
        return base64.b64decode(r.out)

    def write_text(self, path: str, content: str, *, backup: bool = False, mode: str = "") -> None:
        b64 = b64encode(content.encode()).decode()
        backup_cmd = (
            f'[ -f {q(path)} ] && cp -a {q(path)} {q(path)}".bak.$(date +%Y%m%d-%H%M%S)"\n'
            if backup else ""
        )
        chmod_cmd = f"chmod {q(mode)} {q(path)}\n" if mode else ""
        script = (
            "set -e\n"
            f"mkdir -p \"$(dirname {q(path)})\"\n"
            f"tmp=$(mktemp {q(path)}.XXXXXX)\n"
            f"printf '%s' {q(b64)} | base64 -d > \"$tmp\"\n"
            f"{backup_cmd}"
            f"mv \"$tmp\" {q(path)}\n"
            f"{chmod_cmd}"
        )
        self.run(script, timeout=60, check=True)

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {q(path)}", timeout=15).ok

    def rsync(self, src: str, dst: str, *, delete: bool = False, excludes: tuple[str, ...] = ()) -> int:
        raise NotImplementedError

    @contextlib.contextmanager
    def tunnel(self, remote_port: int) -> Iterator[int]:
        """Yield a local port forwarded to 127.0.0.1:remote_port on the server."""
        raise NotImplementedError
        yield 0  # pragma: no cover

    def interactive(self, remote_argv: list[str], *, tty: bool = True) -> int:
        raise NotImplementedError

    def alive(self) -> bool:
        try:
            return self.run("true", timeout=10).ok
        except TransportError:
            return False

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    # convenience used by several subsystems
    def remote_spec(self, path: str) -> str:
        """Return an rsync-style remote spec for `path` (or the path itself locally)."""
        return path


class SshTransport(BaseTransport):
    def __init__(self, cfg: Config):
        s = cfg.server
        self._host = s.host
        self._user = s.user
        self._port = s.ssh_port
        self._key = s.ssh_key       # path to SSH private key, empty = use agent/default
        self._extra = list(s.ssh_options)
        util.ensure_dirs()
        # %C = hash of local host/remote host/port/user: short & collision-free
        self._control = str(util.runtime_dir() / "cm-%C")

    # ---------------------------------------------------------------- argv builders

    @property
    def target(self) -> str:
        return f"{self._user}@{self._host}"

    def _opts(self, *, batch: bool = True) -> list[str]:
        o = ["-p", str(self._port)]
        if self._key:
            o += ["-i", self._key]
        o += [
            "-o", f"ControlPath={self._control}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=300",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if batch:
            o += ["-o", "BatchMode=yes"]  # keys/agent only; never hang on a password prompt
        return o + self._extra

    def _ssh_e(self) -> str:
        """-e string for rsync, sharing the same control socket."""
        return "ssh " + " ".join(shlex.quote(a) for a in self._opts())

    # ---------------------------------------------------------------- core ops

    def run(self, script: str, *, timeout: float = 30, check: bool = False) -> RunResult:
        argv = ["ssh", *self._opts(), self.target, "--", "bash", "-s"]
        log.debug("ssh run (%.0fs timeout): %s", timeout, script.splitlines()[0][:120] if script else "")
        try:
            p = subprocess.run(
                argv, input=script.encode(), capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as e:
            raise TransportError(f"remote command timed out after {timeout:.0f}s") from e
        except FileNotFoundError as e:
            raise TransportError("ssh binary not found — install openssh") from e
        out = p.stdout.decode(errors="replace")
        err = p.stderr.decode(errors="replace")
        if p.returncode == 255:
            raise TransportError(f"ssh connection to {self.target} failed: {err.strip()[-300:]}")
        r = RunResult(p.returncode, out, err)
        if check and not r.ok:
            raise TransportError(
                f"remote command failed (rc={r.rc}): {script.splitlines()[0][:120]}\n{err.strip()[-500:]}"
            )
        return r

    def stream(self, script: str) -> Iterator[str]:
        argv = ["ssh", *self._opts(), self.target, "--", "bash", "-s"]
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        assert p.stdin and p.stdout
        p.stdin.write(script.encode())
        p.stdin.close()
        try:
            for line in p.stdout:
                yield line.decode(errors="replace").rstrip("\n")
        finally:
            with contextlib.suppress(ProcessLookupError):
                p.terminate()
            p.wait(timeout=5)

    def rsync(self, src: str, dst: str, *, delete: bool = False, excludes: tuple[str, ...] = ()) -> int:
        argv = ["rsync", "-az", "--info=progress2", "-e", self._ssh_e()]
        if delete:
            argv.append("--delete")
        for ex in excludes:
            argv += ["--exclude", ex]
        argv += [src, dst]
        log.info("rsync %s -> %s", src, dst)
        try:
            return subprocess.call(argv)
        except FileNotFoundError as e:
            raise TransportError("rsync binary not found — install rsync") from e

    @contextlib.contextmanager
    def tunnel(self, remote_port: int) -> Iterator[int]:
        self.run("true", timeout=15)  # make sure the control master is up
        lport = util.free_port()
        fwd = f"127.0.0.1:{lport}:127.0.0.1:{remote_port}"
        rc = subprocess.call(
            ["ssh", *self._opts(), "-O", "forward", "-L", fwd, self.target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if rc != 0:
            raise TransportError(f"could not open SSH tunnel to remote port {remote_port}")
        try:
            yield lport
        finally:
            subprocess.call(
                ["ssh", *self._opts(), "-O", "cancel", "-L", fwd, self.target],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    def interactive(self, remote_argv: list[str], *, tty: bool = True) -> int:
        argv = ["ssh", *self._opts(batch=False)]
        if tty:
            argv.append("-t")
        argv += [self.target, "--", *remote_argv]
        return subprocess.call(argv)

    def remote_spec(self, path: str) -> str:
        return f"{self.target}:{path}"

    def close(self) -> None:
        subprocess.call(
            ["ssh", *self._opts(), "-O", "exit", self.target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


class LocalTransport(BaseTransport):
    """Run the same bash payloads on this machine (dev mode + tests)."""

    def __init__(self, cfg: Config | None = None):
        self._cfg = cfg

    def run(self, script: str, *, timeout: float = 30, check: bool = False) -> RunResult:
        try:
            p = subprocess.run(["bash", "-s"], input=script.encode(),
                               capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise TransportError(f"local command timed out after {timeout:.0f}s") from e
        r = RunResult(p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace"))
        if check and not r.ok:
            raise TransportError(f"local command failed (rc={r.rc}): {r.err.strip()[-500:]}")
        return r

    def stream(self, script: str) -> Iterator[str]:
        p = subprocess.Popen(["bash", "-s"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert p.stdin and p.stdout
        p.stdin.write(script.encode())
        p.stdin.close()
        try:
            for line in p.stdout:
                yield line.decode(errors="replace").rstrip("\n")
        finally:
            with contextlib.suppress(ProcessLookupError):
                p.terminate()
            p.wait(timeout=5)

    def rsync(self, src: str, dst: str, *, delete: bool = False, excludes: tuple[str, ...] = ()) -> int:
        import shutil
        if shutil.which("rsync"):
            argv = ["rsync", "-az"]
            if delete:
                argv.append("--delete")
            for ex in excludes:
                argv += ["--exclude", ex]
            return subprocess.call([*argv, src, dst])
        # crude fallback for sandboxes without rsync
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        if Path(src).is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return 0

    @contextlib.contextmanager
    def tunnel(self, remote_port: int) -> Iterator[int]:
        yield remote_port  # "remote" is localhost already

    def interactive(self, remote_argv: list[str], *, tty: bool = True) -> int:
        return subprocess.call(remote_argv)


def make_transport(cfg: Config) -> BaseTransport:
    if cfg.server.transport == "local":
        return LocalTransport(cfg)
    return SshTransport(cfg)
