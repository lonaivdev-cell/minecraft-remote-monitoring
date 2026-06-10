"""End-to-end over LocalTransport + a real tmux session.

A fake "java" server (a renamed bash, so pgrep/cwd detection genuinely works)
boots via start.sh exactly like ServerStarterJar would, writes a Done line,
answers console commands from stdin, and exits on `stop`. This exercises:
tmux session creation, readiness detection, PID discovery, console round-trips
via send-keys + log offsets, consistent backups with save-off/on, rotation,
verification, and graceful stop.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest

from mcctl.backup import BackupManager
from mcctl.config import Config
from mcctl.console import Console
from mcctl.server import ServerControl
from mcctl.transport import LocalTransport

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed"),
]

FAKESERVER = r"""#!/usr/bin/env bash
mkdir -p logs world
log() { printf '[%s] [Server thread/INFO]: %s\n' "$(date +%H:%M:%S)" "$1" >> logs/latest.log; }
log "Starting fake minecraft server"
echo "world data $(date +%s)" > world/level.dat
log 'Done (2.718s)! For help, type "help"'
while IFS= read -r line; do
  case "$line" in
    stop) log "Stopping the server"; exit 0 ;;
    save-all*) log "Saved the game" ;;
    save-off) log "Automatic saving is now disabled" ;;
    save-on) log "Automatic saving is now enabled" ;;
    list) log "There are 0 of a max of 20 players online:" ;;
    *) log "cmd: $line" ;;
  esac
done
"""

START_SH = """#!/usr/bin/env bash
exec ./java ./fakeserver.sh
"""


@pytest.fixture
def sandbox(tmp_path):
    srv = tmp_path / "srv"
    srv.mkdir()
    (srv / "fakeserver.sh").write_text(FAKESERVER)
    (srv / "start.sh").write_text(START_SH)
    (srv / "eula.txt").write_text("eula=true\n")
    (srv / "server.properties").write_text("enable-rcon=false\n")
    # a bash that *is* named java: pgrep + /proc cwd detection work for real
    shutil.copy2("/bin/bash", srv / "java")
    os.chmod(srv / "fakeserver.sh", 0o755)
    os.chmod(srv / "start.sh", 0o755)

    cfg = Config()
    cfg.server.transport = "local"
    cfg.server.server_dir = str(srv)
    cfg.server.tmux_session = f"mcctl-test-{uuid.uuid4().hex[:8]}"
    cfg.server.start_timeout = 30
    cfg.server.stop_timeout = 20
    cfg.server.stop_countdown = []
    cfg.backup.remote_dir = str(tmp_path / "backups")
    cfg.backup.min_free_gb = 0.001
    yield cfg
    subprocess.run(["tmux", "kill-session", "-t", cfg.server.tmux_session],
                   capture_output=True, check=False)


def test_full_lifecycle(sandbox):
    cfg = sandbox
    t = LocalTransport(cfg)
    console = Console(cfg, t)
    ctl = ServerControl(cfg, t, console)

    # ---- start: tmux session + readiness via log
    ctl.start(wait=True)
    pid = ctl.find_pid()
    assert pid is not None, "pgrep java + /proc cwd detection must find the fake server"

    st = ctl.status(full=False)
    assert st.running and st.tmux and not st.pane_dead

    # ---- console round-trip over tmux send-keys + log offset
    offset = console.log_size()
    console.tmux_send("say hello world")
    hit = console.wait_in_log(r"cmd: say hello world", offset, timeout=10, poll=0.5)
    assert hit, "console command must land in the server log"

    # ---- consistent backup while running (save-off -> tar -> save-on)
    mgr = BackupManager(cfg, t, console)
    entry = mgr.create()
    assert entry is not None and entry.size > 0
    assert mgr.verify(entry.name)
    assert len(mgr.list()) == 1
    log_text = t.read_text(f"{cfg.server.server_dir}/logs/latest.log")
    assert "Automatic saving is now disabled" in log_text
    assert "Automatic saving is now enabled" in log_text

    # ---- restore refuses while up
    from mcctl.backup import BackupError
    with pytest.raises(BackupError, match="running"):
        mgr.restore(entry.name)

    # ---- graceful stop: console `stop`, pid reaped, session gone
    ctl.stop()
    assert ctl.find_pid() is None
    r = subprocess.run(["tmux", "has-session", "-t", cfg.server.tmux_session],
                       capture_output=True, check=False)
    assert r.returncode != 0, "tmux session must be reaped after stop"

    # ---- offline restore round-trip
    aside = mgr.restore(entry.name)
    assert t.exists(f"{cfg.server.server_dir}/world/level.dat")
    assert t.exists(f"{cfg.server.server_dir}/{aside}/level.dat")


def test_start_twice_refused(sandbox):
    cfg = sandbox
    t = LocalTransport(cfg)
    ctl = ServerControl(cfg, t)
    ctl.start(wait=True)
    try:
        from mcctl.server import ServerError
        with pytest.raises(ServerError, match="already running"):
            ctl.start(wait=True)
    finally:
        ctl.stop()


def test_crash_leaves_inspectable_corpse(sandbox):
    """remain-on-exit keeps the dead pane; probe reports pane_dead=1."""
    cfg = sandbox
    t = LocalTransport(cfg)
    ctl = ServerControl(cfg, t)
    ctl.start(wait=True)
    pid = ctl.find_pid()
    os.kill(pid, 9)  # simulate a hard JVM crash
    deadline = time.monotonic() + 10
    kv = {}
    while time.monotonic() < deadline:
        kv = ctl.probe()
        if kv.get("pid", "") == "" and kv.get("tmux") == "1":
            break
        time.sleep(0.5)
    assert kv.get("tmux") == "1", "session must survive the crash (remain-on-exit)"
    assert kv.get("pane_dead") == "1", "pane must be flagged dead for the watchdog"
    ctl.stop()  # reaps the corpse
