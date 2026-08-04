"""Shared plumbing: XDG paths, logging, locks, humanizers, terminal hygiene, notifications."""

from __future__ import annotations

import fcntl
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import stat
import subprocess
import urllib.request
from pathlib import Path

APP = "lulism"

log = logging.getLogger(APP)


# ---------------------------------------------------------------- XDG paths

def _xdg(env: str, fallback: str) -> Path:
    base = os.environ.get(env, "").strip()
    return Path(base) if base else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP


def runtime_dir() -> Path:
    """Short-path dir for SSH control sockets (unix socket paths are length-limited)."""
    base = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    d = (Path(base) / APP) if base else (cache_dir() / "run")
    return d


def crashes_dir() -> Path:
    return state_dir() / "crashes"


LEGACY_APP = "mcctl"


def _copy_filter(root: Path, *, skip_top: tuple[str, ...] = ()):
    """Build a `shutil.copytree(ignore=...)` callback for the legacy migration.

    Two things never get copied:

    * anything that is not a plain file or a directory. `runtime_dir()` falls
      back to ``cache_dir()/"run"`` whenever ``XDG_RUNTIME_DIR`` is unset (the
      normal case on a headless box), which puts **live SSH ControlMaster unix
      sockets inside the very tree being migrated**. `copytree` raises
      ``OSError: [Errno 6] No such device or address`` on a socket, and the
      half-written destination it leaves behind would then satisfy the
      ``dst.exists()`` guard and skip the migration permanently.
    * `skip_top` names at the top level — used to drop that ``run`` directory
      outright, since the design spec keeps `runtime_dir()` out of the
      migration entirely (it is ephemeral by definition).
    """
    def ignore(directory, names):
        here = Path(directory)
        skip = set(skip_top) if here == root else set()
        for name in names:
            if name in skip:
                continue
            try:
                st = (here / name).stat()  # follows symlinks: a dangling one raises
            except OSError:
                skip.add(name)
                continue
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                skip.add(name)  # socket / fifo / device: not migratable state
        return skip
    return ignore


def migrate_legacy_dirs() -> list[tuple[Path, Path]]:
    """Copy pre-2.0.0 mcctl XDG dirs to their lulism equivalents, once.

    Copies rather than moves so the mcctl trees remain a rollback path. Skips
    any destination that already exists, which makes this idempotent and means
    a hand-edited lulism config is never clobbered.

    Best-effort: a compatibility migration must never make the CLI unusable.
    cli.main() has to call this *before* the try/except that maps exceptions to
    exit codes (the migration must precede setup_logging()'s ensure_dirs()), so
    an exception escaping here is a raw traceback on every single command —
    including the phone's `mcctl agent`. Any copy failure is logged and skipped,
    and the partial destination is removed so a later run can retry.
    """
    pairs = (
        (_xdg("XDG_CONFIG_HOME", ".config") / LEGACY_APP, config_dir(), ()),
        (_xdg("XDG_STATE_HOME", ".local/state") / LEGACY_APP, state_dir(), ()),
        # "run" is runtime_dir()'s no-XDG_RUNTIME_DIR fallback: ephemeral SSH
        # control sockets, explicitly not migrated.
        (_xdg("XDG_CACHE_HOME", ".cache") / LEGACY_APP, cache_dir(), ("run",)),
    )
    done: list[tuple[Path, Path]] = []
    for src, dst, skip_top in pairs:
        if dst.exists() or not src.is_dir():
            continue
        try:
            shutil.copytree(src, dst, ignore=_copy_filter(src, skip_top=skip_top))
        except (OSError, shutil.Error) as e:
            log.warning("could not migrate %s -> %s: %s — continuing without it "
                        "(the original is untouched; retry after fixing the cause)",
                        src, dst, e)
            shutil.rmtree(dst, ignore_errors=True)  # never leave a partial copy behind
            continue
        log.info("migrated %s -> %s (the original is kept as a rollback path)", src, dst)
        done.append((src, dst))
    return done


def metrics_path() -> Path:
    return state_dir() / "metrics.jsonl"


def ensure_dirs() -> None:
    for d in (config_dir(), state_dir(), cache_dir(), crashes_dir()):
        d.mkdir(parents=True, exist_ok=True)
    runtime_dir().mkdir(parents=True, exist_ok=True, mode=0o700)


# ---------------------------------------------------------------- logging

