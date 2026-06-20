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
import re

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

vanilla_dir, mods_dir, packs_dir = sys.argv[1], sys.argv[2], sys.argv[3]

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

for jar_dir in (vanilla_dir, mods_dir):     # vanilla client jar (lowest) then mods
    if os.path.isdir(jar_dir):
        for entry in sorted(os.listdir(jar_dir)):
            if entry.endswith(".jar"):
                scan_zip(os.path.join(jar_dir, entry))
if os.path.isdir(packs_dir):                # resource packs override (highest)
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


def vanilla_cache_dir(cfg: Config) -> str:
    """Where `assets.sync` caches the Mojang client jar (scanned at lowest priority)."""
    return f"{cfg.server.server_dir}/.mcctl/vanilla"


def _asset_dirs(cfg: Config) -> tuple[str, str, str]:
    """(vanilla, mods, resourcepacks) — scanned in that order so packs > mods > vanilla."""
    base = cfg.server.server_dir
    return vanilla_cache_dir(cfg), f"{base}/mods", f"{base}/resourcepacks"


def load_assets(t: BaseTransport, cfg: Config) -> tuple[dict[str, str], dict[str, dict], dict[str, dict]]:
    """Scan the vanilla jar + mod jars + resourcepacks for lang + models (one pass)."""
    vanilla, mods, packs = _asset_dirs(cfg)
    script = (
        f"vanilla={q(vanilla)}\n"
        f"mods={q(mods)}\n"
        f"packs={q(packs)}\n"
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - \"$vanilla\" \"$mods\" \"$packs\" <<'MCCTL_PY'\n{_PY_ASSET_LISTER}\nMCCTL_PY\n"
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
vanilla_dir, mods_dir, packs_dir = sys.argv[1], sys.argv[2], sys.argv[3]
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

for jar_dir in (vanilla_dir, mods_dir):     # vanilla client jar (lowest) then mods
    if os.path.isdir(jar_dir):
        for entry in sorted(os.listdir(jar_dir)):
            if entry.endswith(".jar"):
                scan_zip(os.path.join(jar_dir, entry))
if os.path.isdir(packs_dir):                # resource packs override (highest)
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
    vanilla, mods, packs = _asset_dirs(cfg)
    program = ("import base64, json, os, sys, zipfile\n"
               f"WANTED = json.loads({json.dumps(json.dumps(want))})\n"
               + _PY_ICON_FETCHER)
    script = (
        f"vanilla={q(vanilla)}\n"
        f"mods={q(mods)}\n"
        f"packs={q(packs)}\n"
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - \"$vanilla\" \"$mods\" \"$packs\" <<'MCCTL_PY'\n{program}\nMCCTL_PY\n"
    )
    r = t.run(script, timeout=180)
    if "==NOPY" in r.out:
        raise AssetError("the server has no python3 — icon extraction needs it")
    return parse_icon_listing(r.out)


# ============================================================ vanilla client jar
#
# A Minecraft *server* ships no client assets/ — only mods carry their own — so
# vanilla items (minecraft:*) have no icon or name until we fetch the matching
# Mojang *client* jar, which does contain assets/minecraft/{models,textures,lang}.
# `assets.sync` caches it under vanilla_cache_dir(), where the scans above look
# first (lowest priority). Following the "brain on the box" design, every network
# fetch runs on the server over the transport (it always has internet, and the jar
# has to land there anyway); the smart part — picking the right version's download
# out of Mojang's manifest — stays here as pure, tested functions.

MOJANG_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

_RELEASE_RE = re.compile(r"^1\.\d+(?:\.\d+)?$")


def _is_release(v: str) -> bool:
    return bool(_RELEASE_RE.match(v))


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


# --- version detection (best-effort; [server].mc_version always overrides) ----

_PY_VERSION_PROBE = r'''
import glob, os, re, sys

srv = sys.argv[1]

def emit(src, v):
    sys.stdout.write("==VER\t%s\t%s\n" % (src, v))

server_libs = os.path.join(srv, "libraries", "net", "minecraft", "server")
if os.path.isdir(server_libs):
    for name in sorted(os.listdir(server_libs)):
        m = re.match(r"(1\.\d+(?:\.\d+)?)", name)
        if m:
            emit("lib", m.group(1))

candidates = [os.path.join(srv, "logs", "latest.log")]
candidates += sorted(glob.glob(os.path.join(srv, "logs", "*.log")))
done = set()
for lf in candidates:
    if lf in done:
        continue
    done.add(lf)
    if not os.path.isfile(lf):
        continue
    try:
        with open(lf, errors="ignore") as fh:
            head = fh.read(40000)
    except Exception:
        continue
    for m in re.findall(r"minecraft server version (1\.\d+(?:\.\d+)?)", head):
        emit("log", m)
    for m in re.findall(r"\(MC: (1\.\d+(?:\.\d+)?)\)", head):
        emit("log", m)
'''


def parse_version_probe(out: str) -> str:
    """Pick the MC version from the probe's ==VER lines: logs win over libraries."""
    by_src: dict[str, list[str]] = {"log": [], "lib": []}
    for line in out.splitlines():
        if line.startswith("==VER\t"):
            parts = line.split("\t")
            if len(parts) == 3 and parts[1] in by_src and _is_release(parts[2]):
                by_src[parts[1]].append(parts[2])
    for src in ("log", "lib"):               # logs are authoritative; libraries a fallback
        if by_src[src]:
            return max(set(by_src[src]), key=_version_key)
    return ""


def detect_mc_version(t: BaseTransport, cfg: Config) -> str:
    script = (
        f"srv={q(cfg.server.server_dir)}\n"
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - \"$srv\" <<'MCCTL_PY'\n{_PY_VERSION_PROBE}\nMCCTL_PY\n"
    )
    r = t.run(script, timeout=30)
    if "==NOPY" in r.out:
        return ""
    return parse_version_probe(util.sanitize_terminal(r.out))


def resolve_version(t: BaseTransport, cfg: Config, override: str = "") -> str:
    """Config/CLI override → [server].mc_version → server probe."""
    v = (override or cfg.server.mc_version or "").strip()
    return v or detect_mc_version(t, cfg)


# --- Mojang manifest selection (pure) -----------------------------------------


def pick_version_entry(manifest: dict, version: str) -> str:
    """The per-version metadata URL for `version` in the launcher manifest, or ""."""
    for v in manifest.get("versions", []):
        if isinstance(v, dict) and v.get("id") == version:
            url = v.get("url", "")
            return url if isinstance(url, str) else ""
    return ""


def client_download_of(version_json: dict) -> tuple[str, str, int]:
    """(url, sha1, size) of the `client` download in a per-version json, or ("","",0)."""
    dl = (version_json.get("downloads") or {}).get("client") or {}
    url, sha1, size = dl.get("url", ""), dl.get("sha1", ""), dl.get("size", 0)
    if isinstance(url, str) and url and isinstance(sha1, str):
        return url, sha1, size if isinstance(size, int) else 0
    return "", "", 0


# --- server-side fetch + verified download ------------------------------------

_PY_FETCH_TEXT = r'''
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=30) as r:
    sys.stdout.buffer.write(r.read())
'''

_PY_DOWNLOAD = r'''
import hashlib, os, sys, urllib.request

url, dest, want, force = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"

def sha1_of(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

if os.path.isfile(dest) and not force:
    have = sha1_of(dest)
    if not want or have == want:
        sys.stdout.write("==V\tpresent\t%s\n" % have)
        raise SystemExit
os.makedirs(os.path.dirname(dest), exist_ok=True)
tmp = dest + ".part"
try:
    urllib.request.urlretrieve(url, tmp)
except Exception as e:
    sys.stdout.write("==V\terror\t%s\n" % str(e).replace("\t", " ").replace("\n", " "))
    raise SystemExit
got = sha1_of(tmp)
if want and got != want:
    os.remove(tmp)
    sys.stdout.write("==V\tmismatch\t%s\n" % got)
    raise SystemExit
os.replace(tmp, dest)
sys.stdout.write("==V\tdownloaded\t%s\n" % got)
'''


def _fetch_text(t: BaseTransport, url: str) -> str:
    if not url.startswith("https://"):
        raise AssetError(f"refusing to fetch a non-https URL: {url!r}")
    script = (
        f"url={q(url)}\n"
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - \"$url\" <<'MCCTL_PY'\n{_PY_FETCH_TEXT}\nMCCTL_PY\n"
    )
    r = t.run(script, timeout=90)
    if "==NOPY" in r.out:
        raise AssetError("the server has no python3 — fetching the Mojang manifest needs it")
    return r.out


def resolve_client_jar(t: BaseTransport, version: str) -> tuple[str, str]:
    """(client_jar_url, sha1) for `version`, via two small server-side fetches."""
    import json
    try:
        manifest = json.loads(_fetch_text(t, MOJANG_MANIFEST_URL))
    except ValueError as e:
        raise AssetError(f"could not parse Mojang's version manifest: {e}") from e
    ver_url = pick_version_entry(manifest, version)
    if not ver_url:
        raise AssetError(f"Mojang's version manifest has no entry for {version!r}")
    try:
        version_json = json.loads(_fetch_text(t, ver_url))
    except ValueError as e:
        raise AssetError(f"could not parse the {version} metadata: {e}") from e
    url, sha1, _size = client_download_of(version_json)
    if not url:
        raise AssetError(f"no client download is listed for {version!r}")
    return url, sha1


def parse_sync_result(out: str) -> dict:
    """The ==V status line → {"status", "sha1"} (status: present|downloaded|mismatch|error)."""
    for line in out.splitlines():
        if line.startswith("==V\t"):
            parts = line.split("\t")
            return {"status": parts[1] if len(parts) > 1 else "error",
                    "sha1": parts[2] if len(parts) > 2 else ""}
    return {"status": "error", "sha1": ""}


def download_to(t: BaseTransport, url: str, dest: str, sha1: str = "",
                *, force: bool = False) -> dict:
    """Idempotently download `url` to `dest` on the server, verifying `sha1`."""
    if not url.startswith("https://"):
        raise AssetError(f"refusing to download a non-https URL: {url!r}")
    script = (
        "command -v python3 >/dev/null 2>&1 || { echo '==NOPY'; exit 0; }\n"
        f"python3 - {q(url)} {q(dest)} {q(sha1)} {q('1' if force else '0')} "
        f"<<'MCCTL_PY'\n{_PY_DOWNLOAD}\nMCCTL_PY\n"
    )
    r = t.run(script, timeout=600)        # a client jar is ~25 MB
    if "==NOPY" in r.out:
        raise AssetError("the server has no python3 — downloading the client jar needs it")
    return parse_sync_result(r.out)


def sync_vanilla(t: BaseTransport, cfg: Config, version: str = "",
                 force: bool = False) -> dict:
    """Cache the matching vanilla client jar so vanilla items gain icons + names."""
    ver = resolve_version(t, cfg, version)
    if not ver:
        raise AssetError("could not determine the Minecraft version — set "
                         "[server].mc_version (e.g. \"1.21.1\")")
    url, sha1 = resolve_client_jar(t, ver)
    dest = f"{vanilla_cache_dir(cfg)}/{ver}.jar"
    res = download_to(t, url, dest, sha1, force=force)
    return {"version": ver, "jar": dest, "url": url, "sha1": res["sha1"],
            "status": res["status"], "ok": res["status"] in ("present", "downloaded")}
