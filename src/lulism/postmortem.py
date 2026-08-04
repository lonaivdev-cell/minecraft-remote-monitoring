"""Post-mortem: a deterministic "what went wrong" built from evidence mcctl
already collects — no LLM, no API key, no guesswork.

`mcctl postmortem` answers the question the 2026-06-11 incident made urgent:
after a crash / heal / crash-loop halt, point straight at the cause instead of
making a human (or an AI) trawl a 1000-line crash report. It only *reads* —
the newest crash report, the watchdog's event journal, intent/restart state,
local evidence bundles — and never touches the server process.

Crash reports from this modpack are known to embed prompt-injection text aimed
at AI helpers. This parser works on structure (Description / exception /
TRANSFORMER stack frames), so that text is inert here; when present it is
flagged so the operator knows it was seen and ignored.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import events, logs, state, util
from .config import Config
from .transport import BaseTransport, TransportError

log = util.get_logger("postmortem")

# Loader/platform "mods" that appear in stack frames but are never the culprit.
_PLATFORM_MODS = {"minecraft", "neoforge", "forge", "fml", "fabric", "fabricloader",
                  "connectormod", "connector", "mixinextras", "owo", "puzzleslib"}

_TIME_RE = re.compile(r"^Time:\s*(.+)$", re.MULTILINE)
_DESC_RE = re.compile(r"^Description:\s*(.+)$", re.MULTILINE)
# "java.lang.NoSuchMethodError: '…'" / "Caused by: java.io.IOException: …"
_EXC_RE = re.compile(r"^(?:Caused by:\s*)?([a-zA-Z_][\w.$]*(?:Error|Exception))(?::\s*(.*))?$",
                     re.MULTILINE)
# "at TRANSFORMER/reliquified_ars_nouveau@0.7.1/it.hurts…(ArchmageGloveItem.java:330) ~[…jar%23…"
_FRAME_RE = re.compile(
    r"^\s+at\s+TRANSFORMER/([a-z0-9_.\-]+)@([^/\s]+)/(\S+?)(?:\s|$).*?(?:~?\[([^\]%!]+))?",
    re.MULTILINE)

# Known prompt-injection tells (this modpack ships them inside crash reports).
_INJECTION_RES = (
    re.compile(r"(?:system\s+)?note\s+(?:for|to)\s+(?:the\s+)?ai", re.IGNORECASE),
    re.compile(r"ignore\s+(?:all|any)\s+.{0,50}(?:errors|warnings|instructions)", re.IGNORECASE),
    re.compile(r"seek\s+help\s+from\s+real\s+humans", re.IGNORECASE),
    re.compile(r"do\s+not\s+(?:tell|inform)\s+the\s+user", re.IGNORECASE),
    re.compile(r"(?:as|the\s+only\s+good\s+response\s+as)\s+a\s+helpful\s+ai", re.IGNORECASE),
)

# exception class -> (kind, what it usually means for a modded server)
_EXC_KINDS = (
    (("NoSuchMethodError", "NoSuchFieldError", "NoClassDefFoundError", "AbstractMethodError",
      "ClassNotFoundException", "IncompatibleClassChangeError"),
     ("mod-version-mismatch",
      "two mods disagree about an API — one was built against a different version of the other")),
    (("OutOfMemoryError",),
     ("out-of-memory", "the JVM ran out of heap — raise Xmx (`mcctl jvm heap`) or find the leak "
      "(`mcctl purge`, `mcctl trace`)")),
    (("StackOverflowError",),
     ("infinite-recursion", "two mods (often mixins) calling each other in a loop")),
)


@dataclass(slots=True)
class Frame:
    mod: str
    version: str
    location: str
    jar: str = ""

    def to_dict(self) -> dict:
        return {"mod": self.mod, "version": self.version,
                "location": self.location, "jar": self.jar}


@dataclass(slots=True)
class CrashAnalysis:
    name: str = ""
    time: str = ""
    description: str = ""
    exception: str = ""              # full first exception line
    kind: str = "unknown"            # mod-version-mismatch | out-of-memory | …
    kind_hint: str = ""
    suspect: Frame | None = None     # first non-platform mod on the stack
    mod_frames: list[Frame] = field(default_factory=list)
    injection: list[str] = field(default_factory=list)  # matched snippets, for transparency

    def to_dict(self) -> dict:
        return {"name": self.name, "time": self.time, "description": self.description,
                "exception": self.exception, "kind": self.kind, "kind_hint": self.kind_hint,
                "suspect": self.suspect.to_dict() if self.suspect else None,
                "mod_frames": [f.to_dict() for f in self.mod_frames],
                "injection": list(self.injection)}


def analyze_crash_text(name: str, text: str) -> CrashAnalysis:
    """Pure: structural analysis of one crash report. Never raises on weird input."""
    a = CrashAnalysis(name=name)
    m = _TIME_RE.search(text)
    a.time = m.group(1).strip() if m else ""
    m = _DESC_RE.search(text)
    a.description = m.group(1).strip() if m else ""
    m = _EXC_RE.search(text)
    if m:
        a.exception = (m.group(1) + (f": {m.group(2)}" if m.group(2) else "")).strip()
        cls = m.group(1).rsplit(".", 1)[-1]
        for classes, (kind, hint) in _EXC_KINDS:
            if cls in classes:
                a.kind, a.kind_hint = kind, hint
                break

    seen: set[str] = set()
    for fm in _FRAME_RE.finditer(text):
        mod, ver, loc, jar = fm.group(1), fm.group(2), fm.group(3), fm.group(4) or ""
        if mod in _PLATFORM_MODS or mod in seen:
            continue
        seen.add(mod)
        frame = Frame(mod=mod, version=ver, location=loc, jar=jar.strip())
        a.mod_frames.append(frame)
        if a.suspect is None:
            a.suspect = frame
        if len(a.mod_frames) >= 8:
            break

    for rx in _INJECTION_RES:
        m = rx.search(text)
        if m:
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            snippet = text[start:end if end != -1 else None].strip()
            a.injection.append(snippet[:120])
    return a


# ---------------------------------------------------------------- the report

@dataclass(slots=True)
class Postmortem:
    crash: CrashAnalysis | None = None
    crash_error: str = ""                      # why the crash report couldn't be fetched
    events: list[dict] = field(default_factory=list)
    watchdog: dict = field(default_factory=dict)   # armed/desired/halted + restarts_24h
    evidence: list[str] = field(default_factory=list)  # local bundle dirs, newest first
    summary: list[str] = field(default_factory=list)  # the verdict, one line each
    next_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"crash": self.crash.to_dict() if self.crash else None,
                "crash_error": self.crash_error, "events": self.events,
                "watchdog": self.watchdog, "evidence": self.evidence,
                "summary": self.summary, "next_steps": self.next_steps}


def _summarize(pm: Postmortem) -> None:
    c = pm.crash
    if c and (c.exception or c.description):
        when = f" at {c.time}" if c.time else ""
        pm.summary.append(f"crash{when}: {c.exception or c.description}")
        if c.kind_hint:
            pm.summary.append(f"pattern: {c.kind} — {c.kind_hint}")
        if c.suspect:
            jar = f" ({c.suspect.jar})" if c.suspect.jar else ""
            pm.summary.append(f"suspected mod: {c.suspect.mod} {c.suspect.version}{jar} — "
                              f"topmost non-platform frame: {c.suspect.location}")
            pm.next_steps.append(
                f"remove or update {c.suspect.jar or c.suspect.mod} on the SERVER and every "
                "CLIENT (mod-list parity, or clients get a connection mismatch)")
        if c.injection:
            pm.summary.append("note: this crash report contains AI-prompt-injection text "
                              "(known for this modpack) — it was detected and ignored; the "
                              "diagnosis above comes from the stack trace only")
    elif pm.crash_error:
        pm.summary.append(f"could not read crash reports: {pm.crash_error}")
    else:
        pm.summary.append("no crash reports on the server")

    wd = pm.watchdog
    if wd.get("halted"):
        pm.summary.append(f"watchdog HALTED: {wd.get('restarts_24h', 0)} self-heal restarts in "
                          "24h tripped the crash-loop breaker — it will stay down until "
                          "`mcctl start` (which also clears the halt)")
    elif wd.get("restarts_24h"):
        pm.summary.append(f"watchdog self-healed {wd['restarts_24h']} time(s) in the last 24h")
    halts = [e for e in pm.events if e.get("kind") == "crash-loop-halt"]
    fails = [e for e in pm.events if e.get("kind") == "restart-failed"]
    if fails:
        pm.summary.append(f"{len(fails)} self-heal attempt(s) FAILED — see the event log")
    if halts and not wd.get("halted"):
        pm.summary.append("a crash-loop halt occurred in this window (since cleared)")

    pm.next_steps.append("deeper dive: `mcctl ai crash` (AI root-cause) or `mcctl logs crash`")
    if pm.evidence:
        pm.next_steps.append(f"local evidence bundles: {pm.evidence[0]}")


def build_postmortem(t: BaseTransport, cfg: Config, *, crash_name: str = "",
                     window_h: float = 48.0, now: float | None = None) -> Postmortem:
    """Assemble the report. Every source is best-effort: an unreachable server
    still yields the watchdog/event side of the story."""
    pm = Postmortem()
    now = now if now is not None else time.time()

    try:
        name, text = logs.crash_get(t, cfg, crash_name)
        if name:
            pm.crash = analyze_crash_text(name, text)
    except (TransportError, ValueError) as e:
        pm.crash_error = str(e)

    pm.events = events.read(since=now - window_h * 3600)[-15:]
    st = state.load()
    pm.watchdog = {
        "armed": st.get("armed", False),
        "desired": st.get("desired", "down"),
        "halted": st.get("halted", False),
        "restarts_24h": len([x for x in st.get("restarts", []) if x > now - 86400]),
    }
    try:
        bundles = sorted((p for p in util.crashes_dir().iterdir() if p.is_dir()), reverse=True)
        pm.evidence = [str(p) for p in bundles[:3]]
    except OSError:
        pass

    _summarize(pm)
    return pm
