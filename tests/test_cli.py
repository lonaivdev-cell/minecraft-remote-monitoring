"""CLI wiring: parser integrity, init/doctor/status flows over a FakeTransport."""

from __future__ import annotations

import json
import sys

import pytest

import lulism.cli as cli
from lulism.config import Config

SUBCOMMANDS = [
    "init", "doctor", "status", "start", "stop", "restart", "kill", "console", "cmd",
    "save", "tps", "health", "profile", "purge", "stats", "logs", "backup", "props",
    "jvm", "player", "watchdog", "sync", "rcon", "dash", "watch", "history", "trace", "ai",
]


def test_parser_builds():
    p = cli.build_parser()
    assert p.prog == "lulism"


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_every_subcommand_has_help(sub, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args([sub, "--help"])
    assert exc.value.code == 0
    assert sub in capsys.readouterr().out


def test_no_args_prints_help(capsys):
    assert cli.main([]) == 2
    assert "COMMAND" in capsys.readouterr().out


def test_init_then_load(tmp_path, capsys):
    cfgfile = tmp_path / "c.toml"
    assert cli.main(["init", "--config", str(cfgfile), "--host", "10.1.2.3"]) == 0
    assert Config.load(cfgfile).server.host == "10.1.2.3"
    # second init without --force fails cleanly
    assert cli.main(["init", "--config", str(cfgfile)]) == 1


PROBE_OUT = """\
pid=4242
etimes=120
tmux=1
pane_dead=0
port=1
log_age=2
mem=24000000000 12000000000 11000000000
load=0.50 0.40 0.30
disk_free=42000000000
"""


def _wire_fake(monkeypatch, fake_t):
    monkeypatch.setattr(cli, "make_transport", lambda cfg: fake_t)


def test_status_json(tmp_path, monkeypatch, capsys, fake_t):
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    capsys.readouterr()
    _wire_fake(monkeypatch, fake_t)
    fake_t.expect(lambda s: 'echo "pid=$pid"' in s, out=PROBE_OUT)
    rcode = cli.main(["status", "--json", "--fast", "--config", str(cfgfile)])
    assert rcode == 0
    data = json.loads(capsys.readouterr().out)
    assert data["running"] is True and data["pid"] == 4242
    assert data["port_open"] is True


def test_stats_empty(tmp_path, capsys):
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    capsys.readouterr()
    assert cli.main(["stats", "--config", str(cfgfile)]) == 0
    assert "no samples" in capsys.readouterr().out


def test_history_empty(tmp_path, capsys):
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    capsys.readouterr()
    assert cli.main(["history", "--config", str(cfgfile)]) == 0
    assert "no samples" in capsys.readouterr().out


def test_history_charts_recorded_samples(tmp_path, capsys):
    from lulism import metrics
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    for tps in (20.0, 19.5, 18.0, 12.0):
        metrics.append_sample({"ts": 1, "tps": tps, "running": True})
    capsys.readouterr()
    assert cli.main(["history", "tps", "--config", str(cfgfile)]) == 0
    out = capsys.readouterr().out
    assert "TPS" in out and "last" in out


def test_ai_chat_subcommand_parses():
    args = cli.build_parser().parse_args(["ai", "chat", "why", "is", "tps", "low"])
    assert args.func.__name__ == "cmd_ai" and args.ai_cmd == "chat"
    assert args.question == ["why", "is", "tps", "low"]


def test_watchdog_arm_disarm(tmp_path, capsys):
    from lulism import state
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    assert cli.main(["watchdog", "arm", "--config", str(cfgfile)]) == 0
    assert state.load()["armed"] is True
    assert cli.main(["watchdog", "disarm", "--config", str(cfgfile)]) == 0
    assert state.load()["armed"] is False


def test_props_set_validates(tmp_path, monkeypatch, capsys, fake_t):
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    _wire_fake(monkeypatch, fake_t)
    cfg = Config.load(cfgfile)
    props_path = f"{cfg.server.server_dir}/server.properties"
    fake_t.files[props_path] = "view-distance=10\n"
    fake_t.expect(lambda s: "pgrep java" in s, out="")
    assert cli.main(["props", "set", "view-distance", "12", "--config", str(cfgfile)]) == 0
    assert "view-distance=12" in fake_t.files[props_path]
    # out-of-range rejected, file untouched
    assert cli.main(["props", "set", "view-distance", "99", "--config", str(cfgfile)]) == 1
    assert "view-distance=12" in fake_t.files[props_path]


def test_jvm_heap_rewrites(tmp_path, monkeypatch, capsys, fake_t):
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    _wire_fake(monkeypatch, fake_t)
    cfg = Config.load(cfgfile)
    vpath = f"{cfg.server.server_dir}/variables.txt"
    fake_t.files[vpath] = 'JAVA_ARGS="-Xms12G -Xmx12G -XX:+UseG1GC"\n'
    fake_t.expect("free -b", out="24000000000\n")
    assert cli.main(["jvm", "heap", "14G", "--config", str(cfgfile)]) == 0
    assert "-Xmx14G" in fake_t.files[vpath] and "-XX:+UseG1GC" in fake_t.files[vpath]


def test_backup_list_empty(tmp_path, monkeypatch, capsys, fake_t):
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    capsys.readouterr()
    _wire_fake(monkeypatch, fake_t)
    assert cli.main(["backup", "list", "--config", str(cfgfile)]) == 0
    assert "no backups yet" in capsys.readouterr().out


def test_transport_error_exit_code(tmp_path, monkeypatch, capsys, fake_t):
    from lulism.transport import TransportError
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    _wire_fake(monkeypatch, fake_t)
    fake_t.expect(lambda s: True, TransportError("host unreachable"))
    assert cli.main(["save", "--config", str(cfgfile)]) == 3


def _pin_argv0(monkeypatch, tmp_path, name="lulism"):
    """_install_units() renders ExecStart from sys.argv[0] (cli.py:650), so the
    bytes it writes depend on how the test runner was invoked: `python -m pytest`
    gives .../pytest/__main__.py while the `pytest` console script gives
    <venv>/bin/pytest. render_units() currently clamps both to /usr/bin/lulism,
    so the tests agree today — but only by accident, and the pipx rewrite branch
    they nominally cover is never reached. Own argv[0] and return the exe the
    unit files must name."""
    bindir = tmp_path / "opt" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    exe = bindir / name
    monkeypatch.setattr(sys, "argv", [str(exe), "watchdog", "install"])
    # what render_units() must resolve `exe` to: an absolute path named lulism
    # is honoured verbatim; the mcctl shim is swapped for its lulism sibling.
    return str(bindir / "lulism")


def test_watchdog_install_writes_units_to_xdg(isolated_xdg, tmp_path, capsys, monkeypatch):
    """_install_units() calls util.migrate_units(), whose default `run` shells
    out to the real `systemctl --user` (isolated_xdg only overrides XDG_*, not
    DBUS_SESSION_BUS_ADDRESS). On a box that actually has old mcctl-* units
    enabled, letting that reach the live bus from a test run would stop and
    disable the real watchdog -- so inject a FakeSystemctl, the same way
    test_units_migration.py does, and prove every call landed on the fake."""
    from lulism import util
    from lulism.cli import _install_units

    class FakeSystemctl:
        def __init__(self):
            self.calls: list[list[str]] = []

        def __call__(self, cmd: list[str]) -> int:
            self.calls.append(list(cmd))
            return 0

    exe = _pin_argv0(monkeypatch, tmp_path)
    fake = FakeSystemctl()
    real_migrate_units = util.migrate_units
    monkeypatch.setattr(util, "migrate_units",
                        lambda run=None, **kw: real_migrate_units(fake, **kw))
    # The reload moved out of migrate_units() and into _install_units(); record
    # what was on disk at the moment it fired.
    reloaded_with: list[list[str]] = []
    monkeypatch.setattr(util, "systemd_daemon_reload",
                        lambda run=None: reloaded_with.append(
                            sorted(p.name for p in util.user_unit_dir().iterdir())))

    assert _install_units() == 0

    # migrate_units() really ran its full stop/disable sequence for all 7 legacy
    # units, but every systemctl invocation landed on the fake -- never on
    # subprocess, never on the real user bus.
    assert len(fake.calls) == 7 + 7  # stop*7, disable*7; the reload is the caller's now
    assert all(c[:2] == ["systemctl", "--user"] for c in fake.calls)
    touched = {c[-1] for c in fake.calls if c[-1].endswith((".service", ".timer"))}
    assert touched == set(util.legacy_unit_names())

    written = sorted(p.name for p in util.user_unit_dir().iterdir())
    assert "lulism-watchdog.service" in written and len(written) == 7
    # the ExecStart is fully determined by the argv[0] pinned above — this is the
    # pipx layout (~/.local/bin/lulism), where a bare or /usr/bin ExecStart would
    # fail 203/EXEC because systemd's own PATH excludes it.
    text = (util.user_unit_dir() / "lulism-backup.service").read_text()
    assert f"ExecStart={exe} backup create --notify" in text
    assert "ExecStart=/usr/bin/lulism " not in text

    # Reloading before the new files land leaves systemd unaware of them until
    # the operator runs a reload of their own.
    assert reloaded_with == [written], reloaded_with


@pytest.mark.parametrize("argv0,expected", [
    # reached through the deprecated shim: never hardcode a binary that is
    # removed at 3.0.0 into a long-lived unit — use its `lulism` sibling
    ("mcctl", "sibling"),
    # anything that is not an absolute `lulism`/`mcctl` path (a test runner, a
    # tox shim) falls back rather than being written into a unit file
    ("pytest", "/usr/bin/lulism"),
])
def test_watchdog_install_execstart_never_depends_on_how_it_was_invoked(
        isolated_xdg, tmp_path, capsys, monkeypatch, argv0, expected):
    from lulism import util
    from lulism.cli import _install_units

    monkeypatch.setattr(util, "migrate_units", lambda run=None, **kw: [])
    monkeypatch.setattr(util, "systemd_daemon_reload", lambda run=None: None)
    sibling = _pin_argv0(monkeypatch, tmp_path, name=argv0)
    exe = sibling if expected == "sibling" else expected

    assert _install_units() == 0
    text = (util.user_unit_dir() / "lulism-watchdog.service").read_text()
    assert f"ExecStart={exe} watchdog run" in text


def test_watchdog_install_reports_only_units_that_really_existed(
        isolated_xdg, tmp_path, capsys, monkeypatch):
    """migrate_units() used to return legacy_unit_names() unconditionally, so a
    fresh install announced removing seven units that had never existed."""
    from lulism import util
    from lulism.cli import _install_units

    _pin_argv0(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    real_migrate_units = util.migrate_units
    monkeypatch.setattr(util, "migrate_units",
                        lambda run=None, **kw: real_migrate_units(calls.append, **kw))
    monkeypatch.setattr(util, "systemd_daemon_reload", lambda run=None: None)

    # fresh box: nothing was ever installed under the old names
    assert _install_units() == 0
    assert "removed legacy unit" not in capsys.readouterr().out

    # a real 1.1.2 box: exactly the two units it actually had
    unit_dir = util.user_unit_dir()
    for name in ("mcctl-watchdog.service", "mcctl-backup.timer"):
        (unit_dir / name).write_text("[Unit]\n", encoding="utf-8")
    assert _install_units() == 0
    out = capsys.readouterr().out
    assert out.count("removed legacy unit") == 2
    assert "mcctl-watchdog.service" in out and "mcctl-backup.timer" in out
