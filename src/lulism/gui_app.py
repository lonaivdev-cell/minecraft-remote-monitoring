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
import shlex
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, crafting, inspector, llm, logs, metrics, modconfig, mods, state, util  # noqa: E402
from . import props as propsmod  # noqa: E402
from .backup import BackupManager  # noqa: E402
from .config import Config, ConfigError  # noqa: E402
from .console import Console, ConsoleError  # noqa: E402
from .doctor import Level, run_doctor  # noqa: E402
from .players import PlayerError, Players  # noqa: E402
from .server import ServerControl, Status  # noqa: E402
from .transport import BaseTransport, TransportError, make_transport  # noqa: E402

log = util.get_logger("gui")

APP_ID = "io.github.lonaivdev_cell.mcctl"
FAST_TICK = 5    # seconds: cheap status probe (+ log tail when visible)
SLOW_TICK = 20   # seconds: full status (spark TPS, players, heap)
LOG_LINES = 200          # initial backlog shown when the live log view opens
LOG_MAX_LINES = 5000     # ring-buffer cap so a long live tail can't grow forever

# History page cards: (key, label, fixed upper bound or None = auto, value format)
HISTORY_SPECS = (
    ("tps",     "TPS",      20.0,  "{:.1f}"),
    ("mspt",    "MSPT",     None,  "{:.0f} ms"),
    ("heap",    "Heap",     100.0, "{:.0f}%"),
    ("players", "Players",  None,  "{:.0f}"),
    ("mem",     "Host RAM", 100.0, "{:.0f}%"),
    ("load",    "Load 1m",  None,  "{:.2f}"),
)
HISTORY_COLORS = {
    "tps": (0.22, 0.74, 0.45), "mspt": (0.93, 0.61, 0.22),
    "heap": (0.33, 0.60, 0.95), "players": (0.66, 0.46, 0.92),
    "mem": (0.24, 0.72, 0.72), "load": (0.91, 0.45, 0.45),
}
HISTORY_MIN_CARD = 200   # px floor for a square chart card on narrow windows

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
        self.set_default_size(1120, 760)
        self.remote: Remote = app.remote
        self.worker: Worker = app.worker
        self.status = Status()
        self._busy = ""
        self._refreshing = False
        # live log follower: tmux-like `tail -F` on its own transport + thread
        self._log_following = False
        self._log_thread: threading.Thread | None = None
        self._log_stop: threading.Event | None = None
        self._log_queue: queue.Queue = queue.Queue()
        self._log_drain_id = 0
        self._log_epoch = 0
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
        self._cfg_files: list = []                     # mod-config browser state
        self._cfg_groups: list[tuple[str, list]] = []  # (label, [ConfigFile]) ordered
        self._cfg_file_rows: list[Gtk.Widget] = []
        self._cfg_loaded = False
        self._cfg_loading = False
        self._recipe_rows: list[Gtk.Widget] = []       # crafting page state
        self._recipes: list = []
        self._recipes_searching = False
        self._inspect_refreshing = False
        self._ai_running = False
        self._doctor_rows: list[Gtk.Widget] = []
        self._doctor_running = False
        self._props_rows: dict[str, Gtk.Widget] = {}
        self._props_other_rows: list[Gtk.Widget] = []
        self._props_loading = False
        self._props_pf = None
        self._jvm_loaded = False
        self._jvm_loading = False
        self._crash_rows: list[Gtk.Widget] = []
        self._evidence_rows: list[Gtk.Widget] = []
        self._crashes_refreshing = False
        self._profiler_running = False
        self._chat_messages: list[dict] = []
        self._chat_running = False
        self._history_series: dict[str, list[float | None]] = {}
        self._history_times: list[int | None] = []
        self._history_meta: dict[str, dict] = {}
        self._history_areas: dict[str, Gtk.DrawingArea] = {}
        self._history_refreshing = False
        self._booting = False                         # show "BOOTING SERVER" while a start runs
        self._settings_fields: list[tuple] = []      # (section, key, kind, widget)
        self._settings_saving = False
        self._ollama_models: list[str] = []
        self._ollama_up: bool | None = None           # live ollama detection (None = unprobed)
        self._adopted = False                         # one-shot "connect to running server"

        self._build_ui()
        self._update_llm_widgets()  # initial AI/Chat availability + provider labels
        self.connect("close-request", self._on_close_request)

        act = Gio.SimpleAction.new("refresh", None)
        act.connect("activate", lambda *_: (self._refresh(full=True), self._refresh_aux()))
        self.add_action(act)

        GLib.timeout_add_seconds(FAST_TICK, self._on_tick)
        self._refresh(full=True)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        # 19 pages: a sidebar scales where a view switcher can't
        self.stack = Gtk.Stack(vexpand=True, hexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        for builder, name, title in (
            (self._build_overview, "overview", "Overview"),
            (self._build_history, "history", "History"),
            (self._build_console, "console", "Console"),
            (self._build_logs, "logs", "Logs"),
            (self._build_players, "players", "Players"),
            (self._build_backups, "backups", "Backups"),
            (self._build_mods, "mods", "Mods"),
            (self._build_mod_configs, "modconfig", "Mod Configs"),
            (self._build_craft, "craft", "Crafting"),
            (self._build_inspect, "inspect", "Inspect"),
            (self._build_ai, "ai", "AI"),
            (self._build_chat, "chat", "Chat"),
            (self._build_doctor, "doctor", "Doctor"),
            (self._build_props, "props", "Properties"),
            (self._build_jvm, "jvm", "JVM"),
            (self._build_crashes, "crashes", "Crashes"),
            (self._build_profiler, "profiler", "Profiler"),
            (self._build_sync, "sync", "Sync"),
            (self._build_settings, "settings", "Settings"),
        ):
            self.stack.add_titled(builder(), name, title)
        self.stack.connect("notify::visible-child-name", lambda *_: self._refresh_aux())

        header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title="mcctl"))
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

        sidebar = Gtk.StackSidebar(stack=self.stack)
        sidebar.set_size_request(150, -1)
        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        split.append(sidebar)
        split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        split.append(self.stack)

        self.banner = Adw.Banner()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        box.append(split)
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

        # FlowBox so the action pills reflow onto another row on narrow windows
        # instead of overflowing the clamp and clipping their labels.
        actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                              column_spacing=8, row_spacing=8, halign=Gtk.Align.CENTER,
                              min_children_per_line=2, max_children_per_line=6)
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
        self.row_heals = self._kv(g, "Self-heals")
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
        title = Gtk.Label(
            label=f"{self.remote.cfg.server.log_file} — live (tail -F). New lines stream in; "
                  "scroll up to read back, scroll to the bottom to resume following.",
            halign=Gtk.Align.START, wrap=True)
        title.add_css_class("dim-label")
        title.add_css_class("caption")
        box.append(title)
        self.log_view = self._textview()
        self.log_scroller = Gtk.ScrolledWindow(child=self.log_view, vexpand=True)
        box.append(self.log_scroller)
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

    def _build_mod_configs(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)

        picker = Adw.PreferencesGroup(
            title="Mod configs",
            description="Edit files under config/. Saved changes live-reload where the "
                        "mod supports it; startup/cached values need a restart.")
        reload_btn = Gtk.Button(label="Reload list")
        reload_btn.connect("clicked", lambda *_: self._refresh_mod_configs(force=True))
        picker.set_header_suffix(reload_btn)
        self.cfg_group_drop = Gtk.DropDown(model=Gtk.StringList.new(["(loading…)"]))
        self.cfg_group_drop.connect("notify::selected", lambda *_: self._cfg_render())
        drop_row = Adw.ActionRow(title="Mod")
        drop_row.add_suffix(self.cfg_group_drop)
        picker.add(drop_row)
        self.cfg_search = Gtk.SearchEntry(placeholder_text="Search every config file by name…",
                                          hexpand=True)
        self.cfg_search.connect("search-changed", lambda *_: self._cfg_render())
        search_row = Adw.ActionRow(title="Find")
        search_row.add_suffix(self.cfg_search)
        picker.add(search_row)
        box.append(picker)

        self.cfg_files_group = Adw.PreferencesGroup(title="Files")
        box.append(self.cfg_files_group)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=900, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_craft(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)

        search = Adw.PreferencesGroup(
            title="Crafting",
            description="Pick a recipe and have it crafted for you. mcctl can't reach your "
                        "in-game crafting grid, so it reproduces the result over the console: it "
                        "reads your inventory, consumes the inputs and gives you the output — only "
                        "ever from loose (accessible) inventory, so it can't dupe. Works at a "
                        "crafting table or a Backpacked crafting backpack.")
        self.craft_search = Gtk.SearchEntry(
            placeholder_text="Search recipes by id or output, e.g. chest, iron, torch…",
            hexpand=True)
        self.craft_search.connect("activate", lambda *_: self._refresh_recipes())
        srow = Adw.ActionRow(title="Find a recipe")
        srow.add_suffix(self.craft_search)
        go = Gtk.Button(label="Search", valign=Gtk.Align.CENTER)
        go.add_css_class("suggested-action")
        go.connect("clicked", lambda *_: self._refresh_recipes())
        srow.add_suffix(go)
        search.add(srow)
        box.append(search)

        self.recipes_group = Adw.PreferencesGroup(
            title="Recipes", description="Search to list the pack's shaped/shapeless recipes.")
        box.append(self.recipes_group)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=900, child=box),
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
        # always present (toggled live) so changing [llm] in Settings updates the page
        self.ai_note = Gtk.Label(halign=Gtk.Align.START, selectable=True, wrap=True, visible=False)
        self.ai_note.add_css_class("dim-label")
        self.ai_note.add_css_class("warning")
        box.append(self.ai_note)
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
        row.append(self.ai_run)
        box.append(row)
        self.ai_hint = Gtk.Label(halign=Gtk.Align.START)
        self.ai_hint.add_css_class("dim-label")
        self.ai_hint.add_css_class("caption")
        box.append(self.ai_hint)
        self.ai_view = self._textview()
        self.ai_view.set_monospace(False)
        self.ai_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        box.append(Gtk.ScrolledWindow(child=self.ai_view, vexpand=True))
        return box

    def _build_chat(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.chat_note = Gtk.Label(halign=Gtk.Align.START, selectable=True, wrap=True, visible=False)
        self.chat_note.add_css_class("dim-label")
        self.chat_note.add_css_class("warning")
        box.append(self.chat_note)
        self.chat_view = self._textview()
        self.chat_view.set_monospace(False)
        self.chat_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        box.append(Gtk.ScrolledWindow(child=self.chat_view, vexpand=True))
        row = Gtk.Box(spacing=6)
        self.chat_entry = Gtk.Entry(
            placeholder_text="Ask anything — the live server status & log are attached",
            hexpand=True)
        self.chat_entry.connect("activate", self._on_chat_send)
        self.chat_send = Gtk.Button(label="Send")
        self.chat_send.add_css_class("suggested-action")
        self.chat_send.connect("clicked", self._on_chat_send)
        new_btn = Gtk.Button(label="New", tooltip_text="Start a fresh conversation")
        new_btn.connect("clicked", self._on_chat_new)
        row.append(self.chat_entry)
        row.append(self.chat_send)
        row.append(new_btn)
        box.append(row)
        self.chat_hint = Gtk.Label(halign=Gtk.Align.START)
        self.chat_hint.add_css_class("dim-label")
        self.chat_hint.add_css_class("caption")
        box.append(self.chat_hint)
        return box

    def _build_history(self) -> Gtk.Widget:
        self.history_keys = [k for k, *_ in HISTORY_SPECS]
        self._history_fixed_hi = {k: hi for k, _lbl, hi, _fmt in HISTORY_SPECS if hi}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        bar = Gtk.Box(spacing=8)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload history")
        refresh.connect("clicked", lambda *_: self._refresh_history())
        bar.append(refresh)
        self.history_status = Gtk.Label(
            label="Recorded by the watchdog, `mcctl watch`, this app, and `mcctl dash`.",
            halign=Gtk.Align.START, hexpand=True, xalign=0.0, ellipsize=3)  # 3 = END
        self.history_status.add_css_class("dim-label")
        self.history_status.add_css_class("caption")
        bar.append(self.history_status)
        box.append(bar)

        # Two columns, always. Each card is a square whose size follows the window
        # width; when the rows overflow, the ScrolledWindow shows a vertical bar.
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        for i, (key, label, _hi, _fmt) in enumerate(HISTORY_SPECS):
            grid.attach(self._history_card(key, label), i % 2, i // 2, 1, 1)
        box.append(Gtk.ScrolledWindow(child=grid, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vscrollbar_policy=Gtk.PolicyType.AUTOMATIC))
        return box

    def _history_card(self, key: str, label: str) -> Gtk.Widget:
        area = Gtk.DrawingArea(hexpand=True, vexpand=False)
        area.set_content_height(HISTORY_MIN_CARD)
        area.set_draw_func(self._draw_metric, key)
        area.connect("resize", self._on_history_resize)
        self._history_areas[key] = area
        frame = Gtk.Frame()
        frame.add_css_class("card")
        frame.set_child(area)
        return frame

    def _on_history_resize(self, area: Gtk.DrawingArea, width: int, _height: int) -> None:
        # Keep each card square: height tracks width (with a floor so a narrow
        # window stays legible). Column width is set by the grid, not the height,
        # so driving height from width can't feed back into a resize loop.
        target = max(HISTORY_MIN_CARD, width)
        if area.get_content_height() != target:
            area.set_content_height(target)

    def _build_doctor(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        self.doctor_group = Adw.PreferencesGroup(
            title="Preflight checks",
            description="Verifies SSH → layout → JVM → props end-to-end")
        suffix = Gtk.Box(spacing=8)
        run = Gtk.Button(label="Run checks")
        run.connect("clicked", lambda *_: self._refresh_doctor())
        suffix.append(run)
        fix = Gtk.Button(label="Apply safe fixes")
        fix.add_css_class("suggested-action")
        fix.connect("clicked", lambda *_: self._confirm(
            "Apply safe fixes?",
            "Sets SKIP_JAVA_CHECK / WAIT_FOR_USER_INPUT / FORCE_FETCH in variables.txt, "
            "creates the backup dir, and enables RCON with a generated password "
            "(timestamped .bak files are kept on the server).",
            "Apply", lambda: self._refresh_doctor(fix=True)))
        suffix.append(fix)
        self.doctor_group.set_header_suffix(suffix)
        box.append(self.doctor_group)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=860, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_props(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        self.props_group = Adw.PreferencesGroup(
            title="server.properties",
            description="Validated editor — nothing is written until you review the diff")
        suffix = Gtk.Box(spacing=8)
        reload_btn = Gtk.Button(label="Reload")
        reload_btn.connect("clicked", lambda *_: self._refresh_props())
        suffix.append(reload_btn)
        save = Gtk.Button(label="Review && save…")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_props_save)
        suffix.append(save)
        self.props_group.set_header_suffix(suffix)
        box.append(self.props_group)
        self.props_other_group = Adw.PreferencesGroup(
            title="Other keys on the server", description="Not in the validated set — edit via CLI")
        box.append(self.props_other_group)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=860, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_jvm(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        g = Adw.PreferencesGroup(title="JVM launch settings",
                                 description="variables.txt — changes apply on next restart, "
                                             ".bak kept on the server")
        self.jvm_heap_row = Adw.EntryRow(title="Heap (Xms = Xmx, e.g. 12G)",
                                         show_apply_button=True)
        self.jvm_heap_row.connect("apply", self._on_jvm_heap)
        g.add(self.jvm_heap_row)
        self.jvm_java_row = Adw.EntryRow(title="JAVA binary path on the server",
                                         show_apply_button=True)
        self.jvm_java_row.connect("apply", self._on_jvm_java)
        g.add(self.jvm_java_row)
        box.append(g)
        self.jvm_info_group = Adw.PreferencesGroup(title="Effective configuration")
        box.append(self.jvm_info_group)
        self._jvm_info_rows: list[Gtk.Widget] = []
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=860, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_crashes(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.crash_group = Adw.PreferencesGroup(title="Crash reports on the server")
        box.append(self.crash_group)
        self.evidence_group = Adw.PreferencesGroup(
            title="Local evidence bundles",
            description="Saved by the watchdog before every heal (pane, log tail, crash report)")
        box.append(self.evidence_group)
        self.crash_view = self._textview()
        sw = Gtk.ScrolledWindow(child=self.crash_view, vexpand=True)
        sw.set_min_content_height(260)
        box.append(sw)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=980, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_profiler(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        g = Adw.PreferencesGroup(
            title="spark async profiler",
            description="Samples the running server and uploads to spark.lucko.me")
        self.prof_seconds = Adw.SpinRow(
            title="Duration (seconds)",
            adjustment=Gtk.Adjustment(value=60, lower=10, upper=600, step_increment=10))
        g.add(self.prof_seconds)
        run = Gtk.Button(label="Start profiling", halign=Gtk.Align.START)
        run.add_css_class("suggested-action")
        run.connect("clicked", self._on_profile)
        self.prof_run_btn = run
        row = Adw.ActionRow(title="Run")
        row.add_suffix(run)
        g.add(row)
        box.append(g)
        self.prof_results = Adw.PreferencesGroup(title="Results (this session)")
        box.append(self.prof_results)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=860, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_sync(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        g = Adw.PreferencesGroup(
            title="config/ directory sync",
            description="Fixes Better Compatibility Checker version mismatches (rsync)")
        self.sync_dir_row = Adw.EntryRow(title="Local directory")
        g.add(self.sync_dir_row)
        pull_row = Adw.ActionRow(title="Pull", subtitle="server config/ → local directory")
        pull = Gtk.Button(label="Pull", valign=Gtk.Align.CENTER)
        pull.connect("clicked", lambda *_: self._on_sync(push=False))
        pull_row.add_suffix(pull)
        g.add(pull_row)
        push_row = Adw.ActionRow(title="Push",
                                 subtitle="local directory → server config/ (overwrites!)")
        push = Gtk.Button(label="Push", valign=Gtk.Align.CENTER)
        push.add_css_class("destructive-action")
        push.connect("clicked", lambda *_: self._confirm(
            "Push local config/ over the server's?",
            "The server's config/ files are overwritten by your local copy.",
            "Push", lambda: self._on_sync(push=True)))
        push_row.add_suffix(push)
        g.add(push_row)
        box.append(g)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=860, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    # ------------------------------------------------------------ settings

    def _build_settings(self) -> Gtk.Widget:
        """A full editor for ~/.config/mcctl/config.toml — every section, so you
        never have to hand-edit the file. Connection changes reconnect in place."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        intro = Gtk.Label(
            label="Edit every mcctl setting here — saved straight to your config.toml.",
            halign=Gtk.Align.START, wrap=True)
        intro.add_css_class("dim-label")
        box.append(intro)

        g = Adw.PreferencesGroup(
            title="SSH connection",
            description="How mcctl reaches the server. Saving these reconnects in place.")
        self._add_field(g, "server", "host", "text", "Host / IP")
        self._add_field(g, "server", "ssh_port", "int", "SSH port")
        self._add_field(g, "server", "user", "text", "User")
        self._add_field(g, "server", "ssh_key", "path", "SSH private key",
                        "Path passed to ssh -i; empty = ssh-agent / default key")
        self._add_field(g, "server", "ssh_options", "args", "Extra SSH flags",
                        "Raw ssh args, e.g.  -o IdentityFile=~/.ssh/carborio")
        self._add_field(g, "server", "transport", "choice:ssh|local", "Transport",
                        '"local" runs everything against this machine (dev/testing)')
        box.append(g)

        g = Adw.PreferencesGroup(title="Server layout", description="Remote paths and ports.")
        self._add_field(g, "server", "server_dir", "text", "Server directory (absolute)")
        self._add_field(g, "server", "tmux_session", "text", "tmux session name")
        self._add_field(g, "server", "start_command", "text", "Start command")
        self._add_field(g, "server", "log_file", "text", "Log file (relative to dir)")
        self._add_field(g, "server", "world_dir", "text", "World dir (relative to dir)")
        self._add_field(g, "server", "mc_port", "int", "Minecraft port")
        self._add_field(g, "server", "rcon_port", "int", "RCON port")
        self._add_field(g, "server", "java_home", "text", "JAVA_HOME on the server")
        self._add_field(g, "server", "start_timeout", "int", "Start timeout (s)")
        self._add_field(g, "server", "stop_timeout", "int", "Stop timeout (s)")
        self._add_field(g, "server", "stop_countdown", "ints", "Stop countdown (s)",
                        "Space-separated seconds, e.g.  30 10 5")
        box.append(g)

        g = Adw.PreferencesGroup(title="Backups")
        self._add_field(g, "backup", "remote_dir", "text", "Remote backup dir (absolute)")
        self._add_field(g, "backup", "prefix", "text", "Archive name prefix")
        self._add_field(g, "backup", "compression", "choice:zstd|gzip", "Compression")
        self._add_field(g, "backup", "keep_recent", "int", "Keep newest N")
        self._add_field(g, "backup", "keep_daily", "int", "Keep one/day for D days")
        self._add_field(g, "backup", "keep_weekly", "int", "Keep one/week for W weeks")
        self._add_field(g, "backup", "min_free_gb", "float", "Refuse below free GB")
        self._add_field(g, "backup", "local_dir", "dir", "Local mirror dir (backup pull)")
        self._add_field(g, "backup", "full_excludes", "args", "Excludes for --full")
        box.append(g)

        g = Adw.PreferencesGroup(title="Watchdog and alerts")
        self._add_field(g, "watchdog", "interval", "int", "Check interval (s)")
        self._add_field(g, "watchdog", "freeze_log_age", "int", "Freeze: log age (s)")
        self._add_field(g, "watchdog", "max_restarts", "int", "Max restarts / window")
        self._add_field(g, "watchdog", "restart_window", "int", "Restart window (s)")
        self._add_field(g, "watchdog", "backoff_base", "int", "Backoff base (s)")
        self._add_field(g, "watchdog", "tps_alert", "float", "Alert when TPS below")
        self._add_field(g, "watchdog", "heap_alert_pct", "int", "Alert when heap % above")
        self._add_field(g, "watchdog", "autosave_minutes", "int", "Built-in autosave (min, 0=off)")
        self._add_field(g, "watchdog", "auto_profile_on_lag", "bool", "Auto-profile on low TPS")
        self._add_field(g, "watchdog", "notify_desktop", "bool", "Desktop notifications")
        self._add_field(g, "watchdog", "webhook_url", "text", "Discord-style webhook URL")
        self._add_field(g, "watchdog", "ntfy_url", "text", "ntfy server URL")
        self._add_field(g, "watchdog", "ntfy_topic", "text", "ntfy topic (phone push)")
        self._add_field(g, "watchdog", "ntfy_token", "text", "ntfy bearer token")
        box.append(g)

        g = Adw.PreferencesGroup(
            title="AI / LLM",
            description="provider = anthropic (Claude API) or ollama (local, no key, nothing leaves the box).")
        self._add_field(g, "llm", "provider", "choice:anthropic|ollama", "Provider")
        self._add_field(g, "llm", "model", "text", "Anthropic model id")
        self._add_field(g, "llm", "api_key_env", "text", "API-key env var")
        self._add_field(g, "llm", "max_tokens", "int", "Max output tokens")
        self._add_field(g, "llm", "log_lines", "int", "Log lines sent as context")
        self._add_field(g, "llm", "ollama_url", "text", "ollama server URL")
        ollama_row = self._add_field(g, "llm", "ollama_model", "text", "ollama model")
        choose = Gtk.Button(icon_name="view-list-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Choose from the models ollama has pulled (`ollama list`)")
        choose.add_css_class("flat")
        choose.connect("clicked", self._on_ollama_choose)
        ollama_row.add_suffix(choose)
        box.append(g)

        g = Adw.PreferencesGroup(
            title="Crafting",
            description="Recipe browser + survival command-craft (the Crafting page).")
        self._add_field(g, "crafting", "player", "text", "Your in-game name",
                        "Default receiver of crafted output, e.g. GLEYSSON")
        self._add_field(g, "crafting", "source_player", "text", "Source player (optional)",
                        "Whose inventory supplies materials; empty = same as your name")
        self._add_field(g, "crafting", "max_output_stack", "int", "Craft-max stack cap (1-64)")
        self._add_field(g, "crafting", "include_containers", "bool",
                        "Show backpack/container counts when planning")
        box.append(g)

        g = Adw.PreferencesGroup(title="Metrics and display")
        self._add_field(g, "metrics", "prom_path", "text", "Prometheus .prom path (empty = state dir)")
        self._add_field(g, "ui", "timezone", "text", "Display timezone (IANA; empty = raw)")
        self._add_field(g, "ui", "server_timezone", "text", "Server timezone (IANA)")
        box.append(g)

        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        revert = Gtk.Button(label="Revert")
        revert.connect("clicked", lambda *_: (self._sync_settings(),
                                              self._toast("Reverted to saved values", timeout=2)))
        bar.append(revert)
        save_btn = Gtk.Button(label="Save to config.toml")
        save_btn.add_css_class("suggested-action")
        save_btn.add_css_class("pill")
        save_btn.connect("clicked", self._on_settings_save)
        bar.append(save_btn)
        box.append(bar)
        self.settings_path_label = Gtk.Label(halign=Gtk.Align.END, selectable=True)
        self.settings_path_label.add_css_class("dim-label")
        self.settings_path_label.add_css_class("caption")
        box.append(self.settings_path_label)

        self._sync_settings()
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=820, child=box),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _add_field(self, group: Adw.PreferencesGroup, section: str, key: str, kind: str,
                   title: str, subtitle: str = "") -> Gtk.Widget:
        if kind == "bool":
            widget = Adw.SwitchRow(title=title, subtitle=subtitle)
        elif kind.startswith("choice:"):
            opts = kind.split(":", 1)[1].split("|")
            widget = Adw.ComboRow(title=title, subtitle=subtitle,
                                  model=Gtk.StringList.new(opts))
        else:
            widget = Adw.EntryRow(title=title)
            if subtitle:
                widget.set_tooltip_text(subtitle)
        group.add(widget)
        self._settings_fields.append((section, key, kind, widget))
        if kind in ("path", "dir"):
            folder = kind == "dir"
            btn = Gtk.Button(icon_name="folder-open-symbolic" if folder else "document-open-symbolic",
                             valign=Gtk.Align.CENTER, tooltip_text="Browse")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda *_, w=widget, f=folder: self._on_path_browse(w, f))
            widget.add_suffix(btn)
        return widget

    def _field_widget(self, section: str, key: str) -> Gtk.Widget | None:
        for sec, k, _kind, widget in self._settings_fields:
            if sec == section and k == key:
                return widget
        return None

    @staticmethod
    def _read_field(kind: str, widget: Gtk.Widget):
        """Widget -> typed config value; raises ValueError on bad numeric input."""
        if kind == "bool":
            return widget.get_active()
        if kind.startswith("choice:"):
            return kind.split(":", 1)[1].split("|")[widget.get_selected()]
        text = widget.get_text().strip()
        if kind == "int":
            return int(text)
        if kind == "float":
            return float(text)
        if kind == "args":
            return shlex.split(text)
        if kind == "ints":
            return [int(x) for x in text.replace(",", " ").split()]
        return text  # text / path / dir

    @staticmethod
    def _write_field(kind: str, widget: Gtk.Widget, value) -> None:
        """Config value -> widget."""
        if kind == "bool":
            widget.set_active(bool(value))
        elif kind.startswith("choice:"):
            opts = kind.split(":", 1)[1].split("|")
            widget.set_selected(opts.index(value) if value in opts else 0)
        elif kind == "args":
            widget.set_text(shlex.join(value) if value else "")
        elif kind == "ints":
            widget.set_text(" ".join(str(x) for x in (value or [])))
        else:
            widget.set_text("" if value is None else str(value))

    def _sync_settings(self) -> None:
        for section, key, kind, widget in self._settings_fields:
            dc = getattr(self.remote.cfg, section)
            self._write_field(kind, widget, getattr(dc, key))
        path = self.remote.cfg.path or Config.default_path()
        self.settings_path_label.set_label(f"→ {path}")

    def _on_settings_save(self, *_) -> None:
        if self._settings_saving:
            self._toast("Already reconnecting…")
            return
        cfg = self.remote.cfg
        s = cfg.server
        before = (s.host, s.user, s.ssh_port, s.ssh_key, tuple(s.ssh_options), s.transport)
        # read every widget first; bail without touching cfg if anything is malformed
        pending: dict[tuple, object] = {}
        for section, key, kind, widget in self._settings_fields:
            try:
                pending[(section, key)] = self._read_field(kind, widget)
            except ValueError:
                self._toast(f"{key}: not a valid number", timeout=6)
                return
        snapshot = {sk: getattr(getattr(cfg, sk[0]), sk[1]) for sk in pending}

        def rollback():
            for (sec, key), val in snapshot.items():
                setattr(getattr(cfg, sec), key, val)

        for (sec, key), val in pending.items():
            setattr(getattr(cfg, sec), key, val)
        try:
            saved = cfg.save()  # validates, then writes atomically
        except (ConfigError, OSError) as e:
            rollback()
            self._toast(f"Not saved — {e}", timeout=9)
            return
        self._toast(f"Saved to {saved}", timeout=4)
        self._update_llm_widgets()  # [llm] changes take effect on the AI/Chat pages now
        after = (s.host, s.user, s.ssh_port, s.ssh_key, tuple(s.ssh_options), s.transport)
        if after != before:
            self._reconnect()
        else:
            self._refresh(full=True)

    def _reconnect(self) -> None:
        """Tear down the SSH connection so the next probe uses the new settings."""
        self._settings_saving = True
        self._toast("Reconnecting with the new settings…", timeout=3)

        def job():
            self.remote.close()
            self.remote._t = None
            self.remote._console = None
            self.remote._ctl = None
            return None

        def finish(_=None):
            self._settings_saving = False
            self._adopted = False  # re-discover/adopt on the fresh connection
            self._update_llm_widgets()
            self._refresh(full=True)

        self.worker.submit(job, finish, lambda _e: finish())

    def _on_path_browse(self, widget: Adw.EntryRow, folder: bool) -> None:
        if not hasattr(Gtk, "FileDialog"):
            self._toast("File browser needs GTK 4.10+ — type the path manually", timeout=4)
            return
        dlg = Gtk.FileDialog(title="Select folder" if folder else "Select file")
        start = (Path.home() / ".ssh") if not folder else Path.home()
        if start.exists():
            dlg.set_initial_folder(Gio.File.new_for_path(str(start)))

        def chosen(d, result):
            try:
                f = d.select_folder_finish(result) if folder else d.open_finish(result)
                if f:
                    widget.set_text(f.get_path() or "")
            except GLib.Error:
                pass

        if folder:
            dlg.select_folder(self, None, chosen)
        else:
            dlg.open(self, None, chosen)

    def _on_ollama_choose(self, button: Gtk.Button) -> None:
        url_widget = self._field_widget("llm", "ollama_url")
        url = (url_widget.get_text().strip() if url_widget else "") or self.remote.cfg.llm.ollama_url
        self._toast("Querying ollama…", timeout=2)

        def job():
            saved = self.remote.cfg.llm.ollama_url
            self.remote.cfg.llm.ollama_url = url
            try:
                return llm.list_ollama_models(self.remote.cfg)
            finally:
                self.remote.cfg.llm.ollama_url = saved

        def done(names):
            if not names:
                self._toast("ollama has no models pulled — run `ollama pull <model>`", timeout=6)
                return
            self._ollama_models = names
            self._show_ollama_popover(button, names)

        def err(e):
            self._toast(str(e).splitlines()[0], timeout=8)

        self.worker.submit(job, done, err)

    def _show_ollama_popover(self, button: Gtk.Button, names: list[str]) -> None:
        pop = Gtk.Popover()
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        for name in names:
            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(label=name, xalign=0, margin_top=8, margin_bottom=8,
                                    margin_start=10, margin_end=10))
            lb.append(row)
        # index back into `names` rather than stashing attrs on the GObject row
        lb.connect("row-activated",
                   lambda _lb, row: (self._pick_ollama_model(names[row.get_index()]), pop.popdown()))
        sw = Gtk.ScrolledWindow(child=lb, propagate_natural_height=True,
                                max_content_height=320, width_request=280)
        pop.set_child(sw)
        pop.set_parent(button)
        pop.connect("closed", lambda p: p.unparent())
        pop.popup()

    def _pick_ollama_model(self, name: str) -> None:
        w = self._field_widget("llm", "ollama_model")
        if w:
            w.set_text(name)
        prov = self._field_widget("llm", "provider")  # flip to ollama for convenience
        if prov:
            prov.set_selected(1)  # choice:anthropic|ollama
        self._toast(f"Selected {name} (provider → ollama). Save to apply.", timeout=5)

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
        # Adw.Toast titles are Pango markup: a stray '&' or '<' in a backup name,
        # server reply, or error message would fail to parse and render blank.
        # Disable markup where supported; otherwise escape the text.
        toast = Adw.Toast(timeout=timeout)
        if hasattr(toast, "set_use_markup"):
            toast.set_use_markup(False)
            toast.set_title(msg)
        else:
            toast.set_title(GLib.markup_escape_text(msg))
        self.toasts.add_toast(toast)

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
            if full and not st.errors and not self._adopted and st.running:
                self._maybe_adopt(st)

        def err(e: Exception):
            self._refreshing = False
            self.status.errors = [str(e)]
            self._apply_status(self.status)

        self.worker.submit(job, done, err)

    def _refresh_aux(self) -> None:
        page = self.stack.get_visible_child_name()
        if page != "logs":
            self._stop_log_stream()      # never follow while the page is hidden
        if page == "history":
            self._refresh_history()
        elif page == "logs":
            self._start_log_stream()
        elif page == "players":
            self._refresh_players()
        elif page == "backups":
            self._refresh_backups()
        elif page == "mods" and not self._mods_loaded:
            self._refresh_mods()  # scan once; mods rarely change while we watch
        elif page == "modconfig" and not self._cfg_loaded:
            self._refresh_mod_configs()
        elif page == "inspect" and self.inspect_view.get_buffer().get_char_count() == 0:
            self._refresh_inspect()
        elif page == "doctor" and not self._doctor_rows:
            self._refresh_doctor()
        elif page == "props" and not self._props_rows:
            self._refresh_props()
        elif page == "jvm" and not self._jvm_loaded:
            self._refresh_jvm()
        elif page == "crashes":
            self._refresh_crashes()
        elif page == "settings":
            self._sync_settings()

    def _maybe_adopt(self, st: Status) -> None:
        """One-shot: when the app opens onto an already-running server, connect to
        it (adopt the live tmux session + intent) instead of treating it as foreign.
        Pure-local — never starts/stops anything; see ServerControl.adopt."""
        self._adopted = True
        try:
            note = self.remote.ctl.adopt(st)
        except Exception:  # noqa: BLE001 - adoption is best-effort, never break the UI
            log.debug("adopt failed", exc_info=True)
            return
        if note:
            self._toast(note, timeout=6)
            st.desired = "up"
            self._apply_status(st)            # reflect desired=up immediately
            if self._settings_fields:
                self._sync_settings()         # reflect any adopted session name

    def _start_log_stream(self) -> None:
        """Follow latest.log like tmux/`tail -F`: a dedicated thread with its own
        transport streams new lines into a queue; a GLib timer drains them into
        the view. Lines are appended (and old ones scrolled off) — never a whole-
        buffer rewrite, so the view stops 're-showing' the same 200 lines."""
        if self._log_following:
            return
        self._log_following = True
        self._log_epoch += 1
        epoch = self._log_epoch
        stop = threading.Event()
        self._log_stop = stop
        with contextlib.suppress(queue.Empty):       # drop anything stale
            while True:
                self._log_queue.get_nowait()
        self.log_view.get_buffer().set_text("")
        cfg = self.remote.cfg

        def follow():
            # A second transport that rides the shared SSH control master. We
            # deliberately don't .close() it: closing would `ssh -O exit` the
            # master socket the main worker shares. tail itself is reaped by the
            # stream's `stop` event, so there's nothing else to clean up.
            t = make_transport(cfg)
            while not stop.is_set():
                try:
                    for line in logs.follow(t, cfg, LOG_LINES, stop=stop):
                        if stop.is_set():
                            break
                        self._log_queue.put(line)
                except Exception as e:  # noqa: BLE001 - shown in-view, then retried
                    if stop.is_set():
                        break
                    self._log_queue.put(f"⚠ log stream error: {e}")
                if stop.is_set():
                    break
                self._log_queue.put("— reconnecting to the log —")
                stop.wait(3.0)                       # server restart / blip: back off, then retrace

        self._log_thread = threading.Thread(target=follow, daemon=True, name="mcctl-log-follow")
        self._log_thread.start()
        self._log_drain_id = GLib.timeout_add(200, self._drain_log_queue, epoch)

    def _drain_log_queue(self, epoch: int) -> bool:
        if epoch != self._log_epoch or not self._log_following:
            return False                             # a newer stream (or stop) owns the view now
        lines = []
        with contextlib.suppress(queue.Empty):
            for _ in range(LOG_MAX_LINES):           # bound the work per tick
                lines.append(self._log_queue.get_nowait())
        if lines:
            self._append_log_lines(lines)
        return True

    def _append_log_lines(self, lines: list[str]) -> None:
        adj = self.log_scroller.get_vadjustment()
        # follow only if the user is already at the bottom (tmux scrollback feel)
        at_bottom = adj.get_value() + adj.get_page_size() >= adj.get_upper() - 4.0
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), "\n".join(lines) + "\n")
        extra = buf.get_line_count() - LOG_MAX_LINES
        if extra > 0:                                # trim the oldest lines (ring buffer)
            ok, cut = buf.get_iter_at_line(extra)
            if ok:
                buf.delete(buf.get_start_iter(), cut)
        if at_bottom:
            self._scroll_to_end(self.log_view)

    def _stop_log_stream(self) -> None:
        if not self._log_following:
            return
        self._log_following = False
        self._log_epoch += 1                         # invalidate the drain timer + follower guards
        if self._log_stop:
            self._log_stop.set()                     # tears down the remote tail and its thread
        if self._log_drain_id:
            GLib.source_remove(self._log_drain_id)
            self._log_drain_id = 0
        self._log_stop = None
        self._log_thread = None

    def _on_close_request(self, *_) -> bool:
        self._stop_log_stream()
        return False                                 # let the window close normally

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

    # ------------------------------------------------------------- mod configs

    def _refresh_mod_configs(self, *, force: bool = False) -> None:
        if self._cfg_loading or (self._busy and not force):
            return
        self._cfg_loading = True
        self.cfg_files_group.set_description("Scanning config/ on the server…")

        def done(files):
            self._cfg_loading = False
            self._cfg_loaded = True
            self._cfg_files = files
            self._cfg_build_groups(files)
            labels = [lbl for lbl, _ in self._cfg_groups]
            self.cfg_group_drop.set_model(Gtk.StringList.new(labels or ["(none)"]))
            self._cfg_render()

        def err(e: Exception):
            self._cfg_loading = False
            self.cfg_files_group.set_description(str(e)[:160])

        self.worker.submit(
            lambda: modconfig.list_config_files(self.remote.t, self.remote.cfg), done, err)

    def _cfg_build_groups(self, files) -> None:
        by_mod: dict[str, list] = {}
        for f in files:
            by_mod.setdefault(f.mod_name or f.mod_id or "", []).append(f)
        groups = [(f"{k} ({len(by_mod[k])})", by_mod[k])
                  for k in sorted((k for k in by_mod if k), key=str.lower)]
        if "" in by_mod:
            groups.append((f"Other / unmatched ({len(by_mod[''])})", by_mod[""]))
        self._cfg_groups = groups

    def _cfg_render(self) -> None:
        if not self._cfg_loaded:
            return
        cap = 250
        query = self.cfg_search.get_text().strip().lower()
        if query:
            files = [f for f in self._cfg_files
                     if query in f.path.lower() or query in (f.mod_name or "").lower()]
            scope = f"{len(files)} match"
        else:
            idx = self.cfg_group_drop.get_selected()
            files = self._cfg_groups[idx][1] if 0 <= idx < len(self._cfg_groups) else []
            scope = f"{len(files)} file(s)"
        shown = files[:cap]
        extra = len(files) - len(shown)
        self.cfg_files_group.set_description(
            scope + (f" · showing first {cap}, narrow the search for the rest" if extra > 0 else ""))
        self._cfg_file_rows = self._swap_rows(
            self.cfg_files_group, self._cfg_file_rows, [self._cfg_file_row(f) for f in shown])

    def _cfg_file_row(self, f) -> Adw.ActionRow:
        name = f.path.rsplit("/", 1)[-1]
        bits = ([f.path] if "/" in f.path else []) + [f.fmt or "?", util.human_bytes(f.size)]
        row = Adw.ActionRow(title=name, subtitle=" · ".join(bits))
        edit = Gtk.Button(label="Edit", valign=Gtk.Align.CENTER)
        edit.connect("clicked", lambda *_: self._cfg_open_editor(f))
        row.add_suffix(edit)
        row.set_activatable_widget(edit)
        return row

    def _cfg_open_editor(self, f) -> None:
        win = Adw.Window(title=f.path, transient_for=self, default_width=860, default_height=640)
        header = Adw.HeaderBar()
        restart_btn = Gtk.Button(label="Restart server",
                                 tooltip_text="Graceful restart — guarantees every config change loads")
        restart_btn.add_css_class("destructive-action")
        restart_btn.connect("clicked", lambda *_: self._cfg_restart(win))
        header.pack_start(restart_btn)
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.set_sensitive(False)
        header.pack_end(save_btn)

        info = Gtk.Label(
            label="Save writes the file (atomic, timestamped .bak) and, if the server is up, runs "
                  "/reload. NeoForge live-reloads mods that support it; the rest apply on restart.",
            xalign=0, wrap=True)
        info.add_css_class("dim-label")
        info.add_css_class("caption")
        view = Gtk.TextView(monospace=True, editable=True, wrap_mode=Gtk.WrapMode.NONE)
        for setter in (view.set_left_margin, view.set_right_margin,
                       view.set_top_margin, view.set_bottom_margin):
            setter(8)
        scroller = Gtk.ScrolledWindow(child=view, vexpand=True)
        status = Gtk.Label(label="Loading…", xalign=0, wrap=True)
        status.add_css_class("dim-label")
        status.add_css_class("caption")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
        body.append(info)
        body.append(scroller)
        body.append(status)
        tv = Adw.ToolbarView(content=body)
        tv.add_top_bar(header)
        win.set_content(tv)

        def done(res):
            view.get_buffer().set_text(res["text"])
            status.set_label(f"{res['fmt'] or 'text'} · {util.human_bytes(res['bytes'])} · "
                             "a .bak is kept on every save")
            save_btn.set_sensitive(True)

        def err(e: Exception):
            status.set_label(f"Couldn't load: {e}")

        self.worker.submit(
            lambda: modconfig.read_config(self.remote.t, self.remote.cfg, f.path), done, err)
        save_btn.connect("clicked", lambda *_: self._cfg_save(f, view, status))
        win.present()

    def _cfg_save(self, f, view: Gtk.TextView, status: Gtk.Label) -> None:
        buf = view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        try:
            modconfig.validate_text(f.path, text)  # instant, precise error before any write
        except modconfig.ConfigEditError as e:
            status.set_label(str(e))
            self._toast(str(e), timeout=8)
            return
        if self._busy:
            self._toast(f"Busy: {self._busy}")
            return
        self._set_busy(f"Saving config/{f.path}…")
        status.set_label("Saving…")

        def job():
            modconfig.write_config(self.remote.t, self.remote.cfg, f.path, text)
            running = self.remote.ctl.find_pid() is not None
            reloaded = False
            if running:
                with contextlib.suppress(ConsoleError):
                    modconfig.trigger_reload(self.remote.console)
                    reloaded = True
            return running, reloaded

        def done(res):
            running, reloaded = res
            self._set_busy("")
            if not running:
                msg = "Saved — .bak kept; loads on next start"
            elif reloaded:
                msg = "Saved — .bak kept, /reload run; cached values need a restart"
            else:
                msg = "Saved — .bak kept; live-reload where the mod supports it"
            self._toast(msg, timeout=6)
            status.set_label(msg + ". Restart for a guaranteed full apply.")

        def err(e: Exception):
            self._set_busy("")
            status.set_label(f"Save failed: {e}")
            self._toast(f"Error: {e}", timeout=8)

        self.worker.submit(job, done, err)

    def _cfg_restart(self, win) -> None:
        def go():
            win.close()

            def job(r: Remote) -> str:
                with util.OpsLock():
                    r.ctl.restart(reason="config change")
                return "Restarted — config changes fully applied"

            self._run_action("Restarting…", job, booting=True)

        self._confirm(
            "Restart the server?",
            "A graceful restart (warn players → save → stop → start) guarantees every config "
            "change is loaded, including startup and cached values.",
            "Restart", go)

    # ------------------------------------------------------------- crafting

    def _refresh_recipes(self) -> None:
        if self._recipes_searching:
            return
        query = self.craft_search.get_text().strip()
        self._recipes_searching = True
        self.recipes_group.set_description("Scanning recipes in the jars + datapacks…")

        def done(res):
            recipes, truncated = res
            self._recipes_searching = False
            self._recipes = recipes
            if not recipes:
                self.recipes_group.set_description(
                    f"No crafting recipes match {query!r}." if query
                    else "Type something to search, then press Search.")
            else:
                more = " · more hidden, refine the search" if truncated else ""
                self.recipes_group.set_description(f"{len(recipes)} recipe(s){more}")
            self._recipe_rows = self._swap_rows(
                self.recipes_group, self._recipe_rows, [self._recipe_row(r) for r in recipes])

        def err(e: Exception):
            self._recipes_searching = False
            self.recipes_group.set_description(str(e)[:160])

        self.worker.submit(
            lambda: crafting.search_recipes(self.remote.t, self.remote.cfg, query=query, limit=80),
            done, err)

    def _recipe_row(self, rec) -> Adw.ActionRow:
        sub = " · ".join((rec.rid, rec.rtype, rec.source))
        row = Adw.ActionRow(title=f"{rec.result_count}× {rec.result_item}", subtitle=sub)
        btn = Gtk.Button(label="Craft…", valign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda *_: self._craft_open(rec))
        row.add_suffix(btn)
        row.set_activatable_widget(btn)
        return row

    def _craft_open(self, rec) -> None:
        """A craft dialog: live ingredient counts + tap-to-craft / hold-to-max."""
        cr = self.remote.cfg.crafting
        win = Adw.Window(title=f"Craft {rec.result_item}", transient_for=self,
                         default_width=560, default_height=620)
        header = Adw.HeaderBar()
        busy = {"v": False}  # guards double-submits from this dialog

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)

        title = Gtk.Label(label=f"{rec.rtype} · makes {rec.result_count}× {rec.result_item}",
                          xalign=0, wrap=True)
        title.add_css_class("title-4")
        body.append(title)
        if rec.pattern:
            grid = Gtk.Label(label="\n".join("│ " + r + " │" for r in rec.pattern), xalign=0)
            grid.add_css_class("dim-label")
            grid.add_css_class("monospace")
            body.append(grid)

        who = Adw.PreferencesGroup(
            title="Players",
            description="Source supplies the materials; receiver gets the output "
                        "(blank = your configured defaults).")
        src_row = Adw.EntryRow(title="Source")
        src_row.set_text(cr.source_player or cr.player)
        rcv_row = Adw.EntryRow(title="Receiver")
        rcv_row.set_text(cr.player)
        who.add(src_row)
        who.add(rcv_row)
        body.append(who)

        ing_group = Adw.PreferencesGroup(title="Ingredients", description="Preview to read your inventory.")
        ing_rows: list[Gtk.Widget] = []
        body.append(ing_group)

        status = Gtk.Label(xalign=0, wrap=True)
        status.add_css_class("dim-label")
        body.append(status)

        controls = Gtk.Box(spacing=8)
        count_adj = Gtk.Adjustment(value=1, lower=1, upper=cr.max_output_stack, step_increment=1)
        count_spin = Gtk.SpinButton(adjustment=count_adj, numeric=True, valign=Gtk.Align.CENTER)
        controls.append(Gtk.Label(label="Count:"))
        controls.append(count_spin)
        preview_btn = Gtk.Button(label="Preview")
        preview_btn.connect("clicked", lambda *_: do_preview())
        controls.append(preview_btn)
        craft_btn = Gtk.Button(label="Craft")
        craft_btn.add_css_class("suggested-action")
        craft_btn.set_sensitive(False)
        controls.append(craft_btn)
        max_btn = Gtk.Button(
            label="Craft max", tooltip_text="The phone's hold-to-craft (>3s) gesture: make the "
                                            "most your materials allow, capped at one output stack.")
        max_btn.set_sensitive(False)
        controls.append(max_btn)
        body.append(controls)

        # mutable holder so the craft buttons reuse the latest preview's player names
        plan_box = {"plan": None}

        def render_plan(plan):
            nonlocal ing_rows
            plan_box["plan"] = plan
            rows = []
            for ing in plan.ingredients:
                opts = " / ".join(ing["options"])
                have = "?" if ing["loose"] is None else str(ing["loose"])
                extra = f"  (+{ing['stored']} in storage)" if ing.get("stored") else ""
                rows.append(Adw.ActionRow(title=f"{ing['per_craft']}× {opts}",
                                          subtitle=f"have {have}{extra}"))
            ing_rows = self._swap_rows(ing_group, ing_rows, rows)
            if not plan.online:
                status.set_label("Source player is offline — can't read their inventory. "
                                 "The recipe is shown and planned, not crafted.")
                craft_btn.set_sensitive(False)
                max_btn.set_sensitive(False)
                return
            count_adj.set_upper(max(1, plan.cap))
            status.set_label(
                f"Craftable now: {plan.craftable}  ·  one-stack cap: {plan.cap}.  "
                + ("Not enough materials yet." if plan.craftable == 0
                   else f"Craft makes up to {plan.cap}× per click; Craft max makes {min(plan.craftable, plan.cap)}."))
            craft_btn.set_sensitive(plan.craftable > 0)
            max_btn.set_sensitive(plan.craftable > 0)

        def run_dialog(job, on_done):
            if busy["v"]:
                return
            busy["v"] = True
            status.set_label("Working…")

            def done(res):
                busy["v"] = False
                on_done(res)

            def err(e):
                busy["v"] = False
                status.set_label(f"Error: {e}")
                self._toast(f"Error: {e}", timeout=8)

            self.worker.submit(job, done, err)

        def do_preview(count=1):
            src, rcv = src_row.get_text().strip(), rcv_row.get_text().strip()
            run_dialog(
                lambda: crafting.plan_craft(self.remote.console, self.remote.cfg, rec,
                                            count=count, source=src, receiver=rcv),
                render_plan)

        def do_craft(count):
            plan = plan_box["plan"]
            src, rcv = src_row.get_text().strip(), rcv_row.get_text().strip()
            n = plan.will_craft if (plan and count is None) else (count or 1)
            out = (plan.recipe.result_count * n) if plan else rec.result_count * n
            self._confirm(
                f"Craft {out}× {rec.result_item}?",
                f"This consumes the materials from {src or cr.player}'s loose inventory and gives "
                f"the output to {rcv or cr.player}. This can't be undone.",
                "Craft",
                lambda: run_dialog(
                    lambda: crafting.craft(self.remote.console, self.remote.cfg, rec,
                                           count=count, source=src, receiver=rcv),
                    on_crafted))

        def on_crafted(res):
            if res.ok:
                msg = f"Crafted {res.crafted}× — gave {res.output_count}× {res.output_item}"
                if res.detail:
                    msg += f" ({res.detail})"
                self._toast(msg, timeout=6)
                status.set_label(msg)
            else:
                status.set_label(f"Craft failed: {res.detail}")
            do_preview(int(count_spin.get_value()))  # refresh the counts after crafting

        craft_btn.connect("clicked", lambda *_: do_craft(int(count_spin.get_value())))
        max_btn.connect("clicked", lambda *_: do_craft(None))

        tv = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=body, vexpand=True))
        tv.add_top_bar(header)
        win.set_content(tv)
        win.present()
        do_preview()  # probe inventory as soon as the dialog opens

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

    def _update_llm_widgets(self) -> None:
        """Reflect the current [llm] provider on the AI and Chat pages, so switching
        anthropic <-> ollama (or fixing a key) in Settings takes effect immediately
        instead of after a restart. Also kicks off a live ollama probe so a running
        local model is actually *detected* and surfaced (not only found on failure)."""
        if not (hasattr(self, "ai_run") and hasattr(self, "chat_send")):
            return
        ok, reason = llm.available(self.remote.cfg)
        label = llm.provider_label(self.remote.cfg)
        self.ai_hint.set_label(f"Model: {label} · logs/crash text is secret-redacted and "
                               "sent as untrusted data only.")
        self.chat_hint.set_label(f"{label} · multi-turn; context is secret-redacted and "
                                 "sent as untrusted data only.")
        self._reflect_llm(ok, reason)        # immediate state from the cheap check
        self._probe_ollama_async()           # refine once we know whether ollama answers

    def _reflect_llm(self, ok: bool, reason: str) -> None:
        """Apply AI/Chat availability + notes, blending `available()` with the
        latest live ollama detection (`self._ollama_up`)."""
        cfg = self.remote.cfg
        note = ""
        if cfg.llm.provider == "ollama":
            if self._ollama_up is False:     # selected but not answering (probe done)
                note = (f"ollama is selected but not answering at {cfg.llm.ollama_url} — start it "
                        f"with `ollama serve` (and `ollama pull {cfg.llm.ollama_model}`).")
        elif not ok:                          # anthropic selected, SDK missing
            if self._ollama_up and self._ollama_models:
                shown = ", ".join(self._ollama_models[:3])
                note = (f"Claude needs the `anthropic` package — but a local ollama IS running "
                        f"({shown}). Set [llm].provider = \"ollama\" in Settings to use it now: "
                        "no API key, nothing leaves your machine.")
            else:
                note = f"AI analysis is unavailable:\n{reason}"
        self.ai_note.set_visible(bool(note))
        self.chat_note.set_visible(bool(note))
        if note:
            self.ai_note.set_label(note)
            self.chat_note.set_label(note)
        self.ai_run.set_sensitive(ok and not self._ai_running)
        for w in (self.chat_entry, self.chat_send):
            w.set_sensitive(ok and not self._chat_running)

    def _probe_ollama_async(self) -> None:
        """Detect a running ollama off the main loop (its own thread, no SSH worker,
        no shared transport) so the check never blocks the UI or queues behind SSH."""
        cfg = self.remote.cfg

        def run():
            try:
                up, names = llm.probe_ollama(cfg)
            except Exception:  # noqa: BLE001 - detection is best-effort
                up, names = False, []
            GLib.idle_add(self._on_ollama_probe, up, names)

        threading.Thread(target=run, daemon=True, name="mcctl-ollama-probe").start()

    def _on_ollama_probe(self, up: bool, names: list[str]) -> bool:
        self._ollama_up = up
        if names:
            self._ollama_models = names
        cfg = self.remote.cfg
        if up and cfg.llm.provider == "ollama":
            plural = "" if len(names) == 1 else "s"
            self.ai_hint.set_label(f"Model: {llm.provider_label(cfg)} · ollama detected "
                                   f"({len(names)} model{plural}) · sent as untrusted data only.")
        ok, reason = llm.available(cfg)
        self._reflect_llm(ok, reason)
        return False

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
        ok, reason = llm.available(self.remote.cfg)
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
        self.ai_view.get_buffer().set_text(
            f"Gathering context, asking {llm.provider_label(self.remote.cfg)}…\n\n")
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
            self.ai_run.set_sensitive(llm.available(self.remote.cfg)[0])

        def err(e: Exception):
            self._ai_running = False
            self.ai_run.set_sensitive(llm.available(self.remote.cfg)[0])
            self.ai_view.get_buffer().set_text(f"Analysis failed:\n{e}")

        self.worker.submit(job, done, err)

    def _append_text_raw(self, view: Gtk.TextView, text: str) -> None:
        buf = view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self._scroll_to_end(view)

    # ------------------------------------------------------------- chat page

    def _on_chat_new(self, *_):
        self._chat_messages = []
        self.chat_view.get_buffer().set_text("")
        self._toast("New conversation")

    def _on_chat_send(self, *_):
        text = self.chat_entry.get_text().strip()
        if not text or self._chat_running:
            return
        ok, reason = llm.available(self.remote.cfg)
        if not ok:
            self._toast(reason.splitlines()[0])
            return
        self.chat_entry.set_text("")
        self._append_text(self.chat_view, f"you> {text}")
        self._chat_running = True
        self.chat_send.set_sensitive(False)
        first = not self._chat_messages
        marker = [True]

        def stream_chunk(s: str):
            def apply():
                if marker[0]:
                    marker[0] = False
                    self._append_text_raw(
                        self.chat_view, f"\n{llm.provider_label(self.remote.cfg)}> ")
                self._append_text_raw(self.chat_view, s)
                return False
            GLib.idle_add(apply)

        def job():
            if first:
                ctx = [llm.status_envelope(self.remote.ctl),
                       llm.envelope("latest.log tail", logs.tail(self.remote.t, self.remote.cfg, 120))]
                content = ("You are in an interactive session with the server operator. Use the "
                           "attached current server context when relevant.\n\n"
                           + "\n\n".join(ctx) + f"\n\nOperator: {text}")
            else:
                content = text
            self._chat_messages.append({"role": "user", "content": content})
            reply = llm.Analyst(self.remote.cfg).chat(self._chat_messages, on_text=stream_chunk)
            self._chat_messages.append({"role": "assistant", "content": reply})
            return None

        def done(_):
            self._chat_running = False
            self.chat_send.set_sensitive(True)
            self._append_text_raw(self.chat_view, "\n")

        def err(e: Exception):
            self._chat_running = False
            self.chat_send.set_sensitive(True)
            # drop the half-sent user turn so the next send isn't poisoned
            if self._chat_messages and self._chat_messages[-1]["role"] == "user":
                self._chat_messages.pop()
            self._append_text_raw(self.chat_view, f"\n[error: {e}]\n")

        self.worker.submit(job, done, err)

    # ------------------------------------------------------------- history page

    @staticmethod
    def _history_value(key: str, s: dict):
        if key == "heap":
            used, total = s.get("heap_used"), s.get("heap_max") or s.get("heap_committed")
            return 100.0 * used / total if used and total else None
        if key == "mem":
            used, total = s.get("mem_used"), s.get("mem_total")
            return 100.0 * used / total if used and total else None
        return s.get({"tps": "tps", "mspt": "mspt", "players": "players", "load": "load1"}[key])

    def _refresh_history(self) -> None:
        if self._history_refreshing:
            return
        self._history_refreshing = True

        def job():
            samples = metrics.read_samples(720)
            series = {k: [self._history_value(k, s) for s in samples] for k in self.history_keys}
            times = [s.get("ts") for s in samples]
            return series, times

        def done(res):
            self._history_refreshing = False
            from . import charts
            series, times = res
            self._history_series, self._history_times = series, times
            self._history_meta = {}
            total = 0
            for key in self.history_keys:
                summ = charts.summarize(series[key])
                total = max(total, summ.n)
                fixed = self._history_fixed_hi.get(key)
                hi = fixed if fixed else (self._nice_ceil(summ.max) if summ.max else 1.0)
                self._history_meta[key] = {
                    "summary": summ, "lo": 0.0, "hi": hi, "step": self._nice_step(hi)}
            for area in self._history_areas.values():
                area.queue_draw()
            self.history_status.set_label(
                "no samples yet — run `mcctl watch` or the watchdog"
                if total == 0 else
                f"{total} samples · all metrics shown · reloads while this tab is open")

        def err(e: Exception):
            self._history_refreshing = False
            self.history_status.set_label(f"history unavailable: {e}"[:200])

        self.worker.submit(job, done, err)

    @staticmethod
    def _nice_step(span: float, target: int = 4) -> float:
        """A 'nice' axis step (1/2/2.5/5 × 10ⁿ) giving ~`target` gridlines over span."""
        import math
        raw = max(span, 1e-9) / max(target, 1)
        mag = 10.0 ** math.floor(math.log10(raw))
        for m in (1, 2, 2.5, 5, 10):
            if m * mag >= raw:
                return m * mag
        return 10 * mag

    @classmethod
    def _nice_ceil(cls, x: float) -> float:
        """Round an auto axis maximum up to a clean multiple of a nice step,
        with a little headroom so the peak sample isn't glued to the top."""
        import math
        step = cls._nice_step(x)
        return max(step, math.ceil(x * 1.05 / step) * step)

    @staticmethod
    def _fmt_axis(val: float, step: float) -> str:
        return f"{val:.0f}" if abs(step - round(step)) < 1e-9 else f"{val:.1f}"

    @staticmethod
    def _fmt_clock(ts: int) -> str:
        return time.strftime("%H:%M", time.localtime(ts))

    @staticmethod
    def _chart_text(cr, x: float, y: float, s: str, *, size: float,
                    rgba: tuple, align: str = "left") -> None:
        cr.set_font_size(size)
        ext = cr.text_extents(s)
        if align == "right":
            x -= ext.width
        elif align == "center":
            x -= ext.width / 2
        cr.set_source_rgba(*rgba)
        cr.move_to(x, y)
        cr.show_text(s)

    def _draw_metric(self, _area, cr, width, height, key) -> None:
        """One metric card: title + current value, a y-axis with gridlines and
        numbers, an x-axis with time labels, and the series as a filled line."""
        spec = next((sp for sp in HISTORY_SPECS if sp[0] == key), None)
        if spec is None:
            return
        _k, label, _fixed, fmt = spec
        series = self._history_series.get(key, [])
        times = self._history_times
        meta = self._history_meta.get(key)
        r, g, b = HISTORY_COLORS.get(key, (0.22, 0.72, 0.45))

        cr.set_source_rgba(0.5, 0.5, 0.5, 0.06)          # panel background
        cr.rectangle(0, 0, width, height)
        cr.fill()

        left, right, top, bottom = 48.0, 14.0, 30.0, 26.0
        plot_w = max(1.0, width - left - right)
        plot_h = max(1.0, height - top - bottom)
        x0, y0 = left, top
        lo = meta["lo"] if meta else 0.0
        hi = meta["hi"] if meta else 1.0
        step = meta["step"] if meta else hi
        span = max(hi - lo, 1e-9)
        summ = meta["summary"] if meta else None

        self._chart_text(cr, 10, 20, label, size=13, rgba=(r, g, b, 1.0))      # title
        if summ and summ.last is not None:                                     # current value
            self._chart_text(cr, width - right, 20, fmt.format(summ.last),
                             size=13, rgba=(0.92, 0.92, 0.92, 0.95), align="right")

        cr.set_line_width(1.0)                            # horizontal gridlines + y numbers
        v = lo
        while v <= hi + step * 0.25:
            y = y0 + plot_h * (1.0 - (v - lo) / span)
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.15)
            cr.move_to(x0, y)
            cr.line_to(x0 + plot_w, y)
            cr.stroke()
            cr.set_source_rgba(0.6, 0.6, 0.6, 0.6)        # tick mark
            cr.move_to(x0 - 4, y)
            cr.line_to(x0, y)
            cr.stroke()
            self._chart_text(cr, x0 - 7, y + 3.5, self._fmt_axis(v, step),
                             size=10, rgba=(0.62, 0.62, 0.62, 0.95), align="right")
            v += step

        cr.set_source_rgba(0.6, 0.6, 0.6, 0.55)           # axis lines (L-shape)
        cr.set_line_width(1.2)
        cr.move_to(x0, y0)
        cr.line_to(x0, y0 + plot_h)
        cr.line_to(x0 + plot_w, y0 + plot_h)
        cr.stroke()

        n = len(series)
        if not any(x is not None for x in series) or n == 0:
            self._chart_text(cr, x0 + plot_w / 2, y0 + plot_h / 2 + 4, "no data yet",
                             size=12, rgba=(0.6, 0.6, 0.6, 0.9), align="center")
            return

        def sx(i):
            return x0 + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

        def sy(val):
            return y0 + plot_h * (1.0 - (max(lo, min(hi, val)) - lo) / span)

        if times and n > 1:                               # x-axis time ticks + labels
            for frac in (0.0, 0.5, 1.0):
                idx = min(n - 1, max(0, round(frac * (n - 1))))
                x = x0 + plot_w * frac
                cr.set_source_rgba(0.6, 0.6, 0.6, 0.6)
                cr.move_to(x, y0 + plot_h)
                cr.line_to(x, y0 + plot_h + 4)
                cr.stroke()
                ts = times[idx] if idx < len(times) else None
                if ts:
                    al = "left" if frac == 0.0 else ("right" if frac == 1.0 else "center")
                    self._chart_text(cr, x, y0 + plot_h + 17, self._fmt_clock(ts),
                                     size=10, rgba=(0.62, 0.62, 0.62, 0.9), align=al)

        runs, cur = [], []                                # split on gaps (None)
        for i, val in enumerate(series):
            if val is None:
                if cur:
                    runs.append(cur)
                    cur = []
            else:
                cur.append((sx(i), sy(val)))
        if cur:
            runs.append(cur)

        cr.set_source_rgba(r, g, b, 0.13)                 # area under the line
        for run in runs:
            if len(run) < 2:
                continue
            cr.move_to(run[0][0], y0 + plot_h)
            for px, py in run:
                cr.line_to(px, py)
            cr.line_to(run[-1][0], y0 + plot_h)
            cr.close_path()
            cr.fill()

        cr.set_source_rgba(r, g, b, 1.0)                  # the line itself
        cr.set_line_width(2.0)
        for run in runs:
            if len(run) == 1:
                cr.arc(run[0][0], run[0][1], 1.6, 0.0, 6.2832)
                cr.fill()
                continue
            cr.move_to(*run[0])
            for px, py in run[1:]:
                cr.line_to(px, py)
            cr.stroke()

    # ------------------------------------------------------------- doctor

    _LEVEL_GLYPHS = {Level.OK: ("✓", "success"), Level.WARN: ("!", "warning"),
                     Level.FAIL: ("✗", "error"), Level.FIXED: ("+", "accent"),
                     Level.SKIP: ("–", "dim-label")}

    def _refresh_doctor(self, *, fix: bool = False) -> None:
        if self._doctor_running:
            return
        self._doctor_running = True
        self.doctor_group.set_description("Running checks…" + (" (applying fixes)" if fix else ""))

        def done(results):
            self._doctor_running = False
            counts: dict[Level, int] = {}
            for r in results:
                counts[r.level] = counts.get(r.level, 0) + 1
            self.doctor_group.set_description(
                "  ".join(f"{n} {lv.value}" for lv, n in counts.items()))
            self._doctor_rows = self._swap_rows(
                self.doctor_group, self._doctor_rows, [self._doctor_row(r) for r in results])

        def err(e: Exception):
            self._doctor_running = False
            self.doctor_group.set_description(str(e)[:160])

        self.worker.submit(lambda: run_doctor(self.remote.cfg, self.remote.t, fix=fix),
                           done, err)

    def _doctor_row(self, r) -> Adw.ActionRow:
        sub = r.detail + (f" — {r.hint}" if r.hint else "")
        row = Adw.ActionRow(title=r.name, subtitle=sub)
        glyph, cls = self._LEVEL_GLYPHS[r.level]
        lbl = Gtk.Label(label=glyph, width_chars=2)
        lbl.add_css_class(cls)
        lbl.add_css_class("title-3")
        row.add_prefix(lbl)
        return row

    # ------------------------------------------------------------- properties

    def _refresh_props(self) -> None:
        if self._props_loading:
            return
        self._props_loading = True
        self.props_group.set_description("Loading server.properties…")

        def done(pf):
            self._props_loading = False
            self._props_pf = pf
            self.props_group.set_description(
                "Validated editor — nothing is written until you review the diff")
            known_rows, other_rows = [], []
            rows: dict[str, tuple] = {}
            for key, spec in sorted(propsmod.KNOWN_PROPS.items()):
                if key == "rcon.password":
                    continue
                current = pf.get(key)
                widget, orig = self._props_widget(key, spec, current)
                rows[key] = (spec, widget, orig)
                known_rows.append(widget)
            known = set(propsmod.KNOWN_PROPS)
            for key, value in sorted(pf.items()):
                if key in known:
                    continue
                shown = "********" if "password" in key else value
                other_rows.append(Adw.ActionRow(title=key, subtitle=shown))
            old = [w for (_s, w, _o) in self._props_rows.values()]
            self._swap_rows(self.props_group, old, known_rows)
            self._props_rows = rows
            self._props_other_rows = self._swap_rows(
                self.props_other_group, self._props_other_rows, other_rows)

        def err(e: Exception):
            self._props_loading = False
            self.props_group.set_description(f"Unreadable: {e}"[:200])

        self.worker.submit(lambda: propsmod.load_props(self.remote.t, self.remote.cfg),
                           done, err)

    def _props_widget(self, key: str, spec, current: str | None) -> tuple[Gtk.Widget, str]:
        """Build the editor row for a spec; returns (widget, original-rendered-value)."""
        subtitle = spec.desc + ("" if current is not None else " · (not set on server)")
        if spec.type == "bool":
            row = Adw.SwitchRow(title=key, subtitle=subtitle, active=current == "true")
            return row, ("true" if current == "true" else "false")
        if spec.type == "enum":
            row = Adw.ComboRow(title=key, subtitle=subtitle,
                               model=Gtk.StringList.new(list(spec.enum)))
            idx = spec.enum.index(current) if current in spec.enum else 0
            row.set_selected(idx)
            return row, spec.enum[idx]
        if spec.type == "int":
            lo = spec.lo if spec.lo is not None else -2**31
            hi = spec.hi if spec.hi is not None else 2**31
            try:
                val = int(current) if current is not None else max(lo, 0)
            except ValueError:
                val = max(lo, 0)
            row = Adw.SpinRow(title=key, subtitle=subtitle,
                              adjustment=Gtk.Adjustment(value=val, lower=lo, upper=hi,
                                                        step_increment=1))
            return row, str(val)
        row = Adw.EntryRow(title=f"{key} — {spec.desc}", text=current or "")
        return row, (current or "")

    def _props_value(self, spec, widget) -> str:
        if spec.type == "bool":
            return "true" if widget.get_active() else "false"
        if spec.type == "enum":
            return spec.enum[widget.get_selected()]
        if spec.type == "int":
            return str(int(widget.get_value()))
        return widget.get_text().strip()

    def _on_props_save(self, *_):
        pf = self._props_pf
        if pf is None:
            self._toast("Load the properties first")
            return
        changes: dict[str, str] = {}
        for key, (spec, widget, orig) in self._props_rows.items():
            value = self._props_value(spec, widget)
            if value != orig:
                try:
                    changes[key] = propsmod.validate_prop(key, value)
                except propsmod.PropError as e:
                    self._toast(str(e), timeout=8)
                    return
        if not changes:
            self._toast("No changes to save")
            return
        new_pf = propsmod.PropertiesFile.parse(pf.render())
        for k, v in changes.items():
            new_pf.set(k, v)
        diff = "\n".join(propsmod.props_diff(pf, new_pf))
        live_keys = [k for k in changes if propsmod.KNOWN_PROPS[k].live_cmd]

        dlg = Adw.AlertDialog(heading="Write server.properties?",
                              body=f"{diff}\n\nA timestamped .bak is kept on the server; "
                                   "most keys apply on next restart.")
        live_check = None
        if live_keys:
            live_check = Gtk.CheckButton(
                label=f"Also apply live now: {', '.join(live_keys)}")
            dlg.set_extra_child(live_check)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Write")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp != "save":
                return
            live = bool(live_check and live_check.get_active())

            def job(r: Remote) -> str:
                propsmod.save_props(r.t, r.cfg, new_pf)
                applied = []
                if live and r.ctl.find_pid() is not None:
                    for k in live_keys:
                        spec = propsmod.KNOWN_PROPS[k]
                        v = changes[k]
                        cmd = spec.live_cmd.format(v=v, onoff="on" if v == "true" else "off")
                        with contextlib.suppress(Exception):
                            r.console.send(cmd)
                            applied.append(k)
                msg = f"Wrote {len(changes)} change(s) (.bak kept)"
                if applied:
                    msg += f" · applied live: {', '.join(applied)}"
                return msg
            self._run_action("Writing server.properties…", job)
            GLib.timeout_add_seconds(1, lambda: (self._refresh_props(), False)[1])
        dlg.connect("response", on_resp)
        dlg.present(self)

    # ------------------------------------------------------------- jvm

    def _refresh_jvm(self) -> None:
        if self._jvm_loading:
            return
        self._jvm_loading = True

        def job():
            text = propsmod.load_variables(self.remote.t, self.remote.cfg)
            memr = self.remote.t.run("free -b | awk '/^Mem:/{print $2}'", timeout=15)
            total = int(memr.out.strip() or 0)
            return text, total

        def done(res):
            self._jvm_loading = False
            self._jvm_loaded = True
            text, total = res
            self._jvm_text, self._jvm_ram = text, total
            args = propsmod.get_var(text, "JAVA_ARGS") or ""
            _xms, xmx = propsmod.parse_heap(args)
            self.jvm_heap_row.set_text(xmx or "")
            self.jvm_java_row.set_text(propsmod.get_var(text, "JAVA") or "")
            rows = [Adw.ActionRow(title="JAVA_ARGS", subtitle=args or "(none)",
                                  subtitle_selectable=True)]
            for k in ("SKIP_JAVA_CHECK", "WAIT_FOR_USER_INPUT", "SERVERSTARTERJAR_FORCE_FETCH",
                      "MINECRAFT_VERSION", "MODLOADER", "MODLOADER_VERSION"):
                v = propsmod.get_var(text, k)
                if v is not None:
                    rows.append(Adw.ActionRow(title=k, subtitle=v))
            if total:
                rows.append(Adw.ActionRow(title="Host RAM",
                                          subtitle=util.human_bytes(total)))
            self._jvm_info_rows = self._swap_rows(self.jvm_info_group,
                                                  self._jvm_info_rows, rows)

        def err(e: Exception):
            self._jvm_loading = False
            self.jvm_info_group.set_description(str(e)[:200])

        self.worker.submit(job, done, err)

    def _on_jvm_heap(self, row: Adw.EntryRow) -> None:
        size = row.get_text().strip()
        try:
            heap = propsmod.size_to_bytes(size)
        except propsmod.PropError as e:
            self._toast(str(e), timeout=6)
            return

        def apply():
            def job(r: Remote) -> str:
                text = propsmod.load_variables(r.t, r.cfg)
                propsmod.save_variables(r.t, r.cfg, propsmod.set_heap(text, size))
                return f"Heap set to {size} (Xms=Xmx) — restart to apply"
            self._run_action(f"Setting heap to {size}…", job)
            self._jvm_loaded = False

        total = getattr(self, "_jvm_ram", 0)
        if total and heap > 0.75 * total:
            self._confirm("Heap larger than 75% of host RAM",
                          f"{size} of {util.human_bytes(total)} leaves little room for "
                          "off-heap memory and the OS page cache.", "Set anyway",
                          apply)
        else:
            apply()

    def _on_jvm_java(self, row: Adw.EntryRow) -> None:
        path = row.get_text().strip()
        if not path:
            return

        def job(r: Remote) -> str:
            from .transport import q as _q
            if not r.t.run(f"test -x {_q(path)}", timeout=15).ok:
                raise TransportError(f"{path} is not executable on the server")
            text = propsmod.load_variables(r.t, r.cfg)
            propsmod.save_variables(r.t, r.cfg, propsmod.set_var(text, "JAVA", path))
            return f"JAVA set to {path} — restart to apply"
        self._run_action("Setting JAVA path…", job)
        self._jvm_loaded = False

    # ------------------------------------------------------------- crashes

    def _refresh_crashes(self) -> None:
        if self._crashes_refreshing or self._busy:
            return
        self._crashes_refreshing = True

        def done(reports):
            self._crashes_refreshing = False
            self.crash_group.set_description(
                f"{len(reports)} report(s)" if reports else "No crash reports — good sign!")
            self._crash_rows = self._swap_rows(
                self.crash_group, self._crash_rows,
                [self._crash_row(n, s, m) for n, s, m in reports])

        def err(e: Exception):
            self._crashes_refreshing = False
            self.crash_group.set_description(str(e)[:160])

        self.worker.submit(lambda: logs.crash_list(self.remote.t, self.remote.cfg), done, err)
        self._refresh_evidence()

    def _crash_row(self, name: str, size: int, mtime: int) -> Adw.ActionRow:
        age = util.human_duration(max(0, int(time.time()) - mtime))
        row = Adw.ActionRow(title=name, subtitle=f"{util.human_bytes(size)} · {age} ago")
        view = Gtk.Button(label="View", valign=Gtk.Align.CENTER)
        view.add_css_class("flat")
        view.connect("clicked", lambda *_, n=name: self._show_crash(n))
        row.add_suffix(view)
        ai = Gtk.Button(label="Analyze with AI", valign=Gtk.Align.CENTER)
        ai.add_css_class("flat")
        ai.connect("clicked", lambda *_, n=name: self._crash_ai(n))
        row.add_suffix(ai)
        return row

    def _show_crash(self, name: str) -> None:
        def done(res):
            _n, content = res
            self.crash_view.get_buffer().set_text(content or "(empty)")
        self.worker.submit(lambda: logs.crash_get(self.remote.t, self.remote.cfg, name),
                           done,
                           lambda e: self.crash_view.get_buffer().set_text(f"error: {e}"))

    def _crash_ai(self, name: str) -> None:
        def gather():
            _n, content = logs.crash_get(self.remote.t, self.remote.cfg, name)
            return [llm.envelope(f"crash-report {name}", content),
                    llm.envelope("latest.log tail",
                                 logs.tail(self.remote.t, self.remote.cfg, 120))]
        self.stack.set_visible_child_name("ai")
        self._ai_analyze_with(gather, "crash", "")

    def _refresh_evidence(self) -> None:
        bundles = sorted((d for d in util.crashes_dir().iterdir() if d.is_dir()),
                         reverse=True) if util.crashes_dir().exists() else []
        rows = []
        for d in bundles[:20]:
            reason = ""
            with contextlib.suppress(OSError):
                reason = (d / "reason.txt").read_text(encoding="utf-8").strip()[:120]
            row = Adw.ActionRow(title=d.name, subtitle=reason or "(no reason recorded)")
            b = Gtk.Button(label="View", valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_, p=d: self._show_evidence(p))
            row.add_suffix(b)
            rows.append(row)
        self.evidence_group.set_description(
            f"{len(bundles)} bundle(s) in {util.crashes_dir()}" if bundles
            else "None yet — the watchdog saves one before every heal")
        self._evidence_rows = self._swap_rows(self.evidence_group, self._evidence_rows, rows)

    def _show_evidence(self, bundle: Path) -> None:
        parts = []
        for f in sorted(bundle.iterdir()):
            with contextlib.suppress(OSError):
                parts.append(f"━━━ {f.name} ━━━\n{f.read_text(encoding='utf-8', errors='replace')}")
        self.crash_view.get_buffer().set_text("\n\n".join(parts) or "(empty bundle)")

    # ------------------------------------------------------------- profiler / sync

    def _on_profile(self, *_):
        if self._profiler_running:
            self._toast("Profiler already running")
            return
        secs = int(self.prof_seconds.get_value())
        self._profiler_running = True
        self.prof_run_btn.set_sensitive(False)
        self._toast(f"Profiling for {secs}s — the result link appears below")

        def job():
            from .spark import Spark
            return Spark(self.remote.console).profile(secs)

        def done(url: str):
            self._profiler_running = False
            self.prof_run_btn.set_sensitive(True)
            row = Adw.ActionRow(title=time.strftime("%H:%M:%S"), subtitle=f"{secs}s sample")
            row.add_suffix(Gtk.LinkButton(uri=url, label="open report",
                                          valign=Gtk.Align.CENTER))
            self.prof_results.add(row)
            self._toast("Profile ready")

        def err(e: Exception):
            self._profiler_running = False
            self.prof_run_btn.set_sensitive(True)
            self._toast(f"Profiler failed: {e}", timeout=8)

        self.worker.submit(job, done, err)

    def _on_sync(self, *, push: bool) -> None:
        local = self.sync_dir_row.get_text().strip()
        if not local:
            self._toast("Enter the local directory first")
            return
        local = str(Path(local).expanduser())

        def job(r: Remote) -> str:
            remote = r.t.remote_spec(f"{r.cfg.server.server_dir}/config/")
            if push:
                code = r.t.rsync(local.rstrip("/") + "/", remote)
            else:
                Path(local).mkdir(parents=True, exist_ok=True)
                code = r.t.rsync(remote, local.rstrip("/") + "/")
            if code != 0:
                raise TransportError(f"rsync exited with code {code}")
            return ("Pushed local config/ to the server" if push
                    else f"Pulled server config/ into {local}")
        self._run_action("Syncing config/…", job)

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
                self._booting = False        # fully up: the boot is over
            elif st.running or self._booting:
                kind, text = "warning", "BOOTING SERVER"
            else:
                kind, text = "error", "OFFLINE"
        self._set_badge(kind, text)

        s = self.remote.cfg.server
        target = "this machine (local transport)" if s.transport == "local" else f"{s.user}@{s.host}"
        self.badge_sub.set_label(f"{target} · {s.server_dir}")

        self.row_process.set_label(
            f"pid {st.pid} · up {util.human_duration(st.uptime_s)}"
            + (f" · tmux '{st.tmux_session}'" if st.tmux_session else "")
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
        wd_state = state.load()
        window = self.remote.cfg.watchdog.restart_window
        recent = [t for t in wd_state.get("restarts", []) if t > time.time() - window]
        all_restarts = wd_state.get("restarts", [])
        last = (f" · last {util.human_duration(int(time.time() - max(all_restarts)))} ago"
                if all_restarts else "")
        self.row_heals.set_label(
            f"{len(recent)}/{self.remote.cfg.watchdog.max_restarts} in window{last}")
        self._update_buttons()

    def _set_badge(self, kind: str, text: str) -> None:
        for c in ("success", "warning", "error"):
            self.badge.remove_css_class(c)
        self.badge.add_css_class(kind)
        self.badge.set_label(text)

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

    def _run_action(self, busy: str, job, *, refresh_aux: bool = False,
                    booting: bool = False) -> None:
        """Run job(remote) on the worker; its return string becomes a toast.

        With booting=True the status badge reads "BOOTING SERVER" for the whole
        action — no status probe runs while the worker is busy, so without this
        the badge would otherwise sit on the stale "OFFLINE" until the boot ends."""
        if self._busy:
            self._toast(f"Busy: {self._busy}")
            return
        self._set_busy(busy)
        self._toast(busy, timeout=2)
        if booting:
            self._booting = True
            self._set_badge("warning", "BOOTING SERVER")

        def done(msg):
            self._booting = False
            self._set_busy("")
            if msg:
                self._toast(str(msg), timeout=6)
            self._refresh(full=True)
            if refresh_aux:
                self._refresh_aux()

        def err(e: Exception):
            self._booting = False
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
        self._run_action("Starting server — a modded boot can take minutes…", job, booting=True)

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
                      "Restart", lambda: self._run_action("Restarting server…", job, booting=True))

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
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="-v info, -vv debug on stderr")
    args = ap.parse_args(argv)
    util.setup_logging(args.verbose)
    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    return McctlApp(cfg).run(None)
