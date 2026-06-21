"""assets: EMI-style item index — display names, model→texture resolution, and
the two server-side passes (manifest + icon bytes) over FakeTransport."""

from __future__ import annotations

import base64
import json
import os

import pytest

from mcctl import assets
from mcctl.config import Config

# ---- model fixtures: a generated item, a handheld, and two block items -------

ITEM_MODELS = {
    "minecraft:stick": {"parent": "minecraft:item/generated",
                        "textures": {"layer0": "minecraft:item/stick"}},
    "minecraft:diamond_sword": {"parent": "item/handheld",          # namespaceless parent
                                "textures": {"layer0": "item/diamond_sword"}},
    "minecraft:oak_planks": {"parent": "minecraft:block/oak_planks"},  # block item: no own textures
    "mymod:magic_block": {"parent": "mymod:block/magic_block"},
}

BLOCK_MODELS = {
    "minecraft:block/oak_planks": {"parent": "minecraft:block/cube_all",
                                   "textures": {"all": "minecraft:block/oak_planks"}},
    # a #ref: the shown face points at another key in the same model
    "mymod:block/magic_block": {"parent": "minecraft:block/cube",
                                "textures": {"all": "#side", "side": "mymod:block/magic_side"}},
}

LANG = {
    "item.minecraft.stick": "Stick",
    "block.minecraft.oak_planks": "Oak Planks",
    "item.minecraft.diamond_sword": "Diamond Sword",
}


# ----------------------------------------------------------------- id helpers


def test_texture_path_maps_namespace_and_folder():
    assert assets.texture_path("minecraft:block/oak_planks") == \
        "assets/minecraft/textures/block/oak_planks.png"
    assert assets.texture_path("item/stick") == "assets/minecraft/textures/item/stick.png"
    assert assets.texture_path("mymod:item/wrench") == "assets/mymod/textures/item/wrench.png"


def test_display_name_prefers_lang_then_titlecases():
    assert assets.display_name("minecraft:stick", LANG) == "Stick"
    # falls back from item. to block.
    assert assets.display_name("minecraft:oak_planks", LANG) == "Oak Planks"
    # unknown -> title-cased id, nested path flattened
    assert assets.display_name("mymod:tools/copper_wrench", {}) == "Tools Copper Wrench"
    assert assets.display_name("stick", LANG) == "Stick"          # bare id gains minecraft:


# ----------------------------------------------------------------- icon resolution


def test_resolve_icon_flat_item_uses_layer0():
    assert assets.resolve_icon("minecraft:stick", ITEM_MODELS, BLOCK_MODELS) == \
        "minecraft:item/stick"
    # namespaceless parent + texture both gain minecraft:
    assert assets.resolve_icon("minecraft:diamond_sword", ITEM_MODELS, BLOCK_MODELS) == \
        "minecraft:item/diamond_sword"


def test_resolve_icon_block_item_follows_parent_to_block_face():
    assert assets.resolve_icon("minecraft:oak_planks", ITEM_MODELS, BLOCK_MODELS) == \
        "minecraft:block/oak_planks"


def test_resolve_icon_follows_texture_ref():
    # all -> "#side" -> "mymod:block/magic_side"
    assert assets.resolve_icon("mymod:magic_block", ITEM_MODELS, BLOCK_MODELS) == \
        "mymod:block/magic_side"


def test_resolve_icon_unknown_item_is_empty():
    assert assets.resolve_icon("minecraft:does_not_exist", ITEM_MODELS, BLOCK_MODELS) == ""


def test_resolve_icon_survives_parent_cycle():
    items = {"a:x": {"parent": "a:y"}}
    blocks = {"a:y": {"parent": "a:x", "textures": {"all": "a:block/y"}}}  # y -> x -> y
    assert assets.resolve_icon("a:x", items, blocks) == "a:block/y"


# ----------------------------------------------------------------- manifest


