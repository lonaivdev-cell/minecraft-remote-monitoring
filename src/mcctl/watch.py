"""`mcctl watch` — a line-oriented live monitor.

Where `mcctl dash` takes over the screen, watch streams one compact status line
per interval to stdout: scrollable, greppable, and friendly to a terminal you're
also reading code in. Every full sample is recorded to the metrics history, so a
watch session doubles as a recorder for `mcctl history`.
"""

from __future__ import annotations

import datetime as dt
import time

from rich.console import Console as RichConsole
from rich.text import Text

from . import metrics, util
from .config import Config
from .console import Console
from .server import ServerControl, Status
from .transport import BaseTransport, TransportError

log = util.get_logger("watch")


def _now(cfg: Config) -> str:
    tz = cfg.ui.timezone
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.now(ZoneInfo(tz)).strftime("%H:%M:%S")
        except Exception as e:  # noqa: BLE001 - fall back to local clock
            log.debug("display tz %s unavailable: %s", tz, e)
    return dt.datetime.now().strftime("%H:%M:%S")


def render_line(cfg: Config, st: Status) -> Text:
    """One status line: time | state | players | TPS | MSPT | heap | RAM | load."""
    t = Text(f"{_now(cfg)}  ")
    if st.errors:
        t.append("UNREACHABLE", style="bold white on red")
        t.append(f"  {st.errors[0][:80]}", style="dim")
        return t
    if st.running and st.port_open:
        t.append("ONLINE ", style="bold green")
    elif st.running:
        t.append("BOOTING", style="bold yellow")
    else:
        t.append("OFFLINE", style="bold red")
        return t

    players = f"{st.players.count}/{st.players.max}" if st.players else "-"
    t.append(f"  players {players}")

    tps = (st.tps or {}).get("tps", {})
    tps_now = tps.get("10s") or tps.get("5s") or tps.get("1m")
    if tps_now is not None:
        style = "green" if tps_now >= 18 else "yellow" if tps_now >= 12 else "red"
        t.append("  TPS ")
        t.append(f"{tps_now:4.1f}", style=style)
    mspt = (st.tps or {}).get("mspt", {}).get("median")
    if mspt is not None:
        t.append(f"  MSPT {mspt:5.1f}ms")

    total = st.heap_max or st.heap_committed
    if st.heap_used and total:
        pct = 100.0 * st.heap_used / total
        style = "green" if pct < 70 else "yellow" if pct < 90 else "red"
        t.append("  heap ")
        t.append(f"{pct:3.0f}%", style=style)
    if st.host_mem_used and st.host_mem_total:
        t.append(f"  RAM {100.0 * st.host_mem_used / st.host_mem_total:3.0f}%")
    if st.load:
        t.append(f"  load {st.load[0]:.2f}")
    return t


def run_watch(cfg: Config, transport: BaseTransport, *, interval: float = 10.0,
              count: int = 0) -> None:
    """Print a status line every `interval` seconds (count=0 → until Ctrl-C)."""
    console = Console(cfg, transport)
    ctl = ServerControl(cfg, transport, console)
    rc = RichConsole(highlight=False)
    target = ("local" if cfg.server.transport == "local"
              else f"{cfg.server.user}@{cfg.server.host}")
    rc.print(f"[dim]watching {target} every {interval:g}s — Ctrl-C to stop "
             f"(times in {cfg.ui.timezone or 'server time'})[/dim]")
    i = 0
    try:
        while True:
            try:
                st = ctl.status(full=True)
            except TransportError as e:
                st = Status()
                st.errors = [str(e)]
            if st.running and not st.errors:
                metrics.append_sample(metrics.sample_from_status(st))
            rc.print(render_line(cfg, st))
            i += 1
            if count and i >= count:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        rc.print("[yellow]stopped[/yellow]")
    finally:
        console.close()
