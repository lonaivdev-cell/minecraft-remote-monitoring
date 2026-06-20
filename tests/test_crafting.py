"""crafting: recipe parsing (pure) + the console-driven plan/craft engine."""

from __future__ import annotations

import pytest

from mcctl import crafting
from mcctl.config import Config

SHAPED_CHEST = {
    "type": "minecraft:crafting_shaped",
    "pattern": ["###", "# #", "###"],
    "key": {"#": {"item": "minecraft:oak_planks"}},
    "result": {"item": "minecraft:chest", "count": 1},
}

SHAPELESS_TORCH = {
    "type": "minecraft:crafting_shapeless",
    "ingredients": [{"item": "minecraft:coal"}, {"item": "minecraft:stick"}],
    "result": {"item": "minecraft:torch", "count": 4},
}

TAGGED = {
    "type": "minecraft:crafting_shaped",
    "pattern": ["P", "S"],
    "key": {"P": {"tag": "minecraft:planks"}, "S": [{"item": "minecraft:stick"},
                                                   {"item": "minecraft:bamboo"}]},
    "result": {"id": "minecraft:sign", "count": 3},   # 1.20.5 "id" spelling
}

SMELT_IRON = {
    "type": "minecraft:smelting",
    "ingredient": {"item": "minecraft:raw_iron"},
    "result": "minecraft:iron_ingot",
    "experience": 0.7, "cookingtime": 200,
}

STONECUT_BRICKS = {   # 1.21 spelling: count lives in the result object
    "type": "minecraft:stonecutting",
    "ingredient": "minecraft:stone",
    "result": {"id": "minecraft:stone_bricks", "count": 1},
}

STONECUT_LEGACY = {   # ≤1.20 spelling: result is a string + top-level count
    "type": "minecraft:stonecutting",
    "ingredient": {"item": "minecraft:andesite"},
    "result": "minecraft:andesite_stairs", "count": 1,
}

SMITHING_NETHERITE = {
    "type": "minecraft:smithing_transform",
    "template": {"item": "minecraft:netherite_upgrade_smithing_template"},
    "base": {"item": "minecraft:diamond_sword"},
    "addition": {"item": "minecraft:netherite_ingot"},
    "result": {"id": "minecraft:netherite_sword"},
}


# ----------------------------------------------------------------- pure parsing


def test_parse_shaped_expands_pattern():
    rec = crafting.parse_recipe(SHAPED_CHEST, "minecraft:chest")
    assert rec.rtype == "shaped"
    assert rec.result_item == "minecraft:chest" and rec.result_count == 1
    # 8 '#' slots in the pattern, all the same ingredient
    assert len(rec.slots) == 8
    reqs = rec.requirements()
    assert reqs == [(["minecraft:oak_planks"], 8)]


def test_parse_shapeless():
    rec = crafting.parse_recipe(SHAPELESS_TORCH, "minecraft:torch")
    assert rec.rtype == "shapeless" and rec.result_count == 4
    assert sorted(rec.requirements()) == [(["minecraft:coal"], 1), (["minecraft:stick"], 1)]


def test_parse_tags_and_alternatives_and_id_result():
    rec = crafting.parse_recipe(TAGGED, "minecraft:sign")
    assert rec.result_item == "minecraft:sign" and rec.result_count == 3
    reqs = dict((tuple(p), n) for p, n in rec.requirements())
    assert reqs[("#minecraft:planks",)] == 1
    # the alternatives slot keeps both acceptable predicates
    assert reqs[("minecraft:stick", "minecraft:bamboo")] == 1


def test_shaped_grid_is_positional_for_emi_rendering():
    rec = crafting.parse_recipe(SHAPED_CHEST, "minecraft:chest")
    assert rec.grid_w == 3
    P = "minecraft:oak_planks"
    assert rec.grid == [P, P, P, P, "", P, P, P, P]   # the hole in the middle is preserved
    d = rec.to_dict()
    assert d["grid"] == rec.grid and d["grid_w"] == 3