def test_build_manifest_sorted_with_names_and_icons():
    manifest = assets.build_manifest(ITEM_MODELS, BLOCK_MODELS, LANG)
    by_id = {m["id"]: m for m in manifest}
    assert [m["id"] for m in manifest] == sorted(by_id)           # id-sorted
    assert by_id["minecraft:stick"] == {
        "id": "minecraft:stick", "name": "Stick", "icon": "minecraft:item/stick"}
    assert by_id["minecraft:oak_planks"]["icon"] == "minecraft:block/oak_planks"


def test_build_manifest_query_matches_id_or_name():
    # "diamond" hits the id+name of the sword only
    hits = assets.build_manifest(ITEM_MODELS, BLOCK_MODELS, LANG, query="diamond")
    assert [m["id"] for m in hits] == ["minecraft:diamond_sword"]
    # query by display name works too ("Oak Planks")
    assert [m["id"] for m in assets.build_manifest(ITEM_MODELS, BLOCK_MODELS, LANG, query="oak")] \
        == ["minecraft:oak_planks"]


def test_build_item_index_includes_extra_referenced_items():
    idx = assets.build_item_index(ITEM_MODELS, extra_items=("create:andesite_alloy", "stick"))
    assert "create:andesite_alloy" in idx
    assert "minecraft:stick" in idx                              # bare extra normalized, deduped
    assert idx == sorted(idx)


def test_build_catalog_distinct_textures_sorted():
    # one texture per item, de-duped to the set the offline sync downloads
    assert assets.build_catalog(ITEM_MODELS, BLOCK_MODELS) == [
        "minecraft:block/oak_planks",
        "minecraft:item/diamond_sword",
        "minecraft:item/stick",
        "mymod:block/magic_side",
    ]


def test_build_catalog_drops_unresolvable_and_dedups():
    items = {
        "a:one": {"textures": {"layer0": "a:item/shared"}},
        "a:two": {"textures": {"layer0": "a:item/shared"}},   # same texture -> one entry
        "a:none": {},                                          # no texture -> dropped
    }
    assert assets.build_catalog(items, {}) == ["a:item/shared"]


# ----------------------------------------------------------------- listing parse


def test_parse_asset_listing_splits_lang_and_models_last_wins():
    out = "\n".join([
        "==L\t" + json.dumps({"item.minecraft.stick": "Stick"}),
        "==M\titem\tminecraft:stick\t" + json.dumps({"textures": {"layer0": "minecraft:item/stick"}}),
        "==M\tblock\tminecraft:block/stone\t" + json.dumps({"textures": {"all": "a:old"}}),
        "==M\tblock\tminecraft:block/stone\t" + json.dumps({"textures": {"all": "a:new"}}),  # override
        "garbage line ignored",
    ])
    lang, items, blocks = assets.parse_asset_listing(out)
    assert lang == {"item.minecraft.stick": "Stick"}
    assert items["minecraft:stick"]["textures"]["layer0"] == "minecraft:item/stick"
    assert blocks["minecraft:block/stone"]["textures"]["all"] == "a:new"   # resource pack wins


def test_parse_icon_listing_decodes_base64_last_wins():
    png_a = base64.b64encode(b"OLD").decode()
    png_b = base64.b64encode(b"\x89PNG-bytes").decode()
    out = "\n".join([
        "==P\tminecraft:item/stick\t" + png_a,
        "==P\tminecraft:item/stick\t" + png_b,     # later source overrides
        "==P\tbad\tnot$$base64$$",                 # undecodable -> skipped
        "noise",
    ])
    icons = assets.parse_icon_listing(out)
    assert icons["minecraft:item/stick"] == b"\x89PNG-bytes"


