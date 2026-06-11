"""Preflight checks: local tooling, SSH, remote layout, JVM, RCON, spark, firewall.

Encodes the hard-won CarborioLand knowledge as executable checks:
SKIP_JAVA_CHECK for GraalVM, WAIT_FOR_USER_INPUT inside tmux, the IPv4/IPv6
bind fixes, ServerStarterJar force-fetch, and "verify by process AND session".
"""

from __future__ import annotations

import enum
import getpass
import secrets
import shutil
import socket
import subprocess
from dataclasses import dataclass

from . import props as propsmod
from . import util
from .config import Config
from .transport import BaseTransport, TransportError, q

log = util.get_logger("doctor")


class Level(enum.Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    FIXED = "fixed"
    SKIP = "skip"


@dataclass(slots=True)
class CheckResult:
    name: str
    level: Level
    detail: str = ""
    hint: str = ""


def _ok(name, detail=""):
    return CheckResult(name, Level.OK, detail)


def _warn(name, detail, hint=""):
    return CheckResult(name, Level.WARN, detail, hint)


def _fail(name, detail, hint=""):
    return CheckResult(name, Level.FAIL, detail, hint)


def run_doctor(cfg: Config, t: BaseTransport, *, fix: bool = False) -> list[CheckResult]:
    out: list[CheckResult] = []
    s = cfg.server

    # ---------------- local
    for binname, why in (("ssh", "openssh"), ("rsync", "rsync")):
        if shutil.which(binname):
            out.append(_ok(f"local: {binname}"))
        else:
            lvl = _fail if (binname == "ssh" and s.transport == "ssh") else _warn
            out.append(lvl(f"local: {binname}", "not found in PATH", f"pacman -S {why}"))
    out.append(_ok("local: state dir", str(util.state_dir())))

    # ---------------- connectivity
    try:
        import time
        t0 = time.monotonic()
        t.run("true", timeout=15)
        target = "local transport" if s.transport == "local" else f"{s.user}@{s.host}"
        out.append(_ok("ssh: connect", f"{target} ({(time.monotonic() - t0) * 1000:.0f} ms)"))
    except TransportError as e:
        out.append(_fail("ssh: connect", str(e),
                         "check keys/agent: ssh must work non-interactively (BatchMode)"))
        return out

    # ---------------- remote layout
    if t.exists(s.server_dir):
        out.append(_ok("remote: server_dir", s.server_dir))
    else:
        out.append(_fail("remote: server_dir", f"{s.server_dir} missing"))
        return out
    start_entry = s.start_command.split()[-1]
    if t.exists(f"{s.server_dir}/{start_entry}"):
        out.append(_ok("remote: start script", start_entry))
    else:
        out.append(_fail("remote: start script", f"{start_entry} not in {s.server_dir}",
                         "this pack boots via ServerPackCreator start.sh — check start_command"))

    for binname, hint in (("tmux", "apt install tmux"), ("zstd", "apt install zstd (else gzip fallback)")):
        if t.run(f"command -v {binname} >/dev/null", timeout=15).ok:
            out.append(_ok(f"remote: {binname}"))
        else:
            (out.append(_fail("remote: tmux", "required for the launch model", hint))
             if binname == "tmux" else out.append(_warn("remote: zstd", "missing", hint)))

    # ---------------- eula
    if t.run(f"grep -qs '^eula=true' {q(s.server_dir + '/eula.txt')}", timeout=15).ok:
        out.append(_ok("remote: eula accepted"))
    else:
        out.append(_fail("remote: eula", "eula.txt missing or false",
                         f"echo eula=true > {s.server_dir}/eula.txt (after reading it)"))

    # ---------------- variables.txt (ServerPackCreator)
    try:
        vtext = propsmod.load_variables(t, cfg)
        changed = False
        for key, want, why in (
            ("SKIP_JAVA_CHECK", "true", "SPC's check rejects GraalVM and reaches for Jabba"),
            ("WAIT_FOR_USER_INPUT", "false", "would hang waiting for Enter inside tmux"),
            ("SERVERSTARTERJAR_FORCE_FETCH", "false", "stop re-downloading server.jar each boot"),
        ):
            have = (propsmod.get_var(vtext, key) or "").lower()
            if have == want:
                out.append(_ok(f"vars: {key}={want}"))
            elif fix:
                vtext = propsmod.set_var(vtext, key, want)
                changed = True
                out.append(CheckResult(f"vars: {key}", Level.FIXED, f"{have or 'unset'} -> {want}"))
            else:
                out.append(_warn(f"vars: {key}", f"is {have or 'unset'}, want {want} — {why}",
                                 "mcctl doctor --fix"))
        java = propsmod.get_var(vtext, "JAVA") or ""
        if java:
            if t.run(f"test -x {q(java)}", timeout=15).ok:
                ver = t.run(f"{q(java)} -version 2>&1 | head -1", timeout=20).out.strip()
                out.append(_ok("vars: JAVA", f"{java} ({util.sanitize_terminal(ver)[:60]})"))
            else:
                out.append(_fail("vars: JAVA", f"{java} not executable",
                                 "GraalVM moved? variables.txt pins JAVA explicitly"))
        else:
            out.append(_warn("vars: JAVA", "unset — system java will be used",
                             'set JAVA="/opt/graalvm/bin/java"'))
        xms, xmx = propsmod.parse_heap(propsmod.get_var(vtext, "JAVA_ARGS") or "")
        if xmx:
            heap = propsmod.size_to_bytes(xmx)
            memr = t.run("free -b | awk '/^Mem:/{print $2}'", timeout=15)
            total = int(memr.out.strip() or 0)
            detail = f"Xms={xms} Xmx={xmx}"
            if total and heap > 0.75 * total:
                out.append(_warn("vars: heap", f"{detail} is >75% of host RAM "
                                 f"({util.human_bytes(total)})",
                                 "leave headroom for off-heap + OS page cache"))
            else:
                out.append(_ok("vars: heap", detail))
        else:
            out.append(_warn("vars: heap", "no -Xmx in JAVA_ARGS", "mcctl jvm heap 12G"))
        if changed:
            propsmod.save_variables(t, cfg, vtext)
    except (TransportError, propsmod.PropError) as e:
        out.append(_fail("vars: variables.txt", str(e),
                         "is this really a ServerPackCreator pack?"))

    # ---------------- server.properties
    try:
        pf = propsmod.load_props(t, cfg)
        for key, want, why in (
            ("server-ip", "0.0.0.0", "IPv6 bind fix"),
            ("use-native-transport", "false", "IPv6 bind fix"),
        ):
            have = pf.get(key)
            if have == want:
                out.append(_ok(f"props: {key}={want}"))
            else:
                out.append(_warn(f"props: {key}", f"is {have!r}, expected {want!r} ({why})",
                                 f"mcctl props set {key} {want}"))
        rcon_on = pf.get("enable-rcon") == "true" and bool(pf.get("rcon.password"))
        if rcon_on:
            out.append(_ok("props: rcon enabled", f"port {pf.get('rcon.port')}"))
            if pf.get("broadcast-rcon-to-ops") == "true":
                out.append(_warn("props: broadcast-rcon-to-ops", "true — ops see every mcctl query",
                                 "mcctl props set broadcast-rcon-to-ops false"))
        elif fix:
            pw = secrets.token_urlsafe(24)
            pf.set("enable-rcon", "true")
            pf.set("rcon.port", str(s.rcon_port))
            pf.set("rcon.password", pw)
            pf.set("broadcast-rcon-to-ops", "false")
            propsmod.save_props(t, cfg, pf)
            out.append(CheckResult("props: rcon", Level.FIXED,
                                   "enabled with a generated password (takes effect on restart)"))
        else:
            out.append(_warn("props: rcon", "disabled — mcctl will drive the console via tmux "
                             "(works, but slower and scrape-based)",
                             "mcctl doctor --fix enables it with a random password"))
    except TransportError as e:
        out.append(_warn("props: server.properties", f"unreadable: {e}",
                         "fresh pack? boot once to generate it"))

    # ---------------- world / disk / backups
    if t.exists(f"{s.server_dir}/{s.world_dir}"):
        out.append(_ok("remote: world dir", s.world_dir))
    else:
        out.append(_warn("remote: world dir", f"{s.world_dir} missing (first boot will create it)"))
    if fix:
        t.run(f"mkdir -p {q(cfg.backup.remote_dir)}", timeout=15)
    if t.exists(cfg.backup.remote_dir):
        out.append(_ok("backup: remote_dir", cfg.backup.remote_dir))
    else:
        out.append(_warn("backup: remote_dir", f"{cfg.backup.remote_dir} missing",
                         "mcctl doctor --fix creates it"))
    r = t.run(f"df -B1 --output=avail {q(s.server_dir)} | tail -1", timeout=15)
    if r.out.strip().isdigit():
        free = int(r.out.strip())
        lvl = _ok if free > cfg.backup.min_free_gb * 1024**3 else _warn
        out.append(lvl("remote: disk free", util.human_bytes(free)))

    # ---------------- spark mod
    if t.run(f"ls {q(s.server_dir)}/mods 2>/dev/null | grep -qi spark", timeout=15).ok:
        out.append(_ok("remote: spark mod present"))
    else:
        out.append(_warn("remote: spark", "no spark*.jar in mods/ — TPS monitoring degraded",
                         "MMC5 ships spark; if removed, add it back for `mcctl tps`"))

    # ---------------- one restart authority + boot resilience (2026-06-11 incident)
    out.extend(_ops_checks(cfg, t, fix=fix))

    # ---------------- RCON must NOT be internet-reachable (we tunnel over SSH)
    if s.transport == "ssh":
        try:
            with socket.create_connection((s.host, s.rcon_port), timeout=3):
                out.append(_fail("security: rcon exposure",
                                 f"{s.host}:{s.rcon_port} is reachable from the internet!",
                                 "block it in the OCI security list / ufw — mcctl tunnels RCON "
                                 "over SSH and needs no open port"))
        except OSError:
            out.append(_ok("security: rcon port closed from outside"))

    return out


def _ops_checks(cfg: Config, t: BaseTransport, *, fix: bool) -> list[CheckResult]:
    """The 2026-06-11 incident distilled: a mod crash must stay a *process*
    problem. That requires exactly ONE restart authority and a box that boots
    even when a data volume is missing. Each check below kills one rung of
    that incident's escalation ladder — including the same invariant at
    machine scope: exactly one brain (watchdog + state), on the box, with
    linger so it outlives logins (DESIGN-BRAIN.md)."""
    from . import state as statemod
    out: list[CheckResult] = []
    s = cfg.server
    armed = statemod.load().get("armed", False)

    # a leftover self-healing daemon on the box (the legacy mc-control watchdog)
    r = t.run("pgrep -af 'mc-control|mc_watchdog' 2>/dev/null | grep -v pgrep || true",
              timeout=15)
    if r.out.strip():
        out.append(_warn("ops: legacy watchdog on server",
                         f"running: {r.out.strip().splitlines()[0][:90]}",
                         "two self-healers fight over restarts and can relaunch a "
                         "crash-looping server — disable it; the mcctl watchdog has "
                         "the crash-loop breaker"))
    else:
        out.append(_ok("ops: no legacy watchdog on server"))

    # a systemd unit that ALSO restarts the server while the watchdog is armed
    r = t.run("systemctl show minecraft.service "
              "-p LoadState,ActiveState,Restart 2>/dev/null || true", timeout=15)
    info = dict(line.split("=", 1) for line in r.out.splitlines() if "=" in line)
    if info.get("LoadState") == "loaded":
        restart = info.get("Restart") or "no"
        if restart != "no" and armed:
            out.append(_warn("ops: two restart authorities",
                             f"minecraft.service has Restart={restart} AND the mcctl "
                             "watchdog is armed",
                             "keep ONE healer: set Restart=no in the unit (systemd then "
                             "only bounds shutdown, which is good) or `mcctl watchdog disarm`"))
        else:
            out.append(_ok("ops: systemd unit", f"minecraft.service Restart={restart}, "
                           f"watchdog {'armed' if armed else 'disarmed'} — no conflict"))

    # ServerStarterJar's own in-tmux relaunch loop: a crash loop no external
    # breaker can arrest (this is what pegged the box during the incident)
    try:
        vtext = propsmod.load_variables(t, cfg)
        restart_var = (propsmod.get_var(vtext, "RESTART") or "").lower()
        if restart_var == "true":
            if fix:
                propsmod.save_variables(t, cfg, propsmod.set_var(vtext, "RESTART", "false"))
                out.append(CheckResult("ops: start.sh RESTART", Level.FIXED, "true -> false"))
            else:
                out.append(_warn("ops: start.sh RESTART=true",
                                 "start.sh relaunches the JVM in-place on every crash — "
                                 "an unbounded loop the watchdog's breaker cannot stop",
                                 "mcctl doctor --fix sets RESTART=false; restarts belong to "
                                 "the watchdog (backoff + crash-loop breaker)"))
        elif restart_var:
            out.append(_ok("ops: start.sh RESTART=false"))
    except (TransportError, propsmod.PropError):
        pass  # variables.txt problems are already reported above

    # data-volume mount: a missing/slow volume must degrade, not hang boot
    r = t.run(f"findmnt -no TARGET -T {q(s.server_dir)} 2>/dev/null || true", timeout=15)
    mnt = r.out.strip().splitlines()[-1].strip() if r.out.strip() else ""
    if mnt and mnt != "/":
        fr = t.run(f"awk -v m={q(mnt)} '$2 == m {{print $4}}' /etc/fstab 2>/dev/null || true",
                   timeout=15)
        opts = fr.out.strip()
        if opts and "nofail" not in opts:
            out.append(_warn("ops: fstab nofail", f"{mnt} mounts without nofail",
                             "a missing/slow volume hangs boot with SSH down — add "
                             "nofail,_netdev to its fstab line, then validate with "
                             "`sudo findmnt --verify`"))
        elif opts:
            out.append(_ok("ops: fstab nofail", mnt))

    # the brain — watchdog + state — must live on exactly ONE machine, the box
    # (DESIGN-BRAIN.md): two daemons with separate desired/armed files fight
    # exactly like two restart authorities did, just across machines.
    local_wd = _local_watchdog_active()
    if s.transport == "ssh":
        r = t.run("pgrep -af 'mcctl watchdog run' 2>/dev/null | grep -v pgrep || true",
                  timeout=15)
        box_wd = bool(r.out.strip())
        if box_wd and local_wd:
            out.append(_warn("ops: brain placement",
                             "mcctl watchdog daemons on BOTH this machine and the box — two "
                             "brains with separate desired/armed state will fight over restarts",
                             "keep one (the box, per DESIGN-BRAIN.md): on the other, run "
                             "`mcctl watchdog disarm` and `systemctl --user disable --now "
                             "mcctl-watchdog.service`"))
        elif box_wd:
            out.append(_ok("ops: brain placement",
                           "watchdog runs on the box (target topology — DESIGN-BRAIN.md)"))
            linger = _parse_linger(t.run(
                f"loginctl show-user {q(s.user)} --property=Linger 2>/dev/null || true",
                timeout=15).out)
            lr = _linger_result(linger, s.user)
            if lr:
                out.append(lr)
        elif local_wd:
            out.append(_ok("ops: brain placement",
                           "watchdog runs on this machine (client-brain topology; the target "
                           "is brain-on-box — DESIGN-BRAIN.md §6)"))
        else:
            out.append(_warn("ops: brain placement",
                             "no mcctl watchdog daemon running on either machine — "
                             "self-healing is off",
                             "enable it on the box (DESIGN-BRAIN.md §6 runbook), "
                             "then `mcctl watchdog arm`"))
    else:  # local transport: this machine IS the box (or a dev sandbox)
        if local_wd:
            out.append(_ok("ops: brain placement", "watchdog runs on this machine (the box)"))
            lr = _linger_result(_local_linger(), getpass.getuser())
            if lr:
                out.append(lr)
        else:
            out.append(_warn("ops: brain placement",
                             "no mcctl watchdog daemon running on this machine — "
                             "self-healing is off",
                             "systemctl --user enable --now mcctl-watchdog.service, "
                             "then `mcctl watchdog arm`"))

    return out


def _local_watchdog_active() -> bool:
    """Is an `mcctl watchdog run` daemon alive on THIS machine (unit or loose process)?"""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                            "mcctl-watchdog.service"], timeout=5, capture_output=True)
        if r.returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(["pgrep", "-f", "mcctl watchdog run"], timeout=5, capture_output=True)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _local_linger() -> str:
    """Linger state for the current user: 'yes' | 'no' | '' (loginctl unavailable)."""
    try:
        r = subprocess.run(["loginctl", "show-user", getpass.getuser(), "--property=Linger"],
                           timeout=5, capture_output=True, text=True)
        return _parse_linger(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _parse_linger(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("Linger="):
            return line.split("=", 1)[1].strip()
    return ""


def _linger_result(linger: str, user: str) -> CheckResult | None:
    """None when loginctl gave no answer — stay quiet rather than guess."""
    if linger == "yes":
        return _ok("ops: brain linger",
                   f"enable-linger set for {user} — the brain survives logout/reboot")
    if linger == "no":
        return _warn("ops: brain linger",
                     f"no linger for {user} — user units (the watchdog, the timers) die at "
                     "logout and don't start at boot",
                     f"sudo loginctl enable-linger {user}")
    return None
