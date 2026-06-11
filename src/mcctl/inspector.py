"""Deep introspection: how the OS runs the server, from /proc up to the JVM.

Every section is one SSH round-trip that reads kernel-exported state
(/proc, ss, jcmd) and returns it parsed + rendered. Each section also
carries an EXPLAIN text — a plain-language walkthrough of what the kernel
structures mean — because this tool doubles as an operating-systems
learning instrument, not just a monitor.

Sections:
  tree     process ancestry: java <- start.sh <- tmux server <- init
  proc     /proc/<pid>/status|stat|limits|io|cgroup — the kernel's view
  threads  every thread in the JVM with state + CPU time (task/*/stat)
  memory   virtual memory map categories + smaps_rollup (RSS/PSS/swap)
  fds      open file descriptors classified (files/sockets/pipes/epoll)
  net      live sockets owned by the process (ss)
  env      the process environment block (secret values masked)
  jvm      jcmd: VM version, uptime, flags, heap regions
  host     kernel, PSI pressure, meminfo — the whole box
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import util
from .config import Config
from .transport import BaseTransport, q

log = util.get_logger("inspector")


class InspectError(RuntimeError):
    pass


@dataclass(slots=True)
class SectionReport:
    section: str
    title: str
    data: dict = field(default_factory=dict)
    text: str = ""           # rendered, terminal/GUI-ready

    def to_dict(self) -> dict:
        return {"section": self.section, "title": self.title, "data": self.data}


# ================================================================ explanations

EXPLAIN: dict[str, str] = {
    "tree": (
        "Every Linux process is born from another process (fork/exec) and the kernel\n"
        "remembers the parent PID (PPID). Reading PPIDs from /proc/<pid>/stat and\n"
        "walking upwards reconstructs the chain of custody for your server:\n"
        "  java          the Minecraft server itself (started by ServerStarterJar)\n"
        "  start.sh      the ServerPackCreator launcher script (bash)\n"
        "  tmux server   a daemon holding the pseudo-terminal; survives SSH logout\n"
        "  systemd/init  PID 1, the ancestor of everything\n"
        "If a parent dies, the orphan is re-parented to PID 1 — that's why a java\n"
        "process can show ppid=1 after its launcher exits."
    ),
    "proc": (
        "/proc is not a real filesystem — it's the kernel rendering its internal\n"
        "task_struct for each process as text files on demand.\n"
        "  State      R running, S sleeping (waiting for an event — normal for a\n"
        "             server), D uninterruptible (stuck in disk I/O — bad sign), Z zombie\n"
        "  VmRSS      resident set: bytes actually in physical RAM right now\n"
        "  VmHWM      high-water mark: the most RAM it has ever held\n"
        "  VmSwap     bytes pushed out to swap (non-zero under memory pressure)\n"
        "  Threads    one kernel-schedulable unit each; the JVM maps Java threads 1:1\n"
        "  ctxt_switches  voluntary = the thread chose to wait; nonvoluntary = the\n"
        "             scheduler preempted it (high values hint at CPU contention)\n"
        "  limits     per-process ceilings (ulimits); 'open files' caps sockets+files\n"
        "  cgroup     the resource-accounting box the process lives in (systemd slice)"
    ),
    "threads": (
        "Each line in /proc/<pid>/task/ is a thread — the kernel schedules threads,\n"
        "not processes. utime/stime are CPU ticks spent in userspace vs in the kernel\n"
        "(syscalls, I/O). Reading the names tells you how a modded server works:\n"
        "  Server thread       THE game loop — one tick every 50ms; if this thread\n"
        "                      is busy >50ms/tick, TPS drops below 20\n"
        "  Netty Epoll/IO      network workers moving packets to/from players\n"
        "  IO-Worker-*         async chunk/region file I/O\n"
        "  GC Thread / G1 *    garbage collector crews reclaiming heap memory\n"
        "  C1/C2 CompilerThre  the JIT compilers turning hot bytecode into machine code\n"
        "  modloading-worker   NeoForge parallel mod initialization (boot only)\n"
        "A healthy server: Server thread accumulates CPU steadily, GC threads stay\n"
        "modest, nothing sits in state D."
    ),
    "memory": (
        "A process sees a private *virtual* address space; the kernel maps pieces of\n"
        "it to physical RAM on demand. /proc/<pid>/maps lists every mapping:\n"
        "  anon        plain memory the process asked for — for the JVM this is the\n"
        "              Java heap + code cache + thread stacks (the big one)\n"
        "  [heap]      the C heap (malloc) — small for Java; used by native libs\n"
        "  [stack]     the main thread's stack\n"
        "  .so files   shared libraries mapped read-only — shared across processes\n"
        "  .jar files  mods mapped directly from disk for fast class loading\n"
        "smaps_rollup is the kernel's honest accounting:\n"
        "  Rss   in physical RAM now    Pss   RSS with shared pages split fairly\n"
        "  Anonymous  heap-like memory  Swap  pushed out to disk\n"
        "VmRSS >> Xmx is normal: heap + metaspace + threads + GC + NIO buffers."
    ),
    "fds": (
        "Unix: 'everything is a file descriptor'. /proc/<pid>/fd shows every handle\n"
        "the process holds — each is a small integer indexing a kernel table:\n"
        "  regular files   world region files (.mca), logs, mod jars held open\n"
        "  socket:[inode]  network endpoints — one per connected player + listeners\n"
        "  pipe:[inode]    one-way byte streams (tmux wiring, process plumbing)\n"
        "  anon_inode      kernel objects with no path: eventpoll = epoll instances\n"
        "                  (how Netty waits on thousands of sockets with one thread),\n"
        "                  eventfd = thread wakeup doorbells\n"
        "The 'open files' rlimit caps this table — leak fds and accepts start failing."
    ),
    "net": (
        "Sockets owned by the process, straight from the kernel socket tables (ss).\n"
        "  LISTEN       waiting for connections: 25565 = game port (TCP),\n"
        "               25575 = RCON (must be loopback/firewalled!)\n"
        "  ESTAB        a live connection — one per online player, plus your RCON\n"
        "               tunnel session\n"
        "  Send-Q       bytes the kernel accepted but hasn't pushed to the wire yet —\n"
        "               a growing Send-Q toward a player means *their* link is slow\n"
        "The game protocol is TCP: ordered, reliable, head-of-line blocking and all."
    ),
    "env": (
        "The environment block is a frozen snapshot copied into the process at exec()\n"
        "time — changing a variable in your shell later does NOT affect a running\n"
        "process. This is how start.sh's variables.txt choices (JAVA, heap flags)\n"
        "actually reach the JVM. Values that look like secrets are masked locally\n"
        "before display."
    ),
    "jvm": (
        "jcmd talks to the JVM's attach interface — a unix socket the JVM opens for\n"
        "diagnostics. What you're seeing:\n"
        "  VM.version    which JVM (GraalVM/HotSpot) and JIT is running\n"
        "  VM.flags      the *effective* flags — what the JVM actually chose after\n"
        "                combining your -Xmx with its own ergonomics\n"
        "  GC.heap_info  G1 divides the heap into equal regions; 'used' vs 'total'\n"
        "                committed is your live-data vs reserved picture\n"
        "Key flags: MaxHeapSize (Xmx), G1HeapRegionSize, ParallelGCThreads,\n"
        "ExplicitGCInvokesConcurrent (lets `mcctl purge` run a concurrent GC)."
    ),
    "host": (
        "The whole machine, from the kernel's perspective.\n"
        "  loadavg    runnable+uninterruptible tasks averaged over 1/5/15 min;\n"
        "             rule of thumb: sustained load > CPU count = saturation\n"
        "  PSI        pressure stall information — % of wall time tasks were *stalled*\n"
        "             waiting for cpu/memory/io. 'some' = at least one task stalled;\n"
        "             'full' = everyone stalled. Non-zero memory 'full' is an alarm.\n"
        "  MemAvailable  what the kernel could free without swapping — the honest\n"
        "             'free memory' number (free RAM is wasted RAM; cache fills it)\n"
        "  Dirty      pagecache bytes waiting to be written to disk"
    ),
}

SECTIONS = tuple(EXPLAIN)  # canonical order


# ================================================================ gather scripts

def _need_pid(pid: int | None) -> int:
    if not pid:
        raise InspectError("server is not running — nothing to inspect (try `mcctl inspect host`)")
    return int(pid)


def _blocks(out: str) -> dict[str, str]:
    """Split '== name' delimited script output into {name: body}."""
    parts: dict[str, str] = {}
    name = None
    buf: list[str] = []
    for line in out.splitlines():
        if line.startswith("== "):
            if name is not None:
                parts[name] = "\n".join(buf).strip("\n")
            name, buf = line[3:].strip(), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        parts[name] = "\n".join(buf).strip("\n")
    return parts


def _kv_lines(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- tree

_STAT_RE = re.compile(r"^(\d+) \((.*)\) (\S) (\d+)", re.DOTALL)

_TREE_NOTES = {
    "systemd": "PID 1 — the init system; ancestor of every process",
    "init": "PID 1 — the init system; ancestor of every process",
    "sshd": "the OpenSSH daemon that accepted a login",
    "bash": "a shell (start.sh runs under one)",
    "sh": "a shell",
    "start.sh": "ServerPackCreator launcher",
    "tmux": "tmux — holds the server's pseudo-terminal; survives logout",
    "java": "the Minecraft server JVM",
}


def gather_tree(t: BaseTransport, cfg: Config, pid: int | None) -> SectionReport:
    pid = _need_pid(pid)
    script = (
        f"p={int(pid)}\n"
        "while [ -n \"$p\" ] && [ \"$p\" -gt 0 ] 2>/dev/null; do\n"
        "  stat=$(cat /proc/$p/stat 2>/dev/null) || break\n"
        "  cmd=$(tr '\\0' ' ' < /proc/$p/cmdline 2>/dev/null | head -c 240)\n"
        "  printf '%s\\x1f%s\\x1f%s\\n' \"$p\" \"$stat\" \"$cmd\"\n"
        "  p=$(echo \"$stat\" | sed 's/^[0-9]* (.*) [A-Za-z] \\([0-9]*\\) .*/\\1/')\n"
        "done\n"
    )
    r = t.run(script, timeout=20)
    chain = []
    for line in r.out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        m = _STAT_RE.match(parts[1])
        if not m:
            continue
        comm = m.group(2)
        chain.append({
            "pid": int(m.group(1)), "comm": comm, "state": m.group(3),
            "ppid": int(m.group(4)),
            "cmdline": util.sanitize_terminal(parts[2]).strip(),
            "note": next((n for k, n in _TREE_NOTES.items() if k in comm), ""),
        })
    lines = ["process ancestry (child -> ... -> PID 1):", ""]
    for depth, p in enumerate(chain):
        indent = "  " * (len(chain) - 1 - depth)
        cmd = (p["cmdline"][:90] + "…") if len(p["cmdline"]) > 90 else p["cmdline"]
        lines.append(f"{indent}{p['pid']:>7}  {p['comm']:<18} [{p['state']}]  {cmd}")
        if p["note"]:
            lines.append(f"{indent}         └─ {p['note']}")
    return SectionReport("tree", "Process ancestry", {"chain": chain}, "\n".join(lines))


# ---------------------------------------------------------------- proc

_STATUS_KEYS = ("Name", "State", "Pid", "PPid", "Threads", "VmPeak", "VmSize", "VmRSS",
                "VmHWM", "VmSwap", "VmData", "VmStk", "FDSize",
                "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches")
_LIMIT_KEYS = ("Max open files", "Max processes", "Max locked memory", "Max address space")


def gather_proc(t: BaseTransport, cfg: Config, pid: int | None) -> SectionReport:
    pid = _need_pid(pid)
    p = f"/proc/{int(pid)}"
    script = (
        f"echo '== status'; cat {p}/status 2>/dev/null\n"
        f"echo '== cmdline'; tr '\\0' ' ' < {p}/cmdline 2>/dev/null; echo\n"
        f"echo '== cwd'; readlink -f {p}/cwd 2>/dev/null\n"
        f"echo '== exe'; readlink -f {p}/exe 2>/dev/null\n"
        f"echo '== oom'; cat {p}/oom_score 2>/dev/null\n"
        f"echo '== cgroup'; cat {p}/cgroup 2>/dev/null\n"
        f"echo '== limits'; cat {p}/limits 2>/dev/null\n"
        f"echo '== io'; cat {p}/io 2>/dev/null\n"
        f"echo '== etimes'; ps -o etimes= -p {int(pid)} 2>/dev/null | tr -d ' '\n"
    )
    b = _blocks(t.run(script, timeout=20).out)
    status = _kv_lines(b.get("status", ""))
    io = _kv_lines(b.get("io", ""))
    limits = {}
    for line in b.get("limits", "").splitlines():
        for key in _LIMIT_KEYS:
            if line.startswith(key):
                limits[key] = " ".join(line[len(key):].split()[:2])
    data = {
        "status": {k: status.get(k) for k in _STATUS_KEYS if k in status},
        "limits": limits,
        "io": {k: io.get(k) for k in ("read_bytes", "write_bytes") if k in io},
        "oom_score": b.get("oom", "").strip(),
        "cgroup": b.get("cgroup", "").strip().splitlines()[-1] if b.get("cgroup") else "",
        "cwd": b.get("cwd", "").strip(),
        "exe": b.get("exe", "").strip(),
        "cmdline": util.sanitize_terminal(b.get("cmdline", "").strip()),
        "uptime_s": int(b["etimes"]) if b.get("etimes", "").strip().isdigit() else None,
    }

    def _kb(v: str | None) -> str:
        if not v:
            return "—"
        n = v.split()[0]
        return util.human_bytes(int(n) * 1024) if n.isdigit() else v

    s = data["status"]
    lines = [
        f"the kernel's view of pid {pid} ({s.get('Name', '?')})",
        "",
        f"  state        {s.get('State', '?')}",
        f"  up           {util.human_duration(data['uptime_s'])}",
        f"  threads      {s.get('Threads', '?')}",
        f"  VmRSS        {_kb(s.get('VmRSS'))}   (in physical RAM now)",
        f"  VmHWM        {_kb(s.get('VmHWM'))}   (peak RAM ever)",
        f"  VmSize       {_kb(s.get('VmSize'))}   (virtual address space)",
        f"  VmSwap       {_kb(s.get('VmSwap'))}   (swapped out)",
        f"  ctx switches {s.get('voluntary_ctxt_switches', '?')} voluntary / "
        f"{s.get('nonvoluntary_ctxt_switches', '?')} preempted",
        f"  disk io      read {util.human_bytes(int(io['read_bytes'])) if io.get('read_bytes', '').isdigit() else '—'}"
        f" / written {util.human_bytes(int(io['write_bytes'])) if io.get('write_bytes', '').isdigit() else '—'}",
        f"  oom_score    {data['oom_score'] or '—'}  (higher = killed first under OOM)",
        "",
        "  limits:",
    ]
    for k, v in limits.items():
        lines.append(f"    {k:<18} {v}")
    lines += [
        "",
        f"  cwd     {data['cwd']}",
        f"  exe     {data['exe']}",
        f"  cgroup  {data['cgroup']}",
    ]
    return SectionReport("proc", f"Kernel view of pid {pid}", data, "\n".join(lines))


# ---------------------------------------------------------------- threads

_GROUPS = (
    ("Server thread", "game loop"),
    ("Netty", "network I/O"),
    ("IO-Worker", "async chunk/file I/O"),
    ("GC ", "garbage collection"),
    ("G1 ", "garbage collection"),
    ("CompilerThre", "JIT compiler"),
    ("VM Thread", "JVM internal ops"),
    ("modloading", "mod init workers"),
)


def gather_threads(t: BaseTransport, cfg: Config, pid: int | None) -> SectionReport:
    pid = _need_pid(pid)
    r = t.run(f"for f in /proc/{int(pid)}/task/*/stat; do cat \"$f\" 2>/dev/null; echo; done",
              timeout=25)
    threads = []
    for line in r.out.splitlines():
        line = line.strip()
        if not line or "(" not in line or ")" not in line:
            continue
        tid_s = line.split(" ", 1)[0]
        name = line[line.index("(") + 1:line.rindex(")")]
        rest = line[line.rindex(")") + 2:].split()
        # rest[0]=state, utime/stime are stat fields 14/15 -> rest[11]/rest[12]
        if len(rest) < 13 or not tid_s.isdigit():
            continue
        threads.append({"tid": int(tid_s), "name": name, "state": rest[0],
                        "cpu_ticks": int(rest[11]) + int(rest[12])})
    threads.sort(key=lambda x: -x["cpu_ticks"])
    by_state: dict[str, int] = {}
    for th in threads:
        by_state[th["state"]] = by_state.get(th["state"], 0) + 1
    data = {"count": len(threads), "by_state": by_state, "top": threads[:30]}

    lines = [f"{len(threads)} threads — states: "
             + "  ".join(f"{k}:{v}" for k, v in sorted(by_state.items())),
             "", "top by CPU time (user+kernel ticks since thread start):", ""]
    for th in threads[:30]:
        role = next((why for prefix, why in _GROUPS if th["name"].startswith(prefix)), "")
        lines.append(f"  {th['tid']:>8}  [{th['state']}] {th['cpu_ticks']:>10}  "
                     f"{th['name']:<28} {role}")
    return SectionReport("threads", "JVM threads", data, "\n".join(lines))


# ---------------------------------------------------------------- memory

def _categorize_map(path: str) -> str:
    if not path:
        return "anon"
    if path == "[heap]":
        return "heap(C)"
    if path.startswith("[stack"):
        return "stack"
    if path.startswith("["):
        return "kernel"
    if ".so" in path:
        return "lib(.so)"
    if path.endswith(".jar"):
        return "jar"
    return "file"


def gather_memory(t: BaseTransport, cfg: Config, pid: int | None) -> SectionReport:
    pid = _need_pid(pid)
    p = f"/proc/{int(pid)}"
    script = (
        f"echo '== rollup'; cat {p}/smaps_rollup 2>/dev/null\n"
        f"echo '== maps'; awk '{{print $1\" \"$6}}' {p}/maps 2>/dev/null\n"
    )
    b = _blocks(t.run(script, timeout=30).out)
    rollup = {}
    for k, v in _kv_lines(b.get("rollup", "")).items():
        n = v.split()[0] if v else ""
        if n.isdigit():
            rollup[k] = int(n) * 1024
    cats: dict[str, dict] = {}
    for line in b.get("maps", "").splitlines():
        parts = line.split(" ", 1)
        rng = parts[0].split("-")
        if len(rng) != 2:
            continue
        try:
            size = int(rng[1], 16) - int(rng[0], 16)
        except ValueError:
            continue
        cat = _categorize_map(parts[1].strip() if len(parts) > 1 else "")
        c = cats.setdefault(cat, {"count": 0, "bytes": 0})
        c["count"] += 1
        c["bytes"] += size
    data = {"rollup": rollup, "mappings": cats}

    lines = ["physical memory (smaps_rollup — the kernel's honest accounting):", ""]
    for k in ("Rss", "Pss", "Anonymous", "Shared_Clean", "Private_Dirty", "Swap"):
        if k in rollup:
            lines.append(f"  {k:<14} {util.human_bytes(rollup[k])}")
    lines += ["", "virtual address space by mapping type:", ""]
    for cat, c in sorted(cats.items(), key=lambda kv: -kv[1]["bytes"]):
        lines.append(f"  {cat:<10} {c['count']:>6} maps   {util.human_bytes(c['bytes']):>10} reserved")
    lines += ["", "note: 'anon' holds the Java heap; reserved virtual space is not RAM —",
              "compare with Rss above to see what's actually resident."]
    return SectionReport("memory", "Memory map", data, "\n".join(lines))


# ---------------------------------------------------------------- fds

def gather_fds(t: BaseTransport, cfg: Config, pid: int | None) -> SectionReport:
    pid = _need_pid(pid)
    r = t.run(
        f"for f in /proc/{int(pid)}/fd/*; do readlink \"$f\" 2>/dev/null; done", timeout=25
    )
    counts: dict[str, int] = {}
    files: dict[str, int] = {}
    for target in r.out.splitlines():
        target = target.strip()
        if not target:
            continue
        if target.startswith("socket:"):
            kind = "socket"
        elif target.startswith("pipe:"):
            kind = "pipe"
        elif target.startswith("anon_inode:"):
            kind = target[len("anon_inode:"):].strip("[]") or "anon_inode"
        elif target.startswith("/"):
            kind = "file"
            short = target
            if len(short) > 100:
                short = "…" + short[-99:]
            files[short] = files.get(short, 0) + 1
        else:
            kind = "other"
        counts[kind] = counts.get(kind, 0) + 1
    total = sum(counts.values())
    data = {"total": total, "by_kind": counts,
            "files": dict(sorted(files.items(), key=lambda kv: -kv[1])[:40])}

    lines = [f"{total} open file descriptors:", ""]
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {kind:<12} {n:>6}")
    if files:
        lines += ["", "open regular files (deduplicated):", ""]
        for path, n in list(data["files"].items())[:25]:
            lines.append(f"  {n:>3}x  {path}")
    return SectionReport("fds", "File descriptors", data, "\n".join(lines))


# ---------------------------------------------------------------- net

def gather_net(t: BaseTransport, cfg: Config, pid: int | None) -> SectionReport:
    pid = _need_pid(pid)
    r = t.run(f"ss -tunap 2>/dev/null | awk -v p='pid={int(pid)},' 'NR==1 || index($0, p)'",
              timeout=20)
    rows = []
    lines_out = r.out.splitlines()
    for line in lines_out[1:]:
        parts = line.split()
        if len(parts) >= 6:
            rows.append({"proto": parts[0], "state": parts[1], "recvq": parts[2],
                         "sendq": parts[3], "local": parts[4], "peer": parts[5]})
    data = {"sockets": rows}
    out = [f"{len(rows)} sockets owned by pid {pid}:", ""]
    out.append(f"  {'proto':<6} {'state':<12} {'recv-q':>7} {'send-q':>7}  {'local':<28} peer")
    for s in rows:
        out.append(f"  {s['proto']:<6} {s['state']:<12} {s['recvq']:>7} {s['sendq']:>7}  "
                   f"{s['local']:<28} {s['peer']}")
    if not rows:
        out.append("  (none visible — ss may need the same user as the process)")
    return SectionReport("net", "Network sockets", data, "\n".join(out))


# ---------------------------------------------------------------- env

_SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASS|PWD|CRED|AUTH)", re.IGNORECASE)


def redact_env(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(k, "********" if _SECRET_RE.search(k) else v) for k, v in pairs]


def gather_env(t: BaseTransport, cfg: Config, pid: int | None) -> SectionReport:
    pid = _need_pid(pid)
    r = t.run(f"tr '\\0' '\\n' < /proc/{int(pid)}/environ 2>/dev/null", timeout=15)
    pairs = []
    for line in util.sanitize_terminal(r.out).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            pairs.append((k, v))
    pairs = redact_env(sorted(pairs))
    data = {"env": dict(pairs)}
    lines = [f"environment block frozen at exec() time ({len(pairs)} vars, secrets masked):", ""]
    lines += [f"  {k}={v}" for k, v in pairs]
    return SectionReport("env", "Process environment", data, "\n".join(lines))


# ---------------------------------------------------------------- jvm

def gather_jvm(t: BaseTransport, cfg: Config, pid: int | None) -> SectionReport:
    pid = _need_pid(pid)
    jc = f"{cfg.server.java_home}/bin/jcmd" if cfg.server.java_home else "jcmd"
    runner = f"if [ -x {q(jc)} ]; then jc={q(jc)}; else jc=jcmd; fi\n"
    script = (
        runner
        + f"echo '== version'; \"$jc\" {int(pid)} VM.version 2>&1\n"
        f"echo '== uptime'; \"$jc\" {int(pid)} VM.uptime 2>&1\n"
        f"echo '== heap'; \"$jc\" {int(pid)} GC.heap_info 2>&1\n"
        f"echo '== flags'; \"$jc\" {int(pid)} VM.flags 2>&1\n"
    )
    b = _blocks(t.run(script, timeout=40).out)
    interesting = ("MaxHeapSize", "InitialHeapSize", "G1HeapRegionSize", "MaxMetaspaceSize",
                   "ParallelGCThreads", "ConcGCThreads", "UseG1GC", "UseZGC",
                   "ExplicitGCInvokesConcurrent", "ReservedCodeCacheSize", "MaxDirectMemorySize")
    flags = []
    for tok in b.get("flags", "").replace("\n", " ").split():
        if any(f"{k}=" in tok or tok.lstrip("-+:X").startswith(k) for k in interesting):
            flags.append(tok)
    data = {
        "version": util.sanitize_terminal(b.get("version", "")).strip(),
        "uptime": util.sanitize_terminal(b.get("uptime", "")).strip(),
        "heap_info": util.sanitize_terminal(b.get("heap", "")).strip(),
        "flags": flags,
    }
    lines = ["JVM (via the jcmd attach interface):", ""]
    for ln in data["version"].splitlines()[1:4]:
        lines.append(f"  {ln}")
    up = data["uptime"].splitlines()
    if len(up) > 1:
        lines.append(f"  uptime: {up[-1]}")
    lines += ["", "effective GC/heap flags (post-ergonomics):", ""]
    lines += [f"  {f}" for f in flags] or ["  (jcmd unavailable?)"]
    lines += ["", "heap regions (GC.heap_info):", ""]
    lines += [f"  {ln}" for ln in data["heap_info"].splitlines()[1:8]]
    return SectionReport("jvm", "JVM internals", data, "\n".join(lines))


# ---------------------------------------------------------------- host

def gather_host(t: BaseTransport, cfg: Config, pid: int | None = None) -> SectionReport:
    script = (
        "echo '== kernel'; uname -srmo 2>/dev/null\n"
        "echo '== uptime'; cat /proc/uptime\n"
        "echo '== loadavg'; cat /proc/loadavg\n"
        "echo '== nproc'; nproc\n"
        "echo '== psi_cpu'; cat /proc/pressure/cpu 2>/dev/null\n"
        "echo '== psi_mem'; cat /proc/pressure/memory 2>/dev/null\n"
        "echo '== psi_io'; cat /proc/pressure/io 2>/dev/null\n"
        "echo '== meminfo'; grep -E "
        "'^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Dirty):' /proc/meminfo\n"
    )
    b = _blocks(t.run(script, timeout=20).out)
    mem = {}
    for k, v in _kv_lines(b.get("meminfo", "")).items():
        n = v.split()[0]
        if n.isdigit():
            mem[k] = int(n) * 1024
    up = b.get("uptime", "0 0").split()
    data = {
        "kernel": b.get("kernel", "").strip(),
        "uptime_s": int(float(up[0])) if up and up[0].replace(".", "").isdigit() else None,
        "loadavg": b.get("loadavg", "").strip(),
        "cpus": b.get("nproc", "").strip(),
        "pressure": {k: b.get(f"psi_{k}", "").strip() for k in ("cpu", "mem", "io")},
        "meminfo": mem,
    }
    lines = [
        f"host: {data['kernel']}",
        f"  up {util.human_duration(data['uptime_s'])}, {data['cpus']} CPUs, "
        f"load {' '.join(data['loadavg'].split()[:3])}",
        "",
        "pressure stall information (% of time tasks waited on a resource):",
    ]
    for k in ("cpu", "mem", "io"):
        psi = data["pressure"][k] or "(not exposed by this kernel)"
        lines.append(f"  {k:<4} {psi.splitlines()[0] if psi else ''}")
        if psi and len(psi.splitlines()) > 1:
            lines.append(f"       {psi.splitlines()[1]}")
    lines += ["", "memory:"]
    for k in ("MemTotal", "MemAvailable", "Cached", "Dirty", "SwapTotal", "SwapFree"):
        if k in mem:
            lines.append(f"  {k:<13} {util.human_bytes(mem[k])}")
    return SectionReport("host", "Host machine", data, "\n".join(lines))


# ================================================================ public api

_GATHERERS = {
    "tree": gather_tree, "proc": gather_proc, "threads": gather_threads,
    "memory": gather_memory, "fds": gather_fds, "net": gather_net,
    "env": gather_env, "jvm": gather_jvm, "host": gather_host,
}


def inspect_section(t: BaseTransport, cfg: Config, section: str,
                    pid: int | None) -> SectionReport:
    if section not in _GATHERERS:
        raise InspectError(f"unknown section {section!r} — choose from: {', '.join(SECTIONS)}")
    return _GATHERERS[section](t, cfg, pid)
