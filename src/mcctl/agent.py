"""`mcctl agent` — a JSON-RPC 2.0 server over SSH stdio.

The phone (and any future client) opens one SSH channel, runs `mcctl agent`, and
speaks newline-delimited JSON-RPC. Every method reuses the same tested service
objects the CLI calls (`Ctx → ServerControl / BackupManager / Console / …`) —
nothing is reimplemented, only exposed. One brain, two faces.

Wire format: one compact JSON object per line, UTF-8, both directions.
  → {"jsonrpc":"2.0","id":1,"method":"status","params":{"fast":true}}
  ← {"jsonrpc":"2.0","id":1,"result":{...}}
  ← {"jsonrpc":"2.0","method":"event","params":{...}}   # server-initiated

Security: no new port, no stored credential — auth is the SSH key the client
already holds, the agent runs as the same unprivileged user, and the existing
ControlMaster transport is reused. Destructive methods require both a capability
granted in `agent.hello` and an explicit `confirm: true`.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import fields
from typing import IO, Any

from . import __version__, events, logs, metrics, state, util
from .assets import AssetError
from .backup import BackupEntry, BackupError, BackupManager
from .config import BackupCfg, CraftingCfg, LlmCfg, MetricsCfg, ServerCfg, UiCfg, WatchdogCfg
from .console import ConsoleError
from .crafting import CraftError
from .modconfig import ConfigFile as _ConfigFile
from .players import PlayerError, Players
from .props import PropError
from .server import ServerError, Status
from .spark import Spark, SparkError
from .transport import TransportError

log = util.get_logger("agent")

# Bump ONLY by a deliberate edit. The golden-schema test fails if the generated
# schema changes without a conscious decision here, so the client contract can
# never drift silently. Within a major version, changes must be additive.
AGENT_PROTOCOL = 1

# JSON-RPC reserved codes + mcctl app-level codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
APP_ERROR = -32000          # error.data.exit_code carries mcctl's exit vocabulary
CAP_REQUIRED = -32004
CONFIRM_REQUIRED = -32005

_MAX_LINE = 1_048_576       # 1 MiB guard on a single request line

# Registry: name -> {"fn", "params", "summary", "destructive", "capability"}.
METHODS: dict[str, dict[str, Any]] = {}


def method(name: str, *, params: dict[str, str] | None = None, summary: str = "",
           destructive: bool = False, capability: str | None = None):
    def deco(fn: Callable) -> Callable:
        METHODS[name] = {
            "fn": fn, "params": params or {}, "summary": summary,
            "destructive": destructive, "capability": capability,
        }
        return fn
    return deco


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------- serializers

def _entry(e: BackupEntry) -> dict:
    return {"name": e.name, "path": e.path, "ts": e.ts.isoformat(),
            "size": e.size, "full": e.full, "age_s": round(e.age_s, 1)}


def _players(pl) -> dict | None:
    if pl is None:
        return None
    return {"count": pl.count, "max": pl.max, "names": list(pl.names)}


# ================================================================ the server

class AgentServer:
    def __init__(self, ctx, *, stdout: IO[str] | None = None):
        self.ctx = ctx
        self._out = stdout if stdout is not None else sys.stdout
        self._wlock = threading.Lock()
        self.caps: set[str] = set()
        self._ev_stop = threading.Event()
        self._ev_thread: threading.Thread | None = None

    # ---------------------------------------------------------------- wire I/O

    def _write(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":"))
        with self._wlock:
            self._out.write(line + "\n")
            self._out.flush()

    def _emit_event(self, ev: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": "event", "params": ev})

    # ---------------------------------------------------------------- dispatch

    def handle_request(self, req: dict) -> dict | None:
        """Process one parsed request object; return a response, or None for a
        notification (a request without an id)."""
        rid = req.get("id")
        if req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return self._err(rid, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        name = req["method"]
        params = req.get("params") or {}
        if not isinstance(params, dict):
            return self._err(rid, INVALID_PARAMS, "params must be an object")
        spec = METHODS.get(name)
        if spec is None:
            return self._err(rid, METHOD_NOT_FOUND, f"unknown method: {name}")
        if spec["capability"] and spec["capability"] not in self.caps:
            return self._err(rid, CAP_REQUIRED,
                             f"method {name} needs the '{spec['capability']}' capability "
                             "(request it in agent.hello)")
        if spec["destructive"] and params.get("confirm") is not True:
            return self._err(rid, CONFIRM_REQUIRED,
                             f"method {name} is destructive; pass \"confirm\": true")
        try:
            result = spec["fn"](self, params)
        except RpcError as e:
            return self._err(rid, e.code, e.message, e.data)
        except (KeyError, TypeError, ValueError) as e:
            return self._err(rid, INVALID_PARAMS, str(e))
        except TransportError as e:
            return self._err(rid, APP_ERROR, str(e), {"exit_code": 3})
        except (ServerError, BackupError, ConsoleError, SparkError, PlayerError,
                PropError, CraftError, AssetError, metrics.MetricsError,
                util.LockHeldError) as e:
            return self._err(rid, APP_ERROR, str(e), {"exit_code": 1})
        except Exception as e:  # noqa: BLE001 - the agent must answer, never die mid-request
            log.exception("internal error handling %s", name)
            return self._err(rid, INTERNAL_ERROR, f"{type(e).__name__}: {e}")
        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid, code: int, message: str, data: Any = None) -> dict:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": rid, "error": err}

    # ---------------------------------------------------------------- run loop

    def serve(self, stdin: IO[str] | None = None) -> int:
        stream = stdin if stdin is not None else sys.stdin
        log.info("agent started (protocol %d)", AGENT_PROTOCOL)
        try:
            for raw in stream:
                raw = raw.strip()
                if not raw:
                    continue
                if len(raw) > _MAX_LINE:
                    self._write(self._err(None, INVALID_REQUEST, "request line too large"))
                    continue
                try:
                    req = json.loads(raw)
                except ValueError:
                    self._write(self._err(None, PARSE_ERROR, "invalid JSON"))
                    continue
                if not isinstance(req, dict):
                    self._write(self._err(None, INVALID_REQUEST, "request must be an object"))
                    continue
                resp = self.handle_request(req)
                if resp is not None:
                    self._write(resp)
                if getattr(self, "_shutdown", False):
                    break
        finally:
            self._stop_events()
            self.ctx.close()
        return 0

    # ---------------------------------------------------------------- events

    def _stop_events(self) -> None:
        self._ev_stop.set()
        if self._ev_thread:
            self._ev_thread.join(timeout=2)
            self._ev_thread = None

    def _start_events(self, since: float | None) -> None:
        self._stop_events()
        self._ev_stop = threading.Event()

        def loop() -> None:
            last = since
            for ev in events.read(since=last):
                last = ev.get("ts", last)
                self._emit_event(ev)
            while not self._ev_stop.is_set():
                for ev in events.read(since=last):
                    last = ev.get("ts", last)
                    self._emit_event(ev)
                self._ev_stop.wait(1.0)

        self._ev_thread = threading.Thread(target=loop, name="agent-events", daemon=True)
        self._ev_thread.start()


# ================================================================ methods

@method("agent.hello", params={"capabilities": "list[str]"},
        summary="Handshake: negotiate capabilities, learn protocol + server version.")
def _hello(srv: AgentServer, params: dict) -> dict:
    want = params.get("capabilities") or []
    srv.caps = {c for c in want if c in ("actions", "destructive")}
    return {
        "protocol": AGENT_PROTOCOL,
        "mcctl_version": __version__,
        "capabilities": sorted(srv.caps),
        "methods": sorted(METHODS),
    }


@method("agent.ping", summary="Liveness check.")
def _ping(srv: AgentServer, params: dict) -> dict:
    return {"pong": time.time()}


@method("agent.schema", summary="The versioned, machine-readable contract.")
def _schema(srv: AgentServer, params: dict) -> dict:
    return build_schema()


@method("agent.shutdown", summary="Close the session cleanly.")
def _shutdown(srv: AgentServer, params: dict) -> dict:
    srv._shutdown = True
    return {"ok": True}


@method("status", params={"fast": "bool"},
        summary="Full server status (process/tmux/port/players/TPS/heap/host/backup).")
def _status(srv: AgentServer, params: dict) -> dict:
    return srv.ctx.ctl.status(full=not params.get("fast", False)).to_dict()


@method("start", summary="Start the server in tmux and wait for ready.",
        capability="actions")
def _start(srv: AgentServer, params: dict) -> dict:
    with util.OpsLock():
        srv.ctx.ctl.start(wait=not params.get("no_wait", False))
    return {"ok": True, "status": srv.ctx.ctl.status(full=False).to_dict()}


@method("stop", params={"now": "bool", "reason": "str"},
        summary="Graceful stop (player countdown → flush → stop → escalate).",
        capability="actions")
def _stop(srv: AgentServer, params: dict) -> dict:
    with util.OpsLock():
        srv.ctx.ctl.stop(now=params.get("now", False), reason=params.get("reason", ""))
    return {"ok": True}


@method("restart", params={"now": "bool", "reason": "str"},
        summary="Stop then start.", capability="actions")
def _restart(srv: AgentServer, params: dict) -> dict:
    with util.OpsLock():
        srv.ctx.ctl.restart(now=params.get("now", False),
                            reason=params.get("reason") or "restart")
    return {"ok": True}


@method("kill", summary="Emergency stop (no countdown, no save).",
        capability="actions", destructive=True)
def _kill(srv: AgentServer, params: dict) -> dict:
    with util.OpsLock():
        srv.ctx.ctl.kill()
    return {"ok": True}


@method("save", params={"skip_if_down": "bool"},
        summary="save-all flush and confirm.", capability="actions")
def _save(srv: AgentServer, params: dict) -> dict:
    if srv.ctx.ctl.find_pid() is None:
        if params.get("skip_if_down"):
            return {"ok": True, "saved": False, "detail": "server down"}
        raise RpcError(APP_ERROR, "server is not running", {"exit_code": 1})
    offset = srv.ctx.console.log_size()
    srv.ctx.console.send("save-all flush", timeout=15)
    hit = srv.ctx.console.wait_in_log(r"Saved the game", offset, timeout=60)
    return {"ok": True, "saved": bool(hit)}


@method("cmd", params={"command": "str"},
        summary="Run an arbitrary console command (rcon preferred, tmux fallback).",
        capability="actions")
def _cmd(srv: AgentServer, params: dict) -> dict:
    command = params["command"]
    if not isinstance(command, str) or not command.strip():
        raise RpcError(INVALID_PARAMS, "command must be a non-empty string")
    return {"output": srv.ctx.console.send(command).strip()}


@method("tps", summary="spark TPS/MSPT/CPU.")
def _tps(srv: AgentServer, params: dict) -> dict:
    return Spark(srv.ctx.console).tps().to_dict()


@method("health", summary="spark memory/disk health.")
def _health(srv: AgentServer, params: dict) -> dict:
    return Spark(srv.ctx.console).health().to_dict()


@method("profile", params={"seconds": "int"},
        summary="Run the spark profiler, return the viewer URL.", capability="actions")
def _profile(srv: AgentServer, params: dict) -> dict:
    return {"url": Spark(srv.ctx.console).profile(int(params.get("seconds", 60)))}


@method("purge", summary="jcmd GC.run with a garbage-vs-leak verdict.", capability="actions")
def _purge(srv: AgentServer, params: dict) -> dict:
    pid = srv.ctx.ctl.find_pid()
    if pid is None:
        raise RpcError(APP_ERROR, "server is not running", {"exit_code": 1})
    return metrics.purge(srv.ctx.t, srv.ctx.cfg, pid).to_dict()


@method("players.list", summary="Players currently online.")
def _players_list(srv: AgentServer, params: dict) -> dict | None:
    return _players(srv.ctx.console.players())


@method("players.whitelist", params={"name": "str", "action": "add|remove|on|off"},
        summary="Whitelist add/remove or toggle enforcement.", capability="actions")
def _players_whitelist(srv: AgentServer, params: dict) -> dict:
    p = Players(srv.ctx.cfg, srv.ctx.t, srv.ctx.console)
    action = params.get("action", "add")
    name = params.get("name", "")
    fn = {"add": lambda: p.whitelist_add(name), "remove": lambda: p.whitelist_remove(name),
          "on": p.whitelist_on, "off": p.whitelist_off}.get(action)
    if fn is None:
        raise RpcError(INVALID_PARAMS, "action must be add|remove|on|off")
    if action in ("add", "remove") and not name:
        raise RpcError(INVALID_PARAMS, "name is required for add/remove")
    return {"ok": True, "output": fn().strip()}


@method("players.op", params={"name": "str", "deop": "bool"},
        summary="Grant or revoke operator.", capability="actions")
def _players_op(srv: AgentServer, params: dict) -> dict:
    p = Players(srv.ctx.cfg, srv.ctx.t, srv.ctx.console)
    name = params["name"]
    out = p.deop(name) if params.get("deop") else p.op(name)
    return {"ok": True, "output": out.strip()}


@method("players.kick", params={"name": "str", "reason": "str"},
        summary="Kick a player.", capability="actions")
def _players_kick(srv: AgentServer, params: dict) -> dict:
    p = Players(srv.ctx.cfg, srv.ctx.t, srv.ctx.console)
    return {"ok": True, "output": p.kick(params["name"], params.get("reason", "")).strip()}


@method("players.ban", params={"name": "str", "reason": "str"},
        summary="Ban a player.", capability="actions", destructive=True)
def _players_ban(srv: AgentServer, params: dict) -> dict:
    p = Players(srv.ctx.cfg, srv.ctx.t, srv.ctx.console)
    return {"ok": True, "output": p.ban(params["name"], params.get("reason", "")).strip()}


@method("backup.create", params={"full": "bool"},
        summary="Consistent snapshot + GFS rotation.", capability="actions")
def _backup_create(srv: AgentServer, params: dict) -> dict:
    bm = BackupManager(srv.ctx.cfg, srv.ctx.t, srv.ctx.console)
    with util.OpsLock():
        e = bm.create(full=params.get("full", False))
    return {"ok": True, "entry": _entry(e) if e else None}


@method("backup.list", summary="List archives (newest first).")
def _backup_list(srv: AgentServer, params: dict) -> dict:
    bm = BackupManager(srv.ctx.cfg, srv.ctx.t)
    return {"backups": [_entry(e) for e in bm.list()]}


@method("backup.prune", summary="Apply the rotation policy now.", capability="actions")
def _backup_prune(srv: AgentServer, params: dict) -> dict:
    bm = BackupManager(srv.ctx.cfg, srv.ctx.t)
    with util.OpsLock():
        kept, removed = bm.prune()
    return {"kept": [e.name for e in kept], "removed": [e.name for e in removed]}


@method("backup.verify", params={"name": "str"},
        summary="Integrity-test one archive.")
def _backup_verify(srv: AgentServer, params: dict) -> dict:
    bm = BackupManager(srv.ctx.cfg, srv.ctx.t)
    return {"ok": bm.verify(params["name"])}


@method("backup.restore", params={"name": "str"},
        summary="Replace the live world with a snapshot (refuses a running server).",
        capability="destructive", destructive=True)
def _backup_restore(srv: AgentServer, params: dict) -> dict:
    bm = BackupManager(srv.ctx.cfg, srv.ctx.t)
    with util.OpsLock():
        moved = bm.restore(params["name"])
    return {"ok": True, "previous_world": moved}


@method("logs.tail", params={"lines": "int", "crash": "bool", "name": "str"},
        summary="Tail latest.log, or fetch a crash report.")
def _logs_tail(srv: AgentServer, params: dict) -> dict:
    if params.get("crash"):
        name, text = logs.crash_get(srv.ctx.t, srv.ctx.cfg, params.get("name", ""))
        return {"name": name, "lines": text.splitlines()}
    n = int(params.get("lines", 50))
    text = logs.tail(srv.ctx.t, srv.ctx.cfg, lines=n)
    return {"lines": text.splitlines()}


@method("logs.crashes", params={"limit": "int"},
        summary="List recent crash reports.")
def _logs_crashes(srv: AgentServer, params: dict) -> dict:
    rows = logs.crash_list(srv.ctx.t, srv.ctx.cfg, limit=int(params.get("limit", 15)))
    return {"crashes": [{"name": n, "size": sz, "mtime": mt} for n, sz, mt in rows]}


@method("postmortem", params={"crash": "str"},
        summary="Deterministic what-went-wrong: crash report + events + watchdog history.")
def _postmortem(srv: AgentServer, params: dict) -> dict:
    from . import postmortem
    return postmortem.build_postmortem(
        srv.ctx.t, srv.ctx.cfg, crash_name=params.get("crash", "")).to_dict()


@method("metrics.history", params={"n": "int"},
        summary="Recent recorded metric samples (TPS/MSPT/heap/RAM/players).")
def _metrics_history(srv: AgentServer, params: dict) -> dict:
    return {"samples": metrics.read_samples(int(params.get("n", 120)))}


@method("props.list", summary="server.properties (rcon.password masked).")
def _props_list(srv: AgentServer, params: dict) -> dict:
    from . import props as P
    pf = P.load_props(srv.ctx.t, srv.ctx.cfg)
    out = {}
    for k, v in pf.items():
        out[k] = "********" if k == "rcon.password" and v else v
    return {"props": out}


@method("props.get", params={"key": "str"}, summary="One server.properties value.")
def _props_get(srv: AgentServer, params: dict) -> dict:
    from . import props as P
    pf = P.load_props(srv.ctx.t, srv.ctx.cfg)
    return {"key": params["key"], "value": pf.get(params["key"])}


@method("props.set", params={"key": "str", "value": "str"},
        summary="Set a validated server.properties key (atomic, .bak kept).",
        capability="destructive", destructive=True)
def _props_set(srv: AgentServer, params: dict) -> dict:
    from . import props as P
    key, value = params["key"], params["value"]
    value = P.validate_prop(key, value)
    pf = P.load_props(srv.ctx.t, srv.ctx.cfg)
    new_pf = P.PropertiesFile.parse(pf.render())
    new_pf.set(key, value)
    P.save_props(srv.ctx.t, srv.ctx.cfg, new_pf)
    return {"ok": True, "key": key, "value": value}


@method("jvm.show", summary="JAVA path + Xms/Xmx + key launch variables.")
def _jvm_show(srv: AgentServer, params: dict) -> dict:
    from . import props as P
    text = P.load_variables(srv.ctx.t, srv.ctx.cfg)
    args = P.get_var(text, "JAVA_ARGS") or ""
    xms, xmx = P.parse_heap(args)
    return {"java": P.get_var(text, "JAVA"), "xms": xms, "xmx": xmx, "java_args": args}


@method("jvm.heap", params={"size": "str"},
        summary="Rewrite Xms=Xmx (preserves Aikar's flags).",
        capability="destructive", destructive=True)
def _jvm_heap(srv: AgentServer, params: dict) -> dict:
    from . import props as P
    text = P.load_variables(srv.ctx.t, srv.ctx.cfg)
    P.save_variables(srv.ctx.t, srv.ctx.cfg, P.set_heap(text, params["size"]))
    return {"ok": True, "size": params["size"]}


@method("mods.list", summary="Installed mods (id/version/size from the jars).")
def _mods_list(srv: AgentServer, params: dict) -> dict:
    from . import mods as M
    return {"mods": [m.to_dict() for m in M.list_mods(srv.ctx.t, srv.ctx.cfg)]}


@method("recipes.search",
        params={"query": "str", "limit": "int", "offset": "int", "craftable": "bool",
                "player": "str"},
        summary="Search recipes (crafting/smelting/blasting/smoking/campfire/stonecutting/"
                "smithing) from the mod jars + datapacks. Page the whole pack with offset. "
                "Set craftable=true to keep only what `player` can craft from live inventory.")
def _recipes_search(srv: AgentServer, params: dict) -> dict:
    from . import crafting
    recipes, truncated = crafting.search_recipes(
        srv.ctx.t, srv.ctx.cfg, query=str(params.get("query", "")),
        limit=int(params.get("limit", 60)), offset=int(params.get("offset", 0)))
    if params.get("craftable"):
        tags = crafting.load_tag_map(srv.ctx.t, srv.ctx.cfg)
        recipes = crafting.craftable_filter(
            srv.ctx.console, srv.ctx.cfg, recipes,
            player=str(params.get("player", "")), tags=tags)
    return {"recipes": [r.to_dict() for r in recipes], "truncated": truncated}


@method("recipes.cost", params={"id": "str", "count": "int", "max_depth": "int"},
        summary="Recipe-tree cost: total base materials + leftovers to craft `count` of a "
                "recipe, recursively expanding craftable intermediates (EMI-style).")
def _recipes_cost(srv: AgentServer, params: dict) -> dict:
    from . import crafting
    rid = params.get("id", "")
    if not isinstance(rid, str) or not rid.strip():
        raise RpcError(INVALID_PARAMS, "id must be a non-empty string")
    cb = crafting.recipe_cost(
        srv.ctx.t, srv.ctx.cfg, rid,
        count=int(params.get("count", 1)), max_depth=int(params.get("max_depth", 64)))
    return cb.to_dict()


@method("recipes.get", params={"id": "str"},
        summary="One crafting recipe by id (e.g. \"minecraft:chest\").")
def _recipes_get(srv: AgentServer, params: dict) -> dict:
    from . import crafting
    rid = params.get("id", "")
    if not isinstance(rid, str) or not rid.strip():
        raise RpcError(INVALID_PARAMS, "id must be a non-empty string")
    return {"recipe": crafting.get_recipe(srv.ctx.t, srv.ctx.cfg, rid).to_dict()}


@method("recipes.tag", params={"tag": "str"},
        summary="Resolve a #tag ingredient (e.g. \"minecraft:planks\") to its concrete items.")
def _recipes_tag(srv: AgentServer, params: dict) -> dict:
    from . import crafting
    tag = params.get("tag", "")
    if not isinstance(tag, str) or not tag.strip():
        raise RpcError(INVALID_PARAMS, "tag must be a non-empty string")
    return {"tag": tag.lstrip("#"),
            "items": crafting.resolve_tag(srv.ctx.t, srv.ctx.cfg, tag)}


@method("craft.preview",
        params={"id": "str", "count": "int|null", "source": "str", "receiver": "str",
                "include_stored": "bool"},
        summary="Plan a craft against live inventory (count=null = hold-to-max). No mutation.")
def _craft_preview(srv: AgentServer, params: dict) -> dict:
    from . import crafting
    rec = crafting.get_recipe(srv.ctx.t, srv.ctx.cfg, params["id"])
    count = params.get("count", 1)
    plan = crafting.plan_craft(
        srv.ctx.console, srv.ctx.cfg, rec,
        count=None if count is None else int(count),
        source=params.get("source", ""), receiver=params.get("receiver", ""),
        include_stored=params.get("include_stored"))
    return plan.to_dict()


@method("craft.do",
        params={"id": "str", "count": "int|null", "source": "str", "receiver": "str"},
        summary="Craft for real: consume inputs (/clear) + grant output (/give). "
                "count=null = hold-to-max (one stack).",
        capability="actions", destructive=True)
def _craft_do(srv: AgentServer, params: dict) -> dict:
    from . import crafting
    rec = crafting.get_recipe(srv.ctx.t, srv.ctx.cfg, params["id"])
    count = params.get("count", 1)
    res = crafting.craft(
        srv.ctx.console, srv.ctx.cfg, rec,
        count=None if count is None else int(count),
        source=params.get("source", ""), receiver=params.get("receiver", ""))
    return res.to_dict()


@method("items.manifest", params={"query": "str", "limit": "int", "offset": "int"},
        summary="EMI-style item index: id → display name → icon texture id, from the "
                "mod jars + resourcepacks. Page with offset/limit; icon feeds icons.fetch.")
def _items_manifest(srv: AgentServer, params: dict) -> dict:
    from . import assets
    lang, item_models, block_models = assets.load_assets(srv.ctx.t, srv.ctx.cfg)
    items = assets.build_manifest(item_models, block_models, lang,
                                  query=str(params.get("query", "")))
    limit = max(1, min(int(params.get("limit", 2000)), 10000))
    offset = max(0, int(params.get("offset", 0)))
    page = items[offset:offset + limit]
    return {"items": page, "count": len(items),
            "truncated": offset + limit < len(items)}


@method("icons.fetch", params={"textures": "list[str]"},
        summary="Item icon PNGs (base64) by texture id (from items.manifest), for the "
                "phone to cache & render offline. Returns {icons:{tex:b64}, missing:[…]}.")
def _icons_fetch(srv: AgentServer, params: dict) -> dict:
    import base64 as _b64

    from . import assets
    texs = params.get("textures") or []
    if not isinstance(texs, list):
        raise RpcError(INVALID_PARAMS, "textures must be a list of texture ids")
    texs = [str(x) for x in texs][:500]
    data = assets.fetch_icons(srv.ctx.t, srv.ctx.cfg, texs)
    want = {assets._norm_id(x) for x in texs if x}
    return {"icons": {k: _b64.b64encode(v).decode() for k, v in data.items()},
            "missing": sorted(want - set(data))}


@method("assets.sync", params={"version": "str", "force": "bool"},
        summary="Download the matching vanilla client jar (cached on the server) so vanilla items "
                "get icons + names. version=\"\" auto-detects; mods/resourcepacks still override.",
        capability="actions")
def _assets_sync(srv: AgentServer, params: dict) -> dict:
    from . import assets
    return assets.sync_vanilla(srv.ctx.t, srv.ctx.cfg,
                               version=str(params.get("version", "")),
                               force=bool(params.get("force", False)))


@method("config.tree", params={"mods": "bool"},
        summary="List config/ files (size/mtime/format, best-effort owning mod).")
def _config_tree(srv: AgentServer, params: dict) -> dict:
    from . import modconfig as MC
    associate = params.get("mods", True)
    files = MC.list_config_files(srv.ctx.t, srv.ctx.cfg, associate_mods=associate)
    return {"root": MC.config_dir(srv.ctx.cfg), "files": [f.to_dict() for f in files]}


@method("config.get", params={"path": "str"},
        summary="Read one config/ file (path relative to config/, size-capped).")
def _config_get(srv: AgentServer, params: dict) -> dict:
    from . import modconfig as MC
    return MC.read_config(srv.ctx.t, srv.ctx.cfg, params["path"])


@method("config.set", params={"path": "str", "text": "str", "reload": "bool"},
        summary="Write a config/ file (TOML/JSON validated, atomic, .bak); reload=true runs /reload too.",
        capability="destructive", destructive=True)
def _config_set(srv: AgentServer, params: dict) -> dict:
    from . import modconfig as MC
    path, text = params["path"], params["text"]
    if not isinstance(text, str):
        raise RpcError(INVALID_PARAMS, "text must be a string")
    res = MC.write_config(srv.ctx.t, srv.ctx.cfg, path, text)
    res["ok"] = True
    res["running"] = srv.ctx.ctl.find_pid() is not None
    res["reloaded"] = False
    if params.get("reload") and res["running"]:
        try:
            res["reload_output"] = MC.trigger_reload(srv.ctx.console)[:200]
            res["reloaded"] = True
        except ConsoleError as e:
            res["reload_output"] = f"reload failed: {e}"
    return res


@method("config.reload",
        summary="Run /reload (datapacks/recipes/loot/tags) — not mod TOML configs.",
        capability="actions")
def _config_reload(srv: AgentServer, params: dict) -> dict:
    from . import modconfig as MC
    if srv.ctx.ctl.find_pid() is None:
        raise RpcError(APP_ERROR, "server is not running", {"exit_code": 1})
    return {"ok": True, "output": MC.trigger_reload(srv.ctx.console)}


@method("inspect", params={"section": "str"},
        summary="Deep OS/JVM introspection of one section.")
def _inspect(srv: AgentServer, params: dict) -> dict:
    from . import inspector
    section = params.get("section", "host")
    if section not in inspector.SECTIONS:
        raise RpcError(INVALID_PARAMS,
                       f"section must be one of {list(inspector.SECTIONS)}")
    pid = None if section == "host" else srv.ctx.ctl.find_pid()
    return inspector.inspect_section(srv.ctx.t, srv.ctx.cfg, section, pid).to_dict()


@method("watchdog.state", summary="armed/desired/halted + restart history.")
def _wd_state(srv: AgentServer, params: dict) -> dict:
    return state.load()


@method("watchdog.arm", summary="Allow the watchdog to self-heal.", capability="actions")
def _wd_arm(srv: AgentServer, params: dict) -> dict:
    return state.set_armed(True)


@method("watchdog.disarm", summary="Stop the watchdog from acting.", capability="actions")
def _wd_disarm(srv: AgentServer, params: dict) -> dict:
    return state.set_armed(False)


@method("events.list", params={"since": "float", "limit": "int"},
        summary="Historical events from the journal (no streaming).")
def _events_list(srv: AgentServer, params: dict) -> dict:
    since = params.get("since")
    limit = params.get("limit")
    return {"events": events.read(since=since, limit=limit)}


@method("events.subscribe", params={"since": "float"},
        summary="Stream watchdog actions/alerts as `event` notifications.")
def _events_subscribe(srv: AgentServer, params: dict) -> dict:
    srv._start_events(params.get("since"))
    return {"ok": True, "streaming": True}


@method("events.unsubscribe", summary="Stop the event stream.")
def _events_unsubscribe(srv: AgentServer, params: dict) -> dict:
    srv._stop_events()
    return {"ok": True}


# ================================================================ schema

def _dataclass_types(dc) -> dict[str, str]:
    # field types are strings (PEP 563 / `from __future__ import annotations`)
    return {f.name: (f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type)))
            for f in fields(dc)}


def build_schema() -> dict:
    """Generate the contract from the dataclasses + the method registry.

    Deterministic: a golden-file test serializes this with sorted keys and fails
    if it changes, so the contract cannot drift without bumping AGENT_PROTOCOL.
    """
    methods = {
        name: {
            "summary": spec["summary"],
            "params": spec["params"],
            "destructive": spec["destructive"],
            "capability": spec["capability"],
        }
        for name, spec in METHODS.items()
    }
    types = {
        "Status": _dataclass_types(Status),
        "BackupEntry": {"name": "str", "path": "str", "ts": "str (ISO 8601)",
                        "size": "int", "full": "bool", "age_s": "float"},
        "Event": {"ts": "float", "kind": " | ".join(events.KINDS),
                  "detail": "str", "urgency": "normal | critical", "data": "dict"},
        "ConfigFile": _dataclass_types(_ConfigFile),
        "config.server": _dataclass_types(ServerCfg),
        "config.backup": _dataclass_types(BackupCfg),
        "config.watchdog": _dataclass_types(WatchdogCfg),
        "config.metrics": _dataclass_types(MetricsCfg),
        "config.llm": _dataclass_types(LlmCfg),
        "config.ui": _dataclass_types(UiCfg),
        "config.crafting": _dataclass_types(CraftingCfg),
    }
    return {
        "protocol": AGENT_PROTOCOL,
        "framing": "ndjson",
        "transport": "jsonrpc-2.0/ssh-stdio",
        "capabilities": ["actions", "destructive"],
        "error_codes": {
            "parse": PARSE_ERROR, "invalid_request": INVALID_REQUEST,
            "method_not_found": METHOD_NOT_FOUND, "invalid_params": INVALID_PARAMS,
            "internal": INTERNAL_ERROR, "app": APP_ERROR,
            "capability_required": CAP_REQUIRED, "confirm_required": CONFIRM_REQUIRED,
        },
        "methods": methods,
        "types": types,
    }


def serve(ctx) -> int:
    """Entry point used by `mcctl agent`."""
    return AgentServer(ctx).serve()
