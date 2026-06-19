"""Configuration: TOML at ~/.config/mcctl/config.toml, defaults = CarborioLand stack."""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import util

log = util.get_logger("config")


class ConfigError(RuntimeError):
    pass


def _toml_val(v: object) -> str:
    """Serialise a Python value to its TOML literal representation."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    return str(v)  # int / float


@dataclass(slots=True)
class ServerCfg:
    host: str = "144.33.19.123"
    ssh_port: int = 22
    user: str = "ubuntu"
    ssh_key: str = ""                                     # path to SSH private key (-i), e.g. ~/.ssh/carborio
    ssh_options: list[str] = field(default_factory=list)  # extra raw ssh args / -o options
    transport: str = "ssh"                                # "ssh" | "local" (dev/test)
    server_dir: str = "/opt/minecraft"
    tmux_session: str = "minecraft"
    start_command: str = "bash start.sh"
    log_file: str = "logs/latest.log"                     # relative to server_dir
    world_dir: str = "world"                              # relative to server_dir
    mc_port: int = 25565
    rcon_port: int = 25575
    java_home: str = "/opt/graalvm"
    start_timeout: int = 420                              # modded first boot is slow
    stop_timeout: int = 120
    stop_countdown: list[int] = field(default_factory=lambda: [30, 10, 5])


@dataclass(slots=True)
class BackupCfg:
    remote_dir: str = "/opt/minecraft-backups"
    prefix: str = "world"
    compression: str = "zstd"                             # "zstd" | "gzip"
    keep_recent: int = 8
    keep_daily: int = 7
    keep_weekly: int = 4
    min_free_gb: float = 5.0
    local_dir: str = ""                                   # optional rsync pull target
    full_excludes: list[str] = field(
        default_factory=lambda: ["logs", "crash-reports", ".cache", "libraries/.cache"]
    )


@dataclass(slots=True)
class WatchdogCfg:
    interval: int = 30                                    # seconds between checks
    freeze_log_age: int = 300                             # log stale + console dead => frozen
    max_restarts: int = 3                                 # within restart_window
    restart_window: int = 3600
    backoff_base: int = 20                                # restart backoff: base * 2^n
    tps_alert: float = 15.0
    heap_alert_pct: int = 92
    autosave_minutes: int = 0                             # 0 = rely on systemd timer instead
    auto_profile_on_lag: bool = False                     # auto-run spark profiler on low TPS
    notify_desktop: bool = True
    webhook_url: str = ""                                 # Discord-compatible, optional
    ntfy_url: str = "https://ntfy.sh"                     # ntfy server (also a UnifiedPush distributor)
    ntfy_topic: str = ""                                  # empty disables ntfy push
    ntfy_token: str = ""                                  # optional bearer for protected topics


@dataclass(slots=True)
class MetricsCfg:
    # Prometheus textfile exporter target; point node_exporter's
    # --collector.textfile.directory at the directory holding this file.
    prom_path: str = ""                                   # "" => $XDG_STATE_HOME/mcctl/mcctl.prom


@dataclass(slots=True)
class LlmCfg:
    provider: str = "anthropic"                           # "anthropic" (Claude API) | "ollama" (local)
    model: str = "claude-opus-4-8"                        # Anthropic model id
    api_key_env: str = "ANTHROPIC_API_KEY"                # env var holding the key (never stored)
    max_tokens: int = 8000                                # per-answer output budget
    log_lines: int = 400                                  # log tail size sent with `mcctl ai logs`
    ollama_url: str = "http://localhost:11434"            # local ollama server (provider = "ollama")
    ollama_model: str = "llama3.1"                        # model name as `ollama pull`ed it


@dataclass(slots=True)
class CraftingCfg:
    # Recipe browser + survival "command-craft" (`mcctl craft`, agent craft.* methods).
    player: str = "GLEYSSON"                # default crafted-output receiver (your IGN)
    source_player: str = ""                 # whose inventory supplies materials ("" = player)
    max_output_stack: int = 64              # hold-to-craft-max cap: one output stack
    include_containers: bool = True         # also *show* backpack/container counts (never consumed)
    hold_ms: int = 3000                     # UI contract: hold this long = craft-max


@dataclass(slots=True)
class UiCfg:
    # Log timestamps are shown in this zone; the server writes them in server_timezone.
    # Defaults convert a UTC OCI box's logs to São Paulo wall-clock for easy reading.
    timezone: str = "America/Sao_Paulo"                   # display zone (IANA name, or "" to disable)
    server_timezone: str = "UTC"                          # zone the server's clock/logs use


@dataclass(slots=True)
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    backup: BackupCfg = field(default_factory=BackupCfg)
    watchdog: WatchdogCfg = field(default_factory=WatchdogCfg)
    metrics: MetricsCfg = field(default_factory=MetricsCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    ui: UiCfg = field(default_factory=UiCfg)
    crafting: CraftingCfg = field(default_factory=CraftingCfg)
    path: Path | None = None

    # ---------------------------------------------------------------- load

    @staticmethod
    def default_path() -> Path:
        return util.config_dir() / "config.toml"

    @classmethod
    def load(cls, path: Path | str | None = None) -> Config:
        p = Path(path) if path else cls.default_path()
        cfg = cls(path=p)
        if not p.exists():
            log.info("no config at %s — using built-in defaults (run `mcctl init`)", p)
            cfg.validate()
            return cfg
        try:
            with open(p, "rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"cannot read {p}: {e}") from e
        for section, dc in (("server", cfg.server), ("backup", cfg.backup),
                            ("watchdog", cfg.watchdog), ("metrics", cfg.metrics),
                            ("llm", cfg.llm), ("ui", cfg.ui), ("crafting", cfg.crafting)):
            data = raw.get(section, {})
            if not isinstance(data, dict):
                raise ConfigError(f"[{section}] must be a table")
            known = {f.name: f.type for f in fields(dc)}
            for k, v in data.items():
                if k not in known:
                    log.warning("config: unknown key [%s].%s ignored", section, k)
                    continue
                setattr(dc, k, v)
        for section in raw:
            if section not in ("server", "backup", "watchdog", "metrics", "llm", "ui", "crafting"):
                log.warning("config: unknown section [%s] ignored", section)
        cfg.validate()
        return cfg

    # ---------------------------------------------------------------- validate

    def validate(self) -> None:
        s, b, w = self.server, self.backup, self.watchdog
        problems: list[str] = []
        if not s.host:
            problems.append("server.host must not be empty")
        if s.transport not in ("ssh", "local"):
            problems.append("server.transport must be 'ssh' or 'local'")
        if not (0 < s.ssh_port < 65536 and 0 < s.mc_port < 65536 and 0 < s.rcon_port < 65536):
            problems.append("ports must be 1-65535")
        if not s.server_dir.startswith("/"):
            problems.append("server.server_dir must be absolute")
        if Path(s.log_file).is_absolute() or Path(s.world_dir).is_absolute():
            problems.append("server.log_file and server.world_dir are relative to server_dir")
        if b.compression not in ("zstd", "gzip"):
            problems.append("backup.compression must be 'zstd' or 'gzip'")
        if min(b.keep_recent, b.keep_daily, b.keep_weekly) < 0:
            problems.append("backup.keep_* must be >= 0")
        if b.keep_recent == 0:
            problems.append("backup.keep_recent must be >= 1 (or you'd delete the backup you just made)")
        if not b.remote_dir.startswith("/"):
            problems.append("backup.remote_dir must be absolute")
        if w.interval < 5:
            problems.append("watchdog.interval must be >= 5s")
        if w.max_restarts < 1:
            problems.append("watchdog.max_restarts must be >= 1")
        if w.ntfy_topic and not w.ntfy_url:
            problems.append("watchdog.ntfy_url must be set when ntfy_topic is given")
        llm = self.llm
        if llm.provider not in ("anthropic", "ollama"):
            problems.append("llm.provider must be 'anthropic' or 'ollama'")
        if not llm.model:
            problems.append("llm.model must not be empty")
        if not (256 <= llm.max_tokens <= 64000):
            problems.append("llm.max_tokens must be in [256, 64000]")
        if llm.log_lines < 10:
            problems.append("llm.log_lines must be >= 10")
        if llm.provider == "ollama" and not llm.ollama_model:
            problems.append("llm.ollama_model must not be empty when provider = 'ollama'")
        import re as _re
        cr = self.crafting
        if not _re.match(r"^[A-Za-z0-9_]{1,16}$", cr.player):
            problems.append("crafting.player must be a valid Minecraft name (1-16 of A-Z a-z 0-9 _)")
        if cr.source_player and not _re.match(r"^[A-Za-z0-9_]{1,16}$", cr.source_player):
            problems.append("crafting.source_player must be empty or a valid Minecraft name")
        if not (1 <= cr.max_output_stack <= 64):
            problems.append("crafting.max_output_stack must be in [1, 64]")
        if cr.hold_ms < 0:
            problems.append("crafting.hold_ms must be >= 0")
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        for key, tz in (("ui.timezone", self.ui.timezone),
                        ("ui.server_timezone", self.ui.server_timezone)):
            if tz:  # "" disables conversion for that side
                try:
                    ZoneInfo(tz)
                except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
                    problems.append(f"{key}={tz!r} is not a known IANA timezone")
        if problems:
            raise ConfigError("invalid config:\n  - " + "\n  - ".join(problems))

    def to_dict(self) -> dict:
        return {"server": asdict(self.server), "backup": asdict(self.backup),
                "watchdog": asdict(self.watchdog), "metrics": asdict(self.metrics),
                "llm": asdict(self.llm), "ui": asdict(self.ui),
                "crafting": asdict(self.crafting)}

    # ---------------------------------------------------------------- save

    def save(self, path: Path | str | None = None) -> Path:
        """Write the *whole* config back to TOML (every section), round-trip safe.

        The GUI's Settings page is the primary writer; regenerating all sections
        — not just the ones it edits — means a partial save can never silently
        drop e.g. the [llm] or [ui] tables. Validated before writing so we never
        persist a config that `load()` would then reject.
        """
        self.validate()
        p = Path(path) if path else (self.path or self.default_path())
        sections = (("server", self.server), ("backup", self.backup),
                    ("watchdog", self.watchdog), ("metrics", self.metrics),
                    ("llm", self.llm), ("ui", self.ui), ("crafting", self.crafting))
        lines = ["# mcctl configuration — Minecraft remote control & monitoring",
                 "# Managed by `mcctl init` and the GUI Settings tab; hand-editing is fine too.",
                 ""]
        for name, dc in sections:
            if name in _SECTION_DOC:
                lines.append(f"# {_SECTION_DOC[name]}")
            lines.append(f"[{name}]")
            for f in fields(dc):
                doc = _KEY_DOC.get(f.name)
                if doc:
                    lines.append(f"# {doc}")
                lines.append(f"{f.name} = {_toml_val(getattr(dc, f.name))}")
            lines.append("")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(p)  # atomic: never leave a half-written config
        self.path = p
        log.info("wrote config to %s", p)
        return p


# ---------------------------------------------------------------- comments for save()

_SECTION_DOC = {
    "server": "SSH target + remote layout. transport='local' runs against this machine (dev/test).",
    "backup": "Consistent world snapshots + GFS rotation.",
    "watchdog": "Self-healing daemon thresholds and alert sinks.",
    "metrics": "Prometheus textfile exporter (node_exporter/Grafana).",
    "llm": "AI analysis & chat — provider='anthropic' (Claude) or 'ollama' (local).",
    "ui": "Display preferences (log timestamp timezones).",
    "crafting": "Recipe browser + survival command-craft (`mcctl craft`, phone craft.* methods).",
}

_KEY_DOC = {
    "ssh_key": "Path to the SSH private key (-i). Empty = use ssh-agent / the default key.",
    "ssh_options": 'Extra raw ssh args, e.g. ["-o", "IdentityFile=~/.ssh/carborio"].',
    "transport": '"ssh" for the real server, "local" for dev/testing on this machine.',
    "start_command": "ServerPackCreator entry point (ServerStarterJar), NOT run.sh.",
    "stop_countdown": "In-game warning countdown (seconds) before a graceful stop.",
    "provider": '"anthropic" (Claude API, needs the anthropic package + API key) or "ollama" (local).',
    "api_key_env": "Env var holding the API key — mcctl never stores the key itself.",
    "ollama_model": "Model name as `ollama pull`ed it (pick from `ollama list`).",
    "ntfy_topic": "ntfy push topic (also a UnifiedPush distributor); empty disables push.",
    "timezone": 'Display zone for log timestamps (IANA name, or "" to show raw server time).',
    "player": "Your in-game name — the default receiver of crafted output.",
    "source_player": 'Whose inventory supplies the materials (empty = same as player).',
    "max_output_stack": "Hold-to-craft-max cap: the largest output, limited to one stack (1-64).",
    "include_containers": "Also show backpack/container counts when planning (never consumed).",
}


# ---------------------------------------------------------------- template

TEMPLATE = """\
# mcctl configuration — Minecraft remote control & monitoring
# Defaults target the CarborioLand stack (MMC5 / NeoForge 1.21.1 on an ARM64 OCI box).
# Every key shown here is optional; missing keys fall back to these defaults.

