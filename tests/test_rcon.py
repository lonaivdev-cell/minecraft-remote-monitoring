"""RCON protocol: packet codec + a real (threaded) fake server."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from mcctl.rcon import AUTH, EXEC, RESPONSE, RconClient, RconError, pack_packet, unpack_packet


def test_pack_unpack_roundtrip():
    raw = pack_packet(7, EXEC, "spark tps")
    (length,) = struct.unpack("<i", raw[:4])
    assert length == len(raw) - 4
    rid, ptype, body = unpack_packet(raw[4:])
    assert (rid, ptype, body) == (7, EXEC, "spark tps")


def test_unpack_rejects_short():
    with pytest.raises(RconError):
        unpack_packet(b"\x00\x01")


class FakeRconServer(threading.Thread):
    """Speaks just enough RCON: auth, echo command, fragmented long replies."""

    def __init__(self, password="hunter2", long_reply_for="spark tps"):
        super().__init__(daemon=True)
        self.password = password
        self.long_reply_for = long_reply_for
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def _read(self, conn) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._exact(conn, 4))
        return unpack_packet(self._exact(conn, length))

    @staticmethod
    def _exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def run(self):
        conn, _ = self.sock.accept()
        try:
            while True:
                rid, ptype, body = self._read(conn)
                if ptype == AUTH:
                    ok = body == self.password
                    conn.sendall(pack_packet(rid if ok else -1, EXEC, ""))
                elif ptype == EXEC:
                    if body == self.long_reply_for:
                        # two max-size fragments + one short tail
                        conn.sendall(pack_packet(rid, RESPONSE, "A" * 4090))
                        conn.sendall(pack_packet(rid, RESPONSE, "B" * 4090))
                        conn.sendall(pack_packet(rid, RESPONSE, "tail"))
                    else:
                        conn.sendall(pack_packet(rid, RESPONSE, f"echo:{body}"))
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()


def test_auth_and_command():
    srv = FakeRconServer()
    srv.start()
    with RconClient("127.0.0.1", srv.port, "hunter2", timeout=3) as c:
        assert c.command("list") == "echo:list"
        assert c.command("save-all") == "echo:save-all"


def test_fragmented_reply_reassembled():
    srv = FakeRconServer()
    srv.start()
    with RconClient("127.0.0.1", srv.port, "hunter2", timeout=3) as c:
        out = c.command("spark tps")
    assert out == "A" * 4090 + "B" * 4090 + "tail"


def test_auth_failure():
    srv = FakeRconServer(password="correct")
    srv.start()
    with pytest.raises(RconError, match="authentication failed"):
        RconClient("127.0.0.1", srv.port, "wrong", timeout=3).connect()


def test_connect_refused():
    with pytest.raises(RconError, match="cannot connect"):
        RconClient("127.0.0.1", 1, "x", timeout=0.5).connect()
