"""SshTransport argv building — the -i SSH key flag and option ordering.

No network: we only inspect the argv the transport would hand to ssh."""

from __future__ import annotations

from pathlib import Path

from mcctl.config import Config
from mcctl.transport import SshTransport, make_transport


def _opts(cfg: Config) -> list[str]:
    return SshTransport(cfg)._opts()


def test_no_key_means_no_identity_flag():
    cfg = Config()
    opts = _opts(cfg)
    assert "-i" not in opts
    assert "IdentitiesOnly=yes" not in opts


def test_ssh_key_adds_identity_flag():
    cfg = Config()
    cfg.server.ssh_key = "/home/dickbutt/.ssh/carborio"
    opts = _opts(cfg)
    assert "-i" in opts
    i = opts.index("-i")
    assert opts[i + 1] == "/home/dickbutt/.ssh/carborio"
    # IdentitiesOnly so the explicit key wins over whatever the agent offers
    assert "IdentitiesOnly=yes" in opts


def test_ssh_key_tilde_is_expanded():
    cfg = Config()
    cfg.server.ssh_key = "~/.ssh/carborio"
    opts = _opts(cfg)
    i = opts.index("-i")
    assert opts[i + 1] == str(Path.home() / ".ssh" / "carborio")
    assert "~" not in opts[i + 1]


def test_blank_key_is_ignored():
    cfg = Config()
    cfg.server.ssh_key = "   "
    assert "-i" not in _opts(cfg)


def test_extra_options_still_appended_after_key():
    cfg = Config()
    cfg.server.ssh_key = "/k"
    cfg.server.ssh_options = ["-o", "ProxyJump=bastion"]
    opts = _opts(cfg)
    assert opts[-2:] == ["-o", "ProxyJump=bastion"]
    assert "-i" in opts


def test_make_transport_local_vs_ssh():
    cfg = Config()
    cfg.server.transport = "local"
    from mcctl.transport import LocalTransport
    assert isinstance(make_transport(cfg), LocalTransport)
    cfg.server.transport = "ssh"
    assert isinstance(make_transport(cfg), SshTransport)


# ---------------------------------------------------------------- cancellable stream
# The live log view follows `tail -F` on a background thread; it must be able to
# stop a never-ending stream cleanly (in-band, and while blocked on a read).

def test_stream_stops_in_band_when_event_set():
    import threading

    from mcctl.transport import LocalTransport
    stop = threading.Event()
    seen: list[str] = []

    def consume():
        for line in LocalTransport().stream(
                "i=0; while true; do echo line$i; i=$((i+1)); sleep 0.05; done", stop=stop):
            seen.append(line)
            if len(seen) >= 3:
                stop.set()

    th = threading.Thread(target=consume)
    th.start()
    th.join(timeout=10)
    assert not th.is_alive(), "stream() did not end after stop was set"
    assert seen[:3] == ["line0", "line1", "line2"]


def test_stream_watcher_terminates_blocked_read():
    import threading
    import time

    from mcctl.transport import LocalTransport
    stop = threading.Event()
    out: list[str] = []

    def consume():
        for line in LocalTransport().stream("echo hello; sleep 100", stop=stop):
            out.append(line)

    th = threading.Thread(target=consume)
    started = time.monotonic()
    th.start()
    time.sleep(0.3)          # let it print, then block deep inside `sleep 100`
    stop.set()               # the watcher must kill the child to unblock the read
    th.join(timeout=10)
    assert not th.is_alive()
    assert out == ["hello"]
    assert time.monotonic() - started < 5     # didn't wait out the 100s sleep
