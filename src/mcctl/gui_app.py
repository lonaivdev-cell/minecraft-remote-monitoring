"""GTK4 + libadwaita desktop app: the `mcctl dash` feature set, native-looking.

Only imported by `mcctl.gui` after PyGObject availability is verified — keep
every `gi` touch in this module.

Threading model: GTK owns the main loop; every remote (SSH) operation runs on
a single background worker thread, so operations serialize exactly like the
CLI (and the transport is never used concurrently). Results are marshalled
back to the main loop with GLib.idle_add.
"""

from __future__ import annotations

import argparse
import contextlib
import queue
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, inspector, llm, logs, metrics, mods, state, util  # noqa: E402
from .backup import BackupManager  # noqa: E402
from .config import Config, ConfigError  # noqa: E402
from .console import Console, ConsoleError  # noqa: E402
from .players import PlayerError, Players  # noqa: E402
from .server import ServerControl, Status  # noqa: E402
from .transport import BaseTransport, TransportError, make_transport  # noqa: E402

log = util.get_logger("gui")

APP_ID = "io.github.lonaivdev_cell.mcctl"
FAST_TICK = 5    # seconds: cheap status probe (+ log tail when visible)
SLOW_TICK = 20   # seconds: full status (spark TPS, players, heap)
LOG_LINES = 200

_CSS = """
.status-pill { padding: 6px 20px; border-radius: 999px; font-weight: 800; letter-spacing: 1px; }
.status-pill.success { background: alpha(@success_bg_color, 0.25); }
.status-pill.warning { background: alpha(@warning_bg_color, 0.30); }
.status-pill.error   { background: alpha(@error_bg_color, 0.25); }
"""


