"""Player management: list, whitelist, op/deop, kick/ban — co-op friendly."""

from __future__ import annotations

import json

from . import util
from .config import Config
from .console import Console, ConsoleError, PlayerList
from .transport import BaseTransport, TransportError

log = util.get_logger("players")


class PlayerError(RuntimeError):
    pass


class Players:
    def __init__(self, cfg: Config, transport: BaseTransport, console: Console):
        self.cfg = cfg
        self.t = transport
        self.console = console

    # ---------------------------------------------------------------- queries

    def online(self) -> PlayerList | None:
        return self.console.players()

    def whitelist(self) -> list[str]:
        """Live `whitelist list` when the server is up, whitelist.json otherwise."""
        try:
            out = self.console.send("whitelist list", timeout=8)
            if out and "whitelisted player" in out.lower():
                # "There are N whitelisted player(s): a, b" / "There are no whitelisted players"
                if ":" in out:
                    return [n.strip() for n in out.split(":", 1)[1].split(",") if n.strip()]
                return []
        except (ConsoleError, TransportError):
            pass
        try:
            raw = self.t.read_text(f"{self.cfg.server.server_dir}/whitelist.json", check=False)
            data = json.loads(raw or "[]")
            return sorted(e.get("name", "?") for e in data if isinstance(e, dict))
        except (TransportError, ValueError):
            return []

    # ---------------------------------------------------------------- mutations (server must be up)

    def _cmd(self, command: str, *, ok_words: tuple[str, ...] = ()) -> str:
        try:
            out = self.console.send(command, timeout=10)
        except (ConsoleError, TransportError) as e:
            raise PlayerError(
                f"cannot run {command.split()[0]!r}: server console unreachable ({e})"
            ) from e
        low = (out or "").lower()
        if "unknown" in low and "command" in low:
            raise PlayerError(f"server rejected: {command}")
        if ok_words and not any(w in low for w in ok_words):
            log.warning("unexpected reply to %r: %s", command, (out or "").strip()[:160])
        return out or ""

    def whitelist_add(self, name: str) -> str:
        return self._cmd(f"whitelist add {name}", ok_words=("added", "already"))

    def whitelist_remove(self, name: str) -> str:
        return self._cmd(f"whitelist remove {name}", ok_words=("removed", "not"))

    def whitelist_on(self) -> str:
        return self._cmd("whitelist on", ok_words=("now turned on", "already"))

    def whitelist_off(self) -> str:
        return self._cmd("whitelist off", ok_words=("now turned off", "already"))

    def op(self, name: str) -> str:
        return self._cmd(f"op {name}", ok_words=("opped", "already", "nothing"))

    def deop(self, name: str) -> str:
        return self._cmd(f"deop {name}", ok_words=("no longer", "nothing"))

    def kick(self, name: str, reason: str = "") -> str:
        return self._cmd(f"kick {name} {reason}".strip(), ok_words=("kicked",))

    def ban(self, name: str, reason: str = "") -> str:
        return self._cmd(f"ban {name} {reason}".strip(), ok_words=("banned", "nothing"))

    def pardon(self, name: str) -> str:
        return self._cmd(f"pardon {name}", ok_words=("unbanned", "nothing"))
