"""JVM GC tracer: stream `jstat -gcutil` from the server and surface garbage
collector activity — young/full collections, pause time, and live heap-region
occupancy — as it happens.

This is a "deep-learning instrument": watching GC live is the clearest window
into how the JVM actually manages memory under a running modded server. The
parsing is pure and unit-tested; `gc_trace` does the one streaming round-trip.

`jstat -gcutil <pid> <interval_ms>` prints a header row of column names, then one
row of percentages/counters per interval (it runs until the channel closes):

    S0    S1     E      O      M     CCS    YGC   YGCT    FGC  FGCT   CGC   CGCT     GCT
   0.00  31.25  18.30  45.10  95.20 92.10  1234  45.678   12  8.901    0  0.000   54.579

Columns vary by JDK (CGC/CGCT are absent on older VMs), so rows are parsed by
mapping the header names to values rather than by fixed position.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from . import util
from .config import Config
from .transport import BaseTransport, q

log = util.get_logger("tracer")


class TraceError(RuntimeError):
    pass


def _jstat(cfg: Config) -> str:
    return f"{cfg.server.java_home}/bin/jstat" if cfg.server.java_home else "jstat"


def is_header(line: str) -> bool:
    """A jstat header row contains the column names (letters), data rows don't."""
    toks = line.split()
    return bool(toks) and any(c.isalpha() for c in toks[0])


def parse_row(names: list[str], line: str) -> dict[str, float] | None:
    """Map a jstat data row onto the header names; None if it isn't a data row."""
    toks = line.split()
    if not toks or len(toks) != len(names):
        return None
    try:
        return {name: float(tok) for name, tok in zip(names, toks, strict=True)}
    except ValueError:
        return None


@dataclass(slots=True)
class GcDelta:
    """What changed between two jstat samples."""
    young_gcs: int          # young (minor) collections since last sample
    young_pause_ms: float   # wall time those young collections cost
    full_gcs: int           # full (major / stop-the-world) collections
    full_pause_ms: float
    eden_pct: float         # current eden occupancy (fills then triggers a young GC)
    old_pct: float          # current old-gen occupancy (climbs to a full GC / leak)
    meta_pct: float         # metaspace occupancy (class metadata)

    @property
    def collected(self) -> bool:
        return self.young_gcs > 0 or self.full_gcs > 0


def delta(prev: dict[str, float], cur: dict[str, float]) -> GcDelta:
    def g(d: dict[str, float], k: str) -> float:
        return d.get(k, 0.0)
    return GcDelta(
        young_gcs=int(g(cur, "YGC") - g(prev, "YGC")),
        young_pause_ms=(g(cur, "YGCT") - g(prev, "YGCT")) * 1000.0,
        full_gcs=int(g(cur, "FGC") - g(prev, "FGC")),
        full_pause_ms=(g(cur, "FGCT") - g(prev, "FGCT")) * 1000.0,
        eden_pct=g(cur, "E"),
        old_pct=g(cur, "O"),
        meta_pct=g(cur, "M"),
    )


def gc_trace(t: BaseTransport, cfg: Config, pid: int, *, interval_ms: int = 1000
             ) -> Iterator[tuple[dict[str, float], GcDelta | None]]:
    """Yield (snapshot, delta-since-previous) for each jstat interval until the
    caller stops iterating. The first yield has delta=None (baseline)."""
    if not pid:
        raise TraceError("server is not running — nothing to trace")
    script = f"exec {q(_jstat(cfg))} -gcutil {int(pid)} {int(interval_ms)} 2>&1"
    names: list[str] = []
    prev: dict[str, float] | None = None
    for line in t.stream(script):
        line = util.sanitize_terminal(line).strip()
        if not line:
            continue
        if is_header(line):
            names = line.split()
            continue
        if not names:
            # jstat couldn't attach (no such pid, perm denied) — surface the message
            raise TraceError(f"jstat: {line[:200]}")
        row = parse_row(names, line)
        if row is None:
            continue
        yield row, (delta(prev, row) if prev is not None else None)
        prev = row


EXPLAIN = """\
The JVM heap is split into generations. New objects land in EDEN; when it fills,
a YOUNG (minor) GC copies survivors out and clears eden — cheap and frequent. The
OLD generation holds long-lived objects (loaded chunks, the world, mod state); a
FULL (major) GC scans it and is the expensive, stop-the-world pause that shows up
as a TPS dip. METASPACE (M) holds class metadata — it grows as mods load classes.

Read the trace like this:
  • Eden climbing then snapping back to ~0% with a young GC = normal allocation.
  • Old % drifting up over time and never falling = retained set growing (more
    loaded chunks, more entities — or a leak). A full GC that frees little of it
    means the memory is genuinely live: see `mcctl purge` and `mcctl health`.
  • Frequent or long full-GC pauses are what players feel as lag spikes; pair
    this with `mcctl tps` to correlate pauses with MSPT.
"""
