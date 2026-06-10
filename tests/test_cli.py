"""CLI wiring: parser integrity, init/doctor/status flows over a FakeTransport."""

from __future__ import annotations

import json

import pytest

import mcctl.cli as cli
from mcctl.config import Config

SUBCOMMANDS = [
    "init", "doctor", "status", "start", "stop", "restart", "kill", "console", "cmd",
    "save", "tps", "health", "profile", "purge", "stats", "logs", "backup", "props",
    "jvm", "player", "watchdog", "sync", "rcon", "dash",
]


def test_parser_builds():
    p = cli.build_parser()
    assert p.prog == "mcctl"


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


def test_watchdog_arm_disarm(tmp_path, capsys):
    from mcctl import state
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
    from mcctl.transport import TransportError
    cfgfile = tmp_path / "c.toml"
    cli.main(["init", "--config", str(cfgfile)])
    _wire_fake(monkeypatch, fake_t)
    fake_t.expect(lambda s: True, TransportError("host unreachable"))
    assert cli.main(["save", "--config", str(cfgfile)]) == 3
