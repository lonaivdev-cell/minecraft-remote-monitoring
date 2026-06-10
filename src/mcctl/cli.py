"""mcctl command-line interface.

# TODO(P1): Android companion app with feature parity — full development plan
#           lives in TODO.md at the repo root. `mcctl status --json` and the
#           other --json outputs are the seed of its transport contract.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from rich.console import Console as RichConsole
from rich.table import Table

from . import __version__, logs, metrics, state, util
from .backup import BackupError, BackupManager
from .config import Config, ConfigError, write_template
from .console import Console, ConsoleError
from .doctor import Level, run_doctor
from .players import PlayerError, Players
from .props import PropError
from .server import ServerControl, ServerError
from .spark import Spark, SparkError
from .transport import BaseTransport, TransportError, make_transport

rc = RichConsole(highlight=False)
log = util.get_logger("cli")


class Ctx:
    """Lazy holder so `mcctl --help` etc. never touch the network."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._cfg: Config | None = None
        self._t: BaseTransport | None = None
        self._console: Console | None = None

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            self._cfg = Config.load(self.args.config)
        return self._cfg

    @property
    def t(self) -> BaseTransport:
        if self._t is None:
            self._t = make_transport(self.cfg)
        return self._t

    @property
    def console(self) -> Console:
        if self._console is None:
            self._console = Console(self.cfg, self.t)
        return self._console

    @property
    def ctl(self) -> ServerControl:
        return ServerControl(self.cfg, self.t, self.console)

    def close(self) -> None:
        if self._console:
            self._console.close()


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        rc.print("[red]refusing without --yes (no TTY to confirm)[/red]")
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


# ================================================================ handlers

def cmd_init(ctx: Ctx) -> int:
    a = ctx.args
    path = write_template(a.config, force=a.force, host=a.host, user=a.user,
                          server_dir=a.server_dir, tmux_session=a.tmux_session)
    rc.print(f"[green]wrote[/green] {path}")
    rc.print("next: [bold]mcctl doctor[/bold] to verify the stack end-to-end")
    return 0


def cmd_doctor(ctx: Ctx) -> int:
    results = run_doctor(ctx.cfg, ctx.t, fix=ctx.args.fix)
    table = Table(title="mcctl doctor", show_lines=False)
    table.add_column("check", style="bold")
    table.add_column("status")
    table.add_column("detail")
    style = {Level.OK: "[green]✓ ok[/green]", Level.WARN: "[yellow]! warn[/yellow]",
             Level.FAIL: "[red]✗ fail[/red]", Level.FIXED: "[cyan]+ fixed[/cyan]",
             Level.SKIP: "[dim]- skip[/dim]"}
    worst = 0
    for r in results:
        detail = r.detail + (f"\n[dim]hint: {r.hint}[/dim]" if r.hint else "")
        table.add_row(r.name, style[r.level], detail)
        worst = max(worst, {Level.OK: 0, Level.FIXED: 0, Level.SKIP: 0,
                            Level.WARN: 0, Level.FAIL: 1}[r.level])
    rc.print(table)
    return worst


