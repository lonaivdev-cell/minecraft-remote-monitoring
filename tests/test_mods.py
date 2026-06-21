"""mods: descriptor parsing + the ==JAR/==META stream parser + client/server diff."""

import zipfile

import pytest

from mcctl import mods
from mcctl.mods import ModInfo

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


# ---------------------------------------------------------------- local scan (client pack)

def _jar(path, arcname, content):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(arcname, content)


def test_scan_local_mods_reads_descriptors(tmp_path):
    d = tmp_path / "client-mods"
    d.mkdir()
    _jar(d / "sodium-0.6.5.jar", "META-INF/neoforge.mods.toml", NEOFORGE_TOML)
    _jar(d / "lithium.jar", "fabric.mod.json", FABRIC_JSON)
    _jar(d / "nometa.jar", "pack.mcmeta", "{}")          # a jar with no mod descriptor
    (d / "notes.txt").write_text("ignored, not a jar")   # non-jar ignored
    entries = {m.file: m for m in mods.scan_local_mods(str(d))}
    assert set(entries) == {"sodium-0.6.5.jar", "lithium.jar", "nometa.jar"}
    assert entries["sodium-0.6.5.jar"].mod_id == "sodium"
    assert entries["sodium-0.6.5.jar"].loader == "neoforge"
    assert entries["lithium.jar"].version == "2.3.1"
    assert entries["nometa.jar"].mod_id == ""            # no descriptor -> blank metadata


def test_scan_local_mods_bad_dir():
    with pytest.raises(mods.ModsError, match="not a mods directory"):
        mods.scan_local_mods("/no/such/path/mods")


# ---------------------------------------------------------------- diff (client vs server)

def _m(mod_id="", version="", file="", name=""):
    return ModInfo(file=file or f"{mod_id}.jar", mod_id=mod_id, version=version, name=name)


def test_diff_partitions_by_presence_and_version():
    server = [_m("sodium", "0.6.5"), _m("create", "6.0"), _m("ftbquests", "20")]
    client = [_m("sodium", "0.6.4"), _m("create", "6.0"), _m("jei", "19")]
    d = mods.diff_mods(server, client)
    assert [m.mod_id for m in d.server_only] == ["ftbquests"]   # server, not client
    assert [m.mod_id for m in d.client_only] == ["jei"]         # client, not server
    assert [x.key for x in d.version_mismatch] == ["sodium"]    # both known, differ
    assert d.version_mismatch[0].server_version == "0.6.5"
    assert d.version_mismatch[0].client_version == "0.6.4"
    assert [m.mod_id for m in d.common] == ["create"]           # same id + version
    assert not d.in_sync


def test_diff_in_sync():
    pack = [_m("a", "1"), _m("b", "2")]
    d = mods.diff_mods(pack, list(pack))
    assert d.in_sync and not d.version_mismatch
    assert {m.mod_id for m in d.common} == {"a", "b"}
    assert d.to_dict()["in_sync"] is True


def test_diff_unknown_version_is_not_a_mismatch():
    # one side lacks metadata (no version) -> present on both, NOT a version mismatch
    d = mods.diff_mods([_m("sodium", "0.6.5")], [_m("sodium", "")])
    assert not d.version_mismatch
    assert [m.mod_id for m in d.common] == ["sodium"]


def test_diff_falls_back_to_filename_without_mod_id():
    # no mod_id on either side -> match on filename
    server = [_m(file="cool.jar"), _m(file="server-only.jar")]
    client = [_m(file="cool.jar"), _m(file="client-only.jar")]
    d = mods.diff_mods(server, client)
    assert [m.file for m in d.server_only] == ["server-only.jar"]
    assert [m.file for m in d.client_only] == ["client-only.jar"]
    assert [m.file for m in d.common] == ["cool.jar"]


def test_render_diff_text():
    d = mods.diff_mods([_m("sodium", "0.6.5"), _m("create", "6.0")],
                       [_m("create", "6.0"), _m("jei", "19")])
    text = mods.render_diff(d)
    assert "server-only: 1" in text and "client-only: 1" in text
    assert "sodium" in text.lower() and "jei" in text.lower()
    assert "in sync" in mods.render_diff(mods.diff_mods([_m("a", "1")], [_m("a", "1")]))
