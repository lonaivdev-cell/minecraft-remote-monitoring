"""Recipe browser + survival-safe "command-craft" for the phone/CLI.

The dream this adapts: pick a recipe on your phone and have it crafted for you,
optionally maxed out to one stack. mcctl is a *server-ops* tool — it talks to the
server over RCON/console, it does **not** run inside your game client, so it can't
reach into your open crafting GUI and pre-fill the grid (that's a client mod's job,
e.g. JEI/REI's recipe-transfer button). What it *can* do, and what this module does,
is reproduce the **outcome** entirely through console commands:

  * **Browse** every shaped/shapeless crafting recipe the pack defines — read
    straight out of the mod jars and the world datapacks, exactly like `mods.py`
    reads jar descriptors (one server-side python3 pass; parsing is local + tested).
  * **Plan** a craft against a player's live inventory: how many can they make right
    now, what's the limiting ingredient, what would the output be.
  * **Craft** it for real — consume the inputs with `/clear` and grant the output
    with `/give`. Because `/clear` only ever touches *loose* inventory slots (never
    items nested inside a Backpacked backpack), counting and consuming are inherently
    survival-honest: we can only remove what the player actually, accessibly has.

Two amounts the UI distinguishes (mapping the "tap vs hold >3s" gesture):
  * **tap** → craft `count` (default 1).
  * **hold** → craft the maximum the materials allow, capped at one output stack
    (`[crafting].max_output_stack`, default 64) — "the biggest amount, limited to the
    stack".

Tags (`#minecraft:planks`) are passed to `/clear` verbatim — it accepts an item
*predicate*, so a `#planks` slot counts and consumes any planks, just like the grid
would. Backpack/container contents are surfaced for *planning only* (a best-effort
NBT scan) and clearly flagged as not auto-consumable.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from . import util
from .config import Config
from .console import Console
from .transport import BaseTransport, q

log = util.get_logger("crafting")

CRAFT_SHAPED = "minecraft:crafting_shaped"
CRAFT_SHAPELESS = "minecraft:crafting_shapeless"

# The single-ingredient "cook" family: vanilla recipe type id -> our short rtype.
COOKING_TYPES = {
    "minecraft:smelting": "smelting",
    "minecraft:blasting": "blasting",
    "minecraft:smoking": "smoking",
    "minecraft:campfire_cooking": "campfire",
}

# Every vanilla *data-driven* recipe type we render, EMI-style. Mod machine
# recipes (create:mixing, mekanism:*, …) use bespoke types + per-mod plugins —
# exactly like EMI needs an addon to show them — so they're out of scope here.
RECIPE_TYPES = (
    CRAFT_SHAPED, CRAFT_SHAPELESS, *COOKING_TYPES,
    "minecraft:stonecutting", "minecraft:smithing_transform",
)

# short rtype -> the high-level category the UI groups by (its EMI "tab").
_CATEGORY = {
    "shaped": "crafting", "shapeless": "crafting",
    "smelting": "smelting", "blasting": "blasting",
    "smoking": "smoking", "campfire": "campfire",
    "stonecutting": "stonecutting", "smithing": "smithing",
}

# Back-compat alias for callers that only meant the crafting-grid pair.
CRAFT_TYPES = (CRAFT_SHAPED, CRAFT_SHAPELESS)

# A Minecraft username: 1-16 of [A-Za-z0-9_]. Validated before it ever reaches a
# console command so a player name can never smuggle in extra console syntax.
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
# An item id or a #tag predicate, e.g. "minecraft:oak_planks" or "#c:planks".
_PRED_RE = re.compile(r"^#?[a-z0-9_.-]+:[a-z0-9_./-]+$")


class CraftError(RuntimeError):
    pass


# ================================================================ recipe model


@dataclass(slots=True)
class Recipe:
    rid: str                       # "minecraft:chest"
    rtype: str                     # "shaped"|"shapeless"|"smelting"|…|"smithing"
    result_item: str               # "minecraft:chest"
    result_count: int              # 1
    slots: list[list[str]]         # one entry per non-empty input slot; each = OK predicates
    pattern: list[str] = field(default_factory=list)  # shaped rows, for display ("" rows ok)
    source: str = ""               # jar / datapack it came from
    category: str = "crafting"     # EMI-style tab: crafting|smelting|…|smithing
    cooking_time: int = 0          # ticks to cook (cook family only; 0 otherwise)
    experience: float = 0.0        # xp granted (cook family only; 0 otherwise)

    def requirements(self) -> list[tuple[list[str], int]]:
        """Distinct ingredient -> how many of it one craft consumes."""
        counts = Counter(tuple(s) for s in self.slots if s)
        return [(list(preds), n) for preds, n in sorted(counts.items())]

    def to_dict(self) -> dict:
        d = {
            "id": self.rid, "type": self.rtype, "category": self.category,
            "source": self.source,
            "result_item": self.result_item, "result_count": self.result_count,
            "pattern": self.pattern,
            "ingredients": [{"options": preds, "per_craft": n}
                            for preds, n in self.requirements()],
        }
        if self.cooking_time:
            d["cooking_time"] = self.cooking_time
        if self.experience:
            d["experience"] = self.experience
        return d


def _ingredient_predicates(entry: object) -> list[str]:
    """Normalize one recipe ingredient into `/clear`-usable predicates.

    Handles every shape the vanilla format throws at us across 1.20.x/1.21:
      {"item": "x"} | {"tag": "y"} | {"id": "x"} | "x" | "#y" | a list of any.
    """
    out: list[str] = []
    if entry is None:
        return out
    if isinstance(entry, str):
        out.append(entry)                       # already "item" or "#tag"
    elif isinstance(entry, list):
        for e in entry:
            out.extend(_ingredient_predicates(e))
    elif isinstance(entry, dict):
        if entry.get("item"):
            out.append(str(entry["item"]))
        elif entry.get("tag"):
            out.append("#" + str(entry["tag"]))
        elif entry.get("id"):                   # 1.20.5+ spelling
            out.append(str(entry["id"]))
    # de-dup, preserve order, keep only well-formed predicates
    seen: dict[str, None] = {}
    for p in out:
        if _PRED_RE.match(p):
            seen.setdefault(p, None)
    return list(seen)


def _result_of(d: dict) -> tuple[str, int]:
    res = d.get("result")
    if isinstance(res, str):
        return res, 1
    if isinstance(res, dict):
        item = res.get("item") or res.get("id") or ""
        return str(item), int(res.get("count", 1) or 1)
    return "", 1


def parse_recipe(d: dict, rid: str, source: str = "") -> Recipe | None:
    """Build a Recipe from raw recipe JSON, or None if it isn't a type we render.

    Covers every vanilla data-driven category EMI shows: the crafting grid
    (shaped/shapeless), the cook family (smelting/blasting/smoking/campfire),
    stonecutting, and smithing-transform. Each ends up as the same `Recipe`
    (inputs as `slots`, one output), so the plan/craft engine works for all of
    them unchanged — a "smelt" or "stonecut" reproduces its outcome with the
    same survival-honest /clear+/give as a craft does.
    """
    rtype = d.get("type", "")
    if rtype in (CRAFT_SHAPED, CRAFT_SHAPELESS):
        return _parse_crafting(d, rid, source, rtype)
    if rtype in COOKING_TYPES:
        return _parse_cooking(d, rid, source, rtype)
    if rtype == "minecraft:stonecutting":
        return _parse_stonecutting(d, rid, source)
    if rtype == "minecraft:smithing_transform":
        return _parse_smithing(d, rid, source)
    return None


def _parse_crafting(d: dict, rid: str, source: str, rtype: str) -> Recipe | None:
    result_item, result_count = _result_of(d)
    if not result_item:
        return None
    slots: list[list[str]] = []
    pattern: list[str] = []
    if rtype == CRAFT_SHAPED:
        key = d.get("key", {})
        pattern = [str(r) for r in d.get("pattern", [])]
        for row in pattern:
            for ch in row:
                if ch == " ":
                    continue
                preds = _ingredient_predicates(key.get(ch))
                if preds:
                    slots.append(preds)
    else:  # shapeless
        for entry in d.get("ingredients", []):
            preds = _ingredient_predicates(entry)
            if preds:
                slots.append(preds)
    if not slots:
        return None
    short = "shaped" if rtype == CRAFT_SHAPED else "shapeless"
    return Recipe(rid=rid, rtype=short, result_item=result_item,
                  result_count=result_count, slots=slots, pattern=pattern,
                  source=source, category="crafting")


def _parse_cooking(d: dict, rid: str, source: str, rtype: str) -> Recipe | None:
    preds = _ingredient_predicates(d.get("ingredient"))
    result_item, _ = _result_of(d)               # cook output is always 1
    if not preds or not result_item:
        return None
    short = COOKING_TYPES[rtype]
    return Recipe(rid=rid, rtype=short, result_item=result_item, result_count=1,
                  slots=[preds], source=source, category=_CATEGORY[short],
                  cooking_time=int(d.get("cookingtime", 0) or 0),
                  experience=float(d.get("experience", 0) or 0))


def _parse_stonecutting(d: dict, rid: str, source: str) -> Recipe | None:
    preds = _ingredient_predicates(d.get("ingredient"))
    result_item, count = _result_of(d)
    if isinstance(d.get("result"), str):          # legacy: count is top-level
        count = int(d.get("count", 1) or 1)
    if not preds or not result_item:
        return None
    return Recipe(rid=rid, rtype="stonecutting", result_item=result_item,
                  result_count=count, slots=[preds], source=source,
                  category="stonecutting")


def _parse_smithing(d: dict, rid: str, source: str) -> Recipe | None:
    result_item, count = _result_of(d)
    if not result_item:                           # smithing_trim has no item result -> skip
        return None
    slots: list[list[str]] = []
    for k in ("template", "base", "addition"):
        preds = _ingredient_predicates(d.get(k))
        if preds:
            slots.append(preds)
    if not slots:
        return None
    return Recipe(rid=rid, rtype="smithing", result_item=result_item,
                  result_count=count, slots=slots, source=source,
                  category="smithing")


# ================================================================ remote listing

# Walks mod jars + world datapacks server-side and prints one TSV line per
# recipe: "==R<TAB>source<TAB>rid<TAB>result_item<TAB>compact_json". Datapacks are
# scanned first so a datapack override wins the de-dup. Filtering by `query`,
# skipping `offset` matches and capping at `limit` happen here so a client can
# page the whole pack without ever shipping more than one screenful at a time.
_PY_RECIPE_LISTER = r'''
import json, os, sys, zipfile

mods_dir, packs_dir, query, limit = sys.argv[1], sys.argv[2], sys.argv[3].lower(), int(sys.argv[4])
offset = int(sys.argv[5]) if len(sys.argv) > 5 else 0
CRAFT = ("minecraft:crafting_shaped", "minecraft:crafting_shapeless",
         "minecraft:smelting", "minecraft:blasting", "minecraft:smoking",
         "minecraft:campfire_cooking", "minecraft:stonecutting",
         "minecraft:smithing_transform")
seen = set()
emitted = [0]
hits = [0]
truncated = [False]

def rid_from(rel):
    # data/<ns>/recipe(s)/<path>.json -> <ns>:<path>
    parts = rel.split("/")
    if len(parts) < 4 or parts[0] != "data" or parts[2] not in ("recipe", "recipes"):
        return None
    if not parts[-1].endswith(".json"):
        return None
    name = "/".join(parts[3:])[:-5]
    return parts[1] + ":" + name

def result_id(d):
    r = d.get("result")
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        return str(r.get("item") or r.get("id") or "")
    return ""

def consider(source, rel, data):
    rid = rid_from(rel)
    if not rid or rid in seen:
        return
    try:
        d = json.loads(data)
    except Exception:
        return
    if not isinstance(d, dict) or d.get("type") not in CRAFT:
        return
    res = result_id(d)
    if not res:
        return
    if query and query not in (rid.lower() + " " + res.lower()):
        return
    seen.add(rid)                       # dedup before paging so offset is stable
    hits[0] += 1
    if hits[0] <= offset:               # this match belongs to an earlier page
        return
    if emitted[0] >= limit:
        truncated[0] = True
        return
    compact = json.dumps(d, separators=(",", ":")).replace("\t", " ").replace("\n", " ")
    sys.stdout.write("==R\t%s\t%s\t%s\t%s\n" % (source, rid, res, compact))
    emitted[0] += 1

def scan_zip(path, source):
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return
    for n in z.namelist():
        if n.startswith("data/") and n.endswith(".json") and ("/recipe/" in n or "/recipes/" in n):
            try:
                consider(source, n, z.read(n))
            except Exception:
                pass

def scan_dir(root, source):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.endswith(".json"):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if "/recipe/" in "/" + rel or "/recipes/" in "/" + rel:
                try:
                    with open(full, "rb") as fh:
                        consider(source, rel, fh.read())
                except Exception:
                    pass

# datapacks first (they override), then mods
if os.path.isdir(packs_dir):
    for entry in sorted(os.listdir(packs_dir)):
        p = os.path.join(packs_dir, entry)
        if os.path.isdir(p):
            scan_dir(p, "datapack:" + entry)
        elif entry.endswith(".zip"):
            scan_zip(p, "datapack:" + entry)
if os.path.isdir(mods_dir):
    for entry in sorted(os.listdir(mods_dir)):
        if entry.endswith(".jar"):
            scan_zip(os.path.join(mods_dir, entry), entry)

if truncated[0]:
    sys.stdout.write("==TRUNC\n")
'''


def _datapacks_dir(cfg: Config) -> str:
    return f"{cfg.server.server_dir}/{cfg.server.world_dir}/datapacks"


def parse_recipe_listing(out: str) -> tuple[list[Recipe], bool]:
    """Parse the ==R / ==TRUNC stream into Recipes (+ truncated flag)."""
    recipes: list[Recipe] = []
    truncated = False
    for line in out.splitlines():
        if line == "==TRUNC":
            truncated = True
            continue
        if not line.startswith("==R\t"):
            continue
        parts = line[4:].split("\t", 3)
        if len(parts) != 4:
            continue
        source, rid, _result, raw = parts
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        rec = parse_recipe(d, rid, source)
        if rec:
            recipes.append(rec)
    return recipes, truncated


def search_recipes(t: BaseTransport, cfg: Config, query: str = "",
                   limit: int = 60, offset: int = 0) -> tuple[list[Recipe], bool]:
    """Recipes whose id/result match `query` (substring), newest-pack-wins.

    `offset` skips that many matches first, so a client can page the entire pack
    (empty query) into an offline cache, EMI-style, a screenful per round-trip.
    """
    limit = max(1, min(int(limit), 5000))
    offset = max(0, int(offset))
    script = (
        f"mods={q(cfg.server.server_dir + '/mods')}\n"
        f"packs={q(_datapacks_dir(cfg))}\n"
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - \"$mods\" \"$packs\" {q(query)} {q(str(limit))} {q(str(offset))} <<'MCCTL_PY'\n"
        f"{_PY_RECIPE_LISTER}\nMCCTL_PY\n"
    )
    r = t.run(script, timeout=120)
    if "==NOPY" in r.out:
        raise CraftError("the server has no python3 — recipe extraction needs it "
                         "(same requirement as `mcctl mods`)")
    return parse_recipe_listing(util.sanitize_terminal(r.out))


def get_recipe(t: BaseTransport, cfg: Config, rid: str) -> Recipe:
    """Fetch exactly one recipe by id (e.g. "minecraft:chest")."""
    recipes, _ = search_recipes(t, cfg, query=rid.lower(), limit=200)
    for rec in recipes:
        if rec.rid == rid:
            return rec
    # query is a substring match; a unique hit is still the answer the user meant
    hits = [r for r in recipes if rid in r.rid]
    if len(hits) == 1:
        return hits[0]
    raise CraftError(f"no crafting recipe with id {rid!r}"
                     + (f" ({len(hits)} partial matches — be more specific)" if hits else ""))


# ================================================================ tag resolution

# A `#tag` ingredient (e.g. `#minecraft:planks`) is consumed/counted natively by the
# `/clear` predicate, but it reads opaquely in a UI ingredient list. This resolves a tag
# to the concrete items it stands for, by scanning the same jars + datapacks the recipe
# browser does for `data/<ns>/tags/item(s)/<path>.json`. Item tags merge across every
# source (vanilla + mods + datapacks all add to `minecraft:planks`) and an entry may be
# `#another:tag`, so the merge/recursion lives in the *pure*, tested half; the remote
# script only emits one TSV line per item-tag file it finds:
#   ==G<TAB><ns>:<path><TAB><replace 0|1><TAB><compact json array of string entries>
# Mods are scanned first, datapacks last, so a datapack's `"replace": true` wins.
_PY_TAG_LISTER = r'''
import json, os, sys, zipfile

mods_dir, packs_dir = sys.argv[1], sys.argv[2]

def key_from(rel):
    # data/<ns>/tags/item(s)/<path>.json -> "<ns>:<path>"
    parts = rel.split("/")
    if len(parts) < 5 or parts[0] != "data" or parts[2] != "tags":
        return None
    if parts[3] not in ("item", "items") or not parts[-1].endswith(".json"):
        return None
    return parts[1] + ":" + "/".join(parts[4:])[:-5]

def emit(rel, data):
    k = key_from(rel)
    if not k:
        return
    try:
        d = json.loads(data)
    except Exception:
        return
    if not isinstance(d, dict):
        return
    vals = []
    for v in d.get("values", []):
        if isinstance(v, str):
            vals.append(v)
        elif isinstance(v, dict):
            x = v.get("id")
            if isinstance(x, str):
                vals.append(x)
    rep = "1" if d.get("replace") else "0"
    sys.stdout.write("==G\t%s\t%s\t%s\n" % (k, rep, json.dumps(vals, separators=(",", ":"))))

def scan_zip(path):
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return
    for n in z.namelist():
        if n.startswith("data/") and n.endswith(".json") and "/tags/item" in n:
            try:
                emit(n, z.read(n))
            except Exception:
                pass

def scan_dir(root):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.endswith(".json"):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if "/tags/item" in "/" + rel:
                try:
                    with open(full, "rb") as fh:
                        emit(rel, fh.read())
                except Exception:
                    pass

# mods first, datapacks last (so a datapack "replace" overrides), matching load order
if os.path.isdir(mods_dir):
    for entry in sorted(os.listdir(mods_dir)):
        if entry.endswith(".jar"):
            scan_zip(os.path.join(mods_dir, entry))
if os.path.isdir(packs_dir):
    for entry in sorted(os.listdir(packs_dir)):
        p = os.path.join(packs_dir, entry)
        if os.path.isdir(p):
            scan_dir(p)
        elif entry.endswith(".zip"):
            scan_zip(p)
'''


def _norm_item(entry: str) -> str:
    """A bare item id gains the default `minecraft:` namespace; tags keep their `#`."""
    if entry.startswith("#") or ":" in entry:
        return entry
    return "minecraft:" + entry


def parse_tag_listing(out: str) -> dict[str, list[str]]:
    """Merge the ==G stream into `{"ns:path": [entry, …]}` in emit (load) order.

    Item tags are additive across sources; a file with `"replace": true` resets the
    tag it owns before its values apply. Entries stay raw (`#nested:tag` kept) — the
    recursion into concrete items happens in `resolve_tag_map`.
    """
    tags: dict[str, list[str]] = {}
    for line in out.splitlines():
        if not line.startswith("==G\t"):
            continue
        parts = line[4:].split("\t", 2)
        if len(parts) != 3:
            continue
        key, replace, raw = parts
        try:
            vals = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(vals, list):
            continue
        cur = [] if (replace == "1" or key not in tags) else tags[key]
        for v in vals:
            if isinstance(v, str) and v not in cur:
                cur.append(v)
        tags[key] = cur
    return tags


def resolve_tag_map(tags: dict[str, list[str]], tag: str) -> list[str]:
    """Expand one tag to its concrete item ids, following nested `#tags`, cycle-safe."""
    out: list[str] = []
    seen: set[str] = set()

    def walk(name: str) -> None:
        name = name.lstrip("#")
        if ":" not in name:
            name = "minecraft:" + name
        if name in seen:
            return
        seen.add(name)
        for entry in tags.get(name, []):
            if entry.startswith("#"):
                walk(entry)
            else:
                item = _norm_item(entry)
                if item not in out:
                    out.append(item)

    walk(tag)
    return out


def resolve_tag(t: BaseTransport, cfg: Config, tag: str) -> list[str]:
    """Concrete items a `#tag` ingredient stands for (e.g. `#minecraft:planks`)."""
    script = (
        f"mods={q(cfg.server.server_dir + '/mods')}\n"
        f"packs={q(_datapacks_dir(cfg))}\n"
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - \"$mods\" \"$packs\" <<'MCCTL_PY'\n"
        f"{_PY_TAG_LISTER}\nMCCTL_PY\n"
    )
    r = t.run(script, timeout=120)
    if "==NOPY" in r.out:
        raise CraftError("the server has no python3 — tag resolution needs it "
                         "(same requirement as `mcctl mods`)")
    tags = parse_tag_listing(util.sanitize_terminal(r.out))
    return resolve_tag_map(tags, tag)


# ================================================================ inventory probing


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise CraftError(f"invalid player name {name!r} (expected 1-16 of A-Z a-z 0-9 _)")
    return name


def _check_pred(pred: str) -> str:
    if not _PRED_RE.match(pred or ""):
        raise CraftError(f"refusing unsafe item predicate {pred!r}")
    return pred


_COUNT_RE = re.compile(r"(\d+)")


def _clear_count(out: str) -> int:
    """Parse `/clear ... 0` (probe) or `/clear ... N` (consume) replies to an int.

    Vanilla says "Found N matching item(s) on X" / "Removed N matching items from X"
    / "No items were found ...". We just take the first integer, 0 when there is none.
    """
    low = out.lower()
    if "no " in low and ("found" in low or "match" in low):
        return 0
    m = _COUNT_RE.search(out)
    return int(m.group(1)) if m else 0


def _is_offline(out: str) -> bool:
    low = out.lower()
    return ("no entity was found" in low or "no player was found" in low
            or "unknown" in low and "player" in low)


def probe_loose(console: Console, player: str, pred: str) -> int:
    """How many of `pred` the player has in *loose* (consumable) inventory."""
    out = console.send(f"clear {player} {_check_pred(pred)} 0", timeout=8)
    if _is_offline(out):
        raise CraftError(f"player {player!r} is not online (can't read their inventory)")
    return _clear_count(out)


def _slot_loose(console: Console, player: str, preds: list[str]) -> int:
    """Loose availability for one ingredient = sum over its acceptable predicates."""
    return sum(probe_loose(console, player, p) for p in preds)


# --- best-effort container/backpack scan (planning only, never consumed) ---

_ID_RE = re.compile(r'id:\s*"([a-z0-9_.:/-]+)"')
_NEAR_COUNT_RE = re.compile(r"[Cc]ount:\s*(\d+)b")


def parse_inventory_nbt(text: str) -> dict[str, int]:
    """Best-effort per-item totals from a `data get entity .. Inventory` dump.

    Pairs every `Count:Nb` with the nearest `id:"..."` in a small window, so it also
    sweeps items nested inside backpacks/shulkers. Approximate by design — it backs
    an informational "also in storage" figure, never a consume decision.
    """
    totals: Counter[str] = Counter()
    for m in _NEAR_COUNT_RE.finditer(text):
        count = int(m.group(1))
        window = text[max(0, m.start() - 80): m.end() + 80]
        ids = _ID_RE.findall(window)
        if not ids:
            continue
        # the id physically closest to this Count wins the pairing
        best = min(ids, key=lambda i: abs(window.find(f'"{i}"') - (m.start() - max(0, m.start() - 80))))
        totals[best] += count
    return dict(totals)


def stored_counts(console: Console, player: str) -> dict[str, int] | None:
    out = console.send(f"data get entity {player} Inventory", timeout=10)
    if not out or _is_offline(out) or "following entity data" not in out.lower():
        return None
    return parse_inventory_nbt(out)


# ================================================================ craft planning


@dataclass(slots=True)
class CraftPlan:
    recipe: Recipe
    source: str                 # whose inventory supplies materials
    receiver: str               # who gets the output
    online: bool
    requested: int | None       # None == "max" (hold-to-craft)
    craftable: int              # max craftable from loose materials right now
    cap: int                    # one-stack cap = max_output_stack // result_count
    will_craft: int             # what a do() would actually make
    limited_by: str             # "materials" | "stack" | "request" | "none"
    ingredients: list[dict]     # per ingredient: options, per_craft, loose, stored
    output_item: str
    output_count: int           # result_count * will_craft
    hold_ms: int = 3000         # UI hint: hold the craft button this long = craft-max

    def to_dict(self) -> dict:
        return {
            "recipe": self.recipe.to_dict(),
            "source": self.source, "receiver": self.receiver, "online": self.online,
            "requested": self.requested, "craftable": self.craftable, "cap": self.cap,
            "will_craft": self.will_craft, "limited_by": self.limited_by,
            "ingredients": self.ingredients,
            "output_item": self.output_item, "output_count": self.output_count,
            "hold_ms": self.hold_ms,
        }


def _decide_n(craftable: int, cap: int, requested: int | None) -> tuple[int, str]:
    """How many to actually craft, and what held us back."""
    if requested is None:                      # hold-to-max
        n = min(craftable, cap)
        if craftable == 0:
            return 0, "materials"
        return n, "stack" if cap < craftable else "materials"
    n = min(requested, craftable, cap)
    if n == 0:
        return 0, "materials"
    if n < requested:
        return n, "materials" if craftable <= cap else "stack"
    return n, "none"


def plan_craft(console: Console, cfg: Config, recipe: Recipe, *,
               count: int | None = 1, source: str = "", receiver: str = "",
               include_stored: bool | None = None) -> CraftPlan:
    """Probe live inventory and work out what a craft would do — no mutation."""
    cr = cfg.crafting
    source = _check_name(source or cr.source_player or cr.player)
    receiver = _check_name(receiver or cr.player)
    cap = max(1, cr.max_output_stack // max(1, recipe.result_count))
    if include_stored is None:
        include_stored = cr.include_containers

    online = True
    reqs = recipe.requirements()
    loose: dict[tuple[str, ...], int] = {}
    try:
        for preds, _per in reqs:
            loose[tuple(preds)] = _slot_loose(console, source, preds)
    except CraftError as e:
        if "not online" in str(e):
            online = False
        else:
            raise

    stored = stored_counts(console, source) if (include_stored and online) else None

    if online and reqs:
        craftable = min(loose[tuple(preds)] // per for preds, per in reqs)
    else:
        craftable = 0
    will, limited = _decide_n(craftable, cap, count) if online else (0, "materials")

    ingredients = []
    for preds, per in reqs:
        st = sum(stored.get(p, 0) for p in preds if not p.startswith("#")) if stored else None
        ingredients.append({
            "options": preds, "per_craft": per,
            "loose": loose.get(tuple(preds)) if online else None,
            "stored": st,
        })
    return CraftPlan(
        recipe=recipe, source=source, receiver=receiver, online=online,
        requested=count, craftable=craftable, cap=cap, will_craft=will,
        limited_by=limited, ingredients=ingredients,
        output_item=recipe.result_item, output_count=recipe.result_count * will,
        hold_ms=cr.hold_ms,
    )


# ================================================================ craft execution


@dataclass(slots=True)
class CraftResult:
    ok: bool
    crafted: int                # crafts actually performed
    output_item: str
    output_count: int           # items granted
    consumed: list[dict]        # [{"predicate": .., "removed": ..}]
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "crafted": self.crafted, "output_item": self.output_item,
                "output_count": self.output_count, "consumed": self.consumed,
                "detail": self.detail}


def _give(console: Console, player: str, item: str, n: int) -> str:
    return console.send(f"give {player} {_check_pred(item)} {n}", timeout=10)


def craft(console: Console, cfg: Config, recipe: Recipe, *, count: int | None = 1,
          source: str = "", receiver: str = "") -> CraftResult:
    """Consume inputs (`/clear`) and grant the output (`/give`), survival-honest.

    Re-probes immediately before consuming, then removes exactly what each craft
    needs. If a race trims an ingredient mid-consume, we grant only the fully-backed
    number of crafts (never more than we removed) — anti-dupe by construction.
    """
    plan = plan_craft(console, cfg, recipe, count=count, source=source,
                      receiver=receiver, include_stored=False)
    if not plan.online:
        raise CraftError(f"player {plan.source!r} is not online — can't craft from an "
                         "inventory we can't read")
    n = plan.will_craft
    if n <= 0:
        need = ", ".join(f"{per}x {' or '.join(preds)}" for preds, per in recipe.requirements())
        raise CraftError(f"not enough materials to craft {recipe.result_item} "
                         f"(need per craft: {need})")

    # Consume. Track what each ingredient actually yielded so we never over-grant.
    consumed: list[dict] = []
    removed_per_req: list[int] = []
    for preds, per in recipe.requirements():
        need = per * n
        got = 0
        for p in preds:
            if got >= need:
                break
            out = console.send(f"clear {plan.source} {_check_pred(p)} {need - got}", timeout=10)
            took = _clear_count(out)
            if took:
                consumed.append({"predicate": p, "removed": took})
                got += took
        removed_per_req.append(got // per)        # full crafts this ingredient backs

    effective = min(removed_per_req) if removed_per_req else 0
    detail = ""
    if effective < n:
        # a concurrent inventory change trimmed us; grant only what's fully backed
        detail = (f"requested {n} but materials shifted mid-craft — granted {effective} "
                  "(consumed inputs are not refunded)")
        log.warning("craft %s: %s", recipe.rid, detail)
    if effective <= 0:
        return CraftResult(False, 0, recipe.result_item, 0, consumed,
                           detail or "materials disappeared before crafting")

    out_n = recipe.result_count * effective
    _give(console, plan.receiver, recipe.result_item, out_n)
    return CraftResult(True, effective, recipe.result_item, out_n, consumed, detail)


# ================================================================ rendering (CLI)


def render_recipe(rec: Recipe) -> str:
    lines = [f"{rec.rid}  [{rec.rtype}]  ->  {rec.result_count}x {rec.result_item}"]
    if rec.source:
        lines.append(f"  from {rec.source}")
    if rec.pattern:
        lines.append("  pattern:")
        for row in rec.pattern:
            lines.append(f"    | {row} |")
    if rec.cooking_time:
        cook = f"  cook: {rec.cooking_time / 20:g}s"
        if rec.experience:
            cook += f", {rec.experience:g} xp"
        lines.append(cook)
    lines.append("  needs per craft:")
    for preds, n in rec.requirements():
        lines.append(f"    {n}x  {' / '.join(preds)}")
    return "\n".join(lines)