def cmd_status(ctx: Ctx) -> int:
    st = ctx.ctl.status(full=not ctx.args.fast)
    if ctx.args.json:
        print(json.dumps(st.to_dict(), indent=2))
        return 0 if not st.errors else 3
    if st.errors:
        rc.print(f"[red]server unreachable:[/red] {st.errors[0]}")
        return 3
    badge = ("[black on green] ONLINE [/black on green]" if st.running and st.port_open
             else "[black on yellow] BOOTING [/black on yellow]" if st.running
             else "[white on red] OFFLINE [/white on red]")
    target = ("local" if ctx.cfg.server.transport == "local"
              else f"{ctx.cfg.server.user}@{ctx.cfg.server.host}")
    rc.print(f"{badge}  {target}:{ctx.cfg.server.server_dir}")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", justify="right")
    table.add_column()
    table.add_row("process", f"pid {st.pid}, up {util.human_duration(st.uptime_s)}"
                  if st.running else "not running")
    table.add_row("tmux", ("session up" + (" [red](dead pane)[/red]" if st.pane_dead else ""))
                  if st.tmux else "no session")
    table.add_row("port", f"{ctx.cfg.server.mc_port} " + ("open" if st.port_open else "[red]closed[/red]"))
    if st.players:
        table.add_row("players", f"{st.players.count}/{st.players.max} "
                      + (", ".join(st.players.names) if st.players.names else ""))
    if st.tps:
        tps = st.tps.get("tps", {})
        mspt = st.tps.get("mspt", {})
        table.add_row("tps", "  ".join(f"{k}:{v:.1f}" for k, v in tps.items())
                      + (f"   mspt median {mspt.get('median', 0):.1f}ms" if mspt else ""))
    if st.heap_used:
        table.add_row("heap", f"{util.human_bytes(st.heap_used)} used / "
                      f"{util.human_bytes(st.heap_max or st.heap_committed)}")
    if st.host_mem_total:
        table.add_row("host ram", f"{util.human_bytes(st.host_mem_used)} / "
                      f"{util.human_bytes(st.host_mem_total)}")
    if st.load:
        table.add_row("load", " ".join(f"{x:.2f}" for x in st.load))
    if st.disk_free is not None:
        table.add_row("disk", f"{util.human_bytes(st.disk_free)} free")
    table.add_row("log", f"last write {util.human_duration(st.log_age_s)} ago"
                  if st.log_age_s is not None else "n/a")
    table.add_row("backup", f"{st.last_backup} ({util.human_duration(st.last_backup_age_s)} ago)"
                  if st.last_backup else "[yellow]none yet[/yellow]")
    wd = "[green]armed[/green]" if st.armed else "[dim]disarmed[/dim]"
    if st.halted:
        wd += " [bold red]HALTED (crash loop)[/bold red]"
    table.add_row("watchdog", f"{wd}, desired={st.desired}, channel={st.channel or 'n/a'}")
    rc.print(table)
    return 0


def cmd_start(ctx: Ctx) -> int:
    with util.OpsLock():
        with rc.status("[cyan]starting server…[/cyan]") as status:
            ctx.ctl.start(wait=not ctx.args.no_wait,
                          progress=lambda line: status.update(f"[cyan]boot:[/cyan] [dim]{line}[/dim]"))
    if ctx.args.no_wait:
        rc.print("[yellow]launch dispatched — not waiting for ready (check `mcctl status`)[/yellow]")
    else:
        rc.print("[green]server is up[/green] — `mcctl dash` to watch it")
    return 0


def cmd_stop(ctx: Ctx) -> int:
    with util.OpsLock():
        ctx.ctl.stop(now=ctx.args.now, reason=ctx.args.reason)
    rc.print("[green]server stopped[/green] (watchdog stands down: desired=down)")
    return 0


def cmd_restart(ctx: Ctx) -> int:
    with util.OpsLock():
        with rc.status("[cyan]restarting…[/cyan]") as status:
            ctx.ctl.restart(now=ctx.args.now, reason=ctx.args.reason or "restart",
                            progress=lambda line: status.update(f"[cyan]boot:[/cyan] [dim]{line}[/dim]"))
    rc.print("[green]server restarted[/green]")
    return 0


def cmd_kill(ctx: Ctx) -> int:
    if not _confirm("EMERGENCY kill (no countdown, no save) — proceed?", ctx.args.yes):
        return 1
    with util.OpsLock():
        ctx.ctl.kill()
    rc.print("[yellow]killed[/yellow]")
    return 0


def cmd_console(ctx: Ctx) -> int:
    if ctx.args.command:
        out = ctx.console.send(" ".join(ctx.args.command))
        print(out.strip() or "(no output)")
        return 0
    return ctx.console.attach()


def cmd_cmd(ctx: Ctx) -> int:
    out = ctx.console.send(" ".join(ctx.args.command))
    print(out.strip() or "(no output)")
    return 0


def cmd_save(ctx: Ctx) -> int:
    if ctx.ctl.find_pid() is None:
        if ctx.args.skip_if_down:
            rc.print("[dim]server down — nothing to save[/dim]")
            return 0
        rc.print("[red]server is not running[/red]")
        return 1
    offset = ctx.console.log_size()
    ctx.console.send("save-all flush", timeout=15)
    hit = ctx.console.wait_in_log(r"Saved the game", offset, timeout=60)
    rc.print("[green]world saved[/green]" if hit else
             "[yellow]save-all sent (no confirmation seen in 60s — busy server?)[/yellow]")
    return 0


