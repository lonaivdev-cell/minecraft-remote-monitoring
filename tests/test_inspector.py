"""inspector: /proc parsing against canned kernel output (FakeTransport)."""

import pytest

from mcctl import inspector


def test_sections_registry_complete():
    assert set(inspector.SECTIONS) == set(inspector.EXPLAIN)
    for s in inspector.SECTIONS:
        assert inspector.EXPLAIN[s].strip()


def test_need_pid_raises_when_down():
    with pytest.raises(inspector.InspectError, match="not running"):
        inspector._need_pid(None)


def test_blocks_splitter():
    out = "== a\nline1\nline2\n== b\nx\n"
    assert inspector._blocks(out) == {"a": "line1\nline2", "b": "x"}


def test_gather_tree_parses_ancestry(fake_t, cfg):
    fake_t.expect("while", out=(
        "4242\x1f4242 (java) S 4100 1 1 0 -1\x1f/opt/graalvm/bin/java -Xmx12G\n"
        "4100\x1f4100 (start.sh) S 4000 1 1 0 -1\x1fbash start.sh\n"
        "4000\x1f4000 (tmux: server) S 1 1 1 0 -1\x1ftmux\n"
        "1\x1f1 (systemd) S 0 1 1 0 -1\x1f/sbin/init\n"
    ))
    rep = inspector.gather_tree(fake_t, cfg, 4242)
    chain = rep.data["chain"]
    assert [p["comm"] for p in chain] == ["java", "start.sh", "tmux: server", "systemd"]
    assert chain[0]["ppid"] == 4100
    assert "the Minecraft server JVM" in rep.text
    assert "PID 1" in rep.text


def test_gather_threads_sorts_by_cpu(fake_t, cfg):
    def stat(tid, name, state, utime, stime):
        rest = f"{state} 1 1 1 0 -1 4194560 1 0 0 0 {utime} {stime} 0 0 20 0 1 0 100"
        return f"{tid} ({name}) {rest}"
    fake_t.expect("task", out="\n".join([
        stat(10, "Server thread", "S", 5000, 100),
        stat(11, "Netty Epoll #0", "S", 50, 900),
        stat(12, "GC Thread#0", "R", 10, 5),
    ]))
    rep = inspector.gather_threads(fake_t, cfg, 4242)
    assert rep.data["count"] == 3
    top = rep.data["top"]
    assert top[0]["name"] == "Server thread" and top[0]["cpu_ticks"] == 5100
    assert rep.data["by_state"] == {"S": 2, "R": 1}
    assert "game loop" in rep.text


def test_gather_memory_categorizes_maps(fake_t, cfg):
    fake_t.expect("smaps_rollup", out=(
        "== rollup\n"
        "Rss:            8388608 kB\nPss:            8000000 kB\n"
        "Anonymous:      8000000 kB\nSwap:                 0 kB\n"
        "== maps\n"
        "0000-1000 [heap]\n"
        "0000-2000 \n"
        "0000-1000 /usr/lib/libc.so.6\n"
        "0000-3000 /opt/minecraft/mods/sodium.jar\n"
    ))
    rep = inspector.gather_memory(fake_t, cfg, 4242)
    assert rep.data["rollup"]["Rss"] == 8388608 * 1024
    cats = rep.data["mappings"]
    assert cats["heap(C)"]["bytes"] == 0x1000
    assert cats["anon"]["bytes"] == 0x2000
    assert cats["lib(.so)"]["count"] == 1
    assert cats["jar"]["bytes"] == 0x3000


def test_gather_fds_classifies(fake_t, cfg):
    fake_t.expect("/fd/", out=(
        "/opt/minecraft/world/region/r.0.0.mca\n"
        "/opt/minecraft/world/region/r.0.0.mca\n"
        "socket:[123]\nsocket:[124]\npipe:[55]\n"
        "anon_inode:[eventpoll]\n"
    ))
    rep = inspector.gather_fds(fake_t, cfg, 4242)
    assert rep.data["total"] == 6
    assert rep.data["by_kind"] == {"file": 2, "socket": 2, "pipe": 1, "eventpoll": 1}
    assert rep.data["files"]["/opt/minecraft/world/region/r.0.0.mca"] == 2


def test_gather_net_parses_ss(fake_t, cfg):
    fake_t.expect("ss -tunap", out=(
        "Netid State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'tcp   LISTEN 0      50     0.0.0.0:25565     0.0.0.0:*         users:(("java",pid=4242,fd=140))\n'
        'tcp   ESTAB  0      0      10.0.0.5:25565    93.40.1.2:51234   users:(("java",pid=4242,fd=141))\n'
    ))
    rep = inspector.gather_net(fake_t, cfg, 4242)
    socks = rep.data["sockets"]
    assert len(socks) == 2
    assert socks[0]["state"] == "LISTEN" and "25565" in socks[0]["local"]


def test_gather_env_masks_secrets(fake_t, cfg):
    fake_t.expect("environ", out="JAVA=/opt/graalvm/bin/java\nRCON_PASSWORD=hunter2\nHOME=/home/ubuntu\n")
    rep = inspector.gather_env(fake_t, cfg, 4242)
    assert rep.data["env"]["RCON_PASSWORD"] == "********"
    assert rep.data["env"]["JAVA"] == "/opt/graalvm/bin/java"
    assert "hunter2" not in rep.text


def test_gather_host_parses_psi_and_meminfo(fake_t, cfg):
    fake_t.expect("loadavg", out=(
        "== kernel\nLinux 6.8.0 aarch64 GNU/Linux\n"
        "== uptime\n86400.5 170000.1\n"
        "== loadavg\n1.20 0.80 0.50 2/350 9999\n"
        "== nproc\n4\n"
        "== psi_cpu\nsome avg10=1.50 avg60=0.80 avg300=0.40 total=12345\n"
        "== psi_mem\nsome avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        "== psi_io\n\n"
        "== meminfo\nMemTotal:       24668932 kB\nMemAvailable:    9000000 kB\n"
    ))
    rep = inspector.gather_host(fake_t, cfg)
    assert rep.data["uptime_s"] == 86400
    assert rep.data["cpus"] == "4"
    assert rep.data["meminfo"]["MemTotal"] == 24668932 * 1024
    assert "avg10=1.50" in rep.data["pressure"]["cpu"]


def test_inspect_section_unknown_name(fake_t, cfg):
    with pytest.raises(inspector.InspectError, match="unknown section"):
        inspector.inspect_section(fake_t, cfg, "nope", 1)
