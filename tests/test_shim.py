"""The deprecated `mcctl` entry point.

The stdout-purity test is the load-bearing one: `mcctl agent` is a JSON-RPC 2.0
NDJSON stream on stdout, and every phone in the field still invokes the agent by
that name (its profile DataStore has the literal string "mcctl agent" saved). A
deprecation line on stdout corrupts the first frame and breaks every install.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest
from conftest import integration_gate

from lulism import shim

XDG_VARS = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")


def _scripts_dir() -> Path | None:
    """The directory holding *this project's* console scripts, or None.

    `Path(sys.executable).parent` is right for a venv and for CI, but not for
    the layouts this project actually ships to — pipx, `pip install --user` and
    a PEP 668 system Python all put scripts in ~/.local/bin, nowhere near the
    interpreter. Guessing is not harmless either: `mcctl` is also the name of an
    unrelated AUR package, so an unverified /usr/bin/mcctl would make these
    tests assert on a foreign program's stdout and exit codes. Probe the
    plausible directories and take the first whose `mcctl` is provably lulism's
    shim (pip writes `from lulism.shim import main` into the script).
    """
    cands = [Path(sys.executable).parent, Path(sysconfig.get_path("scripts"))]
    with contextlib.suppress(KeyError, OSError):
        cands.append(Path(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user"))))
    found = shutil.which("mcctl")
    if found:
        cands.append(Path(found).parent)

    seen: set[Path] = set()
    for d in cands:
        if d in seen:
            continue
        seen.add(d)
        mcctl, lulism_exe = d / "mcctl", d / "lulism"
        if not (mcctl.is_file() and lulism_exe.is_file()):
            continue
        try:
            text = mcctl.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "lulism.shim" in text:
            return d
    return None


SCRIPTS = _scripts_dir()
requires_scripts = integration_gate(
    SCRIPTS is not None,
    "lulism's own `mcctl`/`lulism` console scripts are not installed "
    "(`pip install -e .`)")


def _child_env(tmp_home: Path, *, path: str) -> dict[str, str]:
    """Environment for a real `mcctl` child process.

    `mcctl agent --schema` is not read-only: cli.main() runs migrate_legacy_dirs()
    and setup_logging()->ensure_dirs() before dispatching, so it creates config,
    cache and state trees and opens a rotating log. Handing the child a bare
    {PATH, HOME} would drop the XDG vars the autouse `isolated_xdg` fixture sets
    and let it write into (and migrate!) the developer's real $HOME. Carry the
    isolation over, and point HOME at a scratch dir too, so even the
    Path.home() fallback lands inside tmp_path.
    """
    tmp_home.mkdir(parents=True, exist_ok=True)
    env = {"PATH": path, "HOME": str(tmp_home)}
    env.update({k: os.environ[k] for k in XDG_VARS if k in os.environ})
    return env


@pytest.mark.parametrize("sibling_exists", [True, False])
def test_execs_lulism_with_same_argv(monkeypatch, tmp_path, sibling_exists):
    """Both branches of _target(), with sys.argv[0] pinned.

    The shim resolves `lulism` next to argv[0] before falling back to a PATH
    search, so its result depends on where the running program lives. This test
    used to inherit the real sys.argv[0] and assert the PATH-fallback branch,
    which made it pass under `python -m pytest` (argv[0] is pytest's
    __main__.py, no `lulism` sibling) and fail under the `pytest` console script
    (argv[0] is <venv>/bin/pytest, whose `lulism` sibling exists) — the CI
    invocation. Own argv[0] instead, and pin both branches.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "mcctl").write_text("#!/bin/sh\n", encoding="utf-8")
    if sibling_exists:
        (bindir / "lulism").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(bindir / "mcctl"), "status", "--json"])

    seen = {}

    def fake_execvp(file, args):
        seen["file"] = file
        seen["args"] = list(args)
        raise SystemExit(0)  # stand in for "never returns"

    monkeypatch.setattr(os, "execvp", fake_execvp)
    with pytest.raises(SystemExit):
        shim.main(["status", "--json"])

    # sibling present -> exec the absolute path (systemd user units and
    # non-interactive SSH run with a PATH that excludes ~/.local/bin, where pipx
    # puts both scripts); absent -> the bare name, and let execvp search PATH.
    expected = str((bindir / "lulism").resolve()) if sibling_exists else "lulism"
    assert seen["file"] == expected
    # invariant either way: the child's own prog name stays `lulism`, so its
    # --version/usage strings never mention where the binary was found.
    assert seen["args"] == ["lulism", "status", "--json"]


def test_notice_goes_to_stderr_not_stdout(monkeypatch, capsys):
    monkeypatch.setattr(os, "execvp", lambda f, a: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        shim.main(["status"])
    out = capsys.readouterr()
    assert out.out == ""
    assert "deprecated" in out.err


@requires_scripts
@pytest.mark.integration
def test_mcctl_agent_schema_stdout_is_pure_json(tmp_path):
    """The regression test for 'we broke every phone in the field'."""
    env = _child_env(tmp_path / "home",
                     path=f"{SCRIPTS}{os.pathsep}{os.environ.get('PATH', '')}")
    r = subprocess.run([str(SCRIPTS / "mcctl"), "agent", "--schema"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    schema = json.loads(r.stdout)          # must not raise
    assert schema["protocol"] == 1
    assert "deprecated" in r.stderr


@requires_scripts
@pytest.mark.integration
@pytest.mark.parametrize("args,code", [(["--version"], 0), (["definitely-not-a-command"], 2)])
def test_exit_codes_pass_through(tmp_path, args, code):
    env = _child_env(tmp_path / "home",
                     path=f"{SCRIPTS}{os.pathsep}{os.environ.get('PATH', '')}")
    r = subprocess.run([str(SCRIPTS / "mcctl"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == code


@requires_scripts
@pytest.mark.integration
def test_works_when_scripts_dir_is_not_on_path(tmp_path):
    """The field case: systemd user units and non-interactive SSH both run with
    a minimal PATH that excludes ~/.local/bin, where pipx puts both scripts."""
    minimal = os.pathsep.join(d for d in ("/usr/bin", "/bin")
                              if Path(d).resolve() != SCRIPTS.resolve())
    env = _child_env(tmp_path / "home", path=minimal or os.devnull)
    if shutil.which("lulism", path=env["PATH"]):
        pytest.skip("a `lulism` is reachable on the minimal PATH here, so this "
                    "run cannot prove the sibling lookup is what found it")
    r = subprocess.run([str(SCRIPTS / "mcctl"), "agent", "--schema"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["protocol"] == 1


def test_reports_exec_failure_on_stderr(monkeypatch, capsys):
    def boom(file, args):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(os, "execvp", boom)
    monkeypatch.setattr(shim, "_target", lambda: "lulism")
    assert shim.main(["status"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "cannot exec lulism" in out.err