def test_parse_catalog_listing_decodes_and_last_wins():
    out = "\n".join([
        "==H\tminecraft:item/stick\t111\t40",
        "==H\tminecraft:block/stone\t222\t90",
        "==H\tminecraft:block/stone\t333\t95",   # later source (resource pack) overrides
        "==H\tbad\tnot_an_int\t10",              # unparseable -> skipped
        "noise",
    ])
    cat = assets.parse_catalog_listing(out)
    assert cat["minecraft:item/stick"] == {"crc": 111, "size": 40}
    assert cat["minecraft:block/stone"] == {"crc": 333, "size": 95}
    assert "bad" not in cat


# ----------------------------------------------------------------- remote passes


def _cfg(tmp_path) -> Config:
    c = Config()
    c.server.transport = "local"
    c.server.server_dir = str(tmp_path / "srv")
    return c


def test_load_assets_runs_one_pass_and_parses(fake_t, tmp_path):
    listing = "\n".join([
        "==L\t" + json.dumps({"item.minecraft.stick": "Stick"}),
        "==M\titem\tminecraft:stick\t" + json.dumps({"textures": {"layer0": "minecraft:item/stick"}}),
    ])
    fake_t.expect(lambda s: "take_model" in s, out=listing + "\n")
    lang, items, _blocks = assets.load_assets(fake_t, _cfg(tmp_path))
    assert lang["item.minecraft.stick"] == "Stick"
    assert "minecraft:stick" in items


def test_load_assets_without_python_is_an_error(fake_t, tmp_path):
    fake_t.expect(lambda s: "take_model" in s, out="==NOPY\n")
    with pytest.raises(assets.AssetError, match="python3"):
        assets.load_assets(fake_t, _cfg(tmp_path))


def test_fetch_icons_inlines_wanted_and_decodes(fake_t, tmp_path):
    captured: dict[str, str] = {}

    def matcher(script: str) -> bool:
        if "WANTED = json.loads(" in script:
            captured["script"] = script
            return True
        return False

    png = base64.b64encode(b"\x89PNG\r\n").decode()
    fake_t.expect(matcher, out="==P\tminecraft:item/stick\t" + png + "\n")
    got = assets.fetch_icons(fake_t, _cfg(tmp_path),
                             ["minecraft:item/stick", "minecraft:item/missing"])
    assert got == {"minecraft:item/stick": b"\x89PNG\r\n"}
    # the wanted texture's relative path is embedded in the remote program
    assert "assets/minecraft/textures/item/stick.png" in captured["script"]


def test_fetch_icons_empty_is_noop(fake_t, tmp_path):
    assert assets.fetch_icons(fake_t, _cfg(tmp_path), []) == {}
    assert fake_t.calls == []           # no remote round-trip for an empty request


def test_hash_textures_inlines_wanted_and_parses(fake_t, tmp_path):
    captured: dict[str, str] = {}

    def matcher(script: str) -> bool:
        if "zi.CRC" in script:          # the catalog hasher, not the icon fetcher
            captured["script"] = script
            return True
        return False

    fake_t.expect(matcher, out="==H\tminecraft:item/stick\t987\t411\n")
    got = assets.hash_textures(fake_t, _cfg(tmp_path),
                               ["minecraft:item/stick", "minecraft:item/missing"])
    assert got == {"minecraft:item/stick": {"crc": 987, "size": 411}}
    assert "assets/minecraft/textures/item/stick.png" in captured["script"]


def test_hash_textures_empty_is_noop(fake_t, tmp_path):
    assert assets.hash_textures(fake_t, _cfg(tmp_path), []) == {}
    assert fake_t.calls == []


def test_scans_include_vanilla_cache_dir(fake_t, tmp_path):
    fake_t.expect(lambda s: "take_model" in s, out="")
    assets.load_assets(fake_t, _cfg(tmp_path))
    assert any("/.mcctl/vanilla" in c for c in fake_t.calls)   # vanilla jar scanned first


# ----------------------------------------------------------------- vanilla: version


