"""Backup naming, GFS rotation planning, and create/restore orchestration."""

from __future__ import annotations

import datetime as dt

import pytest

from mcctl.backup import (
    BackupEntry,
    BackupError,
    BackupManager,
    make_name,
    parse_name,
    plan_rotation,
)


def _e(name: str, ts: dt.datetime, full=False) -> BackupEntry:
    return BackupEntry(name, f"/bk/{name}", ts, 1000, full)


def _entries_hourly(days: int, per_day: int = 4) -> list[BackupEntry]:
    base = dt.datetime(2026, 6, 10, 22, 0, 0)
    out = []
    for d in range(days):
        for h in range(per_day):
            ts = base - dt.timedelta(days=d, hours=h * 5)
            out.append(_e(make_name("world", ts), ts))
    return out


def test_name_roundtrip():
    now = dt.datetime(2026, 6, 10, 4, 30, 59)
    n = make_name("world", now)
    assert n == "world-world-20260610-043059.tar.zst"
    assert parse_name("world", n) == (now, False)
    nf = make_name("world", now, full=True, compression="gzip")
    assert nf.endswith(".tar.gz")
    assert parse_name("world", nf) == (now, True)
    assert parse_name("world", "random-file.tar.zst") is None
    assert parse_name("world", "world-world-garbage.tar.zst") is None


def test_rotation_counts():
    entries = _entries_hourly(days=30)
    kept, dropped = plan_rotation(entries, keep_recent=8, keep_daily=7, keep_weekly=4)
    names = {e.name for e in kept}
    # newest 8 all kept
    newest = sorted(entries, key=lambda e: e.ts, reverse=True)[:8]
    assert all(e.name in names for e in newest)
    # one per day for the 7 most recent days
    days = {e.ts.date() for e in kept}
    assert len(days) >= 7
    # weekly coverage: at least 4 distinct ISO weeks survive
    weeks = {e.ts.isocalendar()[:2] for e in kept}
    assert len(weeks) >= 4
    # and rotation actually deletes the bulk
    assert len(dropped) == len(entries) - len(kept)
    assert len(kept) < len(entries) / 3


def test_rotation_never_deletes_fulls():
    old_full = _e("world-full-20250101-000000.tar.zst", dt.datetime(2025, 1, 1), full=True)
    entries = [*_entries_hourly(10), old_full]
    kept, dropped = plan_rotation(entries, keep_recent=2, keep_daily=2, keep_weekly=1)
    assert old_full in kept
    assert old_full not in dropped


def test_rotation_empty():
    kept, dropped = plan_rotation([], keep_recent=8, keep_daily=7, keep_weekly=4)
    assert kept == [] and dropped == []


# ---------------------------------------------------------------- orchestration

PROPS_NO_RCON = "enable-rcon=false\n"


def _mgr(cfg, fake_t, running: bool):
    cfg.server.server_dir = "/opt/minecraft"
    cfg.backup.remote_dir = "/opt/minecraft-backups"
    fake_t.files["/opt/minecraft/server.properties"] = PROPS_NO_RCON
    fake_t.files["/opt/minecraft/logs/latest.log"] = "[boot] Done (3.0s)!\n"
    pid_out = "4242\n" if running else ""
    fake_t.expect(lambda s: "pgrep java" in s and "readlink" in s, out=pid_out)
    return BackupManager(cfg, fake_t)


def test_create_orders_saveoff_tar_saveon(cfg, fake_t, clock, monkeypatch):
    mgr = _mgr(cfg, fake_t, running=True)
    monkeypatch.setattr("mcctl.console.time", clock)
    fake_t.expect("df -B1 --output=avail", out=str(50 * 1024**3))
    fake_t.expect("command -v zstd", rc=0)
    fake_t.expect("tar -cf -", out="123456\n")
    entry = mgr.create()
    assert entry is not None and entry.size == 123456
    i_off, i_tar, i_on = fake_t.order_of("save-off", "tar -cf -", "save-on")
    assert -1 not in (i_off, i_tar, i_on)
    assert i_off < i_tar < i_on


