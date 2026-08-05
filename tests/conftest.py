"""Shared fixtures: isolated XDG dirs, FakeTransport, FakeClock."""

from __future__ import annotations

import os
import re
import shlex
from collections import deque
from pathlib import Path

import pytest

from lulism.config import Config
from lulism.transport import BaseTransport, RunResult, TransportError


def package_source_tree() -> Path:
    """The `lulism` package **in this checkout** — src/lulism, not whatever copy
    happens to be importable.

    Every test that audits source *text* (stray `mcctl` identifiers, the shipped
    unit files, gui_app's constants) must read the tree under review. With
    `pip install -e .` `Path(lulism.__file__).parent` resolves to the same
    directory, which is why nobody noticed the difference; under any
    non-editable install — the PKGBUILD's check(), which runs a plain
    `python -m pytest` with src/ not on sys.path — it points at the *previously
    installed* package, and the guards then pass vacuously against stale code no
    matter what the working tree contains."""
    src = Path(__file__).resolve().parent.parent / "src" / "lulism"
    if not src.is_dir():
        raise RuntimeError(f"expected the package source at {src}; these guards audit "
                           "the checkout, not the installed copy")
    return src


def integration_gate(available: bool, reason: str):
    """Skip an integration module whose real dependency is missing — unless
    LULISM_REQUIRE_INTEGRATION=1, where the absence is a hard error instead.

    A bare `skipif` lets the integration job exit 0 while exercising nothing:
    a flaky apt cache with no tmux, or console scripts that landed somewhere
    this file did not look, silently deletes the whole real-process safety net
    and still shows a green tick. CI sets the flag on the integration step, so
    "the safety net did not run" is red rather than invisible; a developer's
    box without tmux still just skips."""
    if not available and os.environ.get("LULISM_REQUIRE_INTEGRATION") == "1":
        raise RuntimeError(
            f"LULISM_REQUIRE_INTEGRATION=1 but {reason} — the integration suite "
            "must actually run here, not skip")
    return pytest.mark.skipif(not available, reason=reason)


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Every test gets its own config/state/cache tree — never touch real $HOME."""
    for var, sub in (("XDG_CONFIG_HOME", "cfg"), ("XDG_STATE_HOME", "state"),
                     ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "run")):
        d = tmp_path / sub
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(d))
    return tmp_path


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config()
    c.server.transport = "local"
    c.server.server_dir = str(tmp_path / "srv")
    c.backup.remote_dir = str(tmp_path / "bk")
    return c


def _unquote(tok: str) -> str:
    try:
        parts = shlex.split(tok)
        return parts[0] if parts else tok
    except ValueError:
        return tok


class FakeTransport(BaseTransport):
    """Scripted transport: explicit expectations first, then a tiny emulation of
    the handful of read-only shell idioms mcctl uses (wc -c, tail -c, cat)."""

    def __init__(self):
        self.calls: list[str] = []
        self.files: dict[str, str] = {}
        self._scripted: list[tuple[object, deque]] = []
        self.default = RunResult(0, "", "")

    # -------- scripting API
    def expect(self, matcher, *results, rc=0, out="", err=""):
        """matcher: substring or callable(script)->bool. results: RunResult/Exception
        consumed in order (last one repeats); or use rc/out/err shorthand."""
        if not results:
            results = (RunResult(rc, out, err),)
        self._scripted.append((matcher, deque(results)))
        return self

    # -------- BaseTransport
    def run(self, script: str, *, timeout: float = 30, check: bool = False) -> RunResult:
        self.calls.append(script)
        for matcher, results in self._scripted:
            hit = matcher(script) if callable(matcher) else (matcher in script)
            if hit:
                r = results.popleft() if len(results) > 1 else results[0]
                if isinstance(r, Exception):
                    raise r
                break
        else:
            r = self._emulate(script)
        if check and not r.ok:
            raise TransportError(f"rc={r.rc}: {r.err}")
        return r

    def _emulate(self, script: str) -> RunResult:
        m = re.search(r"wc -c < (\S+)", script)
        if m:
            content = self.files.get(_unquote(m.group(1)))
            return RunResult(0, f"{len(content.encode()) if content is not None else 0}\n", "")
        m = re.search(r"tail -c \+(\d+) (\S+)", script)
        if m:
            content = self.files.get(_unquote(m.group(2)), "")
            return RunResult(0, content.encode()[int(m.group(1)) - 1:].decode(errors="replace"), "")
        m = re.search(r"^cat (\S+)$", script.strip())
        if m:
            path = _unquote(m.group(1))
            if path in self.files:
                return RunResult(0, self.files[path], "")
            return RunResult(1, "", f"cat: {path}: No such file or directory")
        m = re.search(r"test -e (\S+)", script)
        if m:
            return RunResult(0 if _unquote(m.group(1)) in self.files else 1, "", "")
        return self.default

    def read_text(self, path: str, *, check: bool = True) -> str:
        if path in self.files:
            return self.files[path]
        if check:
            raise TransportError(f"no such file: {path}")
        return ""

    def write_text(self, path, content, *, backup=False, mode=""):
        if backup and path in self.files:
            self.files[path + ".bak"] = self.files[path]
        self.files[path] = content

    def exists(self, path: str) -> bool:
        return path in self.files

    def rsync(self, src, dst, *, delete=False, excludes=()):
        self.calls.append(f"rsync {src} {dst}")
        return 0

    def tunnel(self, remote_port: int):
        import contextlib

        @contextlib.contextmanager
        def cm():
            yield remote_port
        return cm()

    def interactive(self, remote_argv, *, tty=True):
        self.calls.append("interactive: " + " ".join(remote_argv))
        return 0

    # -------- assertions
    def calls_matching(self, needle: str) -> list[str]:
        return [c for c in self.calls if needle in c]

    def order_of(self, *needles: str) -> list[int]:
        """First index in self.calls containing each needle (-1 = never)."""
        out = []
        for n in needles:
            idx = next((i for i, c in enumerate(self.calls) if n in c), -1)
            out.append(idx)
        return out


@pytest.fixture
def fake_t() -> FakeTransport:
    return FakeTransport()


class FakeClock:
    """Drop-in for the `time` module attribute inside mcctl modules."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t + 1.75e9

    def sleep(self, secs: float) -> None:
        self.t += max(0.0, secs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