[server]
host = "{host}"
ssh_port = 22
user = "{user}"
# Path to the SSH private key (-i). Leave empty to use ssh-agent or the default key.
# Example: ssh_key = "~/.ssh/carborio"
ssh_key = ""
# Extra raw ssh args, e.g. ["-o", "IdentityFile=~/.ssh/carborio"]
ssh_options = []
# "ssh" for the real server, "local" runs everything against this machine (dev/testing)
transport = "ssh"
server_dir = "{server_dir}"
tmux_session = "{tmux_session}"
# ServerPackCreator entry point (ServerStarterJar). NOT run.sh — that was the old pack.
start_command = "bash start.sh"
log_file = "logs/latest.log"
world_dir = "world"
mc_port = 25565
rcon_port = 25575
java_home = "/opt/graalvm"
# Seconds to wait for "Done (…)!" on boot — modded first boot is slow.
start_timeout = 420
stop_timeout = 120
# In-game warning countdown before a graceful stop (seconds before shutdown).
stop_countdown = [30, 10, 5]

[backup]
remote_dir = "/opt/minecraft-backups"
prefix = "world"
# zstd strongly preferred (fast on ARM); gzip is the fallback.
compression = "zstd"
# Rotation: keep newest N, plus one per day for D days, plus one per ISO week for W weeks.
keep_recent = 8
keep_daily = 7
keep_weekly = 4
# Refuse to create a backup if the backup filesystem has less free space than this.
min_free_gb = 5.0
# Optional local mirror; `mcctl backup pull` rsyncs archives here.
local_dir = ""
# Paths excluded from `mcctl backup --full` (relative to server_dir).
full_excludes = ["logs", "crash-reports", ".cache", "libraries/.cache"]

