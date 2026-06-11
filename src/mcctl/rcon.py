"""Minimal Source-RCON client (the protocol Minecraft speaks on rcon.port).

Packet: <i32 length> <i32 request-id> <i32 type> <payload> <0x00 0x00>
Types:  3 = SERVERDATA_AUTH, 2 = SERVERDATA_EXECCOMMAND / AUTH_RESPONSE,
        0 = SERVERDATA_RESPONSE_VALUE.

Minecraft fragments long replies (spark output!) into multiple 4096-byte
response packets with the same request id; we read until the socket goes
idle. Auth failure is signalled by request id == -1.

mcctl never exposes RCON to the internet: the port stays loopback-firewalled
on the server and we reach it through an SSH -L tunnel.
"""

from __future__ import annotations

import itertools
import socket
import struct

AUTH = 3
EXEC = 2
RESPONSE = 0

_MAX_PAYLOAD = 4096


class RconError(RuntimeError):
    pass


def pack_packet(req_id: int, ptype: int, payload: str) -> bytes:
    body = payload.encode("utf-8") + b"\x00\x00"
    return struct.pack("<iii", len(body) + 8, req_id, ptype) + body


def unpack_packet(data: bytes) -> tuple[int, int, str]:
    if len(data) < 10:
        raise RconError(f"short RCON packet ({len(data)} bytes)")
    req_id, ptype = struct.unpack("<ii", data[:8])
    return req_id, ptype, data[8:-2].decode("utf-8", errors="replace")


class RconClient:
    def __init__(self, host: str, port: int, password: str, *, timeout: float = 6.0):
        self._addr = (host, port)
        self._password = password
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------- wire

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock
        buf = b""
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except TimeoutError:
                raise  # command() uses idle timeouts to detect a complete reply
            except OSError as e:
                # reset/refused/broken pipe — common through the SSH -L tunnel when
                # the server isn't actually serving RCON; surface as RconError so the
                # Console falls back to tmux instead of crashing the caller.
                raise RconError(f"RCON connection lost: {e}") from e
            if not chunk:
                raise RconError("connection closed by server")
            buf += chunk
        return buf

    def _read_packet(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._recv_exact(4))
        if not 0 < length <= _MAX_PAYLOAD + 16:
            raise RconError(f"implausible RCON packet length {length}")
        return unpack_packet(self._recv_exact(length))

    # ---------------------------------------------------------------- api

    def connect(self) -> RconClient:
        try:
            self._sock = socket.create_connection(self._addr, timeout=self._timeout)
        except OSError as e:
            raise RconError(f"cannot connect to RCON at {self._addr[0]}:{self._addr[1]}: {e}") from e
        req = next(self._ids)
        try:
            self._sock.sendall(pack_packet(req, AUTH, self._password))
        except OSError as e:
            raise RconError(f"RCON connection lost during auth: {e}") from e
        # Some servers send an empty RESPONSE_VALUE before the auth response.
        for _ in range(3):
            rid, ptype, _body = self._read_packet()
            if ptype == EXEC:  # AUTH_RESPONSE shares the type-2 value
                if rid == -1:
                    raise RconError("RCON authentication failed (wrong rcon.password?)")
                return self
        raise RconError("no RCON auth response")

    def command(self, cmd: str, *, idle: float = 0.35, max_wait: float = 8.0) -> str:
        if not self._sock:
            raise RconError("not connected")
        if len(cmd) > 1446:
            raise RconError("command too long for a single RCON packet")
        req = next(self._ids)
        try:
            self._sock.sendall(pack_packet(req, EXEC, cmd))
        except OSError as e:
            raise RconError(f"RCON connection lost sending {cmd!r}: {e}") from e
        parts: list[str] = []
        import time
        start = time.monotonic()
        while time.monotonic() - start < max_wait:
            remaining = max_wait - (time.monotonic() - start)
            self._sock.settimeout(idle if parts else min(remaining, self._timeout))
            try:
                rid, ptype, body = self._read_packet()
            except TimeoutError:
                if parts:
                    break  # idle after at least one fragment: reply complete
                raise RconError(f"no RCON reply to {cmd!r} within {max_wait:.0f}s") from None
            if ptype == RESPONSE and rid == req:
                parts.append(body)
                if len(body.encode()) < _MAX_PAYLOAD - 64:
                    break  # short fragment == final fragment
        return "".join(parts)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> RconClient:
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()
