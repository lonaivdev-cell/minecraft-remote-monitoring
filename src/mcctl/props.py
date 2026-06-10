"""Remote tuning: server.properties and ServerPackCreator variables.txt.

Both editors preserve comments and ordering, validate known keys, keep a
timestamped .bak on the server before every write, and write atomically
(tmp + mv) so a dropped SSH connection can never leave a half-written file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import util
from .config import Config
from .transport import BaseTransport

log = util.get_logger("props")


class PropError(ValueError):
    pass


# ---------------------------------------------------------------- properties model

@dataclass(slots=True)
class _Line:
    kind: str            # "kv" | "raw"
    key: str = ""
    value: str = ""
    raw: str = ""


class PropertiesFile:
    def __init__(self, lines: list[_Line]):
        self._lines = lines

    @classmethod
    def parse(cls, text: str) -> PropertiesFile:
        lines: list[_Line] = []
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith(("#", "!")) and "=" in raw:
                k, v = raw.split("=", 1)
                lines.append(_Line("kv", k.strip(), v.strip()))
            else:
                lines.append(_Line("raw", raw=raw))
        return cls(lines)

    def get(self, key: str) -> str | None:
        for ln in self._lines:
            if ln.kind == "kv" and ln.key == key:
                return ln.value
        return None

    def set(self, key: str, value: str) -> None:
        for ln in self._lines:
            if ln.kind == "kv" and ln.key == key:
                ln.value = value
                return
        self._lines.append(_Line("kv", key, value))

    def items(self) -> list[tuple[str, str]]:
        return [(ln.key, ln.value) for ln in self._lines if ln.kind == "kv"]

    def render(self) -> str:
        out = []
        for ln in self._lines:
            out.append(f"{ln.key}={ln.value}" if ln.kind == "kv" else ln.raw)
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------- known keys

@dataclass(slots=True)
class PropSpec:
    type: str                     # "bool" | "int" | "str" | "enum"
    desc: str
    restart: bool = True          # needs a restart to apply
    enum: tuple[str, ...] = ()
    lo: int | None = None
    hi: int | None = None
    live_cmd: str = ""            # console command applying it live, if any


KNOWN_PROPS: dict[str, PropSpec] = {
    "motd": PropSpec("str", "Server list message"),
    "difficulty": PropSpec("enum", "Game difficulty", enum=("peaceful", "easy", "normal", "hard"),
                           live_cmd="difficulty {v}"),
    "gamemode": PropSpec("enum", "Default gamemode", enum=("survival", "creative", "adventure", "spectator")),
    "hardcore": PropSpec("bool", "Hardcore mode"),
    "pvp": PropSpec("bool", "Player-vs-player damage"),
    "white-list": PropSpec("bool", "Whitelist enforcement", live_cmd="whitelist {onoff}"),
    "enforce-whitelist": PropSpec("bool", "Kick non-whitelisted on reload"),
    "max-players": PropSpec("int", "Player slots", lo=1, hi=1000),
    "view-distance": PropSpec("int", "Chunk view distance", lo=3, hi=32),
    "simulation-distance": PropSpec("int", "Chunk simulation distance", lo=3, hi=32),
    "spawn-protection": PropSpec("int", "Spawn protection radius", lo=0, hi=512),
    "allow-flight": PropSpec("bool", "Allow flight (mods need true)"),
    "enable-command-block": PropSpec("bool", "Command blocks"),
    "online-mode": PropSpec("bool", "Mojang auth"),
    "server-port": PropSpec("int", "Game port", lo=1, hi=65535),
    "server-ip": PropSpec("str", "Bind address (keep 0.0.0.0 — IPv6 bind fix)"),
    "use-native-transport": PropSpec("bool", "Epoll transport (false on this box — IPv6 fix)"),
    "enable-rcon": PropSpec("bool", "RCON console (mcctl's preferred channel)"),
    "rcon.port": PropSpec("int", "RCON port", lo=1, hi=65535),
    "rcon.password": PropSpec("str", "RCON password"),
    "broadcast-rcon-to-ops": PropSpec("bool", "Echo RCON commands to ops (keep false: noisy)"),
    "max-tick-time": PropSpec("int", "Watchdog kill threshold ms (-1 disables)", lo=-1, hi=600000),
    "sync-chunk-writes": PropSpec("bool", "Synchronous chunk writes (false = faster saves)"),
    "level-seed": PropSpec("str", "World seed (new worlds only)"),
    "spawn-monsters": PropSpec("bool", "Monster spawning"),
    "allow-nether": PropSpec("bool", "Nether enabled"),
}


def validate_prop(key: str, value: str) -> str:
    """Normalize + validate; unknown keys pass through with a warning."""
    spec = KNOWN_PROPS.get(key)
    if spec is None:
        log.warning("property %r not in the known-keys table — setting it unvalidated", key)
        return value
    v = value.strip()
    if spec.type == "bool":
        lv = v.lower()
        if lv in ("true", "on", "yes", "1"):
            return "true"
        if lv in ("false", "off", "no", "0"):
            return "false"
        raise PropError(f"{key} expects true/false, got {value!r}")
    if spec.type == "int":
        try:
            n = int(v)
        except ValueError:
            raise PropError(f"{key} expects an integer, got {value!r}") from None
        if spec.lo is not None and n < spec.lo or spec.hi is not None and n > spec.hi:
            raise PropError(f"{key} must be in [{spec.lo}, {spec.hi}], got {n}")
        return str(n)
    if spec.type == "enum":
        if v.lower() not in spec.enum:
            raise PropError(f"{key} must be one of {', '.join(spec.enum)}")
        return v.lower()
    return v


def props_diff(old: PropertiesFile, new: PropertiesFile) -> list[str]:
    o = dict(old.items())
    n = dict(new.items())
    out = []
    for k in sorted(o.keys() | n.keys()):
        if o.get(k) != n.get(k):
            shown_old = "<unset>" if k not in o else o[k]
            shown_new = "<unset>" if k not in n else n[k]
            if k == "rcon.password":
                shown_old, shown_new = "*" * 8, "*" * 8
            out.append(f"  {k}: {shown_old} -> {shown_new}")
    return out


# ---------------------------------------------------------------- remote io

def props_path(cfg: Config) -> str:
    return f"{cfg.server.server_dir}/server.properties"


def load_props(t: BaseTransport, cfg: Config) -> PropertiesFile:
    return PropertiesFile.parse(t.read_text(props_path(cfg), check=True))


def save_props(t: BaseTransport, cfg: Config, pf: PropertiesFile) -> None:
    t.write_text(props_path(cfg), pf.render(), backup=True)


# ---------------------------------------------------------------- variables.txt

_HEAP_RE = re.compile(r"-Xm([sx])(\d+)([kKmMgG])")
_SIZE_RE = re.compile(r"^(\d+)([kKmMgG])$")


def variables_path(cfg: Config) -> str:
    return f"{cfg.server.server_dir}/variables.txt"


def load_variables(t: BaseTransport, cfg: Config) -> str:
    return t.read_text(variables_path(cfg), check=True)


def save_variables(t: BaseTransport, cfg: Config, text: str) -> None:
    t.write_text(variables_path(cfg), text, backup=True)


def get_var(text: str, key: str) -> str | None:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(f"{key}="):
            return s.split("=", 1)[1].strip().strip('"')
    return None


def set_var(text: str, key: str, value: str, *, quote: bool = True) -> str:
    rendered = f'{key}="{value}"' if quote else f"{key}={value}"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = rendered
            break
    else:
        lines.append(rendered)
    return "\n".join(lines) + "\n"


def size_to_bytes(size: str) -> int:
    m = _SIZE_RE.match(size.strip())
    if not m:
        raise PropError(f"bad size {size!r} — use e.g. 12G, 8192M")
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"k": 1024, "m": 1024**2, "g": 1024**3}[unit]


def parse_heap(java_args: str) -> tuple[str | None, str | None]:
    """(Xms, Xmx) like ('12G', '12G')."""
    xms = xmx = None
    for m in _HEAP_RE.finditer(java_args):
        val = f"{m.group(2)}{m.group(3).upper()}"
        if m.group(1) == "s":
            xms = val
        else:
            xmx = val
    return xms, xmx


def set_heap(text: str, size: str) -> str:
    """Rewrite -Xms/-Xmx inside JAVA_ARGS, preserving every other flag."""
    size_to_bytes(size)  # validate format
    java_args = get_var(text, "JAVA_ARGS")
    if java_args is None:
        raise PropError("variables.txt has no JAVA_ARGS line — is this a ServerPackCreator pack?")
    if not _HEAP_RE.search(java_args):
        java_args = f"-Xms{size} -Xmx{size} {java_args}".strip()
    else:
        java_args = _HEAP_RE.sub(lambda m: f"-Xm{m.group(1)}{size}", java_args)
    return set_var(text, "JAVA_ARGS", java_args)