def test_shapeless_grid_lays_out_left_to_right():
    rec = crafting.parse_recipe(SHAPELESS_TORCH, "minecraft:torch")
    assert rec.grid == ["minecraft:coal", "minecraft:stick"] and rec.grid_w == 2
    # non-crafting recipes carry no grid (rendered as input → output instead)
    smelt = crafting.parse_recipe(SMELT_IRON, "minecraft:iron_ingot")
    assert smelt.grid == [] and "grid" not in smelt.to_dict()


def test_parse_smelting_carries_cook_metadata():
    rec = crafting.parse_recipe(SMELT_IRON, "minecraft:iron_ingot")
    assert rec.rtype == "smelting" and rec.category == "smelting"
    assert rec.result_item == "minecraft:iron_ingot" and rec.result_count == 1
    assert rec.cooking_time == 200 and rec.experience == 0.7
    assert rec.requirements() == [(["minecraft:raw_iron"], 1)]


def test_parse_stonecutting_both_count_spellings():
    rec = crafting.parse_recipe(STONECUT_BRICKS, "minecraft:stonecutting/stone_bricks")
    assert rec.rtype == "stonecutting" and rec.category == "stonecutting"
    assert rec.result_item == "minecraft:stone_bricks" and rec.result_count == 1
    legacy = crafting.parse_recipe(STONECUT_LEGACY, "minecraft:stonecutting/andesite_stairs")
    assert legacy.result_item == "minecraft:andesite_stairs" and legacy.result_count == 1
    assert legacy.requirements() == [(["minecraft:andesite"], 1)]


def test_parse_smithing_transform_three_inputs():
    rec = crafting.parse_recipe(SMITHING_NETHERITE, "minecraft:netherite_sword_smithing")
    assert rec.rtype == "smithing" and rec.category == "smithing"
    assert rec.result_item == "minecraft:netherite_sword"
    reqs = dict((tuple(p), n) for p, n in rec.requirements())
    assert reqs[("minecraft:diamond_sword",)] == 1
    assert reqs[("minecraft:netherite_ingot",)] == 1
    assert reqs[("minecraft:netherite_upgrade_smithing_template",)] == 1


def test_to_dict_has_category_and_cook_fields_additively():
    smelt = crafting.parse_recipe(SMELT_IRON, "minecraft:iron_ingot").to_dict()
    assert smelt["category"] == "smelting"
    assert smelt["cooking_time"] == 200 and smelt["experience"] == 0.7
    # a plain craft stays lean: the cook-only keys are omitted, not zeroed
    chest = crafting.parse_recipe(SHAPED_CHEST, "minecraft:chest").to_dict()
    assert chest["category"] == "crafting"
    assert "cooking_time" not in chest and "experience" not in chest


def test_unsupported_recipe_type_is_skipped():
    # a mod's bespoke machine recipe isn't a vanilla data-driven type
    assert crafting.parse_recipe({"type": "create:mixing",
                                  "result": "create:andesite_alloy"}, "x") is None
    # smithing_trim has no item result (it's cosmetic) -> nothing to reproduce
    assert crafting.parse_recipe({"type": "minecraft:smithing_trim",
                                  "template": {"item": "minecraft:coast_armor_trim_smithing_template"},
                                  "base": {"item": "minecraft:iron_chestplate"},
                                  "addition": {"item": "minecraft:redstone"}}, "x") is None


def test_listing_framing_and_dedup():
    import json as _j
    line1 = "==R\tmymod.jar\tmymod:gear\tmymod:gear\t" + _j.dumps(SHAPELESS_TORCH, separators=(",", ":"))
    line2 = "==R\tminecraft\tminecraft:chest\tminecraft:chest\t" + _j.dumps(SHAPED_CHEST, separators=(",", ":"))
    recipes, truncated = crafting.parse_recipe_listing(line1 + "\n" + line2 + "\n==TRUNC\n")
    assert truncated is True
    assert {r.rid for r in recipes} == {"mymod:gear", "minecraft:chest"}


