"""EMI-style item index: display names + icon textures, for the phone.

EMI is a *client* mod, so it has every loaded resource pack on hand — item
models, block models, textures, and the `en_us` lang file — and renders each
stack from them. mcctl is server-side, so to give the phone the same thing it
must read the same files EMI reads and ship the relevant ones down the SSH
channel. That's exactly what this module does, in two passes that mirror
`crafting.py`'s recipe scan:

  * **`load_assets`** runs one server-side python pass over the mod jars and the
    `resourcepacks/` dir and emits, per item/block model, its `parent` + `textures`,
    plus the `item.*`/`block.*` entries of every `en_us.json`. The *pure* half
    (`parse_asset_listing` → `resolve_icon` / `display_name` / `build_manifest`)
    turns that into a manifest: `{id, name, icon}` for every item the pack defines.
  * **`fetch_icons`** takes the texture ids from a manifest and returns the actual
    PNG bytes (base64 on the wire), so the app caches each icon once and renders
    offline — "the full list of files EMI reads, downloaded to your phone".

Resource packs override mods override vanilla, so every scan emits mods first
and resource packs last and the pure merge keeps the last writer — the same
load-order rule the recipe/tag scans already follow. Mods carry their own
`assets/`, so modded items resolve fully; vanilla icons need the client jar or a
resource pack present in `resourcepacks/` (the client jar ships no `assets/`).
"""

from __future__ import annotations

import base64

from . import util
from .config import Config
from .transport import BaseTransport, q

log = util.get_logger("assets")


class AssetError(RuntimeError):
    pass


# ============================================================ id / path helpers


def _norm_id(s: str) -> str:
    """Add the implicit `minecraft:` namespace to a bare id/texture/tag ref."""
    s = s.lstrip("#")
    return s if ":" in s else "minecraft:" + s


def texture_path(tex_id: str) -> str:
    """`ns:block/oak_planks` -> `assets/ns/textures/block/oak_planks.png`."""
    ns, _, path = _norm_id(tex_id).partition(":")
    return f"assets/{ns}/textures/{path}.png"


def _titlecase(path: str) -> str:
    words = path.replace("/", " ").replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words) if words else path


def display_name(item_id: str, lang: dict[str, str]) -> str:
    """The item's localized name (`item.minecraft.oak_planks` → "Oak Planks").

    Tries the `item.` then `block.` translation key; falls back to a title-cased
    id so the UI always has *something* readable even for an unlocalized item.
    """
    ns, _, path = _norm_id(item_id).partition(":")
    key = path.replace("/", ".")
    for prefix in ("item", "block"):
        name = lang.get(f"{prefix}.{ns}.{key}")
        if name:
            return name
    return _titlecase(path)


# ============================================================ icon resolution

# Which texture key in a (merged) model is the one to show as a flat icon, most
# specific first. Items use `layer0`; blocks expose a face under one of these.
# EMI bakes the real 3-D model; a representative face is the honest 2-D stand-in.
_FACE_PRIORITY = (
    "layer0", "layer1", "texture", "all", "particle", "side", "front",
    "top", "end", "cross", "fire", "lantern", "pane", "edge", "0", "1",
)


def _resolve_ref(val: str, merged: dict[str, str], depth: int = 0) -> str:
    """Follow a `#other_key` texture reference to a concrete texture id."""
    while val.startswith("#") and depth < 8:
        nxt = merged.get(val[1:])
        if not nxt:
            return ""
        val, depth = nxt, depth + 1
    return _norm_id(val) if val and not val.startswith("#") else ""


def resolve_icon(item_id: str, item_models: dict[str, dict],
                 block_models: dict[str, dict], *, max_depth: int = 16) -> str:
    """Resolve an item id to the texture id of its icon, or "" if unknown.

    Walks the model `parent` chain (an item model usually parents a block model
    for block items), merging `textures` child-wins, then picks the best face.
    Pure: it only reads the two model maps `parse_asset_listing` produced.
    """
    start = item_models.get(_norm_id(item_id))
    if start is None:
        return ""
    merged: dict[str, str] = {}
    cur: dict | None = start
    seen: set[str] = set()
    depth = 0
    while isinstance(cur, dict) and depth < max_depth:
        textures = cur.get("textures")
        if isinstance(textures, dict):
            for k, v in textures.items():
                if isinstance(v, str):
                    merged.setdefault(k, v)      # child (seen first) wins
        parent = cur.get("parent")
        if not isinstance(parent, str):
            break
        pid = _norm_id(parent)
        short = pid.split(":", 1)[1]
        if pid in seen or short.startswith("builtin/"):
            break
        seen.add(pid)
        cur = item_models.get(pid) or block_models.get(pid)   # vanilla roots → None → stop
        depth += 1
    for key in _FACE_PRIORITY:
        if key in merged:
            tex = _resolve_ref(merged[key], merged)
            if tex:
                return tex
    for val in merged.values():                  # nothing prioritized matched
        tex = _resolve_ref(val, merged)
        if tex:
            return tex
    return ""


