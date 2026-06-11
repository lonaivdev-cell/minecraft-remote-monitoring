"""Event journal: a durable, append-only record of watchdog decisions and alerts.

The watchdog runs as its own systemd unit; the agent (`mcctl agent`) is a
transient per-connection process. They must not be coupled by a live socket, so
the IPC between them is this append-only JSONL journal: the watchdog *emits*
events at the exact moments it already alerts, and any number of readers (the
agent's `events.subscribe` stream, `mcctl events`, the GUI) tail it.

Each line is one JSON object:
    {"ts": 1718107200.0, "kind": "restart", "detail": "…",
     "urgency": "normal"|"critical", "data": {...}}

`kind` is a small, stable vocabulary (see KINDS) so clients can switch on it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from . import util

log = util.get_logger("events")

# Stable event vocabulary. Additive only within a protocol version.
KINDS = (
    "started",            # server came up (manual or self-heal)
    "stopped",            # server taken down on purpose
    "restart",            # watchdog restarted a down server
    "freeze-restart",     # watchdog restarted a frozen server
    "crash-loop-halt",    # crash-loop breaker tripped; watchdog halted
    "alert-tps",          # sustained low TPS
    "alert-heap",         # heap pressure
    "alert-disk",         # low disk on the server
    "alert-ssh",          # server unreachable
    "restart-failed",     # a self-heal attempt failed
)

_MAX_BYTES = 5_000_000


def events_path() -> Path:
    return util.state_dir() / "events.jsonl"


def emit(kind: str, detail: str = "", *, urgency: str = "normal",
         data: dict | None = None, ts: float | None = None) -> dict:
    """Append one event to the journal. Best-effort: never raises to the caller."""
    ev = {
        "ts": round(ts if ts is not None else time.time(), 3),
        "kind": kind,
        "detail": detail,
        "urgency": urgency,
        "data": data or {},
    }
    try:
        util.ensure_dirs()
        p = events_path()
        if p.exists() and p.stat().st_size > _MAX_BYTES:
            p.replace(p.with_suffix(".jsonl.1"))
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
    except OSError as e:  # journaling must never crash a heal
        log.warning("could not append event %s: %s", kind, e)
    return ev


def read(*, since: float | None = None, limit: int | None = None) -> list[dict]:
    """Return journal events, oldest first; only those with ts > since if given."""
    p = events_path()
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if since is not None and ev.get("ts", 0) <= since:
            continue
        out.append(ev)
    if limit is not None and limit > 0:
        out = out[-limit:]
    return out


def follow(*, since: float | None = None, poll: float = 1.0,
           sleeper=time.sleep) -> Iterator[dict]:
    """Yield existing events (after `since`), then watch for appends forever.

    Pure-stdlib stat-poll so it works identically everywhere (no inotify dep).
    Tracks the last ts seen so a truncating rotation can't replay old lines.
    """
    last = since
    seen = read(since=last)
    for ev in seen:
        last = ev.get("ts", last)
        yield ev
    while True:
        sleeper(poll)
        for ev in read(since=last):
            last = ev.get("ts", last)
            yield ev
