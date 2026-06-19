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


def test_non_crafting_recipe_is_skipped():
    assert crafting.parse_recipe({"type": "minecraft:smelting",
                                  "result": "minecraft:iron_ingot"}, "x") is None


def test_listing_framing_and_dedup():
    import json as _j
    line1 = "==R\tmymod.jar\tmymod:gear\tmymod:gear\t" + _j.dumps(SHAPELESS_TORCH, separators=(",", ":"))
    line2 = "==R\tminecraft\tminecraft:chest\tminecraft:chest\t" + _j.dumps(SHAPED_CHEST, separators=(",", ":"))
    recipes, truncated = crafting.parse_recipe_listing(line1 + "\n" + line2 + "\n==TRUNC\n")
    assert truncated is True
    assert {r.rid for r in recipes} == {"mymod:gear", "minecraft:chest"}


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
