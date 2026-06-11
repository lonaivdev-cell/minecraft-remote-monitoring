"""Prometheus textfile exporter: turn a metrics sample into node_exporter's
textfile-collector format.

`render()` is pure (sample dict in, text out) so it is trivially unit-tested.
`export()` writes it atomically (tmp + os.replace) so node_exporter can never
read a half-written file mid-scrape.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import metrics, state, util

log = util.get_logger("prometheus")

# (metric, type, help, sample-key). None key => handled specially below.
_SERIES: tuple[tuple[str, str, str, str | None], ...] = (
    ("mcctl_up", "gauge", "1 if the Minecraft server process is running", "running"),
    ("mcctl_players", "gauge", "Players currently online", "players"),
    ("mcctl_tps", "gauge", "Ticks per second (spark, most recent window)", "tps"),
    ("mcctl_mspt_milliseconds", "gauge", "Milliseconds per tick (median)", "mspt"),
    ("mcctl_heap_used_bytes", "gauge", "JVM heap used", "heap_used"),
    ("mcctl_heap_max_bytes", "gauge", "JVM heap max (Xmx)", "heap_max"),
    ("mcctl_host_mem_used_bytes", "gauge", "Host memory used", "mem_used"),
    ("mcctl_host_mem_total_bytes", "gauge", "Host memory total", "mem_total"),
    ("mcctl_disk_free_bytes", "gauge", "Free disk on the server filesystem", "disk_free"),
    ("mcctl_load1", "gauge", "Host 1-minute load average", "load1"),
    ("mcctl_log_age_seconds", "gauge", "Seconds since the server log last changed", "log_age"),
)


def _esc(label_value: str) -> str:
    return label_value.replace("\\", "\\\\").replace('"', '\\"')


def render(sample: dict, *, host: str = "", restarts: int = 0,
           now: float | None = None) -> str:
    """Render one metrics sample as Prometheus text format."""
    now = now if now is not None else time.time()
    lbl = f'{{host="{_esc(host)}"}}' if host else ""
    lines: list[str] = []

    def emit(metric: str, mtype: str, helptext: str, value) -> None:
        lines.append(f"# HELP {metric} {helptext}")
        lines.append(f"# TYPE {metric} {mtype}")
        if value is None:
            return
        if isinstance(value, bool):
            value = 1 if value else 0
        lines.append(f"{metric}{lbl} {value}")

    for metric, mtype, helptext, key in _SERIES:
        val = sample.get(key) if key else None
        if key == "running":
            val = 1 if sample.get("running") else 0
        emit(metric, mtype, helptext, val)

    emit("mcctl_watchdog_restarts_total", "counter",
         "Watchdog-initiated restarts recorded in the last 24h", restarts)
    emit("mcctl_scrape_timestamp_seconds", "gauge",
         "Unix time this textfile was written", round(now, 3))
    return "\n".join(lines) + "\n"


def default_path() -> Path:
    return util.state_dir() / "mcctl.prom"


def export(cfg, *, out: str | Path | None = None) -> Path:
    """Write the latest recorded sample to a .prom file, atomically.

    Reads the newest line of metrics.jsonl (the same history `mcctl watch`,
    the dashboard and the watchdog all feed) — no extra round-trip to the
    server. Emits mcctl_up=0 with a fresh scrape timestamp when there is no
    sample yet, so Grafana can tell "exporter alive, server down" from
    "exporter dead".
    """
    samples = metrics.read_samples(1)
    sample = samples[-1] if samples else {"running": False}
    restarts = len(state.load().get("restarts", []))
    host = getattr(cfg.server, "host", "") if cfg.server.transport == "ssh" else "local"
    text = render(sample, host=host, restarts=restarts)

    p = Path(out) if out else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    log.info("wrote prometheus textfile %s (%d series)", p, text.count("\n# TYPE"))
    return p
