"""Live TUI dashboard: state, players, TPS sparkline, heap/host gauges, log tail.

Pure `rich` (no extra TUI framework): a Live layout refreshed on two cadences
(cheap probes every tick, spark/players on a slower tick) plus a raw-stdin
key reader for actions.
"""

from __future__ import annotations

import contextlib
import queue
import select
import sys
import termios
import threading
import time
import tty
from collections import deque

from rich.console import Console as RichConsole
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import logs, metrics, util
from .config import Config
from .console import Console
from .server import ServerControl, Status
from .transport import BaseTransport, TransportError

log = util.get_logger("dash")

SPARK_CHARS = "▁▂▃▄▅▆▇█"
FAST_TICK = 3.0
SLOW_TICK = 12.0


def _sparkline(values: list[float | None], lo: float, hi: float, width: int = 40) -> str:
    vals = [v for v in values if v is not None][-width:]
    if not vals:
        return "no data yet"
    span = max(hi - lo, 1e-9)
    chars = []
    for v in vals:
        idx = int((max(lo, min(hi, v)) - lo) / span * (len(SPARK_CHARS) - 1))
        chars.append(SPARK_CHARS[idx])
    return "".join(chars)


def _bar(used: float | None, total: float | None, width: int = 28) -> Text:
    if not used or not total or total <= 0:
        return Text("─" * width, style="dim")
    frac = max(0.0, min(1.0, used / total))
    filled = int(frac * width)
    color = "green" if frac < 0.7 else "yellow" if frac < 0.9 else "red"
    t = Text()
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style="dim")
    t.append(f" {frac * 100:3.0f}%")
    return t


class _KeyReader(threading.Thread):
    def __init__(self, q_: queue.Queue):
        super().__init__(daemon=True)
        self.q = q_
        self.stop_flag = threading.Event()

    def run(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self.stop_flag.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.2)
                if r:
                    self.q.put(sys.stdin.read(1))
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