# ----------------------------------------------------------------- tag resolution


def _tag_line(key: str, vals: list, replace: bool = False) -> str:
    import json as _j
    raw = _j.dumps(vals, separators=(",", ":"))
    return f"==G\t{key}\t{'1' if replace else '0'}\t{raw}"


def test_parse_tag_listing_merges_across_sources():
    # vanilla defines two planks, a mod adds a third to the same tag -> union, in order.
    # (The remote scanner already flattens {"id": x} objects to the string x before emit.)
    out = "\n".join([
        _tag_line("minecraft:planks", ["minecraft:oak_planks", "minecraft:spruce_planks"]),
        _tag_line("minecraft:planks", ["mymod:walnut_planks", "minecraft:oak_planks"]),  # dup ignored
    ])
    tags = crafting.parse_tag_listing(out)
    assert tags["minecraft:planks"] == [
        "minecraft:oak_planks", "minecraft:spruce_planks", "mymod:walnut_planks",
    ]


def test_parse_tag_listing_replace_resets():
    out = "\n".join([
        _tag_line("minecraft:planks", ["minecraft:oak_planks"]),
        _tag_line("minecraft:planks", ["custom:only_planks"], replace=True),  # datapack override
    ])
    tags = crafting.parse_tag_listing(out)
    assert tags["minecraft:planks"] == ["custom:only_planks"]


def test_resolve_tag_map_recurses_and_dedups():
    tags = {
        "minecraft:planks": ["minecraft:oak_planks", "#minecraft:non_flammable_wood", "bamboo_planks"],
        "minecraft:non_flammable_wood": ["minecraft:crimson_planks", "minecraft:oak_planks"],  # dup
    }
    # accepts a leading '#' and a namespaceless entry; flattens nested tags, dedup, order kept
    items = crafting.resolve_tag_map(tags, "#minecraft:planks")
    assert items == [
        "minecraft:oak_planks", "minecraft:crimson_planks", "minecraft:bamboo_planks",
    ]


def test_resolve_tag_map_survives_cycles_and_missing():
    tags = {"a:x": ["#a:y"], "a:y": ["#a:x", "a:thing"]}  # x -> y -> x cycle
    assert crafting.resolve_tag_map(tags, "a:x") == ["a:thing"]
    assert crafting.resolve_tag_map(tags, "a:absent") == []


# ----------------------------------------------------------------- count helpers


@pytest.mark.parametrize("text,expected", [
    ("Found 12 matching items on GLEYSSON", 12),
    ("Removed 8 matching items from player GLEYSSON", 8),
    ("No items were found on player GLEYSSON", 0),
    ("", 0),
])
def test_clear_count(text, expected):
    assert crafting._clear_count(text) == expected


def test_decide_n():
    # request 5 but only 3 craftable -> 3, limited by materials
    assert crafting._decide_n(3, 64, 5) == (3, "materials")
    # plenty of materials, capped at one stack
    assert crafting._decide_n(1000, 16, None) == (16, "stack")
    # max with limited materials
    assert crafting._decide_n(7, 64, None) == (7, "materials")
    # exact request satisfied
    assert crafting._decide_n(10, 64, 4) == (4, "none")
    # nothing craftable
    assert crafting._decide_n(0, 64, 1) == (0, "materials")


def test_inventory_nbt_scan_counts_nested():
    text = ('GLEYSSON has the following entity data: '
            '[{Slot: 0b, id: "minecraft:oak_planks", Count: 40b}, '
            '{Slot: 1b, id: "backpacked:backpack", Count: 1b, tag: {Items: '
            '[{Slot: 0b, id: "minecraft:oak_planks", Count: 22b}]}}]')
    counts = crafting.parse_inventory_nbt(text)
    assert counts["minecraft:oak_planks"] == 62   # 40 loose + 22 in the backpack
    assert counts["backpacked:backpack"] == 1