def test_create_reenables_save_on_failure(cfg, fake_t, clock, monkeypatch):
    from mcctl.transport import TransportError
    mgr = _mgr(cfg, fake_t, running=True)
    monkeypatch.setattr("mcctl.console.time", clock)
    fake_t.expect("df -B1 --output=avail", out=str(50 * 1024**3))
    fake_t.expect("command -v zstd", rc=0)
    fake_t.expect("tar -cf -", TransportError("disk exploded"))
    with pytest.raises(BackupError, match="snapshot failed"):
        mgr.create()
    assert fake_t.calls_matching("save-on"), "save-on must run even when tar fails"


def test_create_disk_guard(cfg, fake_t):
    mgr = _mgr(cfg, fake_t, running=False)
    fake_t.expect("command -v zstd", rc=0)
    fake_t.expect("df -B1 --output=avail", out=str(1 * 1024**3))  # < 5 GiB default
    with pytest.raises(BackupError, match="free"):
        mgr.create()
    assert not fake_t.calls_matching("tar -cf -")


def test_create_skips_console_when_down(cfg, fake_t):
    mgr = _mgr(cfg, fake_t, running=False)
    fake_t.expect("df -B1 --output=avail", out=str(50 * 1024**3))
    fake_t.expect("command -v zstd", rc=0)
    fake_t.expect("tar -cf -", out="99\n")
    entry = mgr.create()
    assert entry is not None
    assert not fake_t.calls_matching("save-off")
    assert not fake_t.calls_matching("save-on")


def test_restore_refuses_running(cfg, fake_t):
    mgr = _mgr(cfg, fake_t, running=True)
    name = "world-world-20260101-000000.tar.zst"
    fake_t.files[f"/opt/minecraft-backups/{name}"] = "binary"
    with pytest.raises(BackupError, match="running"):
        mgr.restore(name)


def test_restore_moves_world_aside(cfg, fake_t):
    mgr = _mgr(cfg, fake_t, running=False)
    name = "world-world-20260101-000000.tar.zst"
    fake_t.files[f"/opt/minecraft-backups/{name}"] = "binary"
    fake_t.expect("zstd -t -q", rc=0)
    fake_t.expect("zstd -dc", rc=0)
    aside = mgr.restore(name)
    assert aside.startswith("world.pre-restore-")
    script = fake_t.calls_matching("zstd -dc")[0]
    assert "mv 'world'" in script or "mv world" in script
    assert "tar -xf -" in script


def test_restore_missing_archive(cfg, fake_t):
    mgr = _mgr(cfg, fake_t, running=False)
    with pytest.raises(BackupError, match="no such backup"):
        mgr.restore("world-world-19990101-000000.tar.zst")


# ---------------------------------------------------------------- full-backup excludes

def test_full_excludes_outside_server_dir(cfg):
    cfg.server.server_dir = "/opt/minecraft"
    cfg.backup.remote_dir = "/opt/minecraft-backups"
    from mcctl.backup import full_backup_excludes
    out = full_backup_excludes(cfg)
    assert out == cfg.backup.full_excludes  # sibling dir: no self-exclude needed


def test_full_excludes_nested_backup_dir(cfg):
    cfg.server.server_dir = "/opt/minecraft"
    cfg.backup.remote_dir = "/opt/minecraft/backups"
    from mcctl.backup import full_backup_excludes
    out = full_backup_excludes(cfg)
    assert out[-1] == "backups"
    assert "logs" in out  # configured excludes still present


def test_full_excludes_prefix_collision_is_not_nested(cfg):
    cfg.server.server_dir = "/opt/minecraft"
    cfg.backup.remote_dir = "/opt/minecraft-backups"  # shares prefix, NOT nested
    from mcctl.backup import full_backup_excludes
    assert "minecraft-backups" not in " ".join(full_backup_excludes(cfg))
