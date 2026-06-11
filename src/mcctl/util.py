"""Shared plumbing: XDG paths, logging, locks, humanizers, terminal hygiene, notifications."""

from __future__ import annotations

import fcntl
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

APP = "mcctl"

log = logging.getLogger(APP)


# ---------------------------------------------------------------- XDG paths

def _xdg(env: str, fallback: str) -> Path:
    base = os.environ.get(env, "").strip()
    return Path(base) if base else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP


def runtime_dir() -> Path:
    """Short-path dir for SSH control sockets (unix socket paths are length-limited)."""
    base = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    d = (Path(base) / APP) if base else (cache_dir() / "run")
    return d


def crashes_dir() -> Path:
    return state_dir() / "crashes"


def metrics_path() -> Path:
    return state_dir() / "metrics.jsonl"


def ensure_dirs() -> None:
    for d in (config_dir(), state_dir(), cache_dir(), crashes_dir()):
        d.mkdir(parents=True, exist_ok=True)
    runtime_dir().mkdir(parents=True, exist_ok=True, mode=0o700)


# ---------------------------------------------------------------- logging

def setup_logging(verbosity: int = 0) -> None:
    """File log always captures DEBUG; console level scales with -v."""
    ensure_dirs()
    root = logging.getLogger(APP)
    if root.handlers:  # already configured (tests / repeated calls)
        return
    root.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(
        state_dir() / "mcctl.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG)
    ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{APP}.{name}")


# ---------------------------------------------------------------- humanize

def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def human_duration(secs: int | float | None) -> str:
    if secs is None:
        return "?"
    s = int(secs)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


# ---------------------------------------------------------------- terminal hygiene

# Remote logs are untrusted input: strip ANSI/OSC escape sequences and C0 controls
# (except \n and \t) before they ever touch the local terminal.
_ANSI_RE = re.compile(
    r"""
    \x1b
    (?:
        \[ [0-?]* [ -/]* [@-~]      # CSI
      | \] .*? (?:\x07|\x1b\\)      # OSC, BEL or ST terminated
      | [PX^_] .*? \x1b\\           # DCS/SOS/PM/APC
      | [@-Z\\-_]                   # other 2-byte sequences
    )
    """,
    re.VERBOSE | re.DOTALL,
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MC_CODE_RE = re.compile("§.")  # Minecraft section-sign color/format codes


def sanitize_terminal(text: str) -> str:
    """Make remote text safe to print on a local terminal."""
    text = _ANSI_RE.sub("", text)
    return _CTRL_RE.sub("", text)


def strip_mc_codes(text: str) -> str:
    return _MC_CODE_RE.sub("", sanitize_terminal(text))


# ---------------------------------------------------------------- locking

class LockHeldError(RuntimeError):
    pass


class OpsLock:
    """flock-based mutex so two mcctl invocations can't fight over start/stop/backup."""

    def __init__(self, name: str = "ops"):
        ensure_dirs()
        self._path = state_dir() / f"{name}.lock"
        self._fh = None

    def __enter__(self) -> OpsLock:
        self._fh = open(self._path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._fh.close()
            self._fh = None
            raise LockHeldError(
                "another mcctl operation is already running (start/stop/backup/watchdog action)"
            ) from e
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- json state files

def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


# ---------------------------------------------------------------- notifications

def notify(title: str, body: str, *, desktop: bool = True, webhook_url: str = "",
           urgency: str = "normal") -> None:
    """Best-effort alerting: desktop notify-send and/or a Discord-compatible webhook."""
    if desktop and shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "-a", APP, "-u", urgency, title, body],
                timeout=5, capture_output=True, check=False,
            )
        except Exception as e:  # noqa: BLE001 - alerting must never crash the caller
            log.debug("notify-send failed: %s", e)
    if webhook_url:
        try:
            payload = json.dumps({"content": f"**{title}**\n{body}"}).encode()
            req = urllib.request.Request(
                webhook_url, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": APP},
            )
            urllib.request.urlopen(req, timeout=5)  # noqa: S310 - user-configured URL
        except Exception as e:  # noqa: BLE001
            log.warning("webhook notification failed: %s", e)


# ---------------------------------------------------------------- systemd units

def user_unit_dir() -> Path:
    """Per the systemd spec, user units live under $XDG_CONFIG_HOME/systemd/user."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def render_units(*, exe: str = "mcctl") -> dict[str, str]:
    """The unit files shipped in mcctl/units/ (the PKGBUILD installs the same
    files verbatim), with ExecStart rewritten for non-/usr/bin installs (pipx)."""
    from importlib import resources
    units: dict[str, str] = {}
    for entry in (resources.files("mcctl") / "units").iterdir():
        if not entry.name.endswith((".service", ".timer")):
            continue
        text = entry.read_text(encoding="utf-8")
        if exe != "/usr/bin/mcctl":
            text = text.replace("ExecStart=/usr/bin/mcctl ", f"ExecStart={exe} ")
        units[entry.name] = text
    return units


# ---------------------------------------------------------------- misc

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