def test_validation_rejects_bad_input():
    with pytest.raises(crafting.CraftError):
        crafting._check_name("not a name!")
    with pytest.raises(crafting.CraftError):
        crafting._check_pred("minecraft:dirt; op @a")


# ----------------------------------------------------------------- console-driven


class FakeConsole:
    """Duck-typed Console: scripts `clear`/`give`/`data get` replies by item."""

    def __init__(self, loose: dict[str, int], *, online: bool = True):
        self.loose = dict(loose)
        self.online = online
        self.sent: list[str] = []

    def send(self, cmd: str, *, timeout: float = 10.0) -> str:
        self.sent.append(cmd)
        parts = cmd.split()
        if parts[0] == "clear":
            _player, pred, n = parts[1], parts[2], int(parts[3])
            if not self.online:
                return "No entity was found"
            have = self.loose.get(pred, 0)
            if n == 0:
                return f"Found {have} matching items on {_player}"
            take = min(have, n)
            self.loose[pred] = have - take
            return f"Removed {take} matching items from player {_player}"
        if parts[0] == "give":
            return f"Gave {parts[3]} {parts[2]} to {parts[1]}"
        if cmd.startswith("data get entity"):
            return "no entity"
        return ""


def _cfg() -> Config:
    c = Config()
    c.crafting.player = "GLEYSSON"
    return c


def test_plan_and_craft_happy_path():
    rec = crafting.parse_recipe(SHAPED_CHEST, "minecraft:chest")
    con = FakeConsole({"minecraft:oak_planks": 40})   # needs 8 per chest -> 5 craftable
    plan = crafting.plan_craft(con, _cfg(), rec, count=2)
    assert plan.online and plan.craftable == 5 and plan.will_craft == 2
    assert plan.output_count == 2

    res = crafting.craft(con, _cfg(), rec, count=2)
    assert res.ok and res.crafted == 2 and res.output_count == 2
    assert con.loose["minecraft:oak_planks"] == 40 - 16   # 8*2 consumed
    assert any(s.startswith("give GLEYSSON minecraft:chest 2") for s in con.sent)


def test_craft_max_capped_to_one_stack():
    rec = crafting.parse_recipe(SHAPELESS_TORCH, "minecraft:torch")  # makes 4 each
    con = FakeConsole({"minecraft:coal": 999, "minecraft:stick": 999})
    plan = crafting.plan_craft(con, _cfg(), rec, count=None)         # hold-to-max
    # 64-stack cap / 4 per craft = 16 crafts max
    assert plan.cap == 16 and plan.will_craft == 16 and plan.limited_by == "stack"
    assert plan.output_count == 64


def test_craft_refuses_without_materials():
    rec = crafting.parse_recipe(SHAPED_CHEST, "minecraft:chest")
    con = FakeConsole({"minecraft:oak_planks": 3})   # < 8 needed
    plan = crafting.plan_craft(con, _cfg(), rec, count=1)
    assert plan.craftable == 0 and plan.will_craft == 0
    with pytest.raises(crafting.CraftError, match="not enough materials"):
        crafting.craft(con, _cfg(), rec, count=1)


def test_offline_player_plans_but_cannot_craft():
    rec = crafting.parse_recipe(SHAPED_CHEST, "minecraft:chest")
    con = FakeConsole({}, online=False)
    plan = crafting.plan_craft(con, _cfg(), rec, count=1)
    assert plan.online is False and plan.will_craft == 0
    with pytest.raises(crafting.CraftError, match="not online"):
        crafting.craft(con, _cfg(), rec, count=1)


def test_source_and_receiver_split():
    rec = crafting.parse_recipe(SHAPED_CHEST, "minecraft:chest")
    con = FakeConsole({"minecraft:oak_planks": 16})
    cfg = _cfg()
    res = crafting.craft(con, cfg, rec, count=1, source="STORAGE", receiver="GLEYSSON")
    assert res.ok
    assert any(s.startswith("clear STORAGE ") for s in con.sent)
    assert any(s.startswith("give GLEYSSON ") for s in con.sent)
