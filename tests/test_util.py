import pytest

from mcctl import util


def test_sanitize_strips_ansi_and_controls():
    hostile = "\x1b[31mred\x1b[0m \x1b]0;title\x07 \x1b[2J\x07bell\x00null\tkeep\nline"
    clean = util.sanitize_terminal(hostile)
    assert "\x1b" not in clean and "\x00" not in clean and "\x07" not in clean
    assert "red" in clean and "keep" in clean and "\n" in clean and "\t" in clean


def test_strip_mc_codes():
    assert util.strip_mc_codes("§a20.0§7, §e15.2") == "20.0, 15.2"


def test_human_bytes():
    assert util.human_bytes(None) == "?"
    assert util.human_bytes(512) == "512 B"
    assert util.human_bytes(12 * 1024**3) == "12.0 GiB"


def test_human_duration():
    assert util.human_duration(42) == "42s"
    assert util.human_duration(3661) == "1h01m"
    assert util.human_duration(90000) == "1d01h"


def test_ops_lock_excludes_second_holder():
    with util.OpsLock():
        with pytest.raises(util.LockHeldError):
            with util.OpsLock():
                pass
    # released: can take it again
    with util.OpsLock():
        pass


def test_json_state_roundtrip(tmp_path):
    p = tmp_path / "x" / "state.json"
    util.save_json(p, {"a": 1})
    assert util.load_json(p, {}) == {"a": 1}
    assert util.load_json(tmp_path / "missing.json", {"d": True}) == {"d": True}


# ---------------------------------------------------------------- systemd units

def test_render_units_ships_all_five():
    from mcctl import util
    units = util.render_units()
    assert set(units) == {"mcctl-watchdog.service", "mcctl-autosave.service",
                          "mcctl-autosave.timer", "mcctl-backup.service",
                          "mcctl-backup.timer"}


def test_render_units_rewrites_execstart_for_pipx():
    from mcctl import util
    units = util.render_units(exe="/home/u/.local/bin/mcctl")
    assert "ExecStart=/home/u/.local/bin/mcctl watchdog run" in units["mcctl-watchdog.service"]
    assert "/usr/bin/mcctl" not in units["mcctl-watchdog.service"]
    # timers carry no ExecStart and must come through untouched
    assert "OnCalendar=*-*-* 04:30:00" in units["mcctl-backup.timer"]


def test_render_units_keeps_usrbin_for_system_install():
    from mcctl import util
    units = util.render_units(exe="/usr/bin/mcctl")
    assert "ExecStart=/usr/bin/mcctl save --skip-if-down" in units["mcctl-autosave.service"]


def test_user_unit_dir_respects_xdg(isolated_xdg):
    from mcctl import util
    assert str(util.user_unit_dir()).startswith(str(isolated_xdg))
    assert str(util.user_unit_dir()).endswith("systemd/user")