class Dashboard:
    def __init__(self, cfg: Config, transport: BaseTransport):
        self.cfg = cfg
        self.t = transport
        self.console = Console(cfg, transport)
        self.ctl = ServerControl(cfg, transport, self.console)
        self.tps_hist: deque[float | None] = deque(maxlen=120)
        self.mspt_hist: deque[float | None] = deque(maxlen=120)
        self.status: Status = Status()
        self.message = "keys: [q]uit  [s]ave  [b]ackup  [p]urge  [S]tart  [X]stop(2x)  [r]efresh"
        self._confirm_stop_until = 0.0
        self._busy = ""

    # ---------------------------------------------------------------- data

    def refresh_fast(self) -> None:
        try:
            st = self.ctl.status(full=False)
            st.tps, st.players = self.status.tps, self.status.players  # keep slow data
            st.heap_used, st.heap_committed = self.status.heap_used, self.status.heap_committed
            st.heap_max = self.status.heap_max
            self.status = st
        except TransportError as e:
            self.status.errors = [str(e)]

    def refresh_slow(self) -> None:
        try:
            st = self.ctl.status(full=True)
            self.status = st
            if st.tps:
                self.tps_hist.append((st.tps.get("tps") or {}).get("10s")
                                     or (st.tps.get("tps") or {}).get("5s"))
                self.mspt_hist.append((st.tps.get("mspt") or {}).get("median"))
            else:
                self.tps_hist.append(None)
            metrics.append_sample(metrics.sample_from_status(st))
        except TransportError as e:
            self.status.errors = [str(e)]

    # ---------------------------------------------------------------- render

    def render(self) -> Layout:
        st = self.status
        root = Layout()
        root.split_column(
            Layout(name="header", size=3),
            Layout(name="middle", size=12),
            Layout(name="logs"),
            Layout(name="footer", size=3),
        )

        if st.errors:
            badge = Text(" UNREACHABLE ", style="bold white on red")
        elif st.running and st.port_open:
            badge = Text(" ONLINE ", style="bold black on green")
        elif st.running:
            badge = Text(" BOOTING ", style="bold black on yellow")
        else:
            badge = Text(" OFFLINE ", style="bold white on red")
        head = Text.assemble(
            badge, "  ",
            (f"{self.cfg.server.user}@{self.cfg.server.host}", "bold cyan"),
            f"  pid {st.pid or '-'}  up {util.human_duration(st.uptime_s) if st.uptime_s else '-'}",
            f"  players {st.players.count if st.players else '-'}"
            f"/{st.players.max if st.players else '-'}",
            f"  ch:{st.channel or '-'}",
            ("  WD:armed" if st.armed else "  WD:off", "green" if st.armed else "dim"),
            (" HALTED" if st.halted else "", "bold red"),
        )
        root["header"].update(Panel(head, title="mcctl — CarborioLand", border_style="cyan"))

        perf = Table.grid(padding=(0, 1))
        perf.add_column(justify="right", style="bold")
        perf.add_column()
        tps_now = self.tps_hist[-1] if self.tps_hist else None
        tps_style = "green" if (tps_now or 0) >= 18 else "yellow" if (tps_now or 0) >= 12 else "red"
        perf.add_row("TPS", Text(f"{tps_now:.1f}" if tps_now else "—", style=tps_style))
        perf.add_row("", Text(_sparkline(list(self.tps_hist), 0, 20), style=tps_style))
        mspt = self.mspt_hist[-1] if self.mspt_hist else None
        perf.add_row("MSPT", Text(f"{mspt:.1f} ms" if mspt else "—"))
        perf.add_row("heap", _bar(st.heap_used, st.heap_max or st.heap_committed))
        perf.add_row("", Text(f"{util.human_bytes(st.heap_used)} / "
                              f"{util.human_bytes(st.heap_max or st.heap_committed)}", style="dim"))

        host = Table.grid(padding=(0, 1))
        host.add_column(justify="right", style="bold")
        host.add_column()
        host.add_row("RAM", _bar(st.host_mem_used, st.host_mem_total))
        host.add_row("", Text(f"{util.human_bytes(st.host_mem_used)} / "
                              f"{util.human_bytes(st.host_mem_total)}", style="dim"))
        host.add_row("load", Text(" ".join(f"{x:.2f}" for x in st.load) if st.load else "—"))
        host.add_row("disk", Text(f"{util.human_bytes(st.disk_free)} free"))
        host.add_row("backup", Text(
            f"{st.last_backup} ({util.human_duration(st.last_backup_age_s)} ago)"
            if st.last_backup else "none", style="dim"))

        mid = Layout()
        mid.split_row(
            Layout(Panel(perf, title="server perf", border_style="green")),
            Layout(Panel(host, title="host (OCI ARM64)", border_style="blue")),
        )
        root["middle"].update(mid)

        try:
            tail = logs.tail(self.t, self.cfg, 14) if not st.errors else "\n".join(st.errors)
        except TransportError as e:
            tail = str(e)
        root["logs"].update(Panel(Text(tail[-4000:], style="dim", no_wrap=False),
                                  title=self.cfg.server.log_file, border_style="white"))
        footer = Text(self._busy or self.message,
                      style="yellow" if self._busy else "dim")
        root["footer"].update(Panel(footer))
        return root

    # ---------------------------------------------------------------- actions

    def handle_key(self, key: str) -> bool:
        """Returns False to quit."""
        if key == "q":
            return False
        if key == "r":
            self.refresh_slow()
        elif key == "s":
            self._run_bg("save-all flush sent", lambda: self.console.send("save-all flush"))
        elif key == "p":
            def do_purge():
                pid = self.ctl.find_pid()
                if pid:
                    rep = metrics.purge(self.t, self.cfg, pid)
                    self.message = (f"purge: freed {util.human_bytes(rep.freed)} "
                                    f"({rep.freed_pct:.0f}%) — {rep.verdict}")
            self._run_bg("running GC purge …", do_purge)
        elif key == "b":
            def do_backup():
                from .backup import BackupManager
                entry = BackupManager(self.cfg, self.t, self.console).create()
                if entry:
                    self.message = f"backup done: {entry.name} ({util.human_bytes(entry.size)})"
            self._run_bg("snapshotting world …", do_backup)
        elif key == "S":
            self._run_bg("starting server …", lambda: self.ctl.start(wait=True))
        elif key == "X":
            now = time.monotonic()
            if now < self._confirm_stop_until:
                self._confirm_stop_until = 0.0
                self._run_bg("stopping server …", lambda: self.ctl.stop())
            else:
                self._confirm_stop_until = now + 3.0
                self.message = "press X again within 3s to STOP the server"
        return True

    def _run_bg(self, busy: str, fn) -> None:
        if self._busy:
            self.message = f"busy: {self._busy}"
            return

        def wrapper():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - surfaced in the footer
                self.message = f"error: {e}"
                log.exception("dashboard action failed")
            finally:
                self._busy = ""
                self.refresh_fast()

        self._busy = busy
        threading.Thread(target=wrapper, daemon=True).start()

    # ---------------------------------------------------------------- loop

    def run(self) -> None:
        if not sys.stdin.isatty():
            raise SystemExit("mcctl dash needs a TTY")
        keys: queue.Queue = queue.Queue()
        reader = _KeyReader(keys)
        reader.start()
        rc = RichConsole()
        self.refresh_slow()
        last_fast = last_slow = time.monotonic()
        try:
            with Live(self.render(), console=rc, screen=True, refresh_per_second=4) as live:
                while True:
                    with contextlib.suppress(queue.Empty):
                        if not self.handle_key(keys.get(timeout=0.25)):
                            break
                    now = time.monotonic()
                    if now - last_slow >= SLOW_TICK:
                        self.refresh_slow()
                        last_slow = last_fast = now
                    elif now - last_fast >= FAST_TICK:
                        self.refresh_fast()
                        last_fast = now
                    live.update(self.render())
        finally:
            reader.stop_flag.set()
            self.console.close()


def run_dash(cfg: Config, transport: BaseTransport) -> None:
    Dashboard(cfg, transport).run()
