from pathlib import Path

import pytest

from mcctl.config import Config, ConfigError, write_template


def test_defaults_are_valid():
    cfg = Config()
    cfg.validate()
    assert cfg.server.host == "144.33.19.123"
    assert cfg.server.start_command == "bash start.sh"
    assert cfg.backup.compression == "zstd"
    assert cfg.watchdog.max_restarts == 3


def test_template_roundtrip(tmp_path: Path):
    p = write_template(tmp_path / "c.toml")
    cfg = Config.load(p)
    # the template must agree with the dataclass defaults — keep them in sync
    assert cfg.to_dict() == Config().to_dict()


def test_template_refuses_overwrite(tmp_path: Path):
    p = write_template(tmp_path / "c.toml")
    with pytest.raises(ConfigError, match="exists"):
        write_template(p)
    write_template(p, force=True, host="10.0.0.5")
    assert Config.load(p).server.host == "10.0.0.5"


def test_missing_file_uses_defaults(tmp_path: Path):
    cfg = Config.load(tmp_path / "nope.toml")
    assert cfg.server.tmux_session == "minecraft"


def test_unknown_keys_ignored(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text("[server]\nhost = 'h'\nbogus_key = 1\n[weird]\nx = 2\n")
    cfg = Config.load(p)
    assert cfg.server.host == "h"


@pytest.mark.parametrize("snippet,msg", [
    ("[server]\nhost = ''", "host"),
    ("[server]\ntransport = 'telnet'", "transport"),
    ("[server]\nserver_dir = 'relative/path'", "absolute"),
    ("[backup]\ncompression = 'xz'", "compression"),
    ("[backup]\nkeep_recent = 0", "keep_recent"),
    ("[watchdog]\ninterval = 1", "interval"),
])
def test_validation_errors(tmp_path: Path, snippet: str, msg: str):
    p = tmp_path / "c.toml"
    p.write_text(snippet + "\n")
    with pytest.raises(ConfigError, match=msg):
        Config.load(p)
