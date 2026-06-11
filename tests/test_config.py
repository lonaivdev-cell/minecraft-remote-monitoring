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
    ("[llm]\nprovider = 'openai'", "provider"),
    ("[ui]\ntimezone = 'Mars/Olympus'", "timezone"),
])
def test_validation_errors(tmp_path: Path, snippet: str, msg: str):
    p = tmp_path / "c.toml"
    p.write_text(snippet + "\n")
    with pytest.raises(ConfigError, match=msg):
        Config.load(p)


def test_ollama_provider_config(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text("[llm]\nprovider = 'ollama'\nollama_model = 'mistral'\n")
    cfg = Config.load(p)
    assert cfg.llm.provider == "ollama"
    assert cfg.llm.ollama_model == "mistral"


def test_ui_timezone_defaults():
    cfg = Config()
    assert cfg.ui.timezone == "America/Sao_Paulo"
    assert cfg.ui.server_timezone == "UTC"


def test_ssh_key_field_defaults_and_roundtrips(tmp_path: Path):
    p = write_template(tmp_path / "c.toml")
    cfg = Config.load(p)
    assert cfg.server.ssh_key == ""              # empty by default
    cfg.server.ssh_key = "~/.ssh/carborio"
    cfg.save(p)
    assert Config.load(p).server.ssh_key == "~/.ssh/carborio"


def test_save_writes_every_section(tmp_path: Path):
    """The GUI's save must regenerate ALL sections — a partial writer that
    dropped [llm]/[ui] is exactly the bug that got the first attempt reverted."""
    cfg = Config()
    cfg.llm.provider = "ollama"
    cfg.llm.ollama_model = "mistral"
    cfg.ui.timezone = "Europe/Berlin"
    cfg.server.ssh_options = ["-o", "IdentityFile=~/.ssh/k"]
    cfg.watchdog.ntfy_topic = "secret-topic"
    p = cfg.save(tmp_path / "out.toml")
    text = p.read_text()
    for section in ("[server]", "[backup]", "[watchdog]", "[metrics]", "[llm]", "[ui]"):
        assert section in text
    # full round-trip: load() of what save() wrote must equal the original
    assert Config.load(p).to_dict() == cfg.to_dict()


def test_save_roundtrips_defaults(tmp_path: Path):
    cfg = Config()
    p = cfg.save(tmp_path / "d.toml")
    assert Config.load(p).to_dict() == Config().to_dict()


def test_save_validates_before_writing(tmp_path: Path):
    cfg = Config()
    cfg.server.host = ""           # invalid
    p = tmp_path / "bad.toml"
    with pytest.raises(ConfigError):
        cfg.save(p)
    assert not p.exists()          # nothing half-written
