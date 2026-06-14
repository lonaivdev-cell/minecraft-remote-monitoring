"""Mod config browser & editor: read, validate, and write files under config/.

The modpack's per-mod settings live in ``<server_dir>/config/`` as TOML (most),
JSON/JSON5 (some), and ``.cfg``/``.properties`` files. This module enumerates
them, best-effort-associates each file with the mod that owns it (by matching
the file name / sub-directory against the mod ids read from the jars by
:mod:`mcctl.mods`), and reads/writes individual files.

Writes go through the transport's atomic write (tmp + ``mv``) with a timestamped
``.bak`` kept on the server — exactly like :mod:`mcctl.props`. TOML and JSON
files are *parsed locally first*, so a syntax error is rejected before it can
brick the next launch.

How "save → auto-load" actually works: NeoForge ships a config ``FileWatcher``
that fires a reload event the moment a watched file changes on disk, so writing
the file IS the trigger — for mods that re-read their values. Startup-type
configs and any mod that caches its values at construction only fully apply on
the next restart, and ``/reload`` (see :func:`trigger_reload`) refreshes
*datapack*-driven data (recipes/loot/tags), not mod TOMLs. There is no console
command or add-on mod that forces a universal hot-reload — that is the loader's
behaviour, not something mcctl can change. The editor is honest about this.

Path safety: a client only ever supplies a path *relative to* ``config/``. Any
absolute path or ``..`` traversal is rejected, and the resolved path must stay
inside ``config/``.
"""

from __future__ import annotations

import json
import posixpath
import tomllib
from dataclasses import asdict, dataclass

from . import util
from .config import Config
from .console import Console, ConsoleError
from .mods import ModInfo, list_mods
from .transport import BaseTransport, q

log = util.get_logger("modconfig")


class ConfigEditError(ValueError):
    """A bad path, a too-large file, or invalid TOML/JSON — never written."""


# Extension -> format tag. Anything else lists as "" (still openable as text).
_FORMATS = {
    ".toml": "toml", ".json": "json", ".json5": "json5", ".jsonc": "json5",
    ".cfg": "cfg", ".conf": "cfg", ".ini": "cfg", ".properties": "properties",
    ".yaml": "yaml", ".yml": "yaml", ".snbt": "snbt", ".txt": "text",
}
# Configs are tiny; refuse to ship anything that clearly is not a config file.
MAX_BYTES = 1_048_576  # 1 MiB


@dataclass(slots=True)
class ConfigFile:
    path: str            # relative to config/, posix separators ("apotheosis/...toml")
    size: int = 0
    mtime: int = 0
    fmt: str = ""        # toml | json | json5 | cfg | properties | yaml | snbt | text | ""
    mod_id: str = ""     # best-effort owning mod id (may be "")
    mod_name: str = ""   # display name of that mod (may be "")

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- paths & format

def config_dir(cfg: Config) -> str:
    return f"{cfg.server.server_dir}/config"


def fmt_for(name: str) -> str:
    dot = name.rfind(".")
    return _FORMATS.get(name[dot:].lower(), "") if dot >= 0 else ""


def safe_rel(relpath: str) -> str:
    """Normalise a client-supplied path and refuse anything that escapes config/."""
    rel = (relpath or "").strip().replace("\\", "/")
    if not rel:
        raise ConfigEditError("empty config path")
    if rel.startswith("/"):
        raise ConfigEditError(f"path must be relative to config/: {relpath!r}")
    norm = posixpath.normpath(rel)
    if norm == ".." or norm.startswith("../") or norm.startswith("/") or "/../" in norm:
        raise ConfigEditError(f"path escapes config/: {relpath!r}")
    return norm


def full_path(cfg: Config, relpath: str) -> str:
    return f"{config_dir(cfg)}/{safe_rel(relpath)}"


# ---------------------------------------------------------------- mod association

def _candidate_keys(relpath: str) -> list[str]:
    """Plausible mod ids for a config path, most-specific first.

    Configs are conventionally named ``<modid>.toml``, ``<modid>-common.toml``,
    ``<modid>-server.toml`` or live under a ``<modid>/`` sub-directory, so the
    top-level directory and the filename stem (split on the usual separators)
    are the keys worth trying.
    """
    parts = relpath.split("/")
    keys: list[str] = []
    if len(parts) > 1:
        keys.append(parts[0])
    stem = parts[-1]
    dot = stem.find(".")
    if dot > 0:
        stem = stem[:dot]
    for sep in ("-", "_"):
        head = stem.split(sep, 1)[0]
        if head:
            keys.append(head)
    keys.append(stem)
    # de-dup, keep order
    seen: set[str] = set()
    out = []
    for k in keys:
        kl = k.lower()
        if kl and kl not in seen:
            seen.add(kl)
            out.append(kl)
    return out


