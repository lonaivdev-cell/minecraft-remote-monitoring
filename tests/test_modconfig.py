"""modconfig: path safety, format/association heuristics, validation, and
LocalTransport-backed list/read/write against a sandbox config/ tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcctl import modconfig as MC
from mcctl.mods import ModInfo
from mcctl.transport import LocalTransport

# ---------------------------------------------------------------- pure helpers

def test_fmt_for():
    assert MC.fmt_for("apotheosis.toml") == "toml"
    assert MC.fmt_for("foo/bar-common.toml") == "toml"
    assert MC.fmt_for("x.json5") == "json5"
    assert MC.fmt_for("x.JSON") == "json"        # case-insensitive
    assert MC.fmt_for("server.properties") == "properties"
    assert MC.fmt_for("noext") == ""


def test_safe_rel_normalises():
    assert MC.safe_rel("a/./b.toml") == "a/b.toml"
    assert MC.safe_rel("apotheosis/adventure.toml") == "apotheosis/adventure.toml"
    assert MC.safe_rel("  spaced.toml  ") == "spaced.toml"


@pytest.mark.parametrize("bad", [
    "../secret", "a/../../b", "..", "../../etc/passwd", "/etc/passwd", "", "   ",
])
def test_safe_rel_rejects_escape(bad):
    with pytest.raises(MC.ConfigEditError):
        MC.safe_rel(bad)


def test_associate_heuristics():
    mods = [
        ModInfo(file="a.jar", mod_id="apotheosis", name="Apotheosis"),
        ModInfo(file="b.jar", mod_id="aether", name="The Aether"),
        ModInfo(file="c.jar", mod_id="ars_nouveau", name="Ars Nouveau"),
    ]
    files = [
        MC.ConfigFile(path="apotheosis/adventure.toml"),     # sub-dir match
        MC.ConfigFile(path="aether-server.toml"),            # stem split on "-"
        MC.ConfigFile(path="ars_nouveau-common.toml"),       # stem split on "-", keeps "_"
        MC.ConfigFile(path="random-unmatched.toml"),         # no mod
    ]
    MC.associate(files, mods)
    got = {f.path: (f.mod_id, f.mod_name) for f in files}
    assert got["apotheosis/adventure.toml"] == ("apotheosis", "Apotheosis")
    assert got["aether-server.toml"] == ("aether", "The Aether")
    assert got["ars_nouveau-common.toml"] == ("ars_nouveau", "Ars Nouveau")
    assert got["random-unmatched.toml"] == ("", "")


def test_validate_text():
    assert MC.validate_text("x.toml", "v = 1\n[s]\na = 2\n") == "toml"
    with pytest.raises(MC.ConfigEditError, match="invalid TOML"):
        MC.validate_text("x.toml", "v = = broken\n")
    assert MC.validate_text("x.json", '{"a": 1}') == "json"
    with pytest.raises(MC.ConfigEditError, match="invalid JSON"):
        MC.validate_text("x.json", "{nope}")
    # formats with no stdlib parser pass through untouched
    assert MC.validate_text("x.json5", "{a: 1 /* ok */}") == "json5"
    assert MC.validate_text("x.cfg", "anything = goes :)") == "cfg"
    assert MC.validate_text("x.properties", "key=value") == "properties"


# ---------------------------------------------------------------- IO (LocalTransport)

@pytest.fixture
def t_local(cfg) -> LocalTransport:
    return LocalTransport(cfg)


def _seed(cfg, files: dict[str, str]) -> Path:
    cdir = Path(MC.config_dir(cfg))
    for rel, content in files.items():
        p = cdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return cdir


def test_list_config_files(cfg, t_local):
    _seed(cfg, {
        "apotheosis/adventure.toml": "[x]\na=1\n",
        "aether-server.toml": "b = 2\n",
        "notes.txt": "hi\n",
    })
    files = MC.list_config_files(t_local, cfg, associate_mods=False)
    byp = {f.path: f for f in files}
    assert set(byp) == {"apotheosis/adventure.toml", "aether-server.toml", "notes.txt"}
    assert byp["aether-server.toml"].fmt == "toml"
    assert byp["notes.txt"].fmt == "text"
    assert byp["apotheosis/adventure.toml"].size > 0
    assert byp["apotheosis/adventure.toml"].mtime > 0


def test_list_config_files_no_dir(cfg, t_local):
    with pytest.raises(MC.ConfigEditError, match="no config/"):
        MC.list_config_files(t_local, cfg, associate_mods=False)


def test_read_config(cfg, t_local):
    _seed(cfg, {"apotheosis/a.toml": "[s]\nv = 1\n"})
    res = MC.read_config(t_local, cfg, "apotheosis/a.toml")
    assert res["text"] == "[s]\nv = 1\n"
    assert res["fmt"] == "toml"
    assert res["bytes"] == len(b"[s]\nv = 1\n")


def test_read_config_missing(cfg, t_local):
    _seed(cfg, {"a.toml": "x = 1\n"})
    with pytest.raises(MC.ConfigEditError, match="no such config file"):
        MC.read_config(t_local, cfg, "missing.toml")


def test_read_config_rejects_traversal(cfg, t_local):
    with pytest.raises(MC.ConfigEditError):
        MC.read_config(t_local, cfg, "../../etc/passwd")


def test_read_config_size_cap(cfg, t_local, monkeypatch):
    _seed(cfg, {"big.toml": "v = 1\n" * 100})
    monkeypatch.setattr(MC, "MAX_BYTES", 10)
    with pytest.raises(MC.ConfigEditError, match="edit cap"):
        MC.read_config(t_local, cfg, "big.toml")


def test_write_config_roundtrip_and_backup(cfg, t_local):
    cdir = _seed(cfg, {"a.toml": "v = 1\n"})
    res = MC.write_config(t_local, cfg, "a.toml", "v = 2\n")
    assert res == {"path": "a.toml", "fmt": "toml", "bytes": len(b"v = 2\n")}
    assert (cdir / "a.toml").read_text() == "v = 2\n"
    baks = list(cdir.glob("a.toml.bak.*"))
    assert baks and baks[0].read_text() == "v = 1\n"


def test_write_config_validates_before_writing(cfg, t_local):
    cdir = _seed(cfg, {"a.toml": "v = 1\n"})
    with pytest.raises(MC.ConfigEditError, match="invalid TOML"):
        MC.write_config(t_local, cfg, "a.toml", "v = = broken\n")
    assert (cdir / "a.toml").read_text() == "v = 1\n"  # left untouched
    assert not list(cdir.glob("a.toml.bak.*"))         # and never backed up


def test_write_config_refuses_new_file(cfg, t_local):
    _seed(cfg, {"a.toml": "v = 1\n"})
    with pytest.raises(MC.ConfigEditError, match="edits existing files only"):
        MC.write_config(t_local, cfg, "brand-new.toml", "v = 1\n")