def test_parse_version_probe_prefers_logs_over_libraries():
    out = "\n".join([
        "==VER\tlib\t1.21.1",
        "==VER\tlog\t1.21.1",
        "==VER\tlog\t1.21.1",
        "garbage",
    ])
    assert assets.parse_version_probe(out) == "1.21.1"
    # libraries-only still works as a fallback
    assert assets.parse_version_probe("==VER\tlib\t1.20.1\n") == "1.20.1"


def test_parse_version_probe_picks_highest_and_ignores_non_releases():
    out = "\n".join([
        "==VER\tlib\t1.20.1",
        "==VER\tlib\t1.21.1",
        "==VER\tlib\tnonsense",        # not a release id -> ignored
    ])
    assert assets.parse_version_probe(out) == "1.21.1"
    assert assets.parse_version_probe("") == ""


def test_resolve_version_override_and_config_skip_the_probe(fake_t, tmp_path):
    cfg = _cfg(tmp_path)
    assert assets.resolve_version(fake_t, cfg, "1.20.1") == "1.20.1"
    cfg.server.mc_version = "1.19.2"
    assert assets.resolve_version(fake_t, cfg, "") == "1.19.2"
    assert fake_t.calls == []          # neither path touches the server


def test_resolve_version_falls_back_to_probe(fake_t, tmp_path):
    fake_t.expect(lambda s: "==VER" in s, out="==VER\tlog\t1.21.1\n")
    assert assets.resolve_version(fake_t, _cfg(tmp_path), "") == "1.21.1"


# ----------------------------------------------------------------- vanilla: manifest


MANIFEST = {"versions": [
    {"id": "1.21.1", "url": "https://piston-meta.mojang.com/v1/packages/abc/1.21.1.json"},
    {"id": "1.20.1", "url": "https://piston-meta.mojang.com/v1/packages/def/1.20.1.json"},
]}
VERSION_JSON = {"downloads": {"client": {
    "url": "https://piston-data.mojang.com/v1/objects/deadbeef/client.jar",
    "sha1": "abc123def456", "size": 26000000}}}


def test_pick_version_entry():
    assert assets.pick_version_entry(MANIFEST, "1.21.1").endswith("/1.21.1.json")
    assert assets.pick_version_entry(MANIFEST, "9.9.9") == ""


def test_client_download_of():
    assert assets.client_download_of(VERSION_JSON) == (
        "https://piston-data.mojang.com/v1/objects/deadbeef/client.jar", "abc123def456", 26000000)
    assert assets.client_download_of({"downloads": {}}) == ("", "", 0)


def test_parse_sync_result():
    assert assets.parse_sync_result("==V\tdownloaded\tabc\n") == {"status": "downloaded", "sha1": "abc"}
    assert assets.parse_sync_result("==V\tpresent\tdef") == {"status": "present", "sha1": "def"}
    assert assets.parse_sync_result("noise")["status"] == "error"


# ----------------------------------------------------------------- vanilla: sync


def test_resolve_client_jar_two_fetches(fake_t, tmp_path):
    import json as _j
    fake_t.expect("version_manifest_v2.json", out=_j.dumps(MANIFEST))
    fake_t.expect("1.21.1.json", out=_j.dumps(VERSION_JSON))
    url, sha1 = assets.resolve_client_jar(fake_t, "1.21.1")
    assert url.endswith("/client.jar") and sha1 == "abc123def456"


def test_resolve_client_jar_unknown_version(fake_t, tmp_path):
    import json as _j
    fake_t.expect("version_manifest_v2.json", out=_j.dumps(MANIFEST))
    with pytest.raises(assets.AssetError, match="no entry for"):
        assets.resolve_client_jar(fake_t, "9.9.9")


