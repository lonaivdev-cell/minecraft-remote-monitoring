"""Migration of ~/.config/mcctl, ~/.local/state/mcctl and ~/.cache/mcctl.

Copies, never moves: the originals stay put as the 2.0.0 rollback path.
"""

from __future__ import annotations

import os
import socket
import stat
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


# ---------------------------------------------------------------- best effort

def _plant_socket(directory: Path, name: str = "ssh-carborio") -> Path:
    """A real, bound unix socket — exactly what runtime_dir() is full of."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    assert stat.S_ISSOCK(path.lstat().st_mode)
    return path


def test_live_ssh_sockets_do_not_break_the_cache_migration(isolated_xdg, monkeypatch):
    """runtime_dir() falls back to cache_dir()/"run" whenever XDG_RUNTIME_DIR is
    unset — the normal case on a headless box — which puts live SSH
    ControlMaster sockets *inside* the tree being migrated. shutil.copytree
    raises `OSError: [Errno 6] No such device or address` on a unix socket and
    leaves a partial destination behind, and migrate_legacy_dirs() is called
    from cli.main() before the try/except that maps exceptions to exit codes:
    the result was a raw traceback on every single command, including the
    phone's `mcctl agent`.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    legacy_cache = _legacy(isolated_xdg, "cache")
    (legacy_cache / "icon.png").write_bytes(b"\x89PNG")
    sock = _plant_socket(legacy_cache / "run")
    try:
        moved = util.migrate_legacy_dirs()
    finally:
        os.unlink(sock)

    assert (util.cache_dir(), ) == tuple(dst for _, dst in moved)
    assert (util.cache_dir() / "icon.png").read_bytes() == b"\x89PNG"
    # runtime_dir() is explicitly NOT migrated (design spec 6.1): ephemeral.
    assert not (util.cache_dir() / "run").exists()


def test_a_socket_anywhere_in_the_tree_is_skipped_not_fatal(isolated_xdg, monkeypatch):
    """The "run" skip is the named case; the type filter is the general one, so
    a stray socket deeper in the tree cannot resurrect the crash either."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    legacy_cache = _legacy(isolated_xdg, "cache")
    (legacy_cache / "emi" / "icons").mkdir(parents=True)
    (legacy_cache / "emi" / "icons" / "stone.png").write_bytes(b"\x89PNG")
    sock = _plant_socket(legacy_cache / "emi", name="weird.sock")
    try:
        util.migrate_legacy_dirs()
    finally:
        os.unlink(sock)

    assert (util.cache_dir() / "emi" / "icons" / "stone.png").exists()
    assert not (util.cache_dir() / "emi" / "weird.sock").exists()


def test_a_failed_copy_warns_and_leaves_no_partial_destination(isolated_xdg, monkeypatch, caplog):
    """A compatibility migration must never make the CLI unusable, and must not
    poison its own idempotency guard: a half-written destination would satisfy
    `dst.exists()` and skip the migration forever."""
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")

    def boom(src, dst, **kw):
        Path(dst).mkdir(parents=True, exist_ok=True)  # a partial copy, as copytree leaves
        raise OSError(6, "No such device or address")

    monkeypatch.setattr(util.shutil, "copytree", boom)
    with caplog.at_level("WARNING"):
        assert util.migrate_legacy_dirs() == []
    assert any("could not migrate" in r.getMessage() for r in caplog.records)
    assert not util.config_dir().exists(), "a partial destination would block every retry"


def test_the_cli_stays_usable_when_the_migration_cannot_run(isolated_xdg, monkeypatch):
    """cli.main() calls migrate_legacy_dirs() outside its exception handler."""
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    monkeypatch.setattr(util.shutil, "copytree",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("nope")))
    assert cli.main([]) == 2  # help + exit 2, not a traceback


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


def test_cli_reannounces_migration_after_setup_logging(isolated_xdg, caplog):
    """Regression: util.migrate_legacy_dirs()'s own log.info() fires through the
    "lulism" logger *before* cli.main() calls util.setup_logging() (it has to,
    see test_migration_actually_runs_through_cli_main above) -- so on a real
    first-ever invocation, no handlers exist yet and that record is silently
    dropped: nothing reaches the console or the log file, even under -v. That
    defeats the point of the migration announcing itself in exactly the
    "where did my config go?" scenario this feature exists for.

    cli.main() must re-announce each migrated pair through its own logger once
    setup_logging() has actually configured handlers. Its wording ("original
    kept as rollback path") is deliberately distinct from util.py's own message
    ("the original is kept as a rollback path"), so this assertion only matches
    the re-announcement -- it fails if that re-announcement is removed, even if
    util.py's own (dropped, in real use) call happens to be visible under the
    test harness's own log capturing.
    """
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")

    with caplog.at_level("INFO"):
        assert cli.main(["-v"]) == 2

    assert any("original kept as rollback path" in r.getMessage() for r in caplog.records)