class Remote:
    """Lazy transport/console/control holder — touched only on the worker thread."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._t: BaseTransport | None = None
        self._console: Console | None = None
        self._ctl: ServerControl | None = None

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
        if self._ctl is None:
            self._ctl = ServerControl(self.cfg, self.t, self.console)
        return self._ctl

    @property
    def players(self) -> Players:
        return Players(self.cfg, self.t, self.console)

    @property
    def backups(self) -> BackupManager:
        return BackupManager(self.cfg, self.t, self.console)

    def close(self) -> None:
        if self._console:
            self._console.close()
        if self._t:
            self._t.close()


class Worker(threading.Thread):
    """Single background thread that serializes all remote operations."""

    def __init__(self):
        super().__init__(daemon=True, name="mcctl-gui-worker")
        self._q: queue.Queue = queue.Queue()
        self.start()

    def submit(self, fn, on_done=None, on_error=None) -> None:
        """Run fn() on the worker; deliver the result/exception on the main loop."""
        self._q.put((fn, on_done, on_error))

    def run(self) -> None:
        while True:
            fn, on_done, on_error = self._q.get()
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001 - every failure becomes a toast
                log.exception("gui worker operation failed")
                if on_error:
                    GLib.idle_add(on_error, e)
            else:
                if on_done:
                    GLib.idle_add(on_done, result)


class Window(Adw.ApplicationWindow):
    def __init__(self, app: McctlApp):
        super().__init__(application=app, title="mcctl")
        self.set_default_size(960, 720)
        self.remote: Remote = app.remote
        self.worker: Worker = app.worker
        self.status = Status()
        self._busy = ""
        self._refreshing = False
        self._logs_refreshing = False
        self._players_refreshing = False
        self._backups_refreshing = False
        self._syncing_armed = False
        self._tick_count = 0
        self._online_rows: list[Gtk.Widget] = []
        self._wl_rows: list[Gtk.Widget] = []
        self._backup_rows: list[Gtk.Widget] = []
        self._mod_rows: list[Gtk.Widget] = []
        self._mods_refreshing = False
        self._mods_loaded = False
        self._inspect_refreshing = False
        self._ai_running = False

        self._build_ui()

        act = Gio.SimpleAction.new("refresh", None)
        act.connect("activate", lambda *_: (self._refresh(full=True), self._refresh_aux()))
        self.add_action(act)

        GLib.timeout_add_seconds(FAST_TICK, self._on_tick)
        self._refresh(full=True)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.add_titled_with_icon(self._build_overview(), "overview", "Overview",
                                        "utilities-system-monitor-symbolic")
        self.stack.add_titled_with_icon(self._build_console(), "console", "Console",
                                        "utilities-terminal-symbolic")
        self.stack.add_titled_with_icon(self._build_logs(), "logs", "Logs",
                                        "text-x-generic-symbolic")
        self.stack.add_titled_with_icon(self._build_players(), "players", "Players",
                                        "system-users-symbolic")
        self.stack.add_titled_with_icon(self._build_backups(), "backups", "Backups",
                                        "drive-harddisk-symbolic")
        self.stack.add_titled_with_icon(self._build_mods(), "mods", "Mods",
                                        "package-x-generic-symbolic")
        self.stack.add_titled_with_icon(self._build_inspect(), "inspect", "Inspect",
                                        "system-search-symbolic")
        self.stack.add_titled_with_icon(self._build_ai(), "ai", "AI",
                                        "starred-symbolic")
        self.stack.connect("notify::visible-child-name", lambda *_: self._refresh_aux())

        header = Adw.HeaderBar(
            title_widget=Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                 tooltip_text="Refresh now (Ctrl+R)",
                                 action_name="win.refresh")
        header.pack_start(refresh_btn)

        menu = Gio.Menu()
        menu.append("About mcctl", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu"))
        self.spinner = Gtk.Spinner()
        header.pack_end(self.spinner)

        self.banner = Adw.Banner()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        box.append(self.stack)
        self.toasts = Adw.ToastOverlay(child=box)

        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_content(view)

    def _build_overview(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)

        self.badge = Gtk.Label(label="CONNECTING…", halign=Gtk.Align.CENTER)
        self.badge.add_css_class("status-pill")
        self.badge.add_css_class("title-2")
        box.append(self.badge)
        self.badge_sub = Gtk.Label(halign=Gtk.Align.CENTER, ellipsize=3)  # 3 = END
        self.badge_sub.add_css_class("dim-label")
        box.append(self.badge_sub)

        actions = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.btn_start = self._pill("Start", self._act_start, "suggested-action")
        self.btn_stop = self._pill("Stop", self._act_stop, "destructive-action")
        self.btn_restart = self._pill("Restart", self._act_restart)
        self.btn_save = self._pill("Save", self._act_save)
        self.btn_backup = self._pill("Back up", self._act_backup)
        self.btn_purge = self._pill("Purge GC", self._act_purge)
        for b in (self.btn_start, self.btn_stop, self.btn_restart,
                  self.btn_save, self.btn_backup, self.btn_purge):
            actions.append(b)
        box.append(actions)

        g = Adw.PreferencesGroup(title="Server")
        self.row_process = self._kv(g, "Process")
        self.row_players = self._kv(g, "Players")
        self.tps_bar, self.row_tps = self._bar_row(g, "TPS", lo=0, hi=20,
                                                   offsets=(("low", 12.0), ("high", 18.0), ("full", 20.0)))
        self.heap_bar, self.row_heap = self._bar_row(g, "Heap")
        self.row_channel = self._kv(g, "Console channel")
        box.append(g)

        g = Adw.PreferencesGroup(title="Host")
        self.ram_bar, self.row_ram = self._bar_row(g, "RAM")
        self.row_load = self._kv(g, "Load")
        self.row_disk = self._kv(g, "Disk free")
        self.row_log = self._kv(g, "Log activity")
        self.row_backup = self._kv(g, "Last backup")
        box.append(g)

        g = Adw.PreferencesGroup(title="Watchdog")
        self.armed_row = Adw.SwitchRow(
            title="Armed",
            subtitle="Heal crashes and freezes automatically while desired state is up")
        self.armed_row.connect("notify::active", self._on_armed_toggled)
        g.add(self.armed_row)
        self.row_wd = self._kv(g, "State")
        box.append(g)

        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=760, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_console(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.console_view = self._textview()
        box.append(Gtk.ScrolledWindow(child=self.console_view, vexpand=True))
        hint = Gtk.Label(label="Sent via RCON over the SSH tunnel (tmux + log fallback).",
                         halign=Gtk.Align.START)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)
        row = Gtk.Box(spacing=6)
        self.cmd_entry = Gtk.Entry(placeholder_text="Console command — e.g. list, say hi, whitelist on",
                                   hexpand=True)
        self.cmd_entry.connect("activate", self._on_console_send)
        send = Gtk.Button(label="Send")
        send.add_css_class("suggested-action")
        send.connect("clicked", self._on_console_send)
        row.append(self.cmd_entry)
        row.append(send)
        box.append(row)
        return box

    def _build_logs(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        title = Gtk.Label(label=f"{self.remote.cfg.server.log_file} — last {LOG_LINES} lines, "
                                f"refreshed every {FAST_TICK}s while visible",
                          halign=Gtk.Align.START)
        title.add_css_class("dim-label")
        title.add_css_class("caption")
        box.append(title)
        self.log_view = self._textview()
        box.append(Gtk.ScrolledWindow(child=self.log_view, vexpand=True))
        return box

    def _build_players(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        self.online_group = Adw.PreferencesGroup(title="Online players")
        box.append(self.online_group)
        self.wl_group = Adw.PreferencesGroup(title="Whitelist")
        self.wl_entry = Adw.EntryRow(title="Add player to whitelist", show_apply_button=True)
        self.wl_entry.connect("apply", self._on_wl_add)
        self.wl_group.add(self.wl_entry)
        box.append(self.wl_group)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=760, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_backups(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        self.backups_group = Adw.PreferencesGroup(title="Archives")
        suffix = Gtk.Box(spacing=8)
        self.backup_full_check = Gtk.CheckButton(label="Full instance",
                                                 tooltip_text="Whole server dir, not just the world")
        suffix.append(self.backup_full_check)
        new_btn = Gtk.Button(label="New backup")
        new_btn.add_css_class("suggested-action")
        new_btn.connect("clicked", self._act_backup)
        suffix.append(new_btn)
        self.backups_group.set_header_suffix(suffix)
        box.append(self.backups_group)
        note = Gtk.Label(
            label="Snapshots are consistent while live (save-off → flush → tar → verify → save-on)\n"
                  "and rotated GFS-style. Restore stays CLI-only: mcctl backup restore NAME.",
            halign=Gtk.Align.START, justify=Gtk.Justification.LEFT)
        note.add_css_class("dim-label")
        note.add_css_class("caption")
        box.append(note)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=760, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_mods(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        self.mods_group = Adw.PreferencesGroup(title="Installed mods",
                                               description="Metadata read from inside each jar")
        refresh = Gtk.Button(label="Rescan")
        refresh.connect("clicked", lambda *_: self._refresh_mods(force=True))
        self.mods_group.set_header_suffix(refresh)
        box.append(self.mods_group)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=860, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_inspect(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        row = Gtk.Box(spacing=8)
        self.inspect_drop = Gtk.DropDown.new_from_strings(list(inspector.SECTIONS))
        self.inspect_drop.connect("notify::selected", lambda *_: self._refresh_inspect())
        row.append(self.inspect_drop)
        self.inspect_learn = Gtk.ToggleButton(label="Learn mode",
                                              tooltip_text="Show the plain-language walkthrough "
                                                           "of what each number means")
        self.inspect_learn.set_active(True)
        self.inspect_learn.connect("toggled", lambda *_: self._refresh_inspect())
        row.append(self.inspect_learn)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Re-probe")
        refresh.connect("clicked", lambda *_: self._refresh_inspect())
        row.append(refresh)
        ask = Gtk.Button(label="Explain with AI")
        ask.connect("clicked", self._on_inspect_ai)
        row.append(ask)
        box.append(row)
        hint = Gtk.Label(label="Live kernel/JVM state: /proc, threads, memory maps, fds, "
                               "sockets, jcmd — how the OS actually runs your server.",
                         halign=Gtk.Align.START)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)
        self.inspect_view = self._textview()
        box.append(Gtk.ScrolledWindow(child=self.inspect_view, vexpand=True))
        return box

    def _build_ai(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        ok, reason = llm.available()
        if not ok:
            note = Gtk.Label(label=f"AI analysis is unavailable:\n{reason}",
                             halign=Gtk.Align.START, selectable=True)
            note.add_css_class("dim-label")
            box.append(note)
        row = Gtk.Box(spacing=8)
        self.ai_kinds = ("logs", "crash", "mods", "ask")
        self.ai_drop = Gtk.DropDown.new_from_strings(
            ["Review logs", "Analyze latest crash", "Explain the mods", "Ask a question"])
        row.append(self.ai_drop)
        self.ai_entry = Gtk.Entry(placeholder_text="Optional question — e.g. why did TPS drop "
                                                   "around 21:30?", hexpand=True)
        self.ai_entry.connect("activate", self._on_ai_run)
        row.append(self.ai_entry)
        self.ai_run = Gtk.Button(label="Analyze")
        self.ai_run.add_css_class("suggested-action")
        self.ai_run.connect("clicked", self._on_ai_run)
        self.ai_run.set_sensitive(ok)
        row.append(self.ai_run)
        box.append(row)
        hint = Gtk.Label(label=f"Model: {self.remote.cfg.llm.model} · logs/crash text is "
                               "secret-redacted and sent as untrusted data only.",
                         halign=Gtk.Align.START)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)
        self.ai_view = self._textview()
        self.ai_view.set_monospace(False)
        self.ai_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        box.append(Gtk.ScrolledWindow(child=self.ai_view, vexpand=True))
        return box

    # ------------------------------------------------------------ UI helpers

    def _pill(self, label: str, cb, *classes: str) -> Gtk.Button:
        b = Gtk.Button(label=label)
        b.add_css_class("pill")
        for c in classes:
            b.add_css_class(c)
        b.connect("clicked", cb)
        return b

    def _kv(self, group: Adw.PreferencesGroup, title: str) -> Gtk.Label:
        row = Adw.ActionRow(title=title)
        label = Gtk.Label(label="—", xalign=1, selectable=True)
        label.add_css_class("dim-label")
        row.add_suffix(label)
        group.add(row)
        return label

    def _bar_row(self, group: Adw.PreferencesGroup, title: str, *, lo: float = 0.0, hi: float = 1.0,
                 offsets: tuple = (("full", 0.70), ("high", 0.90), ("low", 1.0)),
                 ) -> tuple[Gtk.LevelBar, Gtk.Label]:
        """Row with a colored level bar + value label. Default offsets are for
        usage gauges (green < 70%, yellow < 90%, red above)."""
        row = Adw.ActionRow(title=title)
        bar = Gtk.LevelBar(min_value=lo, max_value=hi, valign=Gtk.Align.CENTER)
        bar.set_size_request(160, -1)
        for name, value in offsets:
            bar.add_offset_value(name, value)
        label = Gtk.Label(label="—", xalign=1)
        label.add_css_class("dim-label")
        row.add_suffix(bar)
        row.add_suffix(label)
        group.add(row)
        return bar, label

    def _textview(self) -> Gtk.TextView:
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR)
        for setter in (view.set_left_margin, view.set_right_margin,
                       view.set_top_margin, view.set_bottom_margin):
            setter(8)
        return view

    def _append_text(self, view: Gtk.TextView, text: str) -> None:
        buf = view.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")
        self._scroll_to_end(view)

    def _scroll_to_end(self, view: Gtk.TextView) -> None:
        def go():
            buf = view.get_buffer()
            mark = buf.get_mark("tail") or buf.create_mark("tail", buf.get_end_iter(), False)
            buf.move_mark(mark, buf.get_end_iter())
            view.scroll_mark_onscreen(mark)
            return False
        GLib.idle_add(go)

    def _toast(self, msg: str, timeout: int = 4) -> None:
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=timeout))

    def _confirm(self, heading: str, body: str, action_label: str, on_confirm) -> None:
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("go", action_label)
        dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, resp: on_confirm() if resp == "go" else None)
        dlg.present(self)

    # ------------------------------------------------------------- refresh

    def _on_tick(self) -> bool:
        if self._busy:
            return True  # an action owns the worker; don't pile refreshes behind it
        self._tick_count += 1
        self._refresh(full=self._tick_count % max(1, SLOW_TICK // FAST_TICK) == 0)
        self._refresh_aux()
        return True

    def _refresh(self, *, full: bool) -> None:
        if self._refreshing:
            return
        self._refreshing = True

        def job():
            st = self.remote.ctl.status(full=full)
            if full and not st.errors and st.running:
                metrics.append_sample(metrics.sample_from_status(st))
            return st

        def done(st: Status):
            self._refreshing = False
            if not full and not st.errors:
                prev = self.status  # keep the slow-probe data between full refreshes
                st.tps, st.players, st.channel = prev.tps, prev.players, prev.channel
                st.heap_used, st.heap_committed = prev.heap_used, prev.heap_committed
                st.heap_max = prev.heap_max
            self._apply_status(st)

        def err(e: Exception):
            self._refreshing = False
            self.status.errors = [str(e)]
            self._apply_status(self.status)

        self.worker.submit(job, done, err)

    def _refresh_aux(self) -> None:
        page = self.stack.get_visible_child_name()
        if page == "logs":
            self._refresh_logs()
        elif page == "players":
            self._refresh_players()
        elif page == "backups":
            self._refresh_backups()
        elif page == "mods" and not self._mods_loaded:
            self._refresh_mods()  # scan once; mods rarely change while we watch
        elif page == "inspect" and self.inspect_view.get_buffer().get_char_count() == 0:
            self._refresh_inspect()

    def _refresh_logs(self) -> None:
        if self._logs_refreshing or self._busy:
            return
        self._logs_refreshing = True

        def done(text: str):
            self._logs_refreshing = False
            self.log_view.get_buffer().set_text(text)
            self._scroll_to_end(self.log_view)

        def err(e: Exception):
            self._logs_refreshing = False
            self.log_view.get_buffer().set_text(f"(log unavailable: {e})")

        self.worker.submit(lambda: logs.tail(self.remote.t, self.remote.cfg, LOG_LINES), done, err)

    def _refresh_players(self) -> None:
        if self._players_refreshing or self._busy:
            return
        self._players_refreshing = True

        def job():
            online = self.remote.players.online()
            try:
                wl = self.remote.players.whitelist()
            except (PlayerError, ConsoleError, TransportError):
                wl = None
            return online, wl

        def done(res):
            self._players_refreshing = False
            online, wl = res
            if online is None:
                self.online_group.set_description("Console unreachable — is the server running?")
                names = []
            else:
                self.online_group.set_description(f"{online.count}/{online.max} online")
                names = online.names
            self._online_rows = self._swap_rows(
                self.online_group, self._online_rows, [self._player_row(n) for n in names])
            if wl is None:
                self.wl_group.set_description("Whitelist unavailable (console unreachable)")
                wl = []
            else:
                self.wl_group.set_description(f"{len(wl)} name(s)")
            self._wl_rows = self._swap_rows(
                self.wl_group, self._wl_rows, [self._wl_row(n) for n in sorted(wl)])

        def err(e: Exception):
            self._players_refreshing = False
            self.online_group.set_description(str(e)[:160])

        self.worker.submit(job, done, err)

    def _refresh_backups(self) -> None:
        if self._backups_refreshing or self._busy:
            return
        self._backups_refreshing = True

        def done(entries):
            self._backups_refreshing = False
            d = self.remote.cfg.backup.remote_dir
            self.backups_group.set_description(
                f"{len(entries)} archive(s) in {d}" if entries else f"No backups yet in {d}")
            self._backup_rows = self._swap_rows(
                self.backups_group, self._backup_rows, [self._backup_row(e) for e in entries])

        def err(e: Exception):
            self._backups_refreshing = False
            self.backups_group.set_description(str(e)[:160])

        self.worker.submit(lambda: self.remote.backups.list(), done, err)

    def _refresh_mods(self, *, force: bool = False) -> None:
        if self._mods_refreshing or (self._busy and not force):
            return
        self._mods_refreshing = True
        self.mods_group.set_description("Scanning jars on the server…")

        def done(entries):
            self._mods_refreshing = False
            self._mods_loaded = True
            total = sum(m.size for m in entries)
            self.mods_group.set_description(
                f"{len(entries)} mods · {util.human_bytes(total)} · metadata read from inside each jar")
            self._mod_rows = self._swap_rows(
                self.mods_group, self._mod_rows, [self._mod_row(m) for m in entries])

        def err(e: Exception):
            self._mods_refreshing = False
            self.mods_group.set_description(str(e)[:160])

        self.worker.submit(lambda: mods.list_mods(self.remote.t, self.remote.cfg), done, err)

    def _mod_row(self, m) -> Adw.ActionRow:
        sub = " · ".join(x for x in (m.mod_id, m.version or None,
                                     util.human_bytes(m.size), m.file) if x)
        row = Adw.ActionRow(title=m.name or m.file, subtitle=sub)
        if m.description:
            row.set_tooltip_text(m.description)
        return row

    def _refresh_inspect(self) -> None:
        if self._inspect_refreshing:
            return
        self._inspect_refreshing = True
        section = inspector.SECTIONS[self.inspect_drop.get_selected()]
        learn = self.inspect_learn.get_active()

        def job():
            pid = None if section == "host" else self.remote.ctl.find_pid()
            return inspector.inspect_section(self.remote.t, self.remote.cfg, section, pid)

        def done(rep):
            self._inspect_refreshing = False
            text = f"── {rep.title} ──\n\n{rep.text}"
            if learn:
                text += f"\n\n── what am I looking at? ──\n{inspector.EXPLAIN[section]}"
            self.inspect_view.get_buffer().set_text(text)

        def err(e: Exception):
            self._inspect_refreshing = False
            self.inspect_view.get_buffer().set_text(f"(inspection failed: {e})")

        self.worker.submit(job, done, err)

    # ------------------------------------------------------------- AI page

    def _on_inspect_ai(self, *_):
        section = inspector.SECTIONS[self.inspect_drop.get_selected()]
        buf = self.inspect_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if not text.strip():
            self._toast("Run an inspection first")
            return
        self.stack.set_visible_child_name("ai")
        self._ai_analyze("inspect", [llm.envelope(f"inspect-{section}", text)], "")

    def _on_ai_run(self, *_):
        ok, reason = llm.available()
        if not ok:
            self._toast(reason.splitlines()[0])
            return
        kind = self.ai_kinds[self.ai_drop.get_selected()]
        question = self.ai_entry.get_text().strip()
        if kind == "ask" and not question:
            self._toast("Type a question first")
            return

        def gather():
            r = self.remote
            if kind == "logs":
                return [llm.status_envelope(r.ctl),
                        llm.envelope("latest.log", logs.tail(r.t, r.cfg, r.cfg.llm.log_lines)),
                        llm.metrics_envelope()]
            if kind == "crash":
                name, content = logs.crash_get(r.t, r.cfg)
                if not name:
                    raise llm.LlmError("no crash reports on the server — nothing to analyze")
                return [llm.envelope(f"crash-report {name}", content),
                        llm.envelope("latest.log tail", logs.tail(r.t, r.cfg, 120))]
            if kind == "mods":
                return [llm.envelope("mod-list", mods.render_text(mods.list_mods(r.t, r.cfg))),
                        llm.envelope("latest.log tail", logs.tail(r.t, r.cfg, 150))]
            return [llm.status_envelope(r.ctl),
                    llm.envelope("latest.log tail", logs.tail(r.t, r.cfg, 150))]

        self._ai_analyze_with(gather, kind, question)

    def _ai_analyze(self, kind: str, parts: list[str], question: str) -> None:
        self._ai_analyze_with(lambda: parts, kind, question)

    def _ai_analyze_with(self, gather, kind: str, question: str) -> None:
        if self._ai_running:
            self._toast("An analysis is already running")
            return
        self._ai_running = True
        self.ai_run.set_sensitive(False)
        self.ai_view.get_buffer().set_text(f"Gathering context, asking {self.remote.cfg.llm.model}…\n\n")
        first_chunk = [True]

        def stream_chunk(s: str):
            def apply():
                if first_chunk[0]:
                    first_chunk[0] = False
                    self.ai_view.get_buffer().set_text("")
                self._append_text_raw(self.ai_view, s)
                return False
            GLib.idle_add(apply)

        def job():
            parts = gather()
            return llm.Analyst(self.remote.cfg).analyze(
                kind, parts, question=question, on_text=stream_chunk)

        def done(_text):
            self._ai_running = False
            self.ai_run.set_sensitive(llm.available()[0])

        def err(e: Exception):
            self._ai_running = False
            self.ai_run.set_sensitive(llm.available()[0])
            self.ai_view.get_buffer().set_text(f"Analysis failed:\n{e}")

        self.worker.submit(job, done, err)

    def _append_text_raw(self, view: Gtk.TextView, text: str) -> None:
        buf = view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self._scroll_to_end(view)

    def _swap_rows(self, group: Adw.PreferencesGroup, old: list, new: list) -> list:
        for r in old:
            group.remove(r)
        for r in new:
            group.add(r)
        return new

    # ------------------------------------------------------------- render

    def _apply_status(self, st: Status) -> None:
        self.status = st
        if st.errors:
            kind, text = "error", "UNREACHABLE"
            self.banner.set_title(f"Server unreachable — {st.errors[0][:140]}")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)
            if st.running and st.port_open:
                kind, text = "success", "ONLINE"
            elif st.running:
                kind, text = "warning", "BOOTING"
            else:
                kind, text = "error", "OFFLINE"
        for c in ("success", "warning", "error"):
            self.badge.remove_css_class(c)
        self.badge.add_css_class(kind)
        self.badge.set_label(text)

        s = self.remote.cfg.server
        target = "this machine (local transport)" if s.transport == "local" else f"{s.user}@{s.host}"
        self.badge_sub.set_label(f"{target} · {s.server_dir}")

        self.row_process.set_label(
            f"pid {st.pid} · up {util.human_duration(st.uptime_s)}"
            + (" · dead tmux pane!" if st.pane_dead else "")
            if st.running else "not running")
        if st.players:
            names = ", ".join(st.players.names) if st.players.names else "nobody"
            self.row_players.set_label(f"{st.players.count}/{st.players.max} — {names}")
        else:
            self.row_players.set_label("—")

        tps = ((st.tps or {}).get("tps") or {})
        tps_now = tps.get("10s") or tps.get("5s") or tps.get("1m")
        mspt = ((st.tps or {}).get("mspt") or {}).get("median")
        self.tps_bar.set_value(min(20.0, tps_now or 0.0))
        self.row_tps.set_label(f"{tps_now:.1f}" + (f" · {mspt:.1f} ms" if mspt else "")
                               if tps_now else "—")

        self._gauge(self.heap_bar, self.row_heap, st.heap_used, st.heap_max or st.heap_committed)
        self.row_channel.set_label(st.channel or "—")
        self._gauge(self.ram_bar, self.row_ram, st.host_mem_used, st.host_mem_total)
        self.row_load.set_label(" ".join(f"{x:.2f}" for x in st.load) if st.load else "—")
        self.row_disk.set_label(util.human_bytes(st.disk_free) if st.disk_free is not None else "—")
        self.row_log.set_label(f"last write {util.human_duration(st.log_age_s)} ago"
                               if st.log_age_s is not None else "—")
        self.row_backup.set_label(
            f"{st.last_backup} · {util.human_duration(st.last_backup_age_s)} ago"
            if st.last_backup else "none yet")

        self._syncing_armed = True
        self.armed_row.set_active(st.armed)
        self._syncing_armed = False
        self.row_wd.set_label(f"desired={st.desired}"
                              + (" · HALTED (crash loop breaker)" if st.halted else ""))
        self._update_buttons()

    def _gauge(self, bar: Gtk.LevelBar, label: Gtk.Label,
               used: int | None, total: int | None) -> None:
        if used and total:
            bar.set_value(max(0.0, min(1.0, used / total)))
            label.set_label(f"{util.human_bytes(used)} / {util.human_bytes(total)}")
        else:
            bar.set_value(0)
            label.set_label("—")

    def _update_buttons(self) -> None:
        st = self.status
        idle, reachable = not self._busy, not st.errors
        self.btn_start.set_sensitive(idle and reachable and not st.running)
        for b in (self.btn_stop, self.btn_restart, self.btn_save, self.btn_purge):
            b.set_sensitive(idle and reachable and st.running)
        self.btn_backup.set_sensitive(idle and reachable)

    # ------------------------------------------------------------- actions

    def _set_busy(self, msg: str) -> None:
        self._busy = msg
        self.spinner.set_spinning(bool(msg))
        self.spinner.set_tooltip_text(msg or None)
        self._update_buttons()

    def _run_action(self, busy: str, job, *, refresh_aux: bool = False) -> None:
        """Run job(remote) on the worker; its return string becomes a toast."""
        if self._busy:
            self._toast(f"Busy: {self._busy}")
            return
        self._set_busy(busy)
        self._toast(busy, timeout=2)

        def done(msg):
            self._set_busy("")
            if msg:
                self._toast(str(msg), timeout=6)
            self._refresh(full=True)
            if refresh_aux:
                self._refresh_aux()

        def err(e: Exception):
            self._set_busy("")
            self._toast(f"Error: {e}", timeout=8)
            self._refresh(full=False)

        self.worker.submit(lambda: job(self.remote), done, err)

    def _act_start(self, *_):
        def job(r: Remote) -> str:
            with util.OpsLock():
                r.ctl.start(wait=True, progress=lambda line: GLib.idle_add(
                    self.badge_sub.set_label, f"boot: {line}"))
            return "Server is up"
        self._run_action("Starting server — a modded boot can take minutes…", job)

    def _act_stop(self, *_):
        n = self.status.players.count if self.status.players else 0
        body = (f"{n} player(s) are online — they get a countdown, the world is flushed, "
                "then the server stops." if n
                else "The world is flushed to disk, then the server stops.")
        body += " The watchdog stands down (desired=down)."

        def job(r: Remote) -> str:
            with util.OpsLock():
                r.ctl.stop()
            return "Server stopped"
        self._confirm("Stop the server?", body, "Stop",
                      lambda: self._run_action("Stopping server…", job))

    def _act_restart(self, *_):
        def job(r: Remote) -> str:
            with util.OpsLock():
                r.ctl.restart(progress=lambda line: GLib.idle_add(
                    self.badge_sub.set_label, f"boot: {line}"))
            return "Server restarted"
        self._confirm("Restart the server?",
                      "Graceful stop (countdown + save) followed by a fresh boot.",
                      "Restart", lambda: self._run_action("Restarting server…", job))

    def _act_save(self, *_):
        def job(r: Remote) -> str:
            if r.ctl.find_pid() is None:
                return "Server is not running — nothing to save"
            offset = r.console.log_size()
            r.console.send("save-all flush", timeout=15)
            hit = r.console.wait_in_log(r"Saved the game", offset, timeout=60)
            return "World saved" if hit else "save-all sent (no confirmation seen in 60s)"
        self._run_action("Saving world…", job)

    def _act_backup(self, *_):
        full = self.backup_full_check.get_active()

        def job(r: Remote) -> str:
            with util.OpsLock():
                entry = r.backups.create(full=full)
                _kept, dropped = r.backups.prune()
            assert entry is not None
            msg = f"Backup created: {entry.name} ({util.human_bytes(entry.size)})"
            if dropped:
                msg += f" · rotated out {len(dropped)}"
            return msg
        self._run_action("Snapshotting world…", job, refresh_aux=True)

    def _act_purge(self, *_):
        def job(r: Remote) -> str:
            pid = r.ctl.find_pid()
            if pid is None:
                return "Server is not running"
            rep = metrics.purge(r.t, r.cfg, pid)
            return (f"Purge: freed {util.human_bytes(rep.freed)} ({rep.freed_pct:.0f}%) — "
                    f"{rep.verdict}")
        self._run_action("Running GC purge…", job)

    def _on_armed_toggled(self, row: Adw.SwitchRow, _param) -> None:
        if self._syncing_armed:
            return
        armed = row.get_active()
        state.set_armed(armed)  # local state file — instant, no worker needed
        self._toast("Watchdog armed — it heals crashes while desired=up" if armed
                    else "Watchdog disarmed")

    def _on_console_send(self, *_):
        cmd = self.cmd_entry.get_text().strip()
        if not cmd:
            return
        self.cmd_entry.set_text("")
        self._append_text(self.console_view, f"> {cmd}")
        self.worker.submit(
            lambda: self.remote.console.send(cmd),
            lambda out: self._append_text(self.console_view, out.strip() or "(no output)"),
            lambda e: self._append_text(self.console_view, f"error: {e}"))

    def _on_wl_add(self, row: Adw.EntryRow) -> None:
        name = row.get_text().strip()
        if not name:
            return
        row.set_text("")
        self._act_player("whitelist_add", name)

    def _player_row(self, name: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=name)
        for label, verb in (("Op", "op"), ("Kick", "kick")):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_, v=verb, n=name: self._act_player(v, n))
            row.add_suffix(b)
        return row

    def _wl_row(self, name: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=name)
        b = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
        b.add_css_class("flat")
        b.connect("clicked", lambda *_, n=name: self._act_player("whitelist_remove", n))
        row.add_suffix(b)
        return row

    def _backup_row(self, entry) -> Adw.ActionRow:
        row = Adw.ActionRow(
            title=entry.name,
            subtitle=f"{util.human_bytes(entry.size)} · {util.human_duration(entry.age_s)} ago · "
                     + ("full instance" if entry.full else "world"))
        b = Gtk.Button(label="Verify", valign=Gtk.Align.CENTER,
                       tooltip_text="Integrity-test the archive on the server")
        b.add_css_class("flat")
        b.connect("clicked", lambda *_, n=entry.name: self._act_verify(n))
        row.add_suffix(b)
        return row

    def _act_player(self, verb: str, name: str) -> None:
        def job(r: Remote) -> str:
            out = getattr(r.players, verb)(name).strip()
            first = out.splitlines()[0] if out else "done"
            return f"{verb.replace('_', ' ')} {name}: {first[:120]}"
        self._run_action(f"{verb.replace('_', ' ')} {name}…", job, refresh_aux=True)

    def _act_verify(self, name: str) -> None:
        def job(r: Remote) -> str:
            ok = r.backups.verify(name)
            return f"{name}: archive OK" if ok else f"{name}: INTEGRITY CHECK FAILED"
        self._run_action(f"Verifying {name}…", job)


class McctlApp(Adw.Application):
    def __init__(self, cfg: Config):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.cfg = cfg
        self.remote = Remote(cfg)
        self.worker = Worker()
        for name, cb, accels in (("about", self._on_about, None),
                                 ("quit", lambda *_: self.quit(), ["<primary>q"])):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("win.refresh", ["<primary>r", "F5"])
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)

    def _on_activate(self, *_):
        win = self.props.active_window
        if not win:
            self._load_css()
            win = Window(self)
        win.present()

    def _on_shutdown(self, *_):
        with contextlib.suppress(Exception):
            self.remote.close()

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(_CSS)
        else:  # GTK < 4.12
            provider.load_from_data(_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _on_about(self, *_):
        about = Adw.AboutDialog(
            application_name="mcctl",
            application_icon=APP_ID,
            version=__version__,
            developer_name="CarborioLand",
            license_type=Gtk.License.MIT_X11,
            comments="Remote control & monitoring for a modded Minecraft server over SSH.",
            website="https://github.com/lonaivdev-cell/minecraft-remote-monitoring",
            issue_url="https://github.com/lonaivdev-cell/minecraft-remote-monitoring/issues")
        about.present(self.props.active_window)


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mcctl-gui",
                                 description="GTK4/libadwaita desktop app for mcctl.")
    ap.add_argument("--config", help="config file (default: ~/.config/mcctl/config.toml)")
    args = ap.parse_args(argv)
    util.setup_logging(0)
    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    return McctlApp(cfg).run(None)