def setup_logging(verbosity: int = 0) -> None:
    """File log always captures DEBUG; console level scales with -v."""
    ensure_dirs()
    root = logging.getLogger(APP)
    if root.handlers:  # already configured (tests / repeated calls)
        return
    root.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(
        state_dir() / "lulism.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG)
    ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{APP}.{name}")


# ---------------------------------------------------------------- humanize

def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def human_duration(secs: int | float | None) -> str:
    if secs is None:
        return "?"
    s = int(secs)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


# ---------------------------------------------------------------- terminal hygiene

# Remote logs are untrusted input: strip ANSI/OSC escape sequences and C0 controls
# (except \n and \t) before they ever touch the local terminal.
_ANSI_RE = re.compile(
    r"""
    \x1b
    (?:
        \[ [0-?]* [ -/]* [@-~]      # CSI
      | \] .*? (?:\x07|\x1b\\)      # OSC, BEL or ST terminated
      | [PX^_] .*? \x1b\\           # DCS/SOS/PM/APC
      | [@-Z\\-_]                   # other 2-byte sequences
    )
    """,
    re.VERBOSE | re.DOTALL,
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MC_CODE_RE = re.compile("§.")  # Minecraft section-sign color/format codes


def sanitize_terminal(text: str) -> str:
    """Make remote text safe to print on a local terminal."""
    text = _ANSI_RE.sub("", text)
    return _CTRL_RE.sub("", text)


def strip_mc_codes(text: str) -> str:
    return _MC_CODE_RE.sub("", sanitize_terminal(text))


# ---------------------------------------------------------------- locking

class LockHeldError(RuntimeError):
    pass


class OpsLock:
    """flock-based mutex so two lulism invocations can't fight over start/stop/backup."""

    def __init__(self, name: str = "ops"):
        ensure_dirs()
        self._path = state_dir() / f"{name}.lock"
        self._fh = None

    def __enter__(self) -> OpsLock:
        self._fh = open(self._path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._fh.close()
            self._fh = None
            raise LockHeldError(
                "another lulism operation is already running (start/stop/backup/watchdog action)"
            ) from e
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- json state files

def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


# ---------------------------------------------------------------- notifications

# ntfy priority by urgency: critical => "urgent"(5), normal => "default"(3).
_NTFY_PRIORITY = {"critical": "urgent", "low": "low", "normal": "default"}
_NTFY_TAGS = {"critical": "rotating_light", "normal": "warning", "low": "information_source"}


def notify(title: str, body: str, *, desktop: bool = True, webhook_url: str = "",
           ntfy_url: str = "", ntfy_topic: str = "", ntfy_token: str = "",
           urgency: str = "normal") -> None:
    """Best-effort alerting: desktop notify-send, a Discord-compatible webhook,
    and/or an ntfy topic (which is also a UnifiedPush distributor → phone push)."""
    if desktop and shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "-a", APP, "-u", urgency, title, body],
                timeout=5, capture_output=True, check=False,
            )
        except Exception as e:  # noqa: BLE001 - alerting must never crash the caller
            log.debug("notify-send failed: %s", e)
    if webhook_url:
        try:
            payload = json.dumps({"content": f"**{title}**\n{body}"}).encode()
            req = urllib.request.Request(
                webhook_url, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": APP},
            )
            urllib.request.urlopen(req, timeout=5)  # noqa: S310 - user-configured URL
        except Exception as e:  # noqa: BLE001
            log.warning("webhook notification failed: %s", e)
    if ntfy_url and ntfy_topic:
        try:
            url = ntfy_url.rstrip("/") + "/" + ntfy_topic.lstrip("/")
            # Title/Tags headers must be latin-1-safe; ntfy reads them as ASCII.
            headers = {
                "User-Agent": APP,
                "Title": title.encode("ascii", "replace").decode("ascii"),
                "Priority": _NTFY_PRIORITY.get(urgency, "default"),
                "Tags": _NTFY_TAGS.get(urgency, "warning"),
            }
            if ntfy_token:
                headers["Authorization"] = f"Bearer {ntfy_token}"
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers)
            urllib.request.urlopen(req, timeout=5)  # noqa: S310 - user-configured URL
        except Exception as e:  # noqa: BLE001
            log.warning("ntfy notification failed: %s", e)


# ---------------------------------------------------------------- systemd units

