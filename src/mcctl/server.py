"""Server lifecycle: status probe, start, graceful stop with escalation, restart.

Lessons from CarborioLand baked in:
  - liveness is verified by *process* (java with cwd == server_dir) AND tmux
    session, never by session name alone;
  - start drives `bash start.sh` (ServerStarterJar), not the old run.sh;
  - tmux panes keep their corpse (remain-on-exit) so crash output survives
    for the watchdog to collect;
  - mcctl start/stop records user intent so the watchdog never resurrects a
    server that was stopped on purpose.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import state, util
from .config import Config
from .console import Console, ConsoleError, PlayerList
from .transport import BaseTransport, TransportError, q

log = util.get_logger("server")

READY_RE = r"Done \([0-9.]+s\)!"


class ServerError(RuntimeError):
    pass


@dataclass(slots=True)
class Status:
    running: bool = False
    pid: int | None = None
    uptime_s: int | None = None
    tmux: bool = False
    pane_dead: bool = False
    port_open: bool = False
    log_age_s: int | None = None
    heap_used: int | None = None
    heap_committed: int | None = None
    heap_max: int | None = None
    host_mem_total: int | None = None
    host_mem_used: int | None = None
    host_mem_avail: int | None = None
    load: tuple[float, float, float] | None = None
    disk_free: int | None = None
    players: PlayerList | None = None
    tps: dict | None = None
    channel: str | None = None
    desired: str = "down"
    armed: bool = False
    halted: bool = False
    last_backup: str | None = None
    last_backup_age_s: int | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}  # type: ignore[attr-defined]
        if self.players:
            d["players"] = {"count": self.players.count, "max": self.players.max,
                            "names": self.players.names}
        if self.load:
            d["load"] = list(self.load)
        return d


def _pid_loop(server_dir: str) -> str:
    """Bash that prints the PID of the java process whose cwd is the server dir."""
    return (
        f"dir={q(server_dir)}\n"
        "for p in $(pgrep java 2>/dev/null); do\n"
        '  if [ "$(readlink -f /proc/$p/cwd 2>/dev/null)" = "$dir" ]; then echo "$p"; break; fi\n'
        "done\n"
    )


class ServerControl:
    def __init__(self, cfg: Config, transport: BaseTransport, console: Console | None = None,
                 sleeper: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.t = transport
        self.console = console or Console(cfg, transport)
        self._sleep = sleeper
        self._heap_max_cache: int | None = None

    # ---------------------------------------------------------------- probes

    def find_pid(self) -> int | None:
        r = self.t.run(_pid_loop(self.cfg.server.server_dir), timeout=15)
        out = r.out.strip()
        return int(out) if out.isdigit() else None

    def probe(self) -> dict[str, str]:
        """Single round-trip status probe; returns raw key=value pairs."""
        s = self.cfg.server
        b = self.cfg.backup
        script = (
            f"dir={q(s.server_dir)}; logf={q(s.server_dir + '/' + s.log_file)}; "
            f"sess={q(s.tmux_session)}; port={s.mc_port}; "
            f"bdir={q(b.remote_dir)}; bpfx={q(b.prefix)}\n"
            + _pid_loop(s.server_dir).replace('echo "$p"; break', 'pid="$p"; break')
            + 'echo "pid=$pid"\n'
            'if [ -n "$pid" ]; then echo "etimes=$(ps -o etimes= -p "$pid" | tr -d \' \')"; fi\n'
            'if tmux has-session -t "$sess" 2>/dev/null; then\n'
            "  echo tmux=1\n"
            "  echo \"pane_dead=$(tmux list-panes -t \"$sess\" -F '#{pane_dead}' 2>/dev/null | head -1)\"\n"
            "else echo tmux=0; fi\n"
            'if ss -tln 2>/dev/null | grep -qE ":$port( |$)"; then echo port=1; else echo port=0; fi\n'
            'if [ -f "$logf" ]; then echo "log_age=$(( $(date +%s) - $(stat -c %Y "$logf") ))"; fi\n'
            "echo \"mem=$(free -b | awk '/^Mem:/{print $2\" \"$3\" \"$7}')\"\n"
            "echo \"load=$(cut -d' ' -f1-3 /proc/loadavg)\"\n"
            'echo "disk_free=$(df -B1 --output=avail "$dir" 2>/dev/null | tail -1 | tr -d \' \')"\n'
            'last=$(ls -1t "$bdir/$bpfx"-*.tar.* 2>/dev/null | head -1)\n'
            'if [ -n "$last" ]; then echo "last_backup=$(basename "$last")"; '
            'echo "last_backup_ts=$(stat -c %Y "$last")"; fi\n'
        )
        r = self.t.run(script, timeout=25)
        kv: dict[str, str] = {}
        for line in r.out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        return kv

    def heap_max(self) -> int | None:
        """Xmx in bytes, parsed once from variables.txt."""
        if self._heap_max_cache is not None:
            return self._heap_max_cache
        from . import props
        try:
            text = props.load_variables(self.t, self.cfg)
            args = props.get_var(text, "JAVA_ARGS") or ""
            _, xmx = props.parse_heap(args)
            if xmx:
                self._heap_max_cache = props.size_to_bytes(xmx)
        except (TransportError, props.PropError):
            pass
        return self._heap_max_cache

    def status(self, *, full: bool = True) -> Status:
        st = Status()
        wd = state.load()
        st.desired, st.armed, st.halted = wd["desired"], wd["armed"], wd.get("halted", False)
        try:
            kv = self.probe()
        except TransportError as e:
            st.errors.append(f"ssh: {e}")
            return st
        pid = kv.get("pid", "")
        st.pid = int(pid) if pid.isdigit() else None
        st.running = st.pid is not None
        if kv.get("etimes", "").isdigit():
            st.uptime_s = int(kv["etimes"])
        st.tmux = kv.get("tmux") == "1"
        st.pane_dead = kv.get("pane_dead") == "1"
        st.port_open = kv.get("port") == "1"
        if kv.get("log_age", "").isdigit():
            st.log_age_s = int(kv["log_age"])
        mem = kv.get("mem", "").split()
        if len(mem) == 3 and all(x.isdigit() for x in mem):
            st.host_mem_total, st.host_mem_used, st.host_mem_avail = map(int, mem)
        with contextlib.suppress(ValueError):
            parts = kv.get("load", "").split()
            if len(parts) == 3:
                st.load = (float(parts[0]), float(parts[1]), float(parts[2]))
        if kv.get("disk_free", "").isdigit():
            st.disk_free = int(kv["disk_free"])
        st.last_backup = kv.get("last_backup")
        if kv.get("last_backup_ts", "").isdigit():
            st.last_backup_age_s = max(0, int(time.time()) - int(kv["last_backup_ts"]))

        if full and st.running:
            from . import metrics
            heap = metrics.jvm_heap(self.t, self.cfg, st.pid)
            if heap:
                st.heap_used, st.heap_committed = heap
            st.heap_max = self.heap_max()
            st.players = self.console.players()
            st.channel = self.console.channel
            from .spark import Spark, SparkError
            with contextlib.suppress(SparkError, ConsoleError, TransportError):
                st.tps = Spark(self.console).tps().to_dict()
        return st

    # ---------------------------------------------------------------- start

    def start(self, *, wait: bool = True,
              progress: Callable[[str], None] | None = None) -> None:
        s = self.cfg.server
        pid = self.find_pid()
        if pid:
            raise ServerError(f"server already running (pid {pid}) — nothing to do")

        eula = self.t.run(f"grep -qs '^eula=true' {q(s.server_dir + '/eula.txt')}", timeout=15)
        if not eula.ok:
            raise ServerError(f"eula.txt missing or not accepted in {s.server_dir}")

        # clear a leftover session (dead pane corpse from a previous crash)
        self.t.run(f"tmux kill-session -t {q(s.tmux_session)} 2>/dev/null || true", timeout=15)

        offset = self.console.log_size()
        script = (
            "set -e\n"
            f"tmux new-session -d -s {q(s.tmux_session)} -c {q(s.server_dir)} {q(s.start_command)}\n"
            f"tmux set-option -t {q(s.tmux_session)} remain-on-exit on 2>/dev/null || true\n"
        )
        self.t.run(script, timeout=20, check=True)
        state.set_desired("up")
        log.info("server launch dispatched (tmux session %s)", s.tmux_session)
        if not wait:
            return

        deadline = time.monotonic() + s.start_timeout
        last_line = ""
        while time.monotonic() < deadline:
            self._sleep(2.0)
            delta = self.console.log_from(offset)
            for line in reversed(delta.splitlines()):
                if line.strip():
                    last_line = util.sanitize_terminal(line.strip())[-160:]
                    break
            if progress and last_line:
                progress(last_line)
            if re.search(READY_RE, delta):
                log.info("server is up: %s", last_line)
                return
            if "You need to agree to the EULA" in delta:
                raise ServerError("server exited: EULA not accepted")
            kv = {}
            with contextlib.suppress(TransportError):
                kv = self.probe()
            if kv.get("tmux") == "0" or kv.get("pane_dead") == "1":
                tail = self._capture_pane(40)
                raise ServerError(
                    "server process died during startup; last console output:\n" + tail
                )
        raise ServerError(
            f"server did not report ready within {s.start_timeout}s "
            f"(last log line: {last_line or 'n/a'}) — check `mcctl logs`"
        )

    # ---------------------------------------------------------------- stop

    def stop(self, *, now: bool = False, reason: str = "") -> None:
        s = self.cfg.server
        state.set_desired("down")  # before anything else: the watchdog must stand down
        pid = self.find_pid()
        if pid is None:
            log.info("server already stopped")
            self._reap_session()
            return

        players = self.console.players()
        if not now and players and players.count > 0 and self.cfg.server.stop_countdown:
            steps = sorted(set(self.cfg.server.stop_countdown), reverse=True)
            log.info("warning %d player(s) before stop: %s", players.count, steps)
            for i, secs in enumerate(steps):
                why = f" ({reason})" if reason else ""
                self.console.say(f"Server stopping in {secs}s{why} — pausing world saves.")
                nxt = steps[i + 1] if i + 1 < len(steps) else 0
                self._sleep(max(0, secs - nxt))

        offset = self.console.log_size()
        with contextlib.suppress(ConsoleError, TransportError):
            self.console.send("save-all flush", timeout=10)
            self.console.wait_in_log(r"Saved the game", offset, timeout=30)
        with contextlib.suppress(ConsoleError, TransportError):
            self.console.send("stop", timeout=5)

        if self._wait_pid_gone(pid, self.cfg.server.stop_timeout):
            log.info("server stopped gracefully")
            self._reap_session()
            return

        log.warning("graceful stop timed out after %ss — escalating to SIGTERM", s.stop_timeout)
        self.t.run(f"kill -TERM {pid} 2>/dev/null || true", timeout=15)
        if self._wait_pid_gone(pid, 30):
            self._reap_session()
            return

        log.error("SIGTERM ignored — escalating to SIGKILL (world saved earlier via save-all)")
        self.t.run(f"kill -KILL {pid} 2>/dev/null || true", timeout=15)
        if not self._wait_pid_gone(pid, 15):
            raise ServerError(f"process {pid} survived SIGKILL — inspect the host manually")
        self._reap_session()

    def _wait_pid_gone(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._sleep(2.0)
            r = self.t.run(f"kill -0 {pid} 2>/dev/null && echo alive || echo gone", timeout=15)
            if "gone" in r.out:
                return True
        return False

    def _reap_session(self) -> None:
        self.t.run(
            f"tmux kill-session -t {q(self.cfg.server.tmux_session)} 2>/dev/null || true",
            timeout=15,
        )

    def _capture_pane(self, lines: int = 80) -> str:
        r = self.t.run(
            f"tmux capture-pane -p -t {q(self.cfg.server.tmux_session)} 2>/dev/null | tail -n {int(lines)}",
            timeout=15,
        )
        return util.sanitize_terminal(r.out)

    # ---------------------------------------------------------------- misc

    def restart(self, *, reason: str = "restart", now: bool = False,
                progress: Callable[[str], None] | None = None) -> None:
        self.stop(now=now, reason=reason)
        self._sleep(2.0)
        self.start(wait=True, progress=progress)

    def kill(self) -> None:
        """Emergency stop: TERM, then KILL. No countdown, no save — last resort."""
        state.set_desired("down")
        pid = self.find_pid()
        if pid is None:
            self._reap_session()
            return
        self.t.run(f"kill -TERM {pid} 2>/dev/null || true", timeout=15)
        if not self._wait_pid_gone(pid, 20):
            self.t.run(f"kill -KILL {pid} 2>/dev/null || true", timeout=15)
            self._wait_pid_gone(pid, 10)
        self._reap_session()