# ============================================================ manifest assembly


def build_item_index(item_models: dict[str, dict], extra_items: tuple[str, ...] = ()) -> list[str]:
    """Every item the pack defines: one per `models/item/*.json`, plus any extra
    ids (e.g. recipe outputs whose model wasn't found), sorted & namespaced."""
    ids = set(item_models)
    ids.update(_norm_id(i) for i in extra_items if i)
    return sorted(ids)


def build_manifest(item_models: dict[str, dict], block_models: dict[str, dict],
                   lang: dict[str, str], *, extra_items: tuple[str, ...] = (),
                   query: str = "") -> list[dict]:
    """The EMI item list: `[{"id", "name", "icon"}, …]`, id-sorted.

    `icon` is a texture id (`ns:item/…`) the client passes back to `fetch_icons`;
    it's "" when no model/texture could be resolved (the UI shows a placeholder).
    `query` filters by id or display-name substring (case-insensitive).
    """
    ql = query.lower()
    out: list[dict] = []
    for item_id in build_item_index(item_models, extra_items):
        name = display_name(item_id, lang)
        if ql and ql not in item_id.lower() and ql not in name.lower():
            continue
        out.append({"id": item_id, "name": name,
                    "icon": resolve_icon(item_id, item_models, block_models)})
    return out


# ============================================================ remote: model scan

# One TSV line per item/block model and per lang file, like the recipe scanner:
#   ==M<TAB>item|block<TAB><ns:path><TAB><compact {parent,textures}>
#   ==L<TAB><compact {translation_key: name} of item.*/block.* entries>
# Mods first, resource packs last, so the pure merge lets a pack override a mod.
_PY_ASSET_LISTER = r'''
import json, os, sys, zipfile

mods_dir, packs_dir = sys.argv[1], sys.argv[2]

def emit(tag, *cols):
    sys.stdout.write(tag + "\t" + "\t".join(c.replace("\t", " ").replace("\n", " ") for c in cols) + "\n")

def take_model(rel, data):
    parts = rel.split("/")
    if len(parts) < 5 or parts[0] != "assets" or parts[2] != "models":
        return
    kind = parts[3]
    if kind not in ("item", "block") or not parts[-1].endswith(".json"):
        return
    mid = parts[1] + ":" + "/".join(parts[4:])[:-5]
    try:
        d = json.loads(data)
    except Exception:
        return
    if not isinstance(d, dict):
        return
    slim = {}
    if isinstance(d.get("parent"), str):
        slim["parent"] = d["parent"]
    if isinstance(d.get("textures"), dict):
        slim["textures"] = {k: v for k, v in d["textures"].items() if isinstance(v, str)}
    if "parent" in slim or slim.get("textures"):
        emit("==M", kind, mid, json.dumps(slim, separators=(",", ":")))

def take_lang(data):
    try:
        d = json.loads(data)
    except Exception:
        return
    if not isinstance(d, dict):
        return
    keep = {k: v for k, v in d.items()
            if isinstance(k, str) and isinstance(v, str)
            and (k.startswith("item.") or k.startswith("block."))}
    if keep:
        emit("==L", json.dumps(keep, separators=(",", ":")))

def consider(rel, reader):
    if rel.endswith(".json") and "/models/item/" in "/" + rel:
        try: take_model(rel, reader())
        except Exception: pass
    elif rel.endswith(".json") and "/models/block/" in "/" + rel:
        try: take_model(rel, reader())
        except Exception: pass
    elif rel.endswith("/lang/en_us.json"):
        try: take_lang(reader())
        except Exception: pass

def wanted(n):
    return (n.startswith("assets/") and
            (("/models/item/" in n or "/models/block/" in n) and n.endswith(".json")
             or n.endswith("/lang/en_us.json")))

def scan_zip(path):
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return
    for n in z.namelist():
        if wanted(n):
            consider(n, lambda n=n: z.read(n))

def scan_dir(root):
    for dp, _dirs, files in os.walk(root):
        for f in files:
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if wanted(rel):       # resource pack layout: <root>/assets/<ns>/…
                consider(rel, lambda full=full: open(full, "rb").read())

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


def parse_asset_listing(out: str) -> tuple[dict[str, str], dict[str, dict], dict[str, dict]]:
    """Merge the ==L/==M stream into (lang, item_models, block_models).

    Later writers win (resource packs are emitted last), matching load order.
    """
    import json

    lang: dict[str, str] = {}
    item_models: dict[str, dict] = {}
    block_models: dict[str, dict] = {}
    for line in out.splitlines():
        if line.startswith("==L\t"):
            try:
                d = json.loads(line[4:])
            except ValueError:
                continue
            if isinstance(d, dict):
                lang.update({k: v for k, v in d.items() if isinstance(v, str)})
        elif line.startswith("==M\t"):
            parts = line[4:].split("\t", 2)
            if len(parts) != 3:
                continue
            kind, mid, raw = parts
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                (item_models if kind == "item" else block_models)[mid] = d
    return lang, item_models, block_models


def _asset_dirs(cfg: Config) -> tuple[str, str]:
    base = cfg.server.server_dir
    return f"{base}/mods", f"{base}/resourcepacks"


def load_assets(t: BaseTransport, cfg: Config) -> tuple[dict[str, str], dict[str, dict], dict[str, dict]]:
    """Scan mod jars + resourcepacks for lang + item/block models (one remote pass)."""
    mods, packs = _asset_dirs(cfg)
    script = (
        f"mods={q(mods)}\n"
        f"packs={q(packs)}\n"
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - \"$mods\" \"$packs\" <<'MCCTL_PY'\n{_PY_ASSET_LISTER}\nMCCTL_PY\n"
    )
    r = t.run(script, timeout=180)
    if "==NOPY" in r.out:
        raise AssetError("the server has no python3 — asset extraction needs it "
                         "(same requirement as `mcctl mods`)")
    return parse_asset_listing(util.sanitize_terminal(r.out))


# ============================================================ remote: icon bytes

# Reads each wanted texture's PNG and base64-emits it: "==P<TAB>tex_id<TAB><b64>".
# WANTED (a {relative_path: texture_id} map) is injected ahead of this body, so a
# single pass over the jars/packs serves a whole batch. Resource packs are scanned
# last, so an overriding pack's PNG is what the client keeps.
_PY_ICON_FETCHER = r'''
mods_dir, packs_dir = sys.argv[1], sys.argv[2]
MAXB = 262144   # skip absurdly large textures; item icons are a few hundred bytes

