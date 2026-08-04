"""spark integration: TPS / MSPT / CPU readings, health report, async profiler.

spark replies through the console channel; the profiler's result URL arrives
asynchronously in the server log, so `profile()` watches the log offset the
same way a human watches the console.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import util
from .config import Config  # noqa: F401 - re-exported for typing convenience
from .console import Console

log = util.get_logger("spark")

_NUM = r"\*?([0-9]+(?:[.,][0-9]+)?)\*?"
_URL_RE = re.compile(r"https://spark\.lucko\.me/\S+")
_UNKNOWN_RE = re.compile(r"unknown (?:or incomplete )?command", re.IGNORECASE)

# Server-log line prefix: "[13:27:01] [spark-worker-pool-1-thread-1/INFO]: ".
# RCON replies arrive clean, but the tmux + log-offset fallback reads spark's output
# straight from latest.log, where every line carries this stamp. Left in place, the
# timestamp's digits get scooped up as TPS/MSPT values (e.g. tps_now == the log's
# minutes field), so strip it before parsing. One or more "[...]" logger brackets may
# follow the time (vanilla has one, some loaders add a logger name).
_LOG_PREFIX_RE = re.compile(
    r"^\s*\[\d{2}:\d{2}:\d{2}(?:\.\d+)?\]\s*(?:\[[^\]]*\]\s*)+:?\s?",
    re.MULTILINE,
)


def _strip_log_prefix(text: str) -> str:
    return _LOG_PREFIX_RE.sub("", text)


class SparkError(RuntimeError):
    pass


@dataclass(slots=True)
class TpsReport:
    tps: dict[str, float] = field(default_factory=dict)     # window -> value
    mspt: dict[str, float] = field(default_factory=dict)    # stat -> ms (10s window)
    cpu_system: dict[str, float] = field(default_factory=dict)
    cpu_process: dict[str, float] = field(default_factory=dict)

    @property
    def tps_now(self) -> float | None:
        for k in ("10s", "5s", "1m"):
            if k in self.tps:
                return self.tps[k]
        return next(iter(self.tps.values()), None)

    @property
    def mspt_median(self) -> float | None:
        return self.mspt.get("median")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class HealthReport:
    tps: dict[str, float] = field(default_factory=dict)
    memory_used: int | None = None
    memory_max: int | None = None
    disk_used: int | None = None
    disk_total: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def parse_tps(text: str) -> TpsReport:
    """Tolerant parser for `spark tps` output (color codes already stripped)."""
    rep = TpsReport()
    t = _strip_log_prefix(util.strip_mc_codes(text))

    m = re.search(r"TPS from last(?P<windows>[^:]*):\s*(?P<vals>[^\n;]*)", t, re.IGNORECASE)
    if m:
        windows = re.findall(r"(\d+(?:s|m|h))", m.group("windows"))
        vals = re.findall(_NUM, m.group("vals"))
        rep.tps = {w: _f(v) for w, v in zip(windows, vals, strict=False)}

    m = re.search(
        r"Tick durations \(min/med/95%ile/max ms\)[^:]*:\s*([^\n;]*)", t, re.IGNORECASE
    )
    if m:
        vals = re.findall(_NUM, m.group(1))
        if len(vals) >= 4:
            rep.mspt = {
                "min": _f(vals[0]), "median": _f(vals[1]),
                "p95": _f(vals[2]), "max": _f(vals[3]),
            }

    m = re.search(r"CPU usage from last[^:]*:\s*([^\n]*)", t, re.IGNORECASE)
    if m:
        sys_vals = re.findall(_NUM + r"%(?:\s*,)?\s*(?:\(system\))?", m.group(1))
        if sys_vals:
            for w, v in zip(("10s", "1m", "15m"), sys_vals, strict=False):
                rep.cpu_system[w] = _f(v)
    return rep


def parse_health(text: str) -> HealthReport:
    rep = HealthReport()
    t = _strip_log_prefix(util.strip_mc_codes(text))
    m = re.search(r"TPS from last(?P<w>[^:]*):\s*(?P<v>[^\n;]*)", t, re.IGNORECASE)
    if m:
        windows = re.findall(r"(\d+(?:s|m|h))", m.group("w"))
        vals = re.findall(_NUM, m.group("v"))
        rep.tps = {w: _f(v) for w, v in zip(windows, vals, strict=False)}
    m = re.search(
        r"Memory usage:\s*" + _NUM + r"\s*(GB|MB)\s*/\s*" + _NUM + r"\s*(GB|MB)", t, re.IGNORECASE
    )
    if m:
        mul = {"GB": 1024**3, "MB": 1024**2}
        rep.memory_used = int(_f(m.group(1)) * mul[m.group(2).upper()])
        rep.memory_max = int(_f(m.group(3)) * mul[m.group(4).upper()])
    m = re.search(
        r"Disk usage:\s*" + _NUM + r"\s*(GB|MB)\s*/\s*" + _NUM + r"\s*(GB|MB)", t, re.IGNORECASE
    )
    if m:
        mul = {"GB": 1024**3, "MB": 1024**2}
        rep.disk_used = int(_f(m.group(1)) * mul[m.group(2).upper()])
        rep.disk_total = int(_f(m.group(3)) * mul[m.group(4).upper()])
    return rep


class Spark:
    def __init__(self, console: Console):
        self.console = console

    def _run(self, cmd: str, *, timeout: float = 12.0) -> str:
        out = self.console.send(cmd, timeout=timeout)
        if _UNKNOWN_RE.search(out or ""):
            raise SparkError(
                "spark is not responding to commands — is the spark mod installed on the server?"
            )
        return out or ""

    def available(self) -> bool:
        try:
            return bool(self.tps().tps)
        except SparkError:
            return False

    def tps(self) -> TpsReport:
        rep = parse_tps(self._run("spark tps"))
        if not rep.tps:
            raise SparkError("could not parse `spark tps` output (empty reply?)")
        return rep

    def health(self) -> HealthReport:
        return parse_health(self._run("spark health --memory", timeout=15.0))

    def profile(self, seconds: int = 60, *, progress=None) -> str:
        """Run the async profiler and return the spark viewer URL."""
        offset = self.console.log_size()
        reply = self._run(f"spark profiler start --timeout {int(seconds)}", timeout=10.0)
        m = _URL_RE.search(reply)
        if m:  # spark < async path; unlikely but free to handle
            return m.group(0)
        if progress:
            progress(f"profiler running for {seconds}s …")
        hit = self.console.wait_in_log(_URL_RE.pattern, offset, timeout=seconds + 90, poll=3.0)
        if not hit:
            # try to flush a stuck profiler before giving up
            stop_reply = self._run("spark profiler stop", timeout=15.0)
            m = _URL_RE.search(stop_reply)
            if m:
                return m.group(0)
            raise SparkError("profiler finished but no spark.lucko.me URL appeared in the log")
        return hit
