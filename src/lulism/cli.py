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
from pathlib import Path

from rich.console import Console as RichConsole
from rich.table import Table

from . import __version__, logs, metrics, state, util
from .backup import BackupError, BackupManager
from .config import Config, ConfigError, write_template
from .console import Console, ConsoleError
from .crafting import CraftError
from .doctor import Level, run_doctor
from .inspector import InspectError
from .llm import LlmError
from .modconfig import ConfigEditError
from .mods import ModsError
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


def cmd_postmortem(ctx: Ctx) -> int:
    from . import postmortem as pm
    rep = pm.build_postmortem(ctx.t, ctx.cfg, crash_name=ctx.args.crash)
    if ctx.args.json:
        print(json.dumps(rep.to_dict(), indent=2))
        return 0
    rc.print("[bold]what went wrong[/bold]")
    for line in rep.summary:
        rc.print(f"  [yellow]•[/yellow] {line}")
    c = rep.crash
    if c and c.mod_frames:
        rc.print(f"\n[bold]mod frames in {c.name}[/bold] [dim](topmost first)[/dim]")
        for f in c.mod_frames[:5]:
            rc.print(f"  {f.mod} [dim]{f.version}[/dim]  {f.location}")
    if rep.events:
        rc.print("\n[bold]recent watchdog events[/bold]")
        for ev in rep.events[-8:]:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(ev.get("ts", 0)))
            mark = "[red]![/red]" if ev.get("urgency") == "critical" else "[dim]·[/dim]"
            rc.print(f"  {mark} [dim]{stamp}[/dim] {ev.get('kind')}: {ev.get('detail', '')}")
    if rep.next_steps:
        rc.print("\n[bold]next[/bold]")
        for step in rep.next_steps:
            rc.print(f"  [cyan]→[/cyan] {step}")
    return 0


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
    table.add_row("tmux", ((f"session '{st.tmux_session}'" if st.tmux_session else "session up")
                            + (" [red](dead pane)[/red]" if st.pane_dead else ""))
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
        b = ctx.cfg.backup
        if b.offsite_after_prune and b.offsite_remote.strip():
            try:
                with rc.status("[cyan]off-site mirror…[/cyan]") as status:
                    res = mgr.offsite_sync(progress=lambda m: status.update(f"[cyan]{m}[/cyan]"))
                rc.print(f"[green]off-site → {res['remote']}[/green]"
                         + (f": {res['summary']}" if res["summary"] else ""))
            except BackupError as e:
                # the local backup already succeeded — off-site is best-effort, never fatal
                rc.print(f"[yellow]off-site mirror failed:[/yellow] {e}")
                if a.notify:
                    util.notify("mcctl: off-site backup FAILED", str(e),
                                desktop=ctx.cfg.watchdog.notify_desktop,
                                webhook_url=ctx.cfg.watchdog.webhook_url, urgency="critical")
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
        if a.to:
            rc.print(f"[cyan]extracting {a.name} → {a.to} (live world untouched)…[/cyan]")
            with util.OpsLock():
                dest = mgr.extract(a.name, a.to)
            rc.print(f"[green]extracted {a.name}[/green] → {dest}")
            return 0
        rc.print(f"[bold red]This replaces the live world with {a.name}.[/bold red]")
        rc.print("The current world is moved aside (not deleted). Server must be stopped.")
        rc.print("(To inspect a backup without touching the world, use [bold]--to <dir>[/bold].)")
        if not _confirm("Proceed with restore?", a.yes):
            return 1
        with util.OpsLock():
            aside = mgr.restore(a.name)
        rc.print(f"[green]restored {a.name}[/green] — previous world kept at {aside}")
        rc.print("start when ready: [bold]mcctl start[/bold]")
        return 0

    if sub == "offsite":
        with rc.status("[cyan]rclone…[/cyan]") as status:
            res = mgr.offsite_sync(dry=a.dry_run,
                                   progress=lambda m: status.update(f"[cyan]{m}[/cyan]"))
        verb = "would mirror" if res["dry"] else "mirrored"
        rc.print(f"[green]{verb} → {res['remote']}[/green] ({res['mode']})"
                 + (f": {res['summary']}" if res["summary"] else ""))
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
    """Install the systemd user units shipped inside the package (single source
    shared with the PKGBUILD), rewriting ExecStart for non-/usr/bin installs."""
    units = util.render_units(exe=sys.argv[0] if sys.argv[0].endswith("mcctl") else "mcctl")
    unit_dir = util.user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
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


def cmd_inspect(ctx: Ctx) -> int:
    from . import inspector
    a = ctx.args
    section = a.section or "host"
    pid = None if section == "host" else ctx.ctl.find_pid()
    rep = inspector.inspect_section(ctx.t, ctx.cfg, section, pid)
    if a.json:
        print(json.dumps(rep.to_dict(), indent=2))
        return 0
    rc.print(f"[bold cyan]── {rep.title} ──[/bold cyan]")
    print(rep.text)
    if a.learn:
        rc.print("\n[bold yellow]── what am I looking at? ──[/bold yellow]")
        rc.print(f"[dim]{inspector.EXPLAIN[section]}[/dim]")
    else:
        rc.print("\n[dim]add --learn for the plain-language walkthrough of this section[/dim]")
    return 0


def cmd_mods(ctx: Ctx) -> int:
    from . import mods as M
    a = ctx.args
    entries = M.list_mods(ctx.t, ctx.cfg)

    if getattr(a, "diff", None):
        client = M.scan_local_mods(a.diff)
        d = M.diff_mods(entries, client)
        if a.json:
            print(json.dumps(d.to_dict(), indent=2))
            return 0
        if entries and not any(m.mod_id for m in entries):
            rc.print("[dim]server mod ids unavailable (no python3 on the box) — "
                     "matching by filename, which is less accurate[/dim]")
        rc.print(f"[bold]server[/bold] {ctx.cfg.server.server_dir}/mods "
                 f"({len(entries)})  vs  [bold]client[/bold] {a.diff} ({len(client)})")
        if d.in_sync:
            rc.print("[green]✓ in sync[/green] — every mod matches by id and version")
            return 0

        def _modtable(title, style, rows):
            t = Table(title=title, title_style=style)
            for col, kw in (("mod", {"style": "bold"}), ("id", {"style": "dim"}),
                            ("version", {}), ("file", {"style": "dim"})):
                t.add_column(col, **kw)
            for m in rows:
                t.add_row(m.name or "?", m.mod_id, m.version or "?", m.file)
            rc.print(t)

        if d.server_only:
            _modtable(f"on SERVER, missing from client ({len(d.server_only)})",
                      "red", d.server_only)
        if d.client_only:
            _modtable(f"on CLIENT, missing from server ({len(d.client_only)})",
                      "yellow", d.client_only)
        if d.version_mismatch:
            t = Table(title=f"version mismatch ({len(d.version_mismatch)})", title_style="magenta")
            for col in ("mod", "server", "client"):
                t.add_column(col)
            for dd in d.version_mismatch:
                t.add_row(dd.name, dd.server_version, dd.client_version)
            rc.print(t)
        rc.print(f"[dim]{len(d.common)} mod(s) in sync[/dim]")
        return 0

    if a.json:
        print(json.dumps([m.to_dict() for m in entries], indent=2))
        return 0
    t = Table(title=f"{len(entries)} mods in {ctx.cfg.server.server_dir}/mods "
                    f"({util.human_bytes(sum(m.size for m in entries))})")
    t.add_column("mod", style="bold")
    t.add_column("id", style="dim")
    t.add_column("version")
    t.add_column("size", justify="right")
    t.add_column("file", style="dim")
    for m in entries:
        t.add_row(m.name or "?", m.mod_id, m.version or "?", util.human_bytes(m.size), m.file)
    rc.print(t)
    if entries and not any(m.mod_id for m in entries):
        rc.print("[dim]metadata unavailable — install python3 on the server for full info[/dim]")
    return 0


def cmd_recipes(ctx: Ctx) -> int:
    from . import crafting
    a = ctx.args
    sub = a.recipes_cmd or "search"
    if sub == "search":
        recipes, truncated = crafting.search_recipes(ctx.t, ctx.cfg, query=a.query or "", limit=a.n)
        if a.craftable:
            tags = crafting.load_tag_map(ctx.t, ctx.cfg)
            recipes = crafting.craftable_filter(ctx.console, ctx.cfg, recipes,
                                                player=a.player or "", tags=tags)
        if a.json:
            print(json.dumps({"recipes": [r.to_dict() for r in recipes],
                              "truncated": truncated}, indent=2))
            return 0
        if not recipes:
            scope = " craftable" if a.craftable else ""
            rc.print(f"[yellow]no{scope} crafting recipes match {a.query!r}[/yellow]")
            return 0
        t = Table(title=f"{len(recipes)}{' craftable' if a.craftable else ''} crafting recipes"
                        + (f" matching {a.query!r}" if a.query else ""))
        t.add_column("recipe id", style="bold")
        t.add_column("type")
        t.add_column("makes", justify="right")
        t.add_column("output")
        for r in recipes:
            t.add_row(r.rid, r.rtype, str(r.result_count), r.result_item)
        rc.print(t)
        if truncated:
            rc.print(f"[dim]…more matches hidden — refine the query or raise -n (showing {a.n})[/dim]")
        return 0
    if sub == "show":
        rec = crafting.get_recipe(ctx.t, ctx.cfg, a.id)
        if a.json:
            print(json.dumps(rec.to_dict(), indent=2))
            return 0
        rc.print(crafting.render_recipe(rec))
        return 0
    if sub == "cost":
        cb = crafting.recipe_cost(ctx.t, ctx.cfg, a.id, count=a.count, max_depth=a.max_depth)
        if a.json:
            print(json.dumps(cb.to_dict(), indent=2))
            return 0
        _render_cost(cb)
        return 0
    if sub == "tag":
        items = crafting.resolve_tag(ctx.t, ctx.cfg, a.id)
        name = a.id.lstrip("#")
        if a.json:
            print(json.dumps({"tag": name, "items": items}, indent=2))
            return 0
        if not items:
            rc.print(f"[yellow]#{name} resolved to no items "
                     "(unknown tag, or it only nests empty tags)[/yellow]")
            return 0
        rc.print(f"[bold]#{name}[/bold] → {len(items)} item(s)")
        for it in items:
            rc.print(f"  {it}")
        return 0
    return 2


def cmd_items(ctx: Ctx) -> int:
    from . import assets
    a = ctx.args
    sub = a.items_cmd or "list"
    if sub in ("list", "search"):
        lang, im, bm = assets.load_assets(ctx.t, ctx.cfg)
        items = assets.build_manifest(im, bm, lang, query=a.query or "")
        total = len(items)
        shown = items[: a.n]
        if a.json:
            print(json.dumps({"items": shown, "count": total,
                              "truncated": total > a.n}, indent=2))
            return 0
        if not shown:
            rc.print(f"[yellow]no items match {a.query!r}[/yellow]")
            return 0
        t = Table(title=f"{total} items" + (f" matching {a.query!r}" if a.query else ""))
        t.add_column("item id", style="bold")
        t.add_column("name")
        t.add_column("icon texture", style="dim")
        for it in shown:
            t.add_row(it["id"], it["name"], it["icon"] or "—")
        rc.print(t)
        if total > a.n:
            rc.print(f"[dim]…{total - a.n} more — raise -n or narrow the query[/dim]")
        return 0
    if sub == "icon":
        _lang, im, bm = assets.load_assets(ctx.t, ctx.cfg)
        tex = assets.resolve_icon(a.id, im, bm)
        if not tex:
            rc.print(f"[yellow]no icon texture resolves for {a.id} "
                     "(no item model in the jars/resourcepacks)[/yellow]")
            return 1
        data = assets.fetch_icons(ctx.t, ctx.cfg, [tex])
        png = data.get(assets._norm_id(tex))
        if not png:
            rc.print(f"[yellow]icon {tex} not found in the jars/resourcepacks[/yellow]")
            return 1
        out = a.out or (a.id.split(":")[-1].split("/")[-1] + ".png")
        Path(out).write_bytes(png)
        rc.print(f"[green]wrote[/green] {out}  [dim]({tex}, {len(png)} bytes)[/dim]")
        return 0
    return 2


def cmd_assets(ctx: Ctx) -> int:
    from . import assets
    a = ctx.args
    sub = a.assets_cmd or "status"
    if sub == "status":
        ver = assets.resolve_version(ctx.t, ctx.cfg, a.version or "")
        jar = f"{assets.vanilla_cache_dir(ctx.cfg)}/{ver}.jar" if ver else ""
        cached = bool(jar) and ctx.t.exists(jar)
        if a.json:
            print(json.dumps({"version": ver, "jar": jar, "cached": cached}, indent=2))
            return 0
        rc.print(f"Minecraft version: [bold]{ver or '[red]unknown[/red] (set [server].mc_version)'}[/bold]")
        if ver:
            rc.print(f"vanilla jar: {jar}")
            rc.print("cached: " + ("[green]yes[/green]" if cached
                                   else "[yellow]no — run `mcctl assets sync`[/yellow]"))
        return 0 if ver else 1
    if sub == "sync":
        rc.print("[dim]fetching the vanilla client jar on the server (one-time, ~25 MB)…[/dim]")
        res = assets.sync_vanilla(ctx.t, ctx.cfg, version=a.version or "", force=a.force)
        if a.json:
            print(json.dumps(res, indent=2))
            return 0 if res["ok"] else 1
        if res["ok"]:
            verb = "already cached" if res["status"] == "present" else "downloaded"
            rc.print(f"[green]{verb}[/green] vanilla {res['version']} → {res['jar']}")
            rc.print("[dim]vanilla items now resolve in `mcctl items` and the recipe browser.[/dim]")
            return 0
        rc.print(f"[red]sync failed[/red] ({res['status']}) for {res['version']}")
        return 1
    if sub == "catalog":
        res = assets.catalog(ctx.t, ctx.cfg)
        if a.json:
            print(json.dumps(res, indent=2))
            return 0
        rc.print(f"[bold]{res['count']}[/bold] downloadable item icon(s), "
                 f"[bold]{util.human_bytes(res['bytes'])}[/bold] total "
                 "[dim](the phone's offline-sync set)[/dim]")
        return 0
    return 2


def _render_plan(plan) -> None:
    rec = plan.recipe
    rc.print(f"[bold]{rec.rid}[/bold]  [dim]{rec.rtype}[/dim]  ->  "
             f"{rec.result_count}x {rec.result_item}")
    rc.print(f"source [bold]{plan.source}[/bold] -> receiver [bold]{plan.receiver}[/bold]"
             + ("" if plan.online else "  [red](offline — inventory unreadable)[/red]"))
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold", justify="right")
    t.add_column()
    for ing in plan.ingredients:
        opts = " / ".join(ing["options"])
        have = "?" if ing["loose"] is None else str(ing["loose"])
        extra = (f"  [dim](+{ing['stored']} in storage)[/dim]"
                 if ing.get("stored") else "")
        t.add_row(f"{ing['per_craft']}x", f"{opts}   [dim]have[/dim] {have}{extra}")
    rc.print(t)
    if plan.online:
        rc.print(f"craftable now: [bold]{plan.craftable}[/bold]   "
                 f"one-stack cap: {plan.cap}   "
                 f"[green]would craft {plan.will_craft}[/green] "
                 f"({plan.output_count}x {plan.output_item})")
        if plan.will_craft == 0:
            rc.print("[yellow]not enough materials — recipe shown & planned, not crafted[/yellow]")
        elif plan.limited_by == "stack":
            rc.print("[dim]limited by one output stack (hold/--max caps at a stack)[/dim]")


def _render_cost(cb) -> None:
    head = f"[bold]{cb.target_count}x {cb.target_item}[/bold]  [dim]recipe-tree cost[/dim]"
    if cb.truncated:
        head += "  [yellow](truncated — deep or cyclic tree)[/yellow]"
    rc.print(head)
    if cb.base:
        bt = Table(title="base materials to gather")
        bt.add_column("item", style="bold")
        bt.add_column("count", justify="right")
        for item, n in cb.base.items():
            bt.add_row(item, str(n))
        rc.print(bt)
    intermediates = [s for s in cb.steps if s.depth > 0]
    if intermediates:
        st = Table(title="intermediate crafts (shallow → deep)")
        st.add_column("depth", justify="right", style="dim")
        st.add_column("make")
        st.add_column("crafts", justify="right")
        st.add_column("recipe", style="dim")
        for s in intermediates:
            st.add_row(str(s.depth), f"{s.produced}x {s.item}", str(s.crafts), s.recipe_id)
        rc.print(st)
    if cb.leftovers:
        rc.print("[dim]leftovers:[/dim] "
                 + ", ".join(f"{n}x {it}" for it, n in cb.leftovers.items()))


def cmd_craft(ctx: Ctx) -> int:
    from . import crafting
    a = ctx.args
    rec = crafting.get_recipe(ctx.t, ctx.cfg, a.id)
    count = None if a.max else a.count          # None == hold-to-max
    plan = crafting.plan_craft(ctx.console, ctx.cfg, rec, count=count,
                               source=a.source or "", receiver=a.receiver or "")
    if a.json and a.preview:
        print(json.dumps(plan.to_dict(), indent=2))
        return 0
    _render_plan(plan)
    if a.preview:
        return 0
    if not plan.online:
        rc.print("[red]player is not online — can't craft[/red]")
        return 1
    if plan.will_craft <= 0:
        return 1
    what = f"{plan.output_count}x {plan.output_item} (consumes materials from {plan.source})"
    if not _confirm(f"Craft {what}?", a.yes):
        return 1
    res = crafting.craft(ctx.console, ctx.cfg, rec, count=count,
                         source=a.source or "", receiver=a.receiver or "")
    if a.json:
        print(json.dumps(res.to_dict(), indent=2))
        return 0 if res.ok else 1
    if res.ok:
        rc.print(f"[green]crafted {res.crafted}x[/green] -> gave {res.output_count}x "
                 f"{res.output_item} to {plan.receiver}")
        if res.detail:
            rc.print(f"[yellow]{res.detail}[/yellow]")
    else:
        rc.print(f"[red]craft failed:[/red] {res.detail}")
    return 0 if res.ok else 1


def cmd_config(ctx: Ctx) -> int:
    from . import modconfig as MC
    a = ctx.args
    sub = a.config_cmd or "tree"
    if sub == "tree":
        files = MC.list_config_files(ctx.t, ctx.cfg, associate_mods=not a.no_mods)
        if a.mod:
            files = [f for f in files if f.mod_id == a.mod]
        if a.json:
            print(json.dumps([f.to_dict() for f in files], indent=2))
            return 0
        if not files:
            rc.print("[yellow]no config files found[/yellow]")
            return 0
        total = sum(f.size for f in files)
        t = Table(title=f"{len(files)} config files in {MC.config_dir(ctx.cfg)} "
                        f"({util.human_bytes(total)})")
        t.add_column("file", style="bold")
        t.add_column("mod", style="dim")
        t.add_column("fmt")
        t.add_column("size", justify="right")
        for f in files:
            t.add_row(f.path, f.mod_name or f.mod_id or "—", f.fmt or "?", util.human_bytes(f.size))
        rc.print(t)
        return 0
    if sub == "get":
        res = MC.read_config(ctx.t, ctx.cfg, a.path)
        sys.stdout.write(res["text"])
        if not res["text"].endswith("\n"):
            sys.stdout.write("\n")
        return 0
    if sub == "set":
        from pathlib import Path
        text = sys.stdin.read() if a.stdin else Path(a.file).read_text(encoding="utf-8")
        MC.validate_text(a.path, text)  # fail fast with a clear message before any write
        MC.write_config(ctx.t, ctx.cfg, a.path, text)
        rc.print(f"[green]wrote[/green] config/{a.path} (.bak kept on the server)")
        return _config_apply(ctx, a)
    if sub == "edit":
        return _config_edit(ctx, a)
    return 2


def _config_apply(ctx: Ctx, a: argparse.Namespace) -> int:
    """Honest 'smart apply' after a config write: live-reload reality + opt-in /reload/restart."""
    from . import modconfig as MC
    if ctx.ctl.find_pid() is None:
        rc.print("[dim]server is down — the change loads on next start[/dim]")
        return 0
    rc.print("[dim]NeoForge live-reloads mods that support it; "
             "startup/cached values need a restart[/dim]")
    if a.reload:
        out = MC.trigger_reload(ctx.console)
        rc.print(f"[green]/reload:[/green] {out.strip()[:120]}")
    if a.restart:
        ctx.ctl.restart(reason="config change")
        rc.print("[green]restarted[/green] — change fully applied")
    elif not a.reload:
        rc.print("[dim]add --reload (datapack data) or --restart (guaranteed full apply)[/dim]")
    return 0


def _config_edit(ctx: Ctx, a: argparse.Namespace) -> int:
    import contextlib
    import os
    import shlex
    import subprocess
    import tempfile
    from pathlib import Path

    from . import modconfig as MC
    res = MC.read_config(ctx.t, ctx.cfg, a.path)
    rel = res["path"]
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    with tempfile.NamedTemporaryFile("w", suffix="." + (res["fmt"] or "txt"),
                                     delete=False, encoding="utf-8") as fh:
        fh.write(res["text"])
        tmp = fh.name
    try:
        while True:
            subprocess.call([*shlex.split(editor), tmp])
            new_text = Path(tmp).read_text(encoding="utf-8")
            if new_text == res["text"]:
                rc.print("[yellow]no changes — nothing written[/yellow]")
                return 0
            try:
                MC.validate_text(rel, new_text)
            except ConfigEditError as e:
                rc.print(f"[red]{e}[/red]")
                if not _confirm("re-open the editor to fix it?", a.yes):
                    rc.print("[yellow]aborted — nothing written[/yellow]")
                    return 1
                continue
            break
        MC.write_config(ctx.t, ctx.cfg, rel, new_text)
        rc.print(f"[green]wrote[/green] config/{rel} (.bak kept on the server)")
        return _config_apply(ctx, a)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _ai_chat(ctx: Ctx, opening: str) -> int:
    """Interactive multi-turn conversation. The first turn carries current
    server context (status + recent log); the rest is a plain chat loop."""
    from . import llm
    ok, reason = llm.available(ctx.cfg)
    if not ok:
        rc.print(f"[red]{reason}[/red]")
        return 1
    analyst = llm.Analyst(ctx.cfg)
    context = [llm.status_envelope(ctx.ctl),
               llm.envelope("latest.log tail", logs.tail(ctx.t, ctx.cfg, 120))]
    messages: list[dict] = []
    rc.print(f"[dim]chat with {llm.provider_label(ctx.cfg)} — server context attached. "
             "type 'exit' or Ctrl-D to quit.[/dim]")
    pending = opening
    while True:
        if pending:
            user, pending = pending, ""
        else:
            if not sys.stdin.isatty():
                break
            try:
                user = input("\nyou> ").strip()
            except EOFError:
                rc.print("")
                break
            if not user:
                continue
            if user.lower() in ("exit", "quit", ":q"):
                break
        if not messages:  # first turn: prepend the live server context
            content = ("You are in an interactive session with the server operator. Use "
                       "the attached current server context when relevant.\n\n"
                       + "\n\n".join(context) + f"\n\nOperator: {user}")
        else:
            content = user
        messages.append({"role": "user", "content": content})
        rc.print(f"[dim]{llm.provider_label(ctx.cfg)}…[/dim]")
        try:
            reply = analyst.chat(messages, on_text=lambda s: print(s, end="", flush=True))
        except KeyboardInterrupt:
            rc.print("\n[yellow]interrupted[/yellow]")
            messages.pop()
            continue
        except LlmError as e:
            rc.print(f"\n[red]error:[/red] {e}")
            messages.pop()  # drop the unanswered turn so history stays consistent
            if not sys.stdin.isatty():
                return 1     # non-interactive (one-shot) invocation: surface the failure
            continue
        print()
        messages.append({"role": "assistant", "content": reply})
    return 0


def cmd_ai(ctx: Ctx) -> int:
    from . import inspector, llm
    from . import mods as M
    a = ctx.args
    kind = a.ai_cmd or "logs"
    question = " ".join(a.question) if getattr(a, "question", None) else ""
    if kind == "chat":
        return _ai_chat(ctx, question)
    parts: list[str] = []

    if kind == "logs":
        parts.append(llm.status_envelope(ctx.ctl))
        parts.append(llm.envelope("latest.log", logs.tail(ctx.t, ctx.cfg, ctx.cfg.llm.log_lines)))
        parts.append(llm.metrics_envelope())
    elif kind == "crash":
        name, content = logs.crash_get(ctx.t, ctx.cfg, a.name or "")
        if not name:
            rc.print("[green]no crash reports on the server — nothing to analyze[/green]")
            return 0
        rc.print(f"[dim]analyzing crash-reports/{name}[/dim]")
        parts.append(llm.envelope(f"crash-report {name}", content))
        parts.append(llm.envelope("latest.log tail", logs.tail(ctx.t, ctx.cfg, 120)))
    elif kind == "mods":
        parts.append(llm.envelope("mod-list", M.render_text(M.list_mods(ctx.t, ctx.cfg))))
        parts.append(llm.envelope("latest.log tail", logs.tail(ctx.t, ctx.cfg, 150)))
    elif kind == "inspect":
        section = a.section
        pid = None if section == "host" else ctx.ctl.find_pid()
        rep = inspector.inspect_section(ctx.t, ctx.cfg, section, pid)
        parts.append(llm.envelope(f"inspect-{section}", rep.text))
    elif kind == "ask":
        if not question:
            rc.print("[red]usage: mcctl ai ask QUESTION...[/red]")
            return 2
        parts.append(llm.status_envelope(ctx.ctl))
        parts.append(llm.envelope("latest.log tail", logs.tail(ctx.t, ctx.cfg, 150)))

    rc.print(f"[dim]asking {llm.provider_label(ctx.cfg)}…[/dim]\n")
    llm.Analyst(ctx.cfg).analyze(kind, parts, question=question,
                                 on_text=lambda s: print(s, end="", flush=True))
    print()
    return 0


def cmd_watch(ctx: Ctx) -> int:
    from .watch import run_watch
    run_watch(ctx.cfg, ctx.t, interval=ctx.args.interval, count=ctx.args.count)
    return 0


# metric key -> (label, lo, hi-or-None-for-auto, extractor)
def _heap_pct(s: dict):
    used, total = s.get("heap_used"), s.get("heap_max") or s.get("heap_committed")
    return 100.0 * used / total if used and total else None


def _mem_pct(s: dict):
    used, total = s.get("mem_used"), s.get("mem_total")
    return 100.0 * used / total if used and total else None


_HISTORY_METRICS = {
    "tps": ("TPS", 0.0, 20.0, lambda s: s.get("tps")),
    "mspt": ("MSPT (ms)", 0.0, None, lambda s: s.get("mspt")),
    "heap": ("Heap %", 0.0, 100.0, _heap_pct),
    "players": ("Players", 0.0, None, lambda s: s.get("players")),
    "mem": ("Host RAM %", 0.0, 100.0, _mem_pct),
    "load": ("Load (1m)", 0.0, None, lambda s: s.get("load1")),
}


def cmd_history(ctx: Ctx) -> int:
    from . import charts
    samples = metrics.read_samples(ctx.args.n)
    if not samples:
        rc.print("[yellow]no samples yet — run `mcctl watch`, `mcctl dash`, or the "
                 "watchdog to collect history[/yellow]")
        return 0
    span = ""
    ts = [s.get("ts") for s in samples if s.get("ts")]
    if ts:
        span = (f"{time.strftime('%m-%d %H:%M', time.localtime(min(ts)))} → "
                f"{time.strftime('%m-%d %H:%M', time.localtime(max(ts)))}")
    keys = list(_HISTORY_METRICS) if ctx.args.metric == "all" else [ctx.args.metric]
    rc.print(f"[bold]{len(samples)} samples[/bold]  [dim]{span}[/dim]\n")
    drew = False
    for key in keys:
        label, lo, hi, fn = _HISTORY_METRICS[key]
        values = [fn(s) for s in samples]
        summ = charts.summarize(values)
        if summ.n == 0:
            continue
        drew = True
        top = hi if hi is not None else max(summ.max * 1.15, 1.0)
        for row in charts.block_chart(values, lo=lo, hi=top, width=72, height=8):
            rc.print(f"[green]{row}[/green]")
        rc.print(f"[bold]{label}[/bold]  "
                 f"[dim]last[/dim] {summ.last:.1f}  [dim]min[/dim] {summ.min:.1f}  "
                 f"[dim]avg[/dim] {summ.avg:.1f}  [dim]max[/dim] {summ.max:.1f}  "
                 f"[dim](scale 0–{top:.0f}, n={summ.n})[/dim]\n")
    if not drew:
        rc.print("[yellow]no data for the requested metric(s) in this window[/yellow]")
    return 0


def cmd_trace(ctx: Ctx) -> int:
    from . import tracer
    pid = ctx.ctl.find_pid()
    if not pid:
        rc.print("[red]server is not running — nothing to trace[/red]")
        return 1
    if ctx.args.learn:
        rc.print(f"[dim]{tracer.EXPLAIN}[/dim]")
    rc.print(f"[dim]tracing GC on pid {pid} every {ctx.args.interval}ms — Ctrl-C to stop[/dim]")
    try:
        for snap, d in tracer.gc_trace(ctx.t, ctx.cfg, pid, interval_ms=ctx.args.interval):
            ts = time.strftime("%H:%M:%S")
            line = (f"{ts}  eden [bold]{snap.get('E', 0):5.1f}%[/bold]  "
                    f"old [bold]{snap.get('O', 0):5.1f}%[/bold]  meta {snap.get('M', 0):5.1f}%")
            if d and d.young_gcs:
                line += f"  [yellow]YGC+{d.young_gcs} ({d.young_pause_ms:.1f}ms)[/yellow]"
            if d and d.full_gcs:
                line += f"  [bold red]FGC+{d.full_gcs} ({d.full_pause_ms:.0f}ms)[/bold red]"
            rc.print(line)
    except KeyboardInterrupt:
        rc.print("[yellow]stopped[/yellow]")
    except tracer.TraceError as e:
        rc.print(f"[red]trace error:[/red] {e}")
        return 1
    return 0


def cmd_dash(ctx: Ctx) -> int:
    from .dash import run_dash
    run_dash(ctx.cfg, ctx.t)
    return 0


def cmd_gui(ctx: Ctx) -> int:
    from .gui import main as gui_main
    argv = ["--config", ctx.args.config] if ctx.args.config else []
    argv += ["-v"] * ctx.args.verbose
    return gui_main(argv)


def cmd_agent(ctx: Ctx) -> int:
    from . import agent
    if ctx.args.schema:
        print(json.dumps(agent.build_schema(), indent=2, sort_keys=True))
        return 0
    return agent.serve(ctx)


def _print_event(ev: dict) -> None:
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(ev.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
    color = "red" if ev.get("urgency") == "critical" else "cyan"
    kind = ev.get("kind", "?")
    rc.print(f"[dim]{when}[/dim] [{color}]{kind:<16}[/{color}] {ev.get('detail', '')}")


def cmd_events(ctx: Ctx) -> int:
    from . import events
    since = time.time() - ctx.args.since if ctx.args.since else None
    if ctx.args.follow:
        for ev in events.follow(since=since if since is not None else time.time()):
            _print_event(ev)
        return 0
    evs = events.read(since=since, limit=None if since is not None else ctx.args.n)
    if ctx.args.json:
        print(json.dumps(evs, indent=2))
        return 0
    if not evs:
        rc.print("[dim]no events recorded yet[/dim]")
        return 0
    for ev in evs:
        _print_event(ev)
    return 0


def cmd_metrics(ctx: Ctx) -> int:
    from . import prometheus
    sub = ctx.args.metrics_cmd or "export"
    if sub == "export":
        out = ctx.args.out or (ctx.cfg.metrics.prom_path or None)
        p = prometheus.export(ctx.cfg, out=out)
        if ctx.args.cat:
            print(p.read_text(encoding="utf-8"), end="")
        else:
            rc.print(f"[green]wrote[/green] {p}")
        return 0
    return 2


def cmd_notify_test(ctx: Ctx) -> int:
    w = ctx.cfg.watchdog
    util.notify("mcctl: test alert", "If you can read this, mcctl alerting works.",
                desktop=w.notify_desktop, webhook_url=w.webhook_url,
                ntfy_url=w.ntfy_url, ntfy_topic=w.ntfy_topic, ntfy_token=w.ntfy_token,
                urgency="normal")
    sinks = []
    if w.notify_desktop:
        sinks.append("desktop")
    if w.webhook_url:
        sinks.append("webhook")
    if w.ntfy_topic:
        sinks.append(f"ntfy:{w.ntfy_url.rstrip('/')}/{w.ntfy_topic}")
    rc.print("test alert dispatched to: " + (", ".join(sinks) or "[yellow]no sinks configured[/yellow]"))
    return 0


# ================================================================ parser

def build_parser() -> argparse.ArgumentParser:
    # shared by the main parser and every (nested) subparser, so global flags
    # work in any position: `mcctl --config X status` == `mcctl status --config X`
    # SUPPRESS so a subparser never clobbers a value parsed by the main parser
    # (`mcctl --config X status` used to silently lose X); main() fills the
    # defaults in when the flag was never given at all.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="config file (default: ~/.config/mcctl/config.toml)")
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
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

    sp = sub.add_parser("postmortem",
                        help="what went wrong — newest crash + watchdog events, no AI needed")
    sp.add_argument("--crash", default="", help="crash report filename (default: newest)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_postmortem)

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
    b.add_argument("--to", metavar="DIR", default=None,
                   help="unpack into DIR (server-side) for side-by-side inspection — "
                        "never touches the live world, works while running")
    b.add_argument("--yes", action="store_true")
    b = bsub.add_parser("offsite", help="mirror archives to the off-site rclone remote")
    b.add_argument("--dry-run", action="store_true", help="show what rclone would transfer")
    sp.set_defaults(func=cmd_backup, backup_cmd=None, full=False, dry_run=False, notify=False,
                    to=None)

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

    from .inspector import SECTIONS as _INSPECT_SECTIONS
    sp = sub.add_parser("inspect", help="deep OS/JVM introspection (/proc, threads, jcmd…)")
    sp.add_argument("section", nargs="?", choices=list(_INSPECT_SECTIONS), default="host",
                    help="what to inspect (default: host)")
    sp.add_argument("--learn", action="store_true",
                    help="append the plain-language explanation of what you're seeing")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("mods", help="list server mods with versions (metadata from the jars)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--diff", metavar="CLIENT_MODS_DIR", default=None,
                    help="diff the server's mods against a local client mods/ directory "
                         "(server-only / client-only / version mismatch)")
    sp.set_defaults(func=cmd_mods)

    sp = sub.add_parser("recipes", help="browse the pack's crafting recipes (from jars + datapacks)")
    rsub = nested(sp, "recipes_cmd")
    rs = rsub.add_parser("search", help="find recipes by id/output substring")
    rs.add_argument("query", nargs="?", default="")
    rs.add_argument("-n", type=int, default=60, help="max results")
    rs.add_argument("--craftable", action="store_true",
                    help="only recipes you can craft from live inventory")
    rs.add_argument("--player", default="", help="whose inventory to check (default: config)")
    rs.add_argument("--json", action="store_true")
    rsh = rsub.add_parser("show", help="show one recipe (ingredients + grid)")
    rsh.add_argument("id")
    rsh.add_argument("--json", action="store_true")
    rco = rsub.add_parser("cost", help="recipe-tree: total base materials + leftovers for a deep craft")
    rco.add_argument("id", help="recipe id, e.g. minecraft:chest")
    rco.add_argument("--count", type=int, default=1, help="how many to craft (default 1)")
    rco.add_argument("--max-depth", type=int, default=64, dest="max_depth")
    rco.add_argument("--json", action="store_true")
    rt = rsub.add_parser("tag", help="resolve a #tag ingredient to its concrete items")
    rt.add_argument("id", help="tag id, e.g. minecraft:planks (a leading # is optional)")
    rt.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_recipes, recipes_cmd=None, query="", n=60, json=False, id=None,
                    craftable=False, player="", count=1, max_depth=64)

    sp = sub.add_parser("items", help="EMI-style item index: ids, display names, icon textures")
    isub = nested(sp, "items_cmd")
    il = isub.add_parser("list", help="list items (id · name · icon texture)")
    il.add_argument("query", nargs="?", default="")
    il.add_argument("-n", type=int, default=200, help="max results")
    il.add_argument("--json", action="store_true")
    isr = isub.add_parser("search", help="search items by id or display name")
    isr.add_argument("query", nargs="?", default="")
    isr.add_argument("-n", type=int, default=200, help="max results")
    isr.add_argument("--json", action="store_true")
    ic = isub.add_parser("icon", help="resolve & download one item's icon PNG")
    ic.add_argument("id", help="item id, e.g. minecraft:chest")
    ic.add_argument("-o", "--out", help="output .png path (default: <item>.png)")
    ic.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_items, items_cmd=None, query="", n=200, json=False,
                    id=None, out=None)

    sp = sub.add_parser("assets",
                        help="vanilla item assets: fetch the matching client jar so vanilla "
                             "items get icons + names (mods/resourcepacks still override)")
    asub = nested(sp, "assets_cmd")
    ast = asub.add_parser("status", help="show the detected MC version + whether the jar is cached")
    ast.add_argument("--version", help="override the MC version (e.g. 1.21.1)")
    ast.add_argument("--json", action="store_true")
    asy = asub.add_parser("sync", help="download/refresh the vanilla client jar on the server")
    asy.add_argument("--version", help="override the MC version (default: config or auto-detect)")
    asy.add_argument("--force", action="store_true", help="re-download even if already cached")
    asy.add_argument("--json", action="store_true")
    aca = asub.add_parser("catalog",
                          help="the phone's offline-sync set: every icon texture + crc + size")
    aca.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_assets, assets_cmd=None, version=None, force=False, json=False)

    sp = sub.add_parser("craft",
                        help="survival command-craft: consume materials, give the output")
    sp.add_argument("id", help="recipe id, e.g. minecraft:chest")
    cg = sp.add_mutually_exclusive_group()
    cg.add_argument("--count", type=int, default=1, help="how many to craft (default 1)")
    cg.add_argument("--max", action="store_true",
                    help="craft the most your materials allow (capped at one output stack)")
    sp.add_argument("--source", help="player whose inventory supplies materials (default: config)")
    sp.add_argument("--receiver", help="player who gets the output (default: config)")
    sp.add_argument("--preview", action="store_true", help="plan only — don't craft")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_craft)

    sp = sub.add_parser("config", help="browse & edit mod config files under config/ (live-reload aware)")
    csub = nested(sp, "config_cmd")
    ct = csub.add_parser("tree", help="list config files (best-effort grouped by mod)")
    ct.add_argument("--mod", help="only files owned by this mod id")
    ct.add_argument("--no-mods", dest="no_mods", action="store_true",
                    help="skip jar-metadata association (faster)")
    ct.add_argument("--json", action="store_true")
    cg = csub.add_parser("get", help="print a config file (path relative to config/)")
    cg.add_argument("path")
    cs = csub.add_parser("set", help="overwrite a config file from a local file or stdin")
    cs.add_argument("path")
    csrc = cs.add_mutually_exclusive_group(required=True)
    csrc.add_argument("--file", metavar="LOCAL", help="local file whose contents to upload")
    csrc.add_argument("--stdin", action="store_true", help="read the new contents from stdin")
    cs.add_argument("--reload", action="store_true", help="also run /reload after writing")
    cs.add_argument("--restart", action="store_true", help="graceful restart after writing (full apply)")
    cs.add_argument("--yes", action="store_true")
    ce = csub.add_parser("edit", help="fetch -> $EDITOR -> validate -> upload (.bak kept)")
    ce.add_argument("path")
    ce.add_argument("--reload", action="store_true", help="run /reload after a successful edit")
    ce.add_argument("--restart", action="store_true", help="graceful restart after a successful edit")
    ce.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_config, config_cmd=None, mod=None, no_mods=False, json=False,
                    path=None, file=None, stdin=False, reload=False, restart=False, yes=False)

    sp = sub.add_parser("ai", help="LLM analysis: logs, crash reports, mods, inspections")
    asub = nested(sp, "ai_cmd")
    al = asub.add_parser("logs", help="review server state + recent log")
    al.add_argument("question", nargs="*", help="optional extra question")
    ac = asub.add_parser("crash", help="root-cause the newest (or named) crash report")
    ac.add_argument("name", nargs="?", default="")
    ac.add_argument("question", nargs="*", help="optional extra question")
    am = asub.add_parser("mods", help="explain what the installed mods do & flag offenders")
    am.add_argument("question", nargs="*", help="optional extra question")
    ai_ = asub.add_parser("inspect", help="teacher-mode explanation of an inspector section")
    ai_.add_argument("section", choices=list(_INSPECT_SECTIONS))
    ai_.add_argument("question", nargs="*", help="optional extra question")
    aa = asub.add_parser("ask", help="free-form question with server context attached")
    aa.add_argument("question", nargs="+")
    ach = asub.add_parser("chat", help="interactive multi-turn conversation (Claude or local ollama)")
    ach.add_argument("question", nargs="*", help="optional opening message")
    sp.set_defaults(func=cmd_ai, ai_cmd=None, question=[], name="", section="host")

    sp = sub.add_parser("watch", help="live one-line status stream (also records metric history)")
    sp.add_argument("--interval", type=float, default=10.0, help="seconds between samples")
    sp.add_argument("-n", "--count", type=int, default=0, help="stop after N samples (0 = forever)")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("history", help="TPS/MSPT/heap/players charts from local metric history")
    sp.add_argument("metric", nargs="?", default="tps",
                    choices=["tps", "mspt", "heap", "players", "mem", "load", "all"])
    sp.add_argument("-n", type=int, default=240, help="how many recent samples to chart")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("trace", help="live JVM GC tracer (young/full GC, pauses, heap regions)")
    sp.add_argument("--interval", type=int, default=1000, help="jstat sample interval (ms)")
    sp.add_argument("--learn", action="store_true",
                    help="explain generations, eden/old, and what the pauses mean")
    sp.set_defaults(func=cmd_trace)

    sp = sub.add_parser("dash", help="live TUI dashboard")
    sp.set_defaults(func=cmd_dash)

    sp = sub.add_parser("gui", help="GTK4/libadwaita desktop app (also installed as mcctl-gui)")
    sp.set_defaults(func=cmd_gui)

    sp = sub.add_parser("agent", help="JSON-RPC 2.0 server over stdio (for the phone app / scripts)")
    sp.add_argument("--schema", action="store_true",
                    help="print the versioned contract and exit (no server)")
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("events", help="watchdog/heal/alert event journal (tail or follow)")
    sp.add_argument("-f", "--follow", action="store_true", help="stream new events as they happen")
    sp.add_argument("-n", type=int, default=30, help="how many recent events to show")
    sp.add_argument("--since", type=float, default=0.0, metavar="SECS",
                    help="only events from the last N seconds")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("metrics", help="Prometheus textfile exporter (node_exporter/Grafana)")
    msub = nested(sp, "metrics_cmd")
    me = msub.add_parser("export", help="write the latest sample as a .prom textfile (default)")
    me.add_argument("--out", help="output path (default: [metrics].prom_path or state dir)")
    me.add_argument("--cat", action="store_true", help="also print the rendered file to stdout")
    sp.set_defaults(func=cmd_metrics, metrics_cmd=None, out=None, cat=False)

    sp = sub.add_parser("notify-test", help="fire a test alert through every configured sink")
    sp.set_defaults(func=cmd_notify_test)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "config"):
        args.config = None
    if not hasattr(args, "verbose"):
        args.verbose = 0
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
    except (InspectError, ModsError, LlmError, ConfigEditError, CraftError) as e:
        rc.print(f"[red]error:[/red] {e}")
        return 1
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
