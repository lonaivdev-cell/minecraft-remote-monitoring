"""Metrics: JVM heap via jcmd, sample history (JSONL), and the memory purge.

The purge is the honest one from the old mc-control.sh: jcmd GC.run, then a
verdict on whether the heap was full of reclaimable garbage (fine) or a real
retained set (loaded chunks / a leak — go look at spark health).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass

from . import util
from .config import Config
from .transport import BaseTransport, TransportError, q

log = util.get_logger("metrics")

# G1 heap_info, both shapes seen on 17/21:
#   garbage-first heap   total 12582912K, used 6196224K [0x...]
#   garbage-first heap   total reserved 12582912K, committed 8388608K, used 6196224K [...]
_G1_USED_RE = re.compile(r"garbage-first heap.*?used\s+(\d+)K", re.DOTALL)
_G1_TOTAL_RE = re.compile(r"garbage-first heap\s+total(?:\s+reserved\s+\d+K,\s+committed)?\s+(\d+)K")


class MetricsError(RuntimeError):
    pass


def parse_heap_info(text: str) -> tuple[int, int] | None:
    """(used_bytes, committed_bytes) from `jcmd GC.heap_info` output."""
    mu = _G1_USED_RE.search(text)
    mt = _G1_TOTAL_RE.search(text)
    if not (mu and mt):
        return None
    return int(mu.group(1)) * 1024, int(mt.group(1)) * 1024


def _jcmd(cfg: Config) -> str:
    return f"{cfg.server.java_home}/bin/jcmd" if cfg.server.java_home else "jcmd"


def jvm_heap(t: BaseTransport, cfg: Config, pid: int | None) -> tuple[int, int] | None:
    if not pid:
        return None
    jc = _jcmd(cfg)
    try:
        r = t.run(
            f"if [ -x {q(jc)} ]; then {q(jc)} {int(pid)} GC.heap_info; "
            f"else jcmd {int(pid)} GC.heap_info; fi",
            timeout=20,
        )
    except TransportError:
        return None
    return parse_heap_info(r.out) if r.ok else None


# ---------------------------------------------------------------- purge

@dataclass(slots=True)
class PurgeReport:
    before_used: int
    after_used: int
    committed: int

    @property
    def freed(self) -> int:
        return max(0, self.before_used - self.after_used)

    @property
    def freed_pct(self) -> float:
        return 100.0 * self.freed / self.before_used if self.before_used else 0.0

    @property
    def verdict(self) -> str:
        return verdict_for(self.freed_pct)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(freed=self.freed, freed_pct=round(self.freed_pct, 1), verdict=self.verdict)
        return d


def verdict_for(freed_pct: float) -> str:
    if freed_pct >= 35:
        return ("mostly reclaimable garbage — heap pressure was transient, no leak indicated")
    if freed_pct >= 15:
        return ("partial reclaim — watch the trend; could be genuine load "
                "(chunks/entities) or a slow leak")
    return ("little reclaimed — the retained set is real: heavily loaded chunks/mods "
            "or a leak; check `mcctl health` and consider a heap dump")


def purge(t: BaseTransport, cfg: Config, pid: int) -> PurgeReport:
    """Explicit concurrent GC (needs -XX:+ExplicitGCInvokesConcurrent, which the
    CarborioLand variables.txt carries) with before/after measurement."""
    before = jvm_heap(t, cfg, pid)
    if not before:
        raise MetricsError("could not read heap before purge (jcmd unavailable?)")
    jc = _jcmd(cfg)
    r = t.run(
        f"if [ -x {q(jc)} ]; then {q(jc)} {int(pid)} GC.run; else jcmd {int(pid)} GC.run; fi",
        timeout=60,
    )
    if not r.ok:
        raise MetricsError(f"jcmd GC.run failed: {r.err.strip()[:300]}")
    time.sleep(3.0)  # let the concurrent cycle finish before measuring
    after = jvm_heap(t, cfg, pid)
    if not after:
        raise MetricsError("could not read heap after purge")
    return PurgeReport(before_used=before[0], after_used=after[0], committed=after[1])


# ---------------------------------------------------------------- history

def append_sample(sample: dict) -> None:
    util.ensure_dirs()
    p = util.metrics_path()
    if p.exists() and p.stat().st_size > 5_000_000:
        p.replace(p.with_suffix(".jsonl.1"))
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sample, separators=(",", ":")) + "\n")


def read_samples(n: int = 120) -> list[dict]:
    p = util.metrics_path()
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def sample_from_status(st) -> dict:
    """Flatten a server.Status into a JSONL-friendly metrics sample."""
    tps_now = mspt = None
    if st.tps:
        tps = st.tps.get("tps", {})
        tps_now = tps.get("10s") or tps.get("5s") or tps.get("1m")
        mspt = (st.tps.get("mspt") or {}).get("median")
    return {
        "ts": int(time.time()),
        "running": st.running,
        "players": st.players.count if st.players else None,
        "tps": tps_now,
        "mspt": mspt,
        "heap_used": st.heap_used,
        "heap_committed": st.heap_committed,
        "heap_max": st.heap_max,
        "mem_used": st.host_mem_used,
        "mem_total": st.host_mem_total,
        "load1": st.load[0] if st.load else None,
        "disk_free": st.disk_free,
        "log_age": st.log_age_s,
    }