def cmd_tps(ctx: Ctx) -> int:
    rep = Spark(ctx.console).tps()
    if ctx.args.json:
        print(json.dumps(rep.to_dict(), indent=2))
        return 0
    t = Table(title="spark tps")
    t.add_column("window")
    t.add_column("TPS", justify="right")
    for k, v in rep.tps.items():
        color = "green" if v >= 18 else "yellow" if v >= 12 else "red"
        t.add_row(k, f"[{color}]{v:.1f}[/{color}]")
    rc.print(t)
    if rep.mspt:
        rc.print("mspt (10s): " + "  ".join(f"{k}={v:.1f}ms" for k, v in rep.mspt.items()))
    if rep.cpu_system:
        rc.print("cpu sys:    " + "  ".join(f"{k}={v:.0f}%" for k, v in rep.cpu_system.items()))
    return 0


def cmd_health(ctx: Ctx) -> int:
    rep = Spark(ctx.console).health()
    if ctx.args.json:
        print(json.dumps(rep.to_dict(), indent=2))
        return 0
    if rep.tps:
        rc.print("tps: " + "  ".join(f"{k}={v:.1f}" for k, v in rep.tps.items()))
    if rep.memory_used:
        rc.print(f"memory: {util.human_bytes(rep.memory_used)} / {util.human_bytes(rep.memory_max)}")
    if rep.disk_used:
        rc.print(f"disk:   {util.human_bytes(rep.disk_used)} / {util.human_bytes(rep.disk_total)}")
    if not (rep.tps or rep.memory_used):
        rc.print("[yellow]spark health returned nothing parseable — see `mcctl cmd spark health`[/yellow]")
    return 0


def cmd_profile(ctx: Ctx) -> int:
    with rc.status(f"[cyan]spark profiler running for {ctx.args.seconds}s…[/cyan]") as status:
        url = Spark(ctx.console).profile(ctx.args.seconds,
                                         progress=lambda m: status.update(f"[cyan]{m}[/cyan]"))
    rc.print(f"[green]profile ready:[/green] [bold]{url}[/bold]")
    log.info("spark profile: %s", url)
    return 0


def cmd_purge(ctx: Ctx) -> int:
    pid = ctx.ctl.find_pid()
    if not pid:
        rc.print("[red]server is not running[/red]")
        return 1
    with rc.status("[cyan]requesting concurrent GC + measuring…[/cyan]"):
        rep = metrics.purge(ctx.t, ctx.cfg, pid)
    rc.print(f"heap before: {util.human_bytes(rep.before_used)}")
    rc.print(f"heap after:  {util.human_bytes(rep.after_used)} "
             f"(committed {util.human_bytes(rep.committed)})")
    rc.print(f"freed:       [bold]{util.human_bytes(rep.freed)} ({rep.freed_pct:.0f}%)[/bold]")
    rc.print(f"verdict:     {rep.verdict}")
    return 0


def cmd_stats(ctx: Ctx) -> int:
    samples = metrics.read_samples(ctx.args.n)
    if ctx.args.json:
        print(json.dumps(samples, indent=2))
        return 0
    if not samples:
        rc.print("[yellow]no samples yet — run the watchdog or `mcctl dash` to collect[/yellow]")
        return 0
    t = Table(title=f"last {len(samples)} samples")
    for col in ("time", "tps", "mspt", "players", "heap", "host mem", "load1"):
        t.add_column(col, justify="right")
    for s in samples[-30:]:
        heap = (f"{util.human_bytes(s.get('heap_used'))}" if s.get("heap_used")
                else f"{s.get('heap_pct')}%" if s.get("heap_pct") else "—")
        t.add_row(
            time.strftime("%m-%d %H:%M", time.localtime(s.get("ts", 0))),
            f"{s['tps']:.1f}" if s.get("tps") else "—",
            f"{s['mspt']:.0f}" if s.get("mspt") else "—",
            str(s.get("players")) if s.get("players") is not None else "—",
            heap,
            util.human_bytes(s.get("mem_used")) if s.get("mem_used") else "—",
            f"{s['load1']:.2f}" if s.get("load1") is not None else "—",
        )
    rc.print(t)
    return 0


