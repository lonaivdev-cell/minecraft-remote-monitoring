"""Mod inventory: list every jar in mods/ with metadata pulled from inside it.

Metadata extraction runs server-side in ONE round-trip: a python3 one-shot
opens each jar (they're just zip files) and prints the embedded descriptor —
META-INF/neoforge.mods.toml (NeoForge 1.20.5+), META-INF/mods.toml (Forge),
or fabric.mod.json. Parsing the descriptor happens locally. Falls back to
bare file listing when the server has no python3.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from . import util
from .config import Config
from .transport import BaseTransport, q

log = util.get_logger("mods")


class ModsError(RuntimeError):
    pass


@dataclass(slots=True)
class ModInfo:
    file: str
    size: int = 0
    mtime: int = 0
    mod_id: str = ""
    name: str = ""
    version: str = ""
    loader: str = ""          # neoforge | forge | fabric | "" (unknown)
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- descriptor parsing

_TOML_MODS_RE = re.compile(r"^\s*\[\[mods\]\]\s*$", re.MULTILINE)
_TOML_KV_RE = re.compile(
    r"""^\s*(modId|version|displayName|description)\s*=\s*(?:'''(.*?)'''|"([^"]*)"|'([^']*)')""",
    re.MULTILINE | re.DOTALL)


def parse_mods_toml(text: str) -> dict:
    """First [[mods]] entry of a (neo)forge mods.toml — tolerant, regex-based."""
    m = _TOML_MODS_RE.search(text)
    scope = text[m.end():] if m else text
    nxt = re.search(r"^\s*\[\[", scope, re.MULTILINE)
    if nxt:
        scope = scope[:nxt.start()]
    out = {}
    for kv in _TOML_KV_RE.finditer(scope):
        key = kv.group(1)
        val = next((g for g in kv.groups()[1:] if g is not None), "")
        out[key] = " ".join(val.split())[:200]
    return out


def parse_fabric_json(text: str) -> dict:
    try:
        d = json.loads(text)
    except ValueError:
        return {}
    return {"modId": str(d.get("id", "")), "version": str(d.get("version", "")),
            "displayName": str(d.get("name", "")),
            "description": " ".join(str(d.get("description", "")).split())[:200]}


def parse_listing(out: str) -> list[ModInfo]:
    """Parse the '==JAR name|size|mtime' / '==META path' block stream."""
    mods: list[ModInfo] = []
    cur: ModInfo | None = None
    meta_name = ""
    buf: list[str] = []

    def flush():
        nonlocal buf, meta_name
        if cur is None or not meta_name:
            buf, meta_name = [], ""
            return
        text = "\n".join(buf)
        if meta_name.endswith(".json"):
            d = parse_fabric_json(text)
            cur.loader = "fabric"
        else:
            d = parse_mods_toml(text)
            cur.loader = "neoforge" if "neoforge" in meta_name else "forge"
        cur.mod_id = d.get("modId", "")
        cur.name = d.get("displayName", "")
        version = d.get("version", "")
        cur.version = "" if version.startswith("${") else version
        cur.description = d.get("description", "")
        buf, meta_name = [], ""

    for line in out.splitlines():
        if line.startswith("==JAR "):
            flush()
            parts = line[6:].rsplit("|", 2)
            cur = ModInfo(file=parts[0])
            if len(parts) == 3:
                cur.size = int(parts[1]) if parts[1].isdigit() else 0
                cur.mtime = int(parts[2]) if parts[2].isdigit() else 0
            mods.append(cur)
        elif line.startswith("==META "):
            flush()
            meta_name = line[7:].strip()
        elif line.startswith("==ERR"):
            flush()
        elif meta_name:
            buf.append(line)
    flush()
    return mods


# ---------------------------------------------------------------- remote listing

_PY_LISTER = r'''
import os, sys, zipfile
d = sys.argv[1]
names = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml", "fabric.mod.json")
for fn in sorted(os.listdir(d)):
    if not fn.endswith(".jar"):
        continue
    p = os.path.join(d, fn)
    try:
        st = os.stat(p)
        print("==JAR %s|%d|%d" % (fn, st.st_size, int(st.st_mtime)))
        z = zipfile.ZipFile(p)
        for name in names:
            try:
                data = z.read(name)
            except KeyError:
                continue
            print("==META", name)
            print(data.decode("utf-8", "replace")[:6000])
            break
    except Exception as e:
        print("==ERR", e)
'''


def list_mods(t: BaseTransport, cfg: Config) -> list[ModInfo]:
    mods_dir = f"{cfg.server.server_dir}/mods"
    script = (
        f"dir={q(mods_dir)}\n"
        '[ -d "$dir" ] || { echo "==NODIR"; exit 0; }\n'
        "if command -v python3 >/dev/null 2>&1; then\n"
        f"python3 - \"$dir\" <<'MCCTL_PY'\n{_PY_LISTER}\nMCCTL_PY\n"
        "else\n"
        '  for f in "$dir"/*.jar; do [ -f "$f" ] || continue;\n'
        "    printf '==JAR %s|%s|%s\\n' \"$(basename \"$f\")\" \"$(stat -c %s \"$f\")\" \"$(stat -c %Y \"$f\")\"\n"
        "  done\n"
        "fi\n"
    )
    r = t.run(script, timeout=120)
    if "==NODIR" in r.out:
        raise ModsError(f"no mods/ directory in {cfg.server.server_dir}")
    return parse_listing(util.sanitize_terminal(r.out))


def render_text(mods: list[ModInfo]) -> str:
    """Plain-text table shared by the GUI and `mcctl ai mods` payloads."""
    total = sum(m.size for m in mods)
    lines = [f"{len(mods)} mods, {util.human_bytes(total)} total", ""]
    for m in mods:
        label = m.name or m.mod_id or m.file
        ver = m.version or "?"
        lines.append(f"  {label:<42} {ver:<18} {util.human_bytes(m.size):>10}  {m.file}")
    return "\n".join(lines)