[watchdog]
interval = 30
# Consider the server frozen when the log is older than this AND the console is dead.
freeze_log_age = 300
# Crash-loop breaker: at most max_restarts within restart_window seconds, then halt + alert.
max_restarts = 3
restart_window = 3600
backoff_base = 20
tps_alert = 15.0
heap_alert_pct = 92
# Built-in autosave (minutes, 0 = disabled — prefer the mcctl-autosave systemd timer).
autosave_minutes = 0
# Automatically run a 60s spark profiler when TPS stays below tps_alert.
auto_profile_on_lag = false
notify_desktop = true
# Discord-compatible webhook for crash/restart/alert messages, e.g. co-op channel.
webhook_url = ""
# ntfy push (https://ntfy.sh or self-hosted). ntfy is also a UnifiedPush
# distributor, so a topic here gives the phone app push for free. Leave
# ntfy_topic empty to disable. The topic is a public namespace on ntfy.sh —
# use an unguessable name, a self-hosted server, or an ntfy_token.
ntfy_url = "https://ntfy.sh"
ntfy_topic = ""
ntfy_token = ""

[metrics]
# Prometheus textfile exporter (`mcctl metrics export`, mcctl-metrics.timer).
# Point node_exporter --collector.textfile.directory at this file's directory.
# Empty => $XDG_STATE_HOME/mcctl/mcctl.prom.
prom_path = ""