def cmd_logs(ctx: Ctx) -> int:
    a = ctx.args
    if a.crash:
        if a.get or not a.list:
            name, content = logs.crash_get(ctx.t, ctx.cfg, a.get or "")
            if not name:
                rc.print("[green]no crash reports on the server[/green]")
                return 0
            rc.print(f"[bold]crash-reports/{name}[/bold]\n")
            print(content)
            return 0
        reports = logs.crash_list(ctx.t, ctx.cfg)
        if not reports:
            rc.print("[green]no crash reports on the server[/green]")
            return 0
        t = Table(title="crash reports (newest first)")
        t.add_column("name")
        t.add_column("size", justify="right")
        t.add_column("age", justify="right")
        for name, size, mtime in reports:
            t.add_row(name, util.human_bytes(size),
                      util.human_duration(max(0, int(time.time()) - mtime)))
        rc.print(t)
        return 0
    if a.follow:
        try:
            for line in logs.follow(ctx.t, ctx.cfg, a.lines):
                print(line)
        except KeyboardInterrupt:
            pass
        return 0
    print(logs.tail(ctx.t, ctx.cfg, a.lines))
    return 0


def cmd_backup(ctx: Ctx) -> int:
    a = ctx.args
    mgr = BackupManager(ctx.cfg, ctx.t, ctx.console)
    sub = a.backup_cmd or "create"

    if sub == "create":
        try:
            with util.OpsLock():
                with rc.status("[cyan]snapshotting…[/cyan]") as status:
                    entry = mgr.create(full=a.full, dry=a.dry_run,
                                       progress=lambda m: status.update(f"[cyan]{m}[/cyan]"))
                if entry:
                    kept, dropped = mgr.prune(dry=a.dry_run)
        except BackupError as e:
            if a.notify:
                util.notify("mcctl: backup FAILED", str(e),
                            desktop=ctx.cfg.watchdog.notify_desktop,
                            webhook_url=ctx.cfg.watchdog.webhook_url, urgency="critical")
            raise
        if a.dry_run:
            rc.print("[yellow]dry-run complete (nothing written)[/yellow]")
            return 0
        assert entry is not None
        rc.print(f"[green]backup created:[/green] {entry.name} ({util.human_bytes(entry.size)})")
        if dropped:
            rc.print(f"rotated out {len(dropped)}: " + ", ".join(e.name for e in dropped))
        rc.print(f"retained: {len(kept)} archives")
        return 0

    if sub == "list":
        entries = mgr.list()
        if a.json:
            print(json.dumps([{"name": e.name, "size": e.size, "ts": e.ts.isoformat(),
                               "full": e.full} for e in entries], indent=2))
            return 0
        if not entries:
            rc.print("[yellow]no backups yet — `mcctl backup` makes one[/yellow]")
            return 0
        t = Table(title=f"backups in {ctx.cfg.backup.remote_dir}")
        t.add_column("name")
        t.add_column("size", justify="right")
        t.add_column("age", justify="right")
        t.add_column("kind")
        for e in entries:
            t.add_row(e.name, util.human_bytes(e.size), util.human_duration(e.age_s),
                      "full" if e.full else "world")
        rc.print(t)
        return 0

    if sub == "prune":
        kept, dropped = mgr.prune(dry=a.dry_run)
        verb = "would drop" if a.dry_run else "dropped"
        rc.print(f"{verb} {len(dropped)}: " + (", ".join(e.name for e in dropped) or "nothing"))
        rc.print(f"retained {len(kept)}")
        return 0

    if sub == "pull":
        rc.print("[cyan]pulling archives via rsync…[/cyan]")
        code = mgr.pull(a.dest)
        return 0 if code == 0 else 1

    if sub == "verify":
        ok = mgr.verify(a.name)
        rc.print(f"[green]{a.name}: archive OK[/green]" if ok
                 else f"[red]{a.name}: INTEGRITY CHECK FAILED[/red]")
        return 0 if ok else 1

    if sub == "restore":
        rc.print(f"[bold red]This replaces the live world with {a.name}.[/bold red]")
        rc.print("The current world is moved aside (not deleted). Server must be stopped.")
        if not _confirm("Proceed with restore?", a.yes):
            return 1
        with util.OpsLock():
            aside = mgr.restore(a.name)
        rc.print(f"[green]restored {a.name}[/green] — previous world kept at {aside}")
        rc.print("start when ready: [bold]mcctl start[/bold]")
        return 0
    return 2


