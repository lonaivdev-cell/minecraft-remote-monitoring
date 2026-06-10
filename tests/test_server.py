"""ServerControl: status assembly, start preflight, graceful-stop escalation."""

from __future__ import annotations

import pytest

from mcctl import state
from mcctl.config import Config
from mcctl.server import ServerControl, ServerError
from mcctl.transport import RunResult

PROBE_OUT_UP = """\
pid=4242
etimes=3661
tmux=1
pane_dead=0
port=1
log_age=4
mem=24000000000 12000000000 11000000000
load=1.20 0.90 0.70
disk_free=42000000000
last_backup=world-world-20260610-043000.tar.zst
last_backup_ts=1750000000
"""

PROBE_OUT_DOWN = """\
pid=
tmux=0
port=0
mem=24000000000 4000000000 19000000000
load=0.10 0.10 0.05
disk_free=42000000000
"""


@pytest.fixture
def cfg() -> Config:
    c = Config()
    c.server.stop_countdown = []  # unit tests skip the countdown
    c.server.stop_timeout = 10
    return c


def _probe_matcher(s: str) -> bool:
    return 'echo "pid=$pid"' in s


def _pid_matcher(s: str) -> bool:
    return "pgrep java" in s and "readlink" in s and 'echo "pid=' not in s


def test_status_up(cfg, fake_t):
    fake_t.expect(_probe_matcher, out=PROBE_OUT_UP)
    fake_t.files[f"{cfg.server.server_dir}/server.properties"] = "enable-rcon=false\n"
    ctl = ServerControl(cfg, fake_t)
    st = ctl.status(full=False)
    assert st.running and st.pid == 4242
    assert st.uptime_s == 3661
    assert st.tmux and not st.pane_dead and st.port_open
    assert st.host_mem_total == 24000000000
    assert st.load == (1.20, 0.90, 0.70)
    assert st.last_backup == "world-world-20260610-043000.tar.zst"


def test_status_down(cfg, fake_t):
    fake_t.expect(_probe_matcher, out=PROBE_OUT_DOWN)
    st = ServerControl(cfg, fake_t).status(full=False)
    assert not st.running and st.pid is None and not st.tmux


def test_status_json_shape(cfg, fake_t):
    fake_t.expect(_probe_matcher, out=PROBE_OUT_UP)
    d = ServerControl(cfg, fake_t).status(full=False).to_dict()
    assert d["running"] is True
    assert d["load"] == [1.20, 0.90, 0.70]
    import json
    json.dumps(d)  # must be JSON-serializable


def test_start_refuses_when_running(cfg, fake_t):
    fake_t.expect(_pid_matcher, out="4242\n")
    with pytest.raises(ServerError, match="already running"):
        ServerControl(cfg, fake_t).start()


def test_start_requires_eula(cfg, fake_t):
    fake_t.expect(_pid_matcher, out="")
    fake_t.expect("grep -qs '^eula=true'", rc=1)
    with pytest.raises(ServerError, match="eula"):
        ServerControl(cfg, fake_t).start()


def test_start_happy_path_sets_intent(cfg, fake_t, clock, monkeypatch):
    monkeypatch.setattr("mcctl.server.time", clock)
    monkeypatch.setattr("mcctl.console.time", clock)
    log_path = f"{cfg.server.server_dir}/{cfg.server.log_file}"
    fake_t.files[log_path] = ""
    fake_t.expect(_pid_matcher, out="")
    fake_t.expect("grep -qs '^eula=true'", rc=0)

    launched = []

    def on_new_session(s):
        if "tmux new-session" in s:
            launched.append(s)
            fake_t.files[log_path] = "[12:00] Done (7.5s)! For help, type help\n"
            return True
        return False
    fake_t.expect(on_new_session, rc=0)

    ctl = ServerControl(cfg, fake_t, sleeper=clock.sleep)
    ctl.start(wait=True)
    assert launched and "remain-on-exit" in "\n".join(fake_t.calls)
    assert state.load()["desired"] == "up"


def test_start_detects_dead_pane(cfg, fake_t, clock, monkeypatch):
    monkeypatch.setattr("mcctl.server.time", clock)
    monkeypatch.setattr("mcctl.console.time", clock)
    fake_t.files[f"{cfg.server.server_dir}/{cfg.server.log_file}"] = ""
    fake_t.expect(_pid_matcher, out="")
    fake_t.expect("grep -qs '^eula=true'", rc=0)
    fake_t.expect(_probe_matcher, out="pid=\ntmux=1\npane_dead=1\nport=0\n")
    fake_t.expect("capture-pane", out="java.lang.RuntimeException: boom\n")
    with pytest.raises(ServerError, match="died during startup"):
        ServerControl(cfg, fake_t, sleeper=clock.sleep).start(wait=True)


def test_stop_graceful(cfg, fake_t, clock, monkeypatch):
    monkeypatch.setattr("mcctl.server.time", clock)
    monkeypatch.setattr("mcctl.console.time", clock)
    fake_t.files[f"{cfg.server.server_dir}/server.properties"] = "enable-rcon=false\n"
    fake_t.files[f"{cfg.server.server_dir}/{cfg.server.log_file}"] = "[12:00] boot\n"
    fake_t.expect(_pid_matcher, out="4242\n")
    fake_t.expect("kill -0 4242",
                  RunResult(0, "alive\n", ""), RunResult(0, "alive\n", ""), RunResult(0, "gone\n", ""))
    ctl = ServerControl(cfg, fake_t, sleeper=clock.sleep)
    ctl.stop()
    assert state.load()["desired"] == "down"
    i_save, i_stop, i_reap = fake_t.order_of("save-all flush", "-l stop", "tmux kill-session")
    assert -1 not in (i_save, i_stop, i_reap)
    assert i_save < i_stop < i_reap
    assert not fake_t.calls_matching("kill -TERM")


def test_stop_escalates_to_term_then_kill(cfg, fake_t, clock, monkeypatch):
    monkeypatch.setattr("mcctl.server.time", clock)
    monkeypatch.setattr("mcctl.console.time", clock)
    fake_t.files[f"{cfg.server.server_dir}/server.properties"] = "enable-rcon=false\n"
    fake_t.files[f"{cfg.server.server_dir}/{cfg.server.log_file}"] = "x\n"
    fake_t.expect(_pid_matcher, out="4242\n")
    fake_t.expect("kill -0 4242", RunResult(0, "alive\n", ""))  # never dies
    ctl = ServerControl(cfg, fake_t, sleeper=clock.sleep)
    with pytest.raises(ServerError, match="survived SIGKILL"):
        ctl.stop()
    i_term, i_kill = fake_t.order_of("kill -TERM 4242", "kill -KILL 4242")
    assert -1 not in (i_term, i_kill)
    assert i_term < i_kill


def test_stop_when_already_down(cfg, fake_t):
    fake_t.expect(_pid_matcher, out="")
    ServerControl(cfg, fake_t).stop()  # no exception, desired flips
    assert state.load()["desired"] == "down"
