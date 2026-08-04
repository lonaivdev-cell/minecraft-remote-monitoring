"""The deprecated `mcctl` entry point.

The stdout-purity test is the load-bearing one: `mcctl agent` is a JSON-RPC 2.0
NDJSON stream on stdout, and every phone in the field still invokes the agent by
that name (its profile DataStore has the literal string "mcctl agent" saved). A
deprecation line on stdout corrupts the first frame and breaks every install.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lulism import shim

SCRIPTS = Path(sys.executable).parent


def test_execs_lulism_with_same_argv(monkeypatch, capsys):
    seen = {}

    def fake_execvp(file, args):
        seen["file"] = file
        seen["args"] = list(args)
        raise SystemExit(0)  # stand in for "never returns"

    monkeypatch.setattr(os, "execvp", fake_execvp)
    with pytest.raises(SystemExit):
        shim.main(["status", "--json"])

    assert seen["file"] == "lulism"
    assert seen["args"] == ["lulism", "status", "--json"]


def test_notice_goes_to_stderr_not_stdout(monkeypatch, capsys):
    monkeypatch.setattr(os, "execvp", lambda f, a: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        shim.main(["status"])
    out = capsys.readouterr()
    assert out.out == ""
    assert "deprecated" in out.err


@pytest.mark.integration
def test_mcctl_agent_schema_stdout_is_pure_json():
    """The regression test for 'we broke every phone in the field'."""
    env = {**os.environ, "PATH": f"{SCRIPTS}{os.pathsep}{os.environ.get('PATH', '')}"}
    r = subprocess.run([str(SCRIPTS / "mcctl"), "agent", "--schema"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    schema = json.loads(r.stdout)          # must not raise
    assert schema["protocol"] == 1
    assert "deprecated" in r.stderr


@pytest.mark.integration
@pytest.mark.parametrize("args,code", [(["--version"], 0), (["definitely-not-a-command"], 2)])
def test_exit_codes_pass_through(args, code):
    env = {**os.environ, "PATH": f"{SCRIPTS}{os.pathsep}{os.environ.get('PATH', '')}"}
    r = subprocess.run([str(SCRIPTS / "mcctl"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == code


@pytest.mark.integration
def test_works_when_scripts_dir_is_not_on_path():
    """The field case: systemd user units and non-interactive SSH both run with
    a minimal PATH that excludes ~/.local/bin, where pipx puts both scripts."""
    r = subprocess.run([str(SCRIPTS / "mcctl"), "agent", "--schema"],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")})
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