def cmd_props(ctx: Ctx) -> int:
    from . import props as P
    a = ctx.args
    pf = P.load_props(ctx.t, ctx.cfg)
    sub = a.props_cmd or "list"
    if sub == "list":
        t = Table(title="server.properties")
        t.add_column("key", style="bold")
        t.add_column("value")
        t.add_column("notes", style="dim")
        for k, v in sorted(pf.items()):
            spec = P.KNOWN_PROPS.get(k)
            shown = "********" if k == "rcon.password" and v else v
            t.add_row(k, shown, spec.desc if spec else "")
        rc.print(t)
        return 0
    if sub == "get":
        v = pf.get(a.key)
        if v is None:
            rc.print(f"[yellow]{a.key} is not set[/yellow]")
            return 1
        print(v)
        return 0
    if sub == "set":
        value = P.validate_prop(a.key, a.value)
        old = pf.get(a.key)
        if old == value:
            rc.print(f"[dim]{a.key} already {value!r}[/dim]")
            return 0
        new_pf = P.PropertiesFile.parse(pf.render())
        new_pf.set(a.key, value)
        for line in P.props_diff(pf, new_pf):
            rc.print(line)
        P.save_props(ctx.t, ctx.cfg, new_pf)
        spec = P.KNOWN_PROPS.get(a.key)
        running = ctx.ctl.find_pid() is not None
        rc.print("[green]written[/green] (timestamped .bak kept on the server)")
        if running and spec and spec.live_cmd and a.live:
            cmd = spec.live_cmd.format(v=value, onoff="on" if value == "true" else "off")
            out = ctx.console.send(cmd)
            rc.print(f"[green]applied live:[/green] {cmd} -> {out.strip()[:120]}")
        elif running and (not spec or spec.restart):
            rc.print("[yellow]server is running — takes effect on next restart[/yellow]")
        return 0
    return 2


def cmd_jvm(ctx: Ctx) -> int:
    from . import props as P
    a = ctx.args
    sub = a.jvm_cmd or "show"
    text = P.load_variables(ctx.t, ctx.cfg)
    if sub == "show":
        args = P.get_var(text, "JAVA_ARGS") or "(none)"
        xms, xmx = P.parse_heap(args)
        rc.print(f"JAVA      = {P.get_var(text, 'JAVA') or '(system java)'}")
        rc.print(f"heap      = Xms {xms or '?'} / Xmx {xmx or '?'}")
        rc.print(f"JAVA_ARGS = {args}")
        for k in ("SKIP_JAVA_CHECK", "WAIT_FOR_USER_INPUT", "SERVERSTARTERJAR_FORCE_FETCH",
                  "MINECRAFT_VERSION", "MODLOADER", "MODLOADER_VERSION", "USE_SSJ"):
            v = P.get_var(text, k)
            if v is not None:
                rc.print(f"{k} = {v}")
        return 0
    if sub == "heap":
        new_text = P.set_heap(text, a.size)
        heap = P.size_to_bytes(a.size)
        memr = ctx.t.run("free -b | awk '/^Mem:/{print $2}'", timeout=15)
        total = int(memr.out.strip() or 0)
        if total and heap > 0.75 * total and not _confirm(
                f"{a.size} is >75% of host RAM ({util.human_bytes(total)}) — really?", a.yes):
            return 1
        P.save_variables(ctx.t, ctx.cfg, new_text)
        rc.print(f"[green]heap set to {a.size}[/green] (Xms=Xmx, .bak kept) — restart to apply")
        return 0
    if sub == "java":
        from .transport import q as _q
        if not ctx.t.run(f"test -x {_q(a.path)}", timeout=15).ok:
            rc.print(f"[red]{a.path} is not executable on the server[/red]")
            return 1
        P.save_variables(ctx.t, ctx.cfg, P.set_var(text, "JAVA", a.path))
        rc.print(f"[green]JAVA set to {a.path}[/green] — restart to apply")
        return 0
    return 2