def associate(files: list[ConfigFile], mods: list[ModInfo]) -> None:
    """Stamp ``mod_id``/``mod_name`` onto each file in place (best effort)."""
    ids: dict[str, tuple[str, str]] = {}
    for m in mods:
        if m.mod_id:
            ids[m.mod_id.lower()] = (m.mod_id, m.name or m.mod_id)
    if not ids:
        return
    for f in files:
        for key in _candidate_keys(f.path):
            hit = ids.get(key)
            if hit:
                f.mod_id, f.mod_name = hit
                break
        else:
            # last resort: a long mod id that is a prefix of the filename stem
            stem = f.path.split("/")[-1].lower()
            for mid, (real, name) in ids.items():
                if len(mid) >= 5 and stem.startswith(mid):
                    f.mod_id, f.mod_name = real, name
                    break


# ---------------------------------------------------------------- listing

def list_config_files(t: BaseTransport, cfg: Config, *, associate_mods: bool = True) -> list[ConfigFile]:
    """Enumerate every regular file under config/ in one round-trip.

    Uses GNU find's ``-printf`` (Ubuntu/coreutils) for ``relpath|size|mtime`` and
    sorts server-side so the order is stable.
    """
    cdir = config_dir(cfg)
    script = (
        f"dir={q(cdir)}\n"
        '[ -d "$dir" ] || { echo "==NODIR"; exit 0; }\n'
        'find "$dir" -type f -printf "%P|%s|%T@\\n" 2>/dev/null | LC_ALL=C sort\n'
    )
    r = t.run(script, timeout=60)
    if "==NODIR" in r.out:
        raise ConfigEditError(f"no config/ directory in {cfg.server.server_dir}")
    files: list[ConfigFile] = []
    for line in r.out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        rel, _, rest = line.partition("|")
        size_s, _, mt_s = rest.partition("|")
        if not rel:
            continue
        try:
            size = int(size_s)
        except ValueError:
            size = 0
        try:
            mtime = int(float(mt_s))
        except ValueError:
            mtime = 0
        files.append(ConfigFile(path=rel, size=size, mtime=mtime, fmt=fmt_for(rel)))
    if associate_mods and files:
        try:
            associate(files, list_mods(t, cfg))
        except Exception as e:  # noqa: BLE001 - association is a nicety, never fatal
            log.debug("mod association skipped: %s", e)
    return files


# ---------------------------------------------------------------- read

def read_config(t: BaseTransport, cfg: Config, relpath: str) -> dict:
    """Return ``{path, text, fmt, bytes}`` for one config file (size-capped)."""
    rel = safe_rel(relpath)
    full = f"{config_dir(cfg)}/{rel}"
    probe = t.run(f'f={q(full)}\n[ -f "$f" ] || {{ echo "==NOFILE"; exit 0; }}\nstat -c %s "$f"',
                  timeout=20)
    if "==NOFILE" in probe.out or not probe.ok:
        raise ConfigEditError(f"no such config file: {rel}")
    try:
        size = int(probe.out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        size = 0
    if size > MAX_BYTES:
        raise ConfigEditError(
            f"{rel} is {util.human_bytes(size)} — over the {util.human_bytes(MAX_BYTES)} "
            "edit cap; pull it with `mcctl sync` instead")
    text = t.read_text(full, check=True)
    return {"path": rel, "text": text, "fmt": fmt_for(rel), "bytes": size}


# ---------------------------------------------------------------- validate & write

def validate_text(relpath: str, text: str) -> str:
    """Parse TOML/JSON so a syntax error can never reach the live config dir.

    Other formats (json5/cfg/properties/yaml/snbt/text) have no stdlib parser and
    pass through unvalidated. Returns the format tag.
    """
    fmt = fmt_for(relpath)
    if fmt == "toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigEditError(f"invalid TOML: {e}") from None
    elif fmt == "json":
        try:
            json.loads(text)
        except ValueError as e:
            raise ConfigEditError(f"invalid JSON: {e}") from None
    return fmt


def write_config(t: BaseTransport, cfg: Config, relpath: str, text: str) -> dict:
    """Validate then atomically overwrite an *existing* config file (.bak kept).

    Refuses to create brand-new files: mcctl edits configs the modpack already
    generated, it does not invent them.
    """
    rel = safe_rel(relpath)
    fmt = validate_text(rel, text)
    nbytes = len(text.encode())
    if nbytes > MAX_BYTES:
        raise ConfigEditError(f"refusing to write {util.human_bytes(nbytes)} "
                              f"(over the {util.human_bytes(MAX_BYTES)} cap)")
    full = f"{config_dir(cfg)}/{rel}"
    if not t.exists(full):
        raise ConfigEditError(f"no such config file: {rel} (mcctl edits existing files only)")
    t.write_text(full, text, backup=True)
    log.info("wrote config/%s (%d bytes, .bak kept)", rel, nbytes)
    return {"path": rel, "fmt": fmt, "bytes": nbytes}


def trigger_reload(console: Console) -> str:
    """Run ``/reload`` — refreshes datapacks (recipes/loot/tags/functions).

    This does NOT reload mod TOML configs; NeoForge's file watcher handles those
    when the file content changes. Returns the console's response text.
    """
    try:
        return console.send("reload").strip()
    except ConsoleError:
        raise
