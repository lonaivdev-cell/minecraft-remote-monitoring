"""Configuration: TOML at ~/.config/mcctl/config.toml, defaults = CarborioLand stack."""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import util

log = util.get_logger("config")


class ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class ServerCfg:
    host: str = "144.33.19.123"
    ssh_port: int = 22
    user: str = "ubuntu"
    ssh_options: list[str] = field(default_factory=list)  # extra raw ssh -o options
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


@dataclass(slots=True)
class LlmCfg:
    model: str = "claude-opus-4-8"                        # Anthropic model id
    api_key_env: str = "ANTHROPIC_API_KEY"                # env var holding the key (never stored)
    max_tokens: int = 8000                                # per-answer output budget
    log_lines: int = 400                                  # log tail size sent with `mcctl ai logs`


@dataclass(slots=True)
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    backup: BackupCfg = field(default_factory=BackupCfg)
    watchdog: WatchdogCfg = field(default_factory=WatchdogCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
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
                            ("watchdog", cfg.watchdog), ("llm", cfg.llm)):
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
            if section not in ("server", "backup", "watchdog", "llm"):
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
        llm = self.llm
        if not llm.model:
            problems.append("llm.model must not be empty")
        if not (256 <= llm.max_tokens <= 64000):
            problems.append("llm.max_tokens must be in [256, 64000]")
        if llm.log_lines < 10:
            problems.append("llm.log_lines must be >= 10")
        if problems:
            raise ConfigError("invalid config:\n  - " + "\n  - ".join(problems))

    def to_dict(self) -> dict:
        return {"server": asdict(self.server), "backup": asdict(self.backup),
                "watchdog": asdict(self.watchdog), "llm": asdict(self.llm)}


# ---------------------------------------------------------------- template

TEMPLATE = """\
# mcctl configuration — Minecraft remote control & monitoring
# Defaults target the CarborioLand stack (MMC5 / NeoForge 1.21.1 on an ARM64 OCI box).
# Every key shown here is optional; missing keys fall back to these defaults.

[server]
host = "{host}"
ssh_port = 22
user = "{user}"
# Extra ssh options, e.g. ["-o", "IdentityFile=~/.ssh/carborio"]
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

[llm]
# AI log/crash/mod analysis (`mcctl ai …`, GUI "AI" page). Needs the optional
# `anthropic` package and an API key in the environment — mcctl stores no keys.
model = "claude-opus-4-8"
api_key_env = "ANTHROPIC_API_KEY"
# Output budget per answer (input is whatever context fits the analysis).
max_tokens = 8000
# How many latest.log lines `mcctl ai logs` sends as context.
log_lines = 400
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