def cmd_player(ctx: Ctx) -> int:
    a = ctx.args
    p = Players(ctx.cfg, ctx.t, ctx.console)
    sub = a.player_cmd or "list"
    if sub == "list":
        online = p.online()
        if online is None:
            rc.print("[yellow]server console unreachable (is it running?)[/yellow]")
            return 1
        names = ", ".join(online.names) if online.names else "(nobody)"
        rc.print(f"online {online.count}/{online.max}: {names}")
        return 0
    if sub == "whitelist":
        wa = a.wl_cmd or "list"
        if wa == "list":
            names = p.whitelist()
            rc.print("whitelist: " + (", ".join(names) if names else "(empty)"))
            return 0
        out = {"add": lambda: p.whitelist_add(a.name),
               "remove": lambda: p.whitelist_remove(a.name),
               "on": p.whitelist_on, "off": p.whitelist_off}[wa]()
        print(out.strip() or "done")
        return 0
    action = {"op": lambda: p.op(a.name), "deop": lambda: p.deop(a.name),
              "kick": lambda: p.kick(a.name, a.reason),
              "ban": lambda: p.ban(a.name, a.reason),
              "pardon": lambda: p.pardon(a.name)}[sub]
    print(action().strip() or "done")
    return 0


def cmd_watchdog(ctx: Ctx) -> int:
    from .watchdog import Watchdog
    a = ctx.args
    sub = a.wd_cmd or "status"
    if sub == "run":
        Watchdog(ctx.cfg, ctx.t).run_forever()
        return 0
    if sub == "arm":
        state.set_armed(True)
        rc.print("[green]watchdog armed[/green] — it now heals crashes when desired=up")
        return 0
    if sub == "disarm":
        state.set_armed(False)
        rc.print("[yellow]watchdog disarmed[/yellow] — keep it this way during migrations")
        return 0
    if sub == "status":
        st = state.load()
        rc.print(f"armed:   {st['armed']}")
        rc.print(f"desired: {st['desired']}")
        rc.print(f"halted:  {st.get('halted', False)}")
        recent = [t for t in st["restarts"] if t > time.time() - ctx.cfg.watchdog.restart_window]
        rc.print(f"restarts in window: {len(recent)} (max {ctx.cfg.watchdog.max_restarts})")
        return 0
    if sub == "install":
        return _install_units()
    return 2