def test_sync_vanilla_downloads_and_is_idempotent(fake_t, tmp_path):
    import json as _j
    cfg = _cfg(tmp_path)
    cfg.server.mc_version = "1.21.1"        # skip the probe
    fake_t.expect("version_manifest_v2.json", out=_j.dumps(MANIFEST))
    fake_t.expect("1.21.1.json", out=_j.dumps(VERSION_JSON))
    fake_t.expect("urlretrieve", out="==V\tdownloaded\tabc123def456\n")
    res = assets.sync_vanilla(fake_t, cfg)
    assert res["ok"] and res["status"] == "downloaded" and res["version"] == "1.21.1"
    assert res["jar"].endswith("/.mcctl/vanilla/1.21.1.jar")
    assert res["sha1"] == "abc123def456"


def test_sync_vanilla_present_is_ok(fake_t, tmp_path):
    import json as _j
    cfg = _cfg(tmp_path)
    cfg.server.mc_version = "1.21.1"
    fake_t.expect("version_manifest_v2.json", out=_j.dumps(MANIFEST))
    fake_t.expect("1.21.1.json", out=_j.dumps(VERSION_JSON))
    fake_t.expect("urlretrieve", out="==V\tpresent\tabc123def456\n")   # already cached, sha matches
    assert assets.sync_vanilla(fake_t, cfg)["status"] == "present"


def test_download_to_refuses_non_https(fake_t, tmp_path):
    with pytest.raises(assets.AssetError, match="non-https"):
        assets.download_to(fake_t, "http://evil/jar", "/tmp/x.jar")
    assert fake_t.calls == []


def test_vanilla_jar_resolves_real_items_end_to_end(tmp_path):
    """A jar dropped in the vanilla cache makes minecraft:* items resolve (real pipeline)."""
    import json as _j
    import zipfile

    from mcctl.transport import make_transport

    cfg = _cfg(tmp_path)
    cache = assets.vanilla_cache_dir(cfg)
    os.makedirs(cache, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n" + b"DIAMOND" * 3
    with zipfile.ZipFile(os.path.join(cache, "1.21.1.jar"), "w") as z:
        z.writestr("assets/minecraft/models/item/diamond.json",
                   _j.dumps({"parent": "item/generated", "textures": {"layer0": "minecraft:item/diamond"}}))
        z.writestr("assets/minecraft/textures/item/diamond.png", png)
        z.writestr("assets/minecraft/lang/en_us.json", _j.dumps({"item.minecraft.diamond": "Diamond"}))

    t = make_transport(cfg)
    lang, im, bm = assets.load_assets(t, cfg)
    manifest = assets.build_manifest(im, bm, lang)
    diamond = next(m for m in manifest if m["id"] == "minecraft:diamond")
    assert diamond == {"id": "minecraft:diamond", "name": "Diamond", "icon": "minecraft:item/diamond"}
    icons = assets.fetch_icons(t, cfg, ["minecraft:item/diamond"])
    assert icons["minecraft:item/diamond"] == png


def test_catalog_lists_real_textures_with_crc_and_size(tmp_path):
    """The offline-sync catalog reports a real texture's crc32 + size from the jar."""
    import json as _j
    import zipfile
    import zlib

    from mcctl.transport import make_transport

    cfg = _cfg(tmp_path)
    cache = assets.vanilla_cache_dir(cfg)
    os.makedirs(cache, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n" + b"DIAMOND" * 3
    with zipfile.ZipFile(os.path.join(cache, "1.21.1.jar"), "w") as z:
        z.writestr("assets/minecraft/models/item/diamond.json",
                   _j.dumps({"parent": "item/generated", "textures": {"layer0": "minecraft:item/diamond"}}))
        z.writestr("assets/minecraft/textures/item/diamond.png", png)

    cat = assets.catalog(make_transport(cfg), cfg)
    entry = next(e for e in cat["textures"] if e["id"] == "minecraft:item/diamond")
    assert entry["size"] == len(png)
    assert entry["crc"] == zlib.crc32(png) & 0xffffffff
    assert cat["count"] == len(cat["textures"]) and cat["bytes"] >= len(png)
