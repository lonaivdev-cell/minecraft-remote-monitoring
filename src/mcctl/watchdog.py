"""Self-healing watchdog: observe → decide (pure) → act, with a crash-loop breaker.

Design rules:
  - `decide()` is a pure function over an Observation + persisted state, so the
    whole decision matrix is unit-testable without a server;
  - the watchdog only resurrects the server when user intent (`desired`) is
    "up" — `mcctl stop` will never be fought;
  - at most max_restarts within restart_window, then HALT loudly and stay
    halted until a human runs `mcctl start` or `mcctl watchdog arm`;
  - evidence (pane, log tail, crash report) is collected *before* the corpse
    is reaped.
"""

from __future__ import annotations

import contextlib
import enum
import time
from dataclasses import dataclass

from . import logs, metrics, state, util
from .config import Config
from .console import Console, ConsoleError
from .server import ServerControl, ServerError
from .spark import Spark, SparkError
from .transport import BaseTransport, TransportError

log = util.get_logger("watchdog")


class Action(enum.Enum):
    NOOP = "noop"
    START = "start"                  # process down, desired up
    RESTART_FROZEN = "restart-frozen"
    HALT_CRASHLOOP = "halt-crashloop"
    ALERT_TPS = "alert-tps"
    ALERT_HEAP = "alert-heap"
    ALERT_SSH = "alert-ssh"
    ALERT_DISK = "alert-disk"
    AUTOSAVE = "autosave"


@dataclass(slots=True)
class Observation:
    ts: float
    ssh_ok: bool = False
    proc_up: bool = False
    tmux_up: bool = False
    pane_dead: bool = False
    port_open: bool = False
    log_age_s: int | None = None
    console_ok: bool | None = None      # None = not probed
    tps: float | None = None
    heap_pct: float | None = None
    disk_free: int | None = None
    players: int | None = None


def decide(obs: Observation, st: dict, cfg: Config, *, last_save_ts: float = 0.0) -> list[Action]:
    w = cfg.watchdog
    if not st.get("armed"):
        return [Action.NOOP]
    if st.get("halted"):
        return [Action.NOOP]
    if not obs.ssh_ok:
        return [Action.ALERT_SSH]

    actions: list[Action] = []
    desired_up = st.get("desired") == "up"

    if desired_up and not obs.proc_up:
        recent = [t for t in st.get("restarts", []) if t > obs.ts - w.restart_window]
        if len(recent) >= w.max_restarts:
            return [Action.HALT_CRASHLOOP]
        return [Action.START]

    if obs.proc_up:
        frozen = (
            obs.log_age_s is not None
            and obs.log_age_s >= w.freeze_log_age
            and obs.console_ok is False
        )
        if frozen:
            recent = [t for t in st.get("restarts", []) if t > obs.ts - w.restart_window]
            if len(recent) >= w.max_restarts:
                return [Action.HALT_CRASHLOOP]
            return [Action.RESTART_FROZEN]
        if obs.tps is not None and obs.tps < w.tps_alert:
            actions.append(Action.ALERT_TPS)
        if obs.heap_pct is not None and obs.heap_pct >= w.heap_alert_pct:
            actions.append(Action.ALERT_HEAP)
        if obs.disk_free is not None and obs.disk_free < int(cfg.backup.min_free_gb * 1024**3):
            actions.append(Action.ALERT_DISK)
        if (
            w.autosave_minutes > 0
            and obs.ts - last_save_ts >= w.autosave_minutes * 60
        ):
            actions.append(Action.AUTOSAVE)

    return actions or [Action.NOOP]