def emit(texid, b):
    if 0 < len(b) <= MAXB:
        sys.stdout.write("==P\t" + texid + "\t" + base64.b64encode(b).decode() + "\n")

def scan_zip(path):
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return
    names = set(z.namelist())
    for rel, texid in WANTED.items():
        if rel in names:
            try: emit(texid, z.read(rel))
            except Exception: pass

def scan_dir(root):
    for rel, texid in WANTED.items():
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            try:
                with open(full, "rb") as fh:
                    emit(texid, fh.read())
            except Exception:
                pass

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


def parse_icon_listing(out: str) -> dict[str, bytes]:
    """Decode the ==P stream into `{texture_id: png_bytes}` (last writer wins)."""
    icons: dict[str, bytes] = {}
    for line in out.splitlines():
        if not line.startswith("==P\t"):
            continue
        parts = line[4:].split("\t", 1)
        if len(parts) != 2:
            continue
        texid, b64 = parts
        try:
            icons[texid] = base64.b64decode(b64)
        except ValueError:        # binascii.Error subclasses ValueError
            continue
    return icons


def fetch_icons(t: BaseTransport, cfg: Config, textures: list[str]) -> dict[str, bytes]:
    """PNG bytes for each texture id, read from the mod jars / resource packs.

    The wanted set is inlined into the remote program as a JSON literal — the
    heredoc is single-quoted, so the bytes reach python verbatim, and stdin stays
    free for the heredoc itself (no second channel needed).
    """
    import json

    want = {texture_path(x): _norm_id(x) for x in textures if x}
    if not want:
        return {}
    mods, packs = _asset_dirs(cfg)
    program = ("import base64, json, os, sys, zipfile\n"
               f"WANTED = json.loads({json.dumps(json.dumps(want))})\n"
               + _PY_ICON_FETCHER)
    script = (
        f"mods={q(mods)}\n"
        f"packs={q(packs)}\n"
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - \"$mods\" \"$packs\" <<'MCCTL_PY'\n{program}\nMCCTL_PY\n"
    )
    r = t.run(script, timeout=180)
    if "==NOPY" in r.out:
        raise AssetError("the server has no python3 — icon extraction needs it")
    return parse_icon_listing(r.out)
