"""Console facade: talk to the Minecraft server console over the best channel.

Preference order:
  1. RCON through an SSH tunnel — reliable request/response, no log scraping.
     The password is read from the remote server.properties on demand; mcctl
     stores no secrets locally.
  2. tmux send-keys + log-offset capture — works even with RCON disabled,
     exactly like driving the console by hand.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass

from . import util
from .config import Config
from .rcon import RconClient, RconError
from .transport import BaseTransport, TransportError, q

log = util.get_logger("console")

_LIST_RE = re.compile(r"There are (\d+) of a max of (\d+) players online:?\s*(.*)", re.IGNORECASE)


class ConsoleError(RuntimeError):
    pass


@dataclass(slots=True)
class PlayerList:
    count: int
    max: int
    names: list[str]


class Console:
    def __init__(self, cfg: Config, transport: BaseTransport):
        self.cfg = cfg
        self.t = transport
        self._rcon: RconClient | None = None
        self._tunnel_cm = None
        self._channel: str | None = None

    # ---------------------------------------------------------------- helpers

    def _props_path(self) -> str:
        return f"{self.cfg.server.server_dir}/server.properties"

    def rcon_settings(self) -> tuple[bool, int, str]:
        """(enabled, port, password) straight from the remote server.properties."""
        try:
            text = self.t.read_text(self._props_path(), check=False)
        except TransportError:
            return False, self.cfg.server.rcon_port, ""
        enabled, port, password = False, self.cfg.server.rcon_port, ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("enable-rcon="):
                enabled = line.split("=", 1)[1].strip().lower() == "true"
            elif line.startswith("rcon.port="):
                with contextlib.suppress(ValueError):
                    port = int(line.split("=", 1)[1].strip())
            elif line.startswith("rcon.password="):
                password = line.split("=", 1)[1].strip()
        return enabled and bool(password), port, password

    def _ensure_rcon(self) -> RconClient | None:
        if self._rcon:
            return self._rcon
        enabled, port, password = self.rcon_settings()
        if not enabled:
            return None
        try:
            self._tunnel_cm = self.t.tunnel(port)
            lport = self._tunnel_cm.__enter__()
            self._rcon = RconClient("127.0.0.1", lport, password).connect()
            self._channel = "rcon"
            log.debug("RCON channel up via tunnel :%d", lport)
            return self._rcon
        except (RconError, TransportError) as e:
            log.debug("RCON unavailable (%s); falling back to tmux", e)
            self._teardown_rcon()
            return None

    def _teardown_rcon(self) -> None:
        if self._rcon:
            with contextlib.suppress(Exception):
                self._rcon.close()
            self._rcon = None
        if self._tunnel_cm:
            with contextlib.suppress(Exception):
                self._tunnel_cm.__exit__(None, None, None)
            self._tunnel_cm = None

    # ---------------------------------------------------------------- tmux path

    def _log_path(self) -> str:
        return f"{self.cfg.server.server_dir}/{self.cfg.server.log_file}"

    def log_size(self) -> int:
        r = self.t.run(f"wc -c < {q(self._log_path())} 2>/dev/null || echo 0", timeout=15)
        try:
            return int(r.out.strip())
        except ValueError:
            return 0

    def log_from(self, offset: int) -> str:
        r = self.t.run(
            f"tail -c +{offset + 1} {q(self._log_path())} 2>/dev/null || true", timeout=20
        )
        return r.out

    def tmux_send(self, cmd: str) -> None:
        s = self.cfg.server.tmux_session
        script = (
            f"tmux send-keys -t {q(s)} -l {q(cmd)} && tmux send-keys -t {q(s)} Enter"
        )
        r = self.t.run(script, timeout=15)
        if not r.ok:
            raise ConsoleError(
                f"tmux session '{s}' not reachable — is the server running? ({r.err.strip()[:200]})"
            )

    def _tmux_query(self, cmd: str, *, settle: float = 1.2, timeout: float = 8.0) -> str:
        """Send via tmux and return the log delta — best-effort output capture."""
        offset = self.log_size()
        self.tmux_send(cmd)
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            time.sleep(min(settle, max(0.0, deadline - time.monotonic())))
            delta = self.log_from(offset)
            if delta and delta == last:
                break  # output stopped growing
            last = delta
            if delta and timeout <= settle:
                break
        return last

    # ---------------------------------------------------------------- public api

    @property
    def channel(self) -> str | None:
        return self._channel

    def send(self, cmd: str, *, timeout: float = 10.0) -> str:
        """Run a console command, return its (color-stripped) output, '' if unknown."""
        rcon = self._ensure_rcon()
        if rcon:
            try:
                return util.strip_mc_codes(rcon.command(cmd, max_wait=timeout))
            except RconError as e:
                log.warning("RCON send failed (%s); retrying over tmux", e)
                self._teardown_rcon()
        self._channel = "tmux"
        return util.strip_mc_codes(self._tmux_query(cmd, timeout=timeout))

    def send_and_wait(self, cmd: str, pattern: str, *, timeout: float = 30.0) -> str | None:
        """Send a command and wait for `pattern` (regex) in its reply or the server log.

        The log offset is captured before sending so async results (e.g. spark's
        profiler-complete line, "Saved the game") are caught even over RCON,
        which only relays the immediate reply.
        """
        rx = re.compile(pattern)
        offset = self.log_size()
        sent = False
        rcon = self._ensure_rcon()
        if rcon:
            try:
                reply = util.strip_mc_codes(rcon.command(cmd, max_wait=min(timeout, 10.0)))
                sent = True
                m = rx.search(reply)
                if m:
                    return m.group(0)
            except RconError:
                self._teardown_rcon()
        if not sent:
            self._channel = "tmux"
            self.tmux_send(cmd)
        return self.wait_in_log(pattern, offset, timeout=timeout)

    def wait_in_log(self, pattern: str, offset: int, *, timeout: float = 30.0,
                    poll: float = 1.0) -> str | None:
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = rx.search(self.log_from(offset))
            if m:
                return m.group(0)
            time.sleep(poll)
        return None

    def players(self) -> PlayerList | None:
        try:
            out = self.send("list", timeout=8.0)
        except (ConsoleError, TransportError):
            return None
        m = _LIST_RE.search(out)
        if not m:
            return None
        names = [n.strip() for n in m.group(3).split(",") if n.strip()]
        return PlayerList(int(m.group(1)), int(m.group(2)), names)

    def say(self, message: str) -> None:
        with contextlib.suppress(ConsoleError, TransportError):
            self.send(f"say {message}", timeout=5.0)

    def attach(self) -> int:
        s = self.cfg.server.tmux_session
        print(f"Attaching to tmux session '{s}' — detach with Ctrl-b d (NOT Ctrl-c).")
        return self.t.interactive(["tmux", "attach", "-t", s])

    def rcon_available(self) -> bool:
        return self._ensure_rcon() is not None

    def close(self) -> None:
        self._teardown_rcon()