[llm]
# AI log/crash/mod analysis & chat (`mcctl ai …`, GUI "AI"/"Chat" pages).
# provider = "anthropic" -> Claude API (needs the optional `anthropic` package
#                           and an API key in the environment; mcctl stores no keys).
# provider = "ollama"    -> a local LLM served by ollama (no API key, no data leaves
#                           the box). Nothing extra to install — mcctl talks HTTP.
provider = "anthropic"
# Anthropic model id (provider = "anthropic").
model = "claude-opus-4-8"
api_key_env = "ANTHROPIC_API_KEY"
# Output budget per answer (input is whatever context fits the analysis).
max_tokens = 8000
# How many latest.log lines `mcctl ai logs` sends as context.
log_lines = 400
# Local ollama server + model (provider = "ollama"). Run `ollama pull <model>` first.
ollama_url = "http://localhost:11434"
ollama_model = "llama3.1"

[ui]
# Log timestamps are rewritten from server_timezone into timezone for display,
# so a UTC server's logs read in your local wall-clock. IANA names; set timezone
# to "" to show raw server time. Defaults: UTC server -> São Paulo (UTC-3).
timezone = "America/Sao_Paulo"
server_timezone = "UTC"

[crafting]
# Recipe browser + survival "command-craft" (`mcctl craft`, the phone's craft.* RPCs).
# mcctl can't reach your client's crafting grid, so instead it reproduces the outcome:
# it reads your inventory, consumes the inputs with /clear, and grants the output with
# /give — only ever from loose (accessible) inventory, so it stays survival-honest.
# player    = your in-game name; the default receiver of the crafted output.
player = "GLEYSSON"
# source_player = whose inventory supplies the materials. Empty = same as `player`.
# Set this to craft FROM a shared storage account's inventory INTO `player`.
source_player = ""
# Hold-to-craft-max caps the output at one stack of this size (1-64).
max_output_stack = 64
# Also show backpack/container contents when planning a craft (informational only —
# items nested in a backpack can't be auto-consumed by /clear, so they're never used).
include_containers = true
# UI contract: holding the craft button this many ms triggers craft-max.
hold_ms = 3000
"""


def write_template(path: Path | str | None = None, *, force: bool = False,
                   host: str | None = None, user: str | None = None,
                   server_dir: str | None = None, tmux_session: str | None = None) -> Path:
    p = Path(path) if path else Config.default_path()
    if p.exists() and not force:
        raise ConfigError(f"{p} already exists (use --force to overwrite)")
    defaults = ServerCfg()
    text = TEMPLATE.format(
        host=host or defaults.host,
        user=user or defaults.user,
        server_dir=server_dir or defaults.server_dir,
        tmux_session=tmux_session or defaults.tmux_session,
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
