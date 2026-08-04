"""Watchdog / intent state shared between CLI, server control and the watchdog daemon.

The state file records *user intent* (`desired`: should the server be up?) separately
from the watchdog *arm switch*. `mcctl stop` sets desired=down, which is what stops the
watchdog from resurrecting a server you took down on purpose — the migration foot-gun
called out in the CarborioLand notes.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import util

DEFAULT_STATE = {
    "armed": False,          # watchdog disarmed by default: arm explicitly after setup
    "desired": "down",       # "up" | "down" — user intent, set by mcctl start/stop
    "restarts": [],          # unix timestamps of watchdog-initiated restarts
    "halted": False,         # crash-loop breaker tripped; requires manual re-arm
    "last_alerts": {},       # alert-key -> unix ts (rate limiting)
}


def path() -> Path:
    return util.state_dir() / "watchdog.json"


def load() -> dict:
    st = dict(DEFAULT_STATE)
    st.update(util.load_json(path(), {}))
    # JSON round-trips lists fine; just defend against manual edits
    if not isinstance(st.get("restarts"), list):
        st["restarts"] = []
    return st


def save(st: dict) -> None:
    util.save_json(path(), st)


def set_desired(desired: str) -> dict:
    st = load()
    st["desired"] = desired
    if desired == "up":
        st["halted"] = False  # a manual start clears the crash-loop breaker
    save(st)
    return st


def set_armed(armed: bool) -> dict:
    st = load()
    st["armed"] = armed
    if armed:
        st["halted"] = False
    save(st)
    return st


def record_restart(ts: float | None = None) -> dict:
    st = load()
    st["restarts"] = [t for t in st["restarts"] if t > time.time() - 86400] + [ts or time.time()]
    save(st)
    return st


def should_alert(st: dict, key: str, min_interval: float, now: float | None = None) -> bool:
    """Rate-limit repeated alerts; mutates and persists last_alerts on True."""
    now = now or time.time()
    last = float(st.get("last_alerts", {}).get(key, 0))
    if now - last < min_interval:
        return False
    st.setdefault("last_alerts", {})[key] = now
    save(st)
    return True
