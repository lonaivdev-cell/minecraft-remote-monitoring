"""assets: EMI-style item index — display names, model→texture resolution, and
the two server-side passes (manifest + icon bytes) over FakeTransport."""

from __future__ import annotations

import base64
import json

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
