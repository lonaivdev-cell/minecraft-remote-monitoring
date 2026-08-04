"""Migration of ~/.config/mcctl, ~/.local/state/mcctl and ~/.cache/mcctl.

Copies, never moves: the originals stay put as the 2.0.0 rollback path.
"""

from __future__ import annotations

from pathlib import Path

from lulism import cli, util
from lulism.config import Config

V112_CONFIG = """\
[server]
host = "carborio"
user = "ubuntu"
transport = "ssh"

[backup]
remote_dir = "/opt/backups"

[watchdog]
poll_s = 30

[metrics]
prom_path = ""

[llm]
provider = "ollama"

[ui]
tz = "local"

[crafting]
enabled = true
"""


def _legacy(root: Path, kind: str) -> Path:
    d = {"cfg": root / "cfg" / "mcctl", "state": root / "state" / "mcctl",
         "cache": root / "cache" / "mcctl"}[kind]
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_migrates_config_state_and_cache(isolated_xdg):
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    (_legacy(isolated_xdg, "state") / "metrics.jsonl").write_text('{"tps":19.9}\n', encoding="utf-8")
    (_legacy(isolated_xdg, "cache") / "icon.png").write_bytes(b"\x89PNG")

    moved = util.migrate_legacy_dirs()

    assert len(moved) == 3
    assert (util.config_dir() / "config.toml").read_text(encoding="utf-8") == V112_CONFIG
    assert (util.state_dir() / "metrics.jsonl").exists()
    assert (util.cache_dir() / "icon.png").read_bytes() == b"\x89PNG"


def test_migrated_v112_config_loads_clean(isolated_xdg):
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    util.migrate_legacy_dirs()

    cfg = Config.load()

    assert cfg.server.host == "carborio"
    assert cfg.server.user == "ubuntu"
    assert cfg.llm.provider == "ollama"


def test_originals_survive(isolated_xdg):
    legacy = _legacy(isolated_xdg, "cfg")
    (legacy / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    util.migrate_legacy_dirs()
    assert (legacy / "config.toml").exists(), "migration must copy, not move"


def test_is_idempotent_and_never_clobbers(isolated_xdg):
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    util.migrate_legacy_dirs()

    util.config_dir().mkdir(parents=True, exist_ok=True)
    (util.config_dir() / "config.toml").write_text("# hand-edited\n", encoding="utf-8")

    assert util.migrate_legacy_dirs() == []
    assert (util.config_dir() / "config.toml").read_text(encoding="utf-8") == "# hand-edited\n"


def test_no_legacy_dir_is_a_noop(isolated_xdg):
    assert util.migrate_legacy_dirs() == []
    assert not util.config_dir().exists()


def test_migration_actually_runs_through_cli_main(isolated_xdg):
    """Regression: cli.main() calls util.setup_logging(), which calls
    util.ensure_dirs(), which pre-creates config_dir()/state_dir()/cache_dir()
    as empty directories. migrate_legacy_dirs() skips any destination that
    already exists, so if it ran *after* setup_logging() it would find those
    dirs already present and silently never migrate anything for a real CLI
    invocation. This exercises the real entry point end-to-end, not just the
    util function in isolation, to guard against that ordering regression.
    """
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")

    assert cli.main([]) == 2  # no subcommand: prints help, exits 2

    assert (util.config_dir() / "config.toml").read_text(encoding="utf-8") == V112_CONFIG