class Watchdog:
    def __init__(self, cfg: Config, transport: BaseTransport):
        self.cfg = cfg
        self.t = transport
        self.console = Console(cfg, transport)
        self.ctl = ServerControl(cfg, transport, self.console)
        self._last_save = time.time()
        self._lag_strikes = 0

    # ---------------------------------------------------------------- observe

    def observe(self) -> Observation:
        obs = Observation(ts=time.time())
        try:
            kv = self.ctl.probe()
        except TransportError as e:
            log.warning("probe failed: %s", e)
            return obs
        obs.ssh_ok = True
        pid = kv.get("pid", "")
        obs.proc_up = pid.isdigit()
        obs.tmux_up = kv.get("tmux") == "1"
        obs.pane_dead = kv.get("pane_dead") == "1"
        obs.port_open = kv.get("port") == "1"
        if kv.get("log_age", "").isdigit():
            obs.log_age_s = int(kv["log_age"])
        if kv.get("disk_free", "").isdigit():
            obs.disk_free = int(kv["disk_free"])

        if obs.proc_up:
            # console probe doubles as freeze detector and player counter
            try:
                players = self.console.players()
                obs.console_ok = players is not None
                obs.players = players.count if players else None
            except (ConsoleError, TransportError):
                obs.console_ok = False
            with contextlib.suppress(SparkError, ConsoleError, TransportError):
                rep = Spark(self.console).tps()
                obs.tps = rep.tps_now
            heap = metrics.jvm_heap(self.t, self.cfg, int(pid))
            hmax = self.ctl.heap_max()
            if heap and hmax:
                obs.heap_pct = 100.0 * heap[0] / hmax
        return obs

    # ---------------------------------------------------------------- act

    def _notify(self, title: str, body: str, *, urgency: str = "normal") -> None:
        w = self.cfg.watchdog
        util.notify(title, body, desktop=w.notify_desktop, webhook_url=w.webhook_url,
                    urgency=urgency)

    def act(self, obs: Observation, actions: list[Action]) -> None:
        st = state.load()
        w = self.cfg.watchdog
        for action in actions:
            if action is Action.NOOP:
                continue
            log.info("watchdog action: %s", action.value)

            if action is Action.START:
                reason = "process down" + (" (crashed pane)" if obs.pane_dead or obs.tmux_up else "")
                logs.collect_evidence(self.t, self.cfg, f"watchdog restart: {reason}")
                n_recent = len([x for x in st.get("restarts", []) if x > obs.ts - w.restart_window])
                backoff = w.backoff_base * (2 ** n_recent)
                log.warning("server down (desired up) — restarting in %ds", backoff)
                time.sleep(backoff)
                state.record_restart()
                try:
                    with util.OpsLock():
                        self.ctl.start(wait=True)
                    self._notify("mcctl: server restarted",
                                 f"Self-heal after: {reason}. Restart #{n_recent + 1} this window.")
                except (ServerError, TransportError, util.LockHeldError) as e:
                    log.error("self-heal start failed: %s", e)
                    self._notify("mcctl: restart FAILED", str(e), urgency="critical")

            elif action is Action.RESTART_FROZEN:
                logs.collect_evidence(self.t, self.cfg, "watchdog restart: frozen "
                                      f"(log stale {obs.log_age_s}s, console dead)")
                self._thread_dump()
                state.record_restart()
                try:
                    with util.OpsLock():
                        self.ctl.restart(reason="server frozen — automatic restart", now=True)
                    self._notify("mcctl: frozen server restarted",
                                 f"Log stale {obs.log_age_s}s and console unresponsive.")
                except (ServerError, TransportError, util.LockHeldError) as e:
                    log.error("freeze restart failed: %s", e)
                    self._notify("mcctl: freeze restart FAILED", str(e), urgency="critical")

            elif action is Action.HALT_CRASHLOOP:
                st = state.load()
                if not st.get("halted"):
                    st["halted"] = True
                    state.save(st)
                    msg = (f"{w.max_restarts} restarts within {w.restart_window}s — watchdog "
                           "halted. Investigate (mcctl logs crash), then `mcctl start`.")
                    log.error(msg)
                    self._notify("mcctl: CRASH LOOP — watchdog halted", msg, urgency="critical")

            elif action is Action.ALERT_TPS:
                self._lag_strikes += 1
                if self._lag_strikes >= 3 and state.should_alert(st, "tps", 1800, obs.ts):
                    body = f"TPS {obs.tps:.1f} below {w.tps_alert} for 3+ checks."
                    if w.auto_profile_on_lag:
                        with contextlib.suppress(Exception):
                            url = Spark(self.console).profile(60)
                            body += f" Profiler: {url}"
                    self._notify("mcctl: server lagging", body)
            elif action is Action.ALERT_HEAP:
                if state.should_alert(st, "heap", 3600, obs.ts):
                    self._notify("mcctl: heap pressure",
                                 f"Heap at {obs.heap_pct:.0f}% of Xmx. Try `mcctl purge`.")
            elif action is Action.ALERT_DISK:
                if state.should_alert(st, "disk", 3600, obs.ts):
                    self._notify("mcctl: low disk on server",
                                 f"Free: {util.human_bytes(obs.disk_free)}.", urgency="critical")
            elif action is Action.ALERT_SSH:
                if state.should_alert(st, "ssh", 900, obs.ts):
                    self._notify("mcctl: server unreachable",
                                 "SSH probe failing — OCI box down or network out.",
                                 urgency="critical")
            elif action is Action.AUTOSAVE:
                with contextlib.suppress(ConsoleError, TransportError):
                    self.console.send("save-all", timeout=15)
                    self._last_save = obs.ts
                    log.info("autosave triggered")

        if Action.ALERT_TPS not in actions:
            self._lag_strikes = 0

    def _thread_dump(self) -> None:
        """jcmd Thread.print into the evidence dir — gold for freeze diagnosis."""
        pid = None
        with contextlib.suppress(TransportError):
            pid = self.ctl.find_pid()
        if not pid:
            return
        jc = f"{self.cfg.server.java_home}/bin/jcmd" if self.cfg.server.java_home else "jcmd"
        with contextlib.suppress(TransportError, OSError):
            r = self.t.run(f"{jc} {pid} Thread.print 2>/dev/null | head -c 200000", timeout=30)
            if r.out:
                import datetime as dt
                p = util.crashes_dir() / f"threaddump-{dt.datetime.now():%Y%m%d-%H%M%S}.txt"
                p.write_text(util.sanitize_terminal(r.out), encoding="utf-8")
                log.info("thread dump saved to %s", p)

    # ---------------------------------------------------------------- loop

    def step(self) -> list[Action]:
        obs = self.observe()
        st = state.load()
        actions = decide(obs, st, self.cfg, last_save_ts=self._last_save)
        if obs.ssh_ok and obs.proc_up:
            from .server import Status  # cheap partial sample for history
            s = Status(running=True, log_age_s=obs.log_age_s, disk_free=obs.disk_free)
            sample = metrics.sample_from_status(s)
            sample.update(tps=obs.tps, players=obs.players,
                          heap_pct=round(obs.heap_pct, 1) if obs.heap_pct else None)
            metrics.append_sample(sample)
        self.act(obs, actions)
        return actions

    def run_forever(self) -> None:
        log.info("watchdog started (interval %ds, armed=%s, desired=%s)",
                 self.cfg.watchdog.interval, state.load()["armed"], state.load()["desired"])
        while True:
            try:
                self.step()
            except Exception:  # noqa: BLE001 - the daemon must survive anything
                log.exception("watchdog step crashed; continuing")
            time.sleep(self.cfg.watchdog.interval)
