"""Mod inventory: list every jar in mods/ with metadata pulled from inside it.

Metadata extraction runs server-side in ONE round-trip: a python3 one-shot
opens each jar (they're just zip files) and prints the embedded descriptor —
META-INF/neoforge.mods.toml (NeoForge 1.20.5+), META-INF/mods.toml (Forge),
or fabric.mod.json. Parsing the descriptor happens locally. Falls back to
bare file listing when the server has no python3.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
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


# ---------------------------------------------------------------- local scan (client pack)

def _local_listing(mods_dir: str) -> str:
    """The same ==JAR/==META stream the remote lister emits, produced in-process.

    The client pack lives on *this* machine (mcctl runs on the player's desktop),
    so we read the jars directly instead of shelling python3 over SSH — then feed
    the identical stream through `parse_listing`, reusing every descriptor parser.
    """
    names = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml", "fabric.mod.json")
    out: list[str] = []
    for fn in sorted(os.listdir(mods_dir)):
        if not fn.endswith(".jar"):
            continue
        p = os.path.join(mods_dir, fn)
        try:
            st = os.stat(p)
            out.append(f"==JAR {fn}|{st.st_size}|{int(st.st_mtime)}")
            with zipfile.ZipFile(p) as z:
                for name in names:
                    try:
                        data = z.read(name)
                    except KeyError:
                        continue
                    out.append(f"==META {name}")
                    out.append(data.decode("utf-8", "replace")[:6000])
                    break
        except (OSError, zipfile.BadZipFile) as e:
            out.append(f"==ERR {e}")
    return "\n".join(out)


def scan_local_mods(mods_dir: str) -> list[ModInfo]:
    """List mods from a LOCAL directory (the client pack), with full metadata."""
    from pathlib import Path
    d = Path(mods_dir).expanduser()
    if not d.is_dir():
        raise ModsError(f"not a mods directory: {mods_dir}")
    return parse_listing(util.sanitize_terminal(_local_listing(str(d))))


# ---------------------------------------------------------------- diff (client vs server)

@dataclass(slots=True)
class ModDelta:
    """A mod present on both sides whose version differs."""
    key: str                  # mod_id, or filename when metadata is unavailable
    name: str = ""
    server_version: str = ""
    client_version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ModDiff:
    server_only: list[ModInfo]      # on the server, missing from the client pack
    client_only: list[ModInfo]      # on the client, missing from the server
    version_mismatch: list[ModDelta]
    common: list[ModInfo]           # same mod, same (or unknown) version

    @property
    def in_sync(self) -> bool:
        return not (self.server_only or self.client_only or self.version_mismatch)

    def to_dict(self) -> dict:
        return {
            "in_sync": self.in_sync,
            "server_only": [m.to_dict() for m in self.server_only],
            "client_only": [m.to_dict() for m in self.client_only],
            "version_mismatch": [d.to_dict() for d in self.version_mismatch],
            "common": [m.to_dict() for m in self.common],
        }


def _key(m: ModInfo) -> str:
    """Match on mod_id when known (stable across versioned filenames); else file."""
    return m.mod_id or m.file.lower()


def diff_mods(server: list[ModInfo], client: list[ModInfo]) -> ModDiff:
    """Pure set diff of two mod lists. A version mismatch is only flagged when
    *both* sides report a version — an unknown version (no metadata) is never
    guessed to be a mismatch, only a presence difference."""
    s_by: dict[str, ModInfo] = {}
    for m in server:
        s_by.setdefault(_key(m), m)
    c_by: dict[str, ModInfo] = {}
    for m in client:
        c_by.setdefault(_key(m), m)

    server_only: list[ModInfo] = []
    version_mismatch: list[ModDelta] = []
    common: list[ModInfo] = []
    for k, sm in s_by.items():
        cm = c_by.get(k)
        if cm is None:
            server_only.append(sm)
        elif sm.version and cm.version and sm.version != cm.version:
            version_mismatch.append(ModDelta(
                key=k, name=sm.name or cm.name or sm.mod_id or sm.file,
                server_version=sm.version, client_version=cm.version))
        else:
            common.append(sm)
    client_only = [cm for k, cm in c_by.items() if k not in s_by]

    label = lambda m: (m.name or m.file).lower()  # noqa: E731
    server_only.sort(key=label)
    client_only.sort(key=label)
    common.sort(key=label)
    version_mismatch.sort(key=lambda d: d.name.lower())
    return ModDiff(server_only, client_only, version_mismatch, common)


def render_diff(diff: ModDiff) -> str:
    """Plain-text diff summary (shared by the GUI and `mcctl ai` payloads)."""
    def line(m: ModInfo) -> str:
        return f"  {m.name or m.mod_id or m.file} ({m.version or '?'})  [{m.file}]"

    out = [
        f"server-only: {len(diff.server_only)}   client-only: {len(diff.client_only)}   "
        f"version-mismatch: {len(diff.version_mismatch)}   in-sync: {len(diff.common)}",
        "",
    ]
    if diff.in_sync:
        out.append("packs are in sync (every mod matches by id and version)")
        return "\n".join(out)
    if diff.server_only:
        out.append("on SERVER but missing from client:")
        out += [line(m) for m in diff.server_only]
        out.append("")
    if diff.client_only:
        out.append("on CLIENT but missing from server:")
        out += [line(m) for m in diff.client_only]
        out.append("")
    if diff.version_mismatch:
        out.append("version mismatch (server vs client):")
        out += [f"  {d.name}: {d.server_version} vs {d.client_version}"
                for d in diff.version_mismatch]
    return "\n".join(out).rstrip()
