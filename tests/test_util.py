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