def _install_units() -> int:
    from pathlib import Path
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    exe = sys.argv[0] if sys.argv[0].endswith("mcctl") else "mcctl"
    units = {
        "mcctl-watchdog.service": (
            "[Unit]\nDescription=mcctl self-healing watchdog\nAfter=network-online.target\n\n"
            f"[Service]\nExecStart={exe} watchdog run\nRestart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n"
        ),
        "mcctl-autosave.service": (
            "[Unit]\nDescription=mcctl autosave trigger\n\n"
            f"[Service]\nType=oneshot\nExecStart={exe} save --skip-if-down\n"
        ),
        "mcctl-autosave.timer": (
            "[Unit]\nDescription=mcctl autosave every 20 minutes\n\n"
            "[Timer]\nOnCalendar=*:0/20\nPersistent=false\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
        "mcctl-backup.service": (
            "[Unit]\nDescription=mcctl world backup + rotation\n\n"
            f"[Service]\nType=oneshot\nExecStart={exe} backup create --notify\n"
        ),
        "mcctl-backup.timer": (
            "[Unit]\nDescription=mcctl daily world backup\n\n"
            "[Timer]\nOnCalendar=*-*-* 04:30:00\nRandomizedDelaySec=10m\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
    }
    for name, content in units.items():
        (unit_dir / name).write_text(content, encoding="utf-8")
        rc.print(f"[green]wrote[/green] {unit_dir / name}")
    rc.print("\nenable with (fish):")
    rc.print("  systemctl --user daemon-reload")
    rc.print("  systemctl --user enable --now mcctl-watchdog.service mcctl-backup.timer mcctl-autosave.timer")
    return 0


def cmd_sync(ctx: Ctx) -> int:
    a = ctx.args
    remote = ctx.t.remote_spec(f"{ctx.cfg.server.server_dir}/config/")
    if a.pull:
        code = ctx.t.rsync(remote, a.pull.rstrip("/") + "/")
    else:
        if not _confirm("push local config/ over the server's config/ ?", a.yes):
            return 1
        code = ctx.t.rsync(a.push.rstrip("/") + "/", remote)
    if code == 0:
        rc.print("[green]config sync complete[/green] — fixes Better Compatibility Checker "
                 "version mismatches")
    return 0 if code == 0 else 1


def cmd_rcon(ctx: Ctx) -> int:
    enabled, port, _pw = ctx.console.rcon_settings()
    rc.print(f"enable-rcon: {enabled} (port {port})")
    if enabled:
        ok = ctx.console.rcon_available()
        rc.print(f"tunnel + auth: {'[green]working[/green]' if ok else '[red]failing[/red]'}")
        return 0 if ok else 1
    rc.print("hint: [bold]mcctl doctor --fix[/bold] enables RCON with a generated password")
    return 1


def cmd_dash(ctx: Ctx) -> int:
    from .dash import run_dash
    run_dash(ctx.cfg, ctx.t)
    return 0


# ================================================================ parser

def build_parser() -> argparse.ArgumentParser:
    # shared by the main parser and every (nested) subparser, so global flags
    # work in any position: `mcctl --config X status` == `mcctl status --config X`
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="config file (default: ~/.config/mcctl/config.toml)")
    common.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v info, -vv debug on stderr")

    p = argparse.ArgumentParser(
        prog="mcctl",
        description="Remote control & monitoring for a modded Minecraft server over SSH.",
        epilog="start here: mcctl init  ->  mcctl doctor  ->  mcctl start  ->  mcctl dash",
        parents=[common],
    )
    p.add_argument("--version", action="version", version=f"mcctl {__version__}")
    subaction = p.add_subparsers(dest="cmd", metavar="COMMAND")

    class _Sub:
        """add_parser wrapper that injects the common parent everywhere."""

        def __init__(self, action):
            self._action = action

        def add_parser(self, name, **kw):
            kw.setdefault("parents", [common])
            return self._action.add_parser(name, **kw)

    sub = _Sub(subaction)

    def nested(parent, dest):
        return _Sub(parent.add_subparsers(dest=dest))

    sp = sub.add_parser("init", help="write a config template")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--host")
    sp.add_argument("--user")
    sp.add_argument("--server-dir", dest="server_dir")
    sp.add_argument("--tmux-session", dest="tmux_session")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("doctor", help="preflight checks (encodes the hard-won knowledge)")
    sp.add_argument("--fix", action="store_true", help="apply safe fixes (vars flags, rcon, dirs)")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("status", help="full server status")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--fast", action="store_true", help="skip spark/players/heap probes")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("start", help="start the server (tmux + start.sh) and wait for ready")
    sp.add_argument("--no-wait", action="store_true")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="graceful stop: warn players, flush save, escalate if needed")
    sp.add_argument("--now", action="store_true", help="skip the player countdown")
    sp.add_argument("--reason", default="")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("restart", help="stop then start")
    sp.add_argument("--now", action="store_true")
    sp.add_argument("--reason", default="")
    sp.set_defaults(func=cmd_restart)

    sp = sub.add_parser("kill", help="emergency stop (no countdown, no save)")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_kill)

    sp = sub.add_parser("console", help="attach to the live console (or -c for one command)")
    sp.add_argument("-c", "--command", nargs=argparse.REMAINDER,
                    help="run one console command and print the reply")
    sp.set_defaults(func=cmd_console)

    sp = sub.add_parser("cmd", help="run any console command (rcon preferred, tmux fallback)")
    sp.add_argument("command", nargs="+", metavar="COMMAND")
    sp.set_defaults(func=cmd_cmd)

    sp = sub.add_parser("save", help="save-all flush and confirm")
    sp.add_argument("--skip-if-down", action="store_true",
                    help="exit 0 quietly when the server is down (for timers)")
    sp.set_defaults(func=cmd_save)

    sp = sub.add_parser("tps", help="spark TPS/MSPT/CPU")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_tps)

    sp = sub.add_parser("health", help="spark health (memory, disk)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("profile", help="run the spark profiler, return the viewer URL")
    sp.add_argument("--seconds", type=int, default=60)
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("purge", help="jcmd GC.run + honest leak-vs-garbage verdict")
    sp.set_defaults(func=cmd_purge)

    sp = sub.add_parser("stats", help="recent metric samples")
    sp.add_argument("-n", type=int, default=120)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("logs", help="tail/follow latest.log, or crash reports")
    sp.add_argument("-n", "--lines", type=int, default=50)
    sp.add_argument("-f", "--follow", action="store_true")
    sp.add_argument("crash", nargs="?", choices=["crash"],
                    help="crash report mode: `mcctl logs crash [--list|--get NAME]`")
    sp.add_argument("--list", action="store_true", help="list crash reports")
    sp.add_argument("--get", metavar="NAME", help="print a specific crash report")
    sp.set_defaults(func=cmd_logs)

    sp = sub.add_parser("backup", help="snapshot, rotate, pull, verify, restore")
    bsub = nested(sp, "backup_cmd")
    b = bsub.add_parser("create", help="consistent snapshot + rotation (default)")
    b.add_argument("--full", action="store_true", help="whole instance, not just the world")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--notify", action="store_true", help="notify on failure (for timers)")
    b = bsub.add_parser("list")
    b.add_argument("--json", action="store_true")
    b = bsub.add_parser("prune", help="apply the rotation policy now")
    b.add_argument("--dry-run", action="store_true")
    b = bsub.add_parser("pull", help="rsync archives to this machine")
    b.add_argument("dest", nargs="?", default=None)
    b = bsub.add_parser("verify", help="integrity-test one archive")
    b.add_argument("name")
    b = bsub.add_parser("restore", help="replace the live world with a snapshot")
    b.add_argument("name")
    b.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_backup, backup_cmd=None, full=False, dry_run=False, notify=False)

    sp = sub.add_parser("props", help="view/edit server.properties (validated, atomic, .bak)")
    psub = nested(sp, "props_cmd")
    psub.add_parser("list")
    pg = psub.add_parser("get")
    pg.add_argument("key")
    ps = psub.add_parser("set")
    ps.add_argument("key")
    ps.add_argument("value")
    ps.add_argument("--live", action="store_true",
                    help="also apply live via console when the key supports it")
    sp.set_defaults(func=cmd_props, props_cmd=None)

    sp = sub.add_parser("jvm", help="variables.txt: heap, JAVA path, flags")
    jsub = nested(sp, "jvm_cmd")
    jsub.add_parser("show")
    jh = jsub.add_parser("heap", help="set -Xms/-Xmx (e.g. 12G)")
    jh.add_argument("size")
    jh.add_argument("--yes", action="store_true")
    jj = jsub.add_parser("java", help="pin the JAVA binary path")
    jj.add_argument("path")
    sp.set_defaults(func=cmd_jvm, jvm_cmd=None)

    sp = sub.add_parser("player", help="players: list, whitelist, op, kick, ban")
    plsub = nested(sp, "player_cmd")
    plsub.add_parser("list")
    wl = plsub.add_parser("whitelist")
    wlsub = nested(wl, "wl_cmd")
    wlsub.add_parser("list")
    for actname in ("add", "remove"):
        wa = wlsub.add_parser(actname)
        wa.add_argument("name")
    wlsub.add_parser("on")
    wlsub.add_parser("off")
    for actname in ("op", "deop", "pardon"):
        pa = plsub.add_parser(actname)
        pa.add_argument("name")
    for actname in ("kick", "ban"):
        pa = plsub.add_parser(actname)
        pa.add_argument("name")
        pa.add_argument("reason", nargs="?", default="")
    sp.set_defaults(func=cmd_player, player_cmd=None, wl_cmd=None)

    sp = sub.add_parser("watchdog", help="self-healing daemon: run/arm/disarm/status/install")
    wsub = nested(sp, "wd_cmd")
    for c in ("run", "arm", "disarm", "status", "install"):
        wsub.add_parser(c)
    sp.set_defaults(func=cmd_watchdog, wd_cmd=None)

    sp = sub.add_parser("sync", help="rsync the config/ directory (client<->server)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--pull", metavar="DEST", help="server config/ -> local DEST")
    g.add_argument("--push", metavar="SRC", help="local SRC -> server config/")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("rcon", help="show RCON channel status")
    sp.set_defaults(func=cmd_rcon)

    sp = sub.add_parser("dash", help="live TUI dashboard")
    sp.set_defaults(func=cmd_dash)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    util.setup_logging(args.verbose)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    ctx = Ctx(args)
    try:
        return args.func(ctx)
    except KeyboardInterrupt:
        rc.print("\n[yellow]interrupted[/yellow]")
        return 130
    except ConfigError as e:
        rc.print(f"[red]config error:[/red] {e}")
        return 1
    except TransportError as e:
        rc.print(f"[red]connection error:[/red] {e}")
        return 3
    except (ServerError, BackupError, SparkError, ConsoleError, PlayerError,
            metrics.MetricsError, util.LockHeldError, PropError) as e:
        rc.print(f"[red]error:[/red] {e}")
        return 1
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
