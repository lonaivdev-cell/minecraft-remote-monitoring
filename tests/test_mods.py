"""mods: descriptor parsing + the ==JAR/==META stream parser."""

import pytest

from mcctl import mods

NEOFORGE_TOML = """\
modLoader="javafml"
loaderVersion="[4,)"
[[mods]]
modId="sodium"
version="0.6.5"
displayName="Sodium"
description='''A modern rendering engine
for Minecraft.'''
[[dependencies.sodium]]
modId="minecraft"
"""

FABRIC_JSON = '{"id": "lithium", "version": "2.3.1", "name": "Lithium", "description": "Game logic optimizer"}'


def test_parse_mods_toml_first_entry_only():
    d = mods.parse_mods_toml(NEOFORGE_TOML)
    assert d["modId"] == "sodium"
    assert d["version"] == "0.6.5"
    assert d["displayName"] == "Sodium"
    assert "rendering engine" in d["description"]


def test_parse_fabric_json():
    d = mods.parse_fabric_json(FABRIC_JSON)
    assert d == {"modId": "lithium", "version": "2.3.1", "displayName": "Lithium",
                 "description": "Game logic optimizer"}
    assert mods.parse_fabric_json("not json") == {}


def test_parse_listing_mixed_loaders():
    out = (
        "==JAR sodium-0.6.5.jar|123456|1700000000\n"
        "==META META-INF/neoforge.mods.toml\n"
        + NEOFORGE_TOML +
        "==JAR lithium.jar|9999|1700000001\n"
        "==META fabric.mod.json\n"
        + FABRIC_JSON + "\n"
        "==JAR broken.jar|11|1700000002\n"
        "==ERR Bad zip file\n"
    )
    parsed = mods.parse_listing(out)
    assert [m.file for m in parsed] == ["sodium-0.6.5.jar", "lithium.jar", "broken.jar"]
    assert parsed[0].mod_id == "sodium" and parsed[0].loader == "neoforge"
    assert parsed[0].size == 123456
    assert parsed[1].mod_id == "lithium" and parsed[1].loader == "fabric"
    assert parsed[2].mod_id == "" and parsed[2].size == 11


def test_jar_version_placeholder_hidden():
    out = ("==JAR x.jar|1|1\n==META META-INF/mods.toml\n"
           '[[mods]]\nmodId="x"\nversion="${file.jarVersion}"\n')
    (m,) = mods.parse_listing(out)
    assert m.version == ""
    assert m.loader == "forge"


def test_list_mods_no_dir(fake_t, cfg):
    fake_t.expect("==NODIR", rc=0, out="==NODIR\n")
    with pytest.raises(mods.ModsError, match="no mods/"):
        mods.list_mods(fake_t, cfg)


def test_list_mods_end_to_end(fake_t, cfg):
    fake_t.expect("python3", out="==JAR a.jar|10|1\n==META fabric.mod.json\n" + FABRIC_JSON + "\n")
    entries = mods.list_mods(fake_t, cfg)
    assert len(entries) == 1 and entries[0].name == "Lithium"
    text = mods.render_text(entries)
    assert "Lithium" in text and "1 mods" in text