def user_unit_dir() -> Path:
    """Per the systemd spec, user units live under $XDG_CONFIG_HOME/systemd/user."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def render_units(*, exe: str = "/usr/bin/lulism") -> dict[str, str]:
    """The unit files shipped in lulism/units/ (the PKGBUILD installs the same
    files verbatim), with ExecStart rewritten for non-/usr/bin installs (pipx).

    `exe` must be an **absolute path** to a `lulism` binary, and the default is
    absolute for the same reason: systemd resolves a bare `ExecStart=` filename
    against its own fixed search path, which does not include ~/.local/bin, so a
    relative exe fails with 203/EXEC on precisely the pipx deployment this
    project targets.

    An `exe` naming the deprecated `mcctl` shim is never honoured either, even
    if a caller passes one explicitly: a systemd unit is long-lived and must not
    hardcode a permanent dependency on a shim that is removed at 3.0.0. pipx
    installs the shim and the real entry point into the same bin directory, so
    the shim's `lulism` sibling is the right substitute. The clamp matches the
    exact basename ("mcctl"), not a suffix, so an unrelated install path that
    merely ends in those letters (e.g. a fork's /opt/foomcctl/bin/foomcctl)
    keeps its own exe untouched.

    Anything left that is not an absolute path named `lulism` falls back to
    /usr/bin/lulism rather than being written into a unit file.
    """
    from importlib import resources
    p = Path(exe)
    if p.name == "mcctl":
        p = p.parent / "lulism"
        log.debug("render_units: exe %r names the deprecated mcctl shim, using %s instead", exe, p)
    if p.name != "lulism" or not p.is_absolute():
        log.debug("render_units: exe %r is not an absolute lulism path, using /usr/bin/lulism", exe)
        p = Path("/usr/bin/lulism")
    resolved = str(p)
    units: dict[str, str] = {}
    for entry in (resources.files("lulism") / "units").iterdir():
        if not entry.name.endswith((".service", ".timer")):
            continue
        text = entry.read_text(encoding="utf-8")
        if resolved != "/usr/bin/lulism":
            text = text.replace("ExecStart=/usr/bin/lulism ", f"ExecStart={resolved} ")
        units[entry.name] = text
    return units


def legacy_unit_names() -> tuple[str, ...]:
    """The pre-2.0.0 unit names. All seven, not just the three the CLI hints at
    enabling — an operator may have enabled lulism-metrics.timer's predecessor
    by hand, and that is the one most often forgotten."""
    return (
        "mcctl-watchdog.service",
        "mcctl-autosave.service", "mcctl-autosave.timer",
        "mcctl-backup.service", "mcctl-backup.timer",
        "mcctl-metrics.service", "mcctl-metrics.timer",
    )


def _systemctl_user(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("%s failed: %s", " ".join(cmd), e)
        return 1


def systemd_daemon_reload(run=None) -> None:
    """`systemctl --user daemon-reload`, injectable so callers stay testable."""
    (run or _systemctl_user)(["systemctl", "--user", "daemon-reload"])


def migrate_units(run=None, *, daemon_reload: bool = True) -> list[str]:
    """Stop, disable and remove the pre-2.0.0 units. Returns what it removed.

    Ordering is the safety property: a box with both mcctl-watchdog.service and
    lulism-watchdog.service enabled has two restart authorities, which is the
    2026-06-11 outage. Every old unit is stopped and disabled before any new one
    is installed. `run` is injected so the ordering is testable without systemd.

    Stop and disable run for all seven names unconditionally — disabling a unit
    whose file is already gone is what clears a dangling enable symlink, which
    `pacman`'s `replaces=('mcctl')` path leaves behind. The **return value**, by
    contrast, lists only the unit files that actually existed and were deleted,
    so a caller cannot announce removals that never happened on a fresh install.

    `daemon_reload=False` hands the reload back to the caller, which matters for
    an install: reloading before the new unit files land leaves systemd unaware
    of them until somebody reloads a second time.
    """
    run = run or _systemctl_user

    names = legacy_unit_names()
    for unit in names:
        run(["systemctl", "--user", "stop", unit])
    for unit in names:
        run(["systemctl", "--user", "disable", unit])
    unit_dir = user_unit_dir()
    removed: list[str] = []
    for unit in names:
        path = unit_dir / unit
        if path.exists():
            path.unlink()
            removed.append(unit)
    if daemon_reload:
        systemd_daemon_reload(run)
    log.info("stopped and disabled %d pre-2.0.0 systemd units, removed %d unit file(s)",
             len(names), len(removed))
    return removed


# ---------------------------------------------------------------- misc

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
