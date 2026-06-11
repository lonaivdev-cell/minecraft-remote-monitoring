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
