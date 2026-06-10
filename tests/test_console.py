"""Console facade: rcon settings parsing, tmux command shape, player list parsing."""

from __future__ import annotations

from mcctl.config import Config
from mcctl.console import Console, PlayerList


def _cfg() -> Config:
    return Config()


def test_rcon_settings_parsed(fake_t):
    cfg = _cfg()
    fake_t.files[f"{cfg.server.server_dir}/server.properties"] = (
        "enable-rcon=true\nrcon.port=25599\nrcon.password=s3cret\n"
    )
    c = Console(cfg, fake_t)
    assert c.rcon_settings() == (True, 25599, "s3cret")


def test_rcon_disabled_without_password(fake_t):
    cfg = _cfg()
    fake_t.files[f"{cfg.server.server_dir}/server.properties"] = (
        "enable-rcon=true\nrcon.password=\n"
    )
    enabled, _, _ = Console(cfg, fake_t).rcon_settings()
    assert enabled is False


def test_rcon_settings_unreachable(fake_t):
    cfg = _cfg()
    enabled, port, pw = Console(cfg, fake_t).rcon_settings()
    assert enabled is False and port == cfg.server.rcon_port


def test_tmux_send_uses_literal_keys(fake_t):
    cfg = _cfg()
    c = Console(cfg, fake_t)
    c.tmux_send("say hello; rm -rf /")  # hostile-looking console text stays literal
    sends = fake_t.calls_matching("send-keys")
    assert len(sends) == 1
    assert "-l 'say hello; rm -rf /'" in sends[0]
    assert sends[0].count("send-keys") == 2  # text, then Enter separately


def test_players_parsing(fake_t, monkeypatch):
    cfg = _cfg()
    c = Console(cfg, fake_t)
    monkeypatch.setattr(
        c, "send",
        lambda cmd, timeout=10: "There are 2 of a max of 20 players online: Carborio, Wife",
    )
    pl = c.players()
    assert pl == PlayerList(2, 20, ["Carborio", "Wife"])


def test_players_empty(fake_t, monkeypatch):
    c = Console(_cfg(), fake_t)
    monkeypatch.setattr(c, "send",
                        lambda cmd, timeout=10: "There are 0 of a max of 20 players online:")
    pl = c.players()
    assert pl.count == 0 and pl.names == []


def test_players_unparseable(fake_t, monkeypatch):
    c = Console(_cfg(), fake_t)
    monkeypatch.setattr(c, "send", lambda cmd, timeout=10: "cmd: list")
    assert c.players() is None
