"""World backups: consistent snapshots, GFS-style rotation, pull, verify, restore.

Snapshot procedure when the server is up (the only way to get a consistent
copy of a live world):  save-off  ->  save-all flush  ->  wait "Saved the
game"  ->  tar | zstd on the server  ->  archive integrity test  ->  save-on.
save-on lives in a `finally`: no code path may leave autosave disabled.

Rotation keeps: the newest `keep_recent`, plus the newest archive of each of
the last `keep_daily` days, plus the newest of each of the last `keep_weekly`
ISO weeks. Pure function, fully unit-tested.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import util
from .config import Config
from .console import Console, ConsoleError
from .transport import BaseTransport, TransportError, q

log = util.get_logger("backup")

_STAMP = "%Y%m%d-%H%M%S"


class BackupError(RuntimeError):
    pass


@dataclass(slots=True)
class BackupEntry:
    name: str
    path: str
    ts: dt.datetime
    size: int
    full: bool = False

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.ts.timestamp())


def make_name(prefix: str, now: dt.datetime, *, full: bool = False, compression: str = "zstd") -> str:
    kind = "full" if full else "world"
    ext = "tar.zst" if compression == "zstd" else "tar.gz"
    return f"{prefix}-{kind}-{now.strftime(_STAMP)}.{ext}"


def parse_name(prefix: str, name: str) -> tuple[dt.datetime, bool] | None:
    """Return (timestamp, is_full) or None for foreign files."""
    if not name.startswith(f"{prefix}-"):
        return None
    rest = name[len(prefix) + 1:]
    for kind, full in (("world-", False), ("full-", True)):
        if rest.startswith(kind):
            stamp = rest[len(kind):].split(".")[0]
            try:
                return dt.datetime.strptime(stamp, _STAMP), full
            except ValueError:
                return None
    return None


def plan_rotation(entries: list[BackupEntry], *, keep_recent: int, keep_daily: int,
                  keep_weekly: int, now: dt.datetime | None = None
                  ) -> tuple[list[BackupEntry], list[BackupEntry]]:
    """(keep, delete). Full backups are never auto-deleted — they were deliberate."""
    now = now or dt.datetime.now()
    snaps = sorted([e for e in entries if not e.full], key=lambda e: e.ts, reverse=True)
    keep: set[str] = set()

    keep.update(e.name for e in snaps[:keep_recent])

    by_day: dict[dt.date, BackupEntry] = {}
    for e in snaps:  # newest first, so first hit per day wins
        by_day.setdefault(e.ts.date(), e)
    for day in sorted(by_day, reverse=True)[:keep_daily]:
        keep.add(by_day[day].name)

    by_week: dict[tuple[int, int], BackupEntry] = {}
    for e in snaps:
        iso = e.ts.isocalendar()
        by_week.setdefault((iso.year, iso.week), e)
    for week in sorted(by_week, reverse=True)[:keep_weekly]:
        keep.add(by_week[week].name)

    kept = [e for e in entries if e.full or e.name in keep]
    dropped = [e for e in snaps if e.name not in keep]
    return kept, dropped


def full_backup_excludes(cfg: Config) -> list[str]:
    """Tar excludes for --full archives. The backup dir itself is excluded only
    when it actually lives inside server_dir (tar paths are relative to -C)."""
    out = list(cfg.backup.full_excludes)
    server = cfg.server.server_dir.rstrip("/")
    remote = cfg.backup.remote_dir.rstrip("/")
    if remote.startswith(server + "/"):
        out.append(remote[len(server) + 1:])  # same non-anchored form as the others
    return out


class BackupManager:
    def __init__(self, cfg: Config, transport: BaseTransport, console: Console | None = None):
        self.cfg = cfg
        self.t = transport
        self.console = console or Console(cfg, transport)

    # ---------------------------------------------------------------- listing

    def list(self) -> list[BackupEntry]:
        b = self.cfg.backup
        r = self.t.run(
            f"for f in {q(b.remote_dir)}/{q(b.prefix)}-*.tar.*; do\n"
            '  [ -f "$f" ] || continue\n'
            '  printf "%s|%s\\n" "$f" "$(stat -c %s "$f")"\n'
            "done\n",
            timeout=30,
        )
        entries: list[BackupEntry] = []
        for line in r.out.splitlines():
            if "|" not in line:
                continue
            path, size = line.rsplit("|", 1)
            name = path.rsplit("/", 1)[-1]
            parsed = parse_name(b.prefix, name)
            if parsed and size.strip().isdigit():
                ts, full = parsed
                entries.append(BackupEntry(name, path, ts, int(size), full))
        return sorted(entries, key=lambda e: e.ts, reverse=True)

    def latest(self) -> BackupEntry | None:
        entries = self.list()
        return entries[0] if entries else None

    # ---------------------------------------------------------------- create

    @contextlib.contextmanager
    def _save_paused(self):
        """save-off for the duration; save-on is guaranteed on every exit path."""
        running = self._server_running()
        if running:
            try:
                offset = self.console.log_size()
                self.console.send("save-off", timeout=10)
                self.console.send("save-all flush", timeout=15)
                self.console.wait_in_log(r"Saved the game", offset, timeout=60)
                log.info("world flushed to disk; autosave paused for snapshot")
            except (ConsoleError, TransportError) as e:
                # try to undo a possibly-applied save-off before bailing
                with contextlib.suppress(Exception):
                    self.console.send("save-on", timeout=10)
                raise BackupError(f"could not flush/pause saves: {e}") from e
        try:
            yield
        finally:
            if running:
                with contextlib.suppress(Exception):
                    self.console.send("save-on", timeout=10)
                    log.info("autosave re-enabled")

    def _server_running(self) -> bool:
        from .server import ServerControl
        return ServerControl(self.cfg, self.t, self.console).find_pid() is not None

    def _disk_guard(self) -> None:
        b = self.cfg.backup
        r = self.t.run(
            f"mkdir -p {q(b.remote_dir)} && df -B1 --output=avail {q(b.remote_dir)} | tail -1",
            timeout=20, check=True,
        )
        avail = int(r.out.strip() or 0)
        need = int(b.min_free_gb * 1024**3)
        if avail < need:
            raise BackupError(
                f"only {util.human_bytes(avail)} free on backup filesystem "
                f"(min_free_gb={b.min_free_gb}) — prune backups or raise the limit"
            )

    def create(self, *, full: bool = False, dry: bool = False,
               progress: Callable[[str], None] | None = None) -> BackupEntry | None:
        b, s = self.cfg.backup, self.cfg.server
        comp = b.compression
        zstd_ok = self.t.run("command -v zstd >/dev/null", timeout=15).ok
        if comp == "zstd" and not zstd_ok:
            log.warning("zstd missing on server — falling back to gzip (apt install zstd)")
            comp = "gzip"
        name = make_name(b.prefix, dt.datetime.now(), full=full, compression=comp)
        target = f"{b.remote_dir}/{name}"
        if dry:
            log.info("dry-run: would create %s", target)
            return None
        self._disk_guard()

        if full:
            excludes = " ".join(f"--exclude={q(e)}" for e in full_backup_excludes(self.cfg))
            src = f"-C {q(s.server_dir)} {excludes} ."
        else:
            src = f"-C {q(s.server_dir)} {q(s.world_dir)}"

        pack = "zstd -T0 -q" if comp == "zstd" else "gzip"
        test = f"zstd -t -q {q(target + '.tmp')}" if comp == "zstd" else f"gzip -t {q(target + '.tmp')}"
        script = (
            "set -e -o pipefail\n"
            f"tar -cf - {src} | {pack} > {q(target + '.tmp')}\n"
            f"{test}\n"
            f"mv {q(target + '.tmp')} {q(target)}\n"
            f"stat -c %s {q(target)}\n"
        )
        if progress:
            progress(f"snapshotting {'instance' if full else s.world_dir} -> {name}")
        started = time.monotonic()
        with self._save_paused():
            try:
                r = self.t.run(script, timeout=3600, check=True)
            except TransportError as e:
                self.t.run(f"rm -f {q(target + '.tmp')}", timeout=20)
                raise BackupError(f"snapshot failed: {e}") from e
        size = int(r.out.strip().splitlines()[-1])
        took = time.monotonic() - started
        log.info("backup %s created (%s in %.0fs)", name, util.human_bytes(size), took)
        return BackupEntry(name, target, dt.datetime.now(), size, full)

    # ---------------------------------------------------------------- rotation / verify

    def prune(self, *, dry: bool = False) -> tuple[list[BackupEntry], list[BackupEntry]]:
        b = self.cfg.backup
        kept, dropped = plan_rotation(
            self.list(), keep_recent=b.keep_recent, keep_daily=b.keep_daily,
            keep_weekly=b.keep_weekly,
        )
        if dropped and not dry:
            files = " ".join(q(e.path) for e in dropped)
            self.t.run(f"rm -f -- {files}", timeout=60, check=True)
            log.info("pruned %d backup(s): %s", len(dropped), ", ".join(e.name for e in dropped))
        return kept, dropped

    def verify(self, name: str) -> bool:
        path = f"{self.cfg.backup.remote_dir}/{name}"
        if name.endswith(".zst"):
            return self.t.run(f"zstd -t -q {q(path)}", timeout=900).ok
        return self.t.run(f"gzip -t {q(path)}", timeout=900).ok

    # ---------------------------------------------------------------- pull / restore

    def pull(self, dest: str | None = None) -> int:
        d = dest or self.cfg.backup.local_dir
        if not d:
            raise BackupError("no destination: pass one or set backup.local_dir in the config")
        from pathlib import Path
        Path(d).expanduser().mkdir(parents=True, exist_ok=True)
        spec = self.t.remote_spec(f"{self.cfg.backup.remote_dir}/")
        return self.t.rsync(spec, str(Path(d).expanduser()) + "/")

    def restore(self, name: str) -> str:
        """Unpack a snapshot as the live world. Caller must have stopped the server.

        The current world is moved aside (never deleted) to world.pre-restore-<ts>.
        """
        b, s = self.cfg.backup, self.cfg.server
        path = f"{b.remote_dir}/{name}"
        if not self.t.exists(path):
            raise BackupError(f"no such backup on server: {name}")
        if self._server_running():
            raise BackupError("server is running — `mcctl stop` first")
        parsed = parse_name(b.prefix, name)
        if parsed and parsed[1]:
            raise BackupError("refusing to auto-restore a --full archive over the instance; "
                              "unpack it manually where you want it")
        if not self.verify(name):
            raise BackupError(f"archive {name} fails integrity check — NOT restoring")
        unpack = "zstd -dc" if name.endswith(".zst") else "gzip -dc"
        aside = f"{s.world_dir}.pre-restore-{dt.datetime.now().strftime(_STAMP)}"
        script = (
            "set -e -o pipefail\n"
            f"cd {q(s.server_dir)}\n"
            f"if [ -d {q(s.world_dir)} ]; then mv {q(s.world_dir)} {q(aside)}; fi\n"
            f"{unpack} {q(path)} | tar -xf -\n"
            f"test -d {q(s.world_dir)}\n"
        )
        self.t.run(script, timeout=3600, check=True)
        log.warning("restored %s; previous world preserved at %s/%s", name, s.server_dir, aside)
        return aside
