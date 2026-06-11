# Design — v0.5.0: the agent + ops glue

**Theme:** stop being a thing you *run* and start being a thing you can *talk to*.

0.5.0 turns the tested Python core into a programmable surface. The headline is
**`mcctl agent`** — a long-lived JSON-RPC 2.0 server over SSH stdio that any
client (the planned Android app first, but also scripts, Grafana sidecars, a
future web UI) can drive. Folded in alongside it are the two ops-glue pieces the
Android plan's Phase 2 alerting depends on anyway: a **push bridge** (ntfy /
UnifiedPush) and a **Prometheus textfile exporter**.

Guiding principle, unchanged from TODO.md: **one brain, two faces.** Every
behavior already lives in this repo's tested core (watchdog semantics, backup
safety, fish-proof remote scripting). The agent does not reimplement any of it —
it *exposes* it. New clients become renderers, never a second source of truth.

```
              ┌──────────── this repo (the brain) ────────────┐
  phone ──ssh─┤  mcctl agent  ──>  Ctx ──> ServerControl       │
  script ─────┤   (JSON-RPC)        │      backup / console …   │
  grafana ────┤                     └──>  state.py / metrics    │
              └────────────────────────────────────────────────┘
                       ▲ push (ntfy)        ▲ scrape (.prom)
```

---

## 1. `mcctl agent` — JSON-RPC 2.0 over stdio

### 1.1 Why stdio, why JSON-RPC

The phone opens **one** SSH channel and runs `mcctl agent`. No new listening
port, no daemon to supervise, no stored credentials — auth is the SSH key the
client already holds, the agent runs as the same unprivileged user, and the
existing ControlMaster/`bash -s` transport is reused verbatim. When the channel
closes, the agent exits. This is the cheapest possible surface that still gives
us a typed request/response contract.

JSON-RPC 2.0 because it is the smallest standard that gives us: correlated
request/response (`id`), server-initiated **notifications** (no `id` — used for
the event stream), and a defined error envelope. We do not invent a protocol.

### 1.2 Framing — newline-delimited JSON (NDJSON)

One JSON object per line, both directions, UTF-8, `\n`-terminated. We
deliberately **do not** use LSP-style `Content-Length` framing: SSH gives us a
clean bidirectional byte pipe, payloads are small, and line-framing is trivial
to implement on the Kotlin side and to eyeball in a terminal during dogfooding.

```
→ {"jsonrpc":"2.0","id":1,"method":"status","params":{"fast":true}}
← {"jsonrpc":"2.0","id":1,"result":{"running":true,"players":{"count":3,...}}}
← {"jsonrpc":"2.0","method":"event","params":{"kind":"alert-tps","tps":11.4,...}}
```

A request whose payload happens to contain a newline is not possible — we emit
compact JSON (`separators=(",", ":")`, no embedded literal newlines) and the
reader splits on `\n`. Oversized lines (> 1 MiB) are rejected with
`-32600` to bound memory.

### 1.3 Method surface

Methods map onto the **same service objects** the `cmd_*` handlers already use.
The agent builds a single `Ctx` once (lazy transport, ControlMaster stays warm
across the whole session) and dispatches through a registry:

```python
# agent.py (sketch)
@method("status")
def _status(ctx: Ctx, *, fast: bool = False) -> dict:
    return ctx.ctl.status(full=not fast).to_dict()
```

| Method | Maps to | Params | Result |
|---|---|---|---|
| `agent.hello` | — | — | `{protocol, mcctl_version, capabilities[]}` |
| `agent.schema` | §1.5 | — | full schema document |
| `agent.ping` | — | — | `{pong: ts}` |
| `status` | `ctl.status` | `fast?` | `Status.to_dict()` |
| `start` / `stop` / `restart` / `kill` | `ServerControl` | `yes?`, stop opts | `{ok, state, detail}` |
| `save` | `ctl.save` | `skip_if_down?` | `{ok, saved}` |
| `cmd` | `console.send_and_wait` | `command` | `{output}` |
| `tps` / `health` | `spark` | — | parsed spark dicts |
| `profile` | `spark.profile` | `duration?` | `{url}` |
| `purge` | `metrics.purge` | — | `{before, after, verdict}` |
| `players.list` | `players` | — | `{count, max, names}` |
| `players.whitelist` / `.op` / `.kick` / `.ban` | `players` | `name`, `action` | `{ok}` |
| `backup.create` | `backup` | `full?` | `{archive, bytes, verified}` |
| `backup.list` | `backup` | — | `[{name, ts, bytes}]` |
| `backup.prune` / `.verify` | `backup` | `name?` | `{...}` |
| `logs.tail` | `logs` | `lines?`, `crash?` | `{lines[]}` |
| `metrics.history` | `metrics.read_samples` | `n?` | `[sample]` |
| `props.list` / `.get` / `.set` | `props` | `key?`, `value?`, `live?` | `{...}` |
| `jvm.show` / `jvm.heap` | `props`/jvm | `size?` | `{xms, xmx, java}` |
| `mods.list` | `mods` | — | `[{id, version, bytes}]` |
| `inspect` | `inspector` | `section?` | `{section: {...}}` |
| `watchdog.state` | `state.load` | — | `{armed, desired, halted, restarts[]}` |
| `watchdog.arm` / `.disarm` | `state` | — | `{armed}` |
| `events.subscribe` | §1.4 | `since?` | stream of `event` notifications |
| `events.unsubscribe` | §1.4 | — | `{ok}` |

**Destructive-action policy (non-negotiable).** `backup.restore`, `props.set`
on protected keys, and anything that destroys state require an explicit
`confirm: true` param *and* are gated by a capability the client must request in
`agent.hello`. Restore stays additionally guarded by the same "refuses a running
server / never deletes the old world" logic in `backup.py` — the agent cannot
bypass it because it calls the same function.

### 1.4 The event stream — a shared journal, not a socket

The watchdog runs as its own systemd unit; the agent is a transient
per-connection process. They must not be coupled by a live socket. The clean IPC
is an **append-only event journal**:

- New file `~/.local/state/mcctl/events.jsonl` (rotates at 5 MiB like
  `metrics.jsonl`). Each line:
  `{"ts":..., "kind":"restart"|"freeze-restart"|"crash-loop-halt"|"alert-tps"|"alert-heap"|"alert-disk"|"alert-ssh"|"started"|"stopped", "detail":"…", "urgency":"normal"|"critical", "data":{…}}`.
- `watchdog._notify()` gains a sibling `_emit_event()` that appends to the
  journal **in addition** to calling `util.notify()`. These are exactly the
  moments it already alerts on (see `watchdog.py:175–224`), so no new policy —
  just durable, structured records of decisions it already makes.
- `events.subscribe` long-polls the journal: seek to `since` (a ts/offset the
  client passes for resume-after-reconnect), stream historical lines, then watch
  for appends (inotify where available, 1 s stat-poll fallback) and emit each new
  line as a JSON-RPC `event` notification. Idempotent: the client dedupes on
  `ts`.
- **Free CLI win:** `mcctl events [--follow] [--since DUR]` tails the same
  journal. The watchdog finally has a queryable audit log of every heal and
  alert, on the box and in the GUI.

This also means the journal is the single feed for **both** the agent's event
stream **and** the push bridge (§2) — the watchdog emits once; consumers fan out.

### 1.5 Versioned schema + golden tests (the anti-drift contract)

The whole point of "one brain" collapses if the contract drifts silently between
a Python release and a shipped phone binary. So:

- `AGENT_PROTOCOL_VERSION` constant (starts at `1`), returned in `agent.hello`
  and stamped into the schema document. Bumped **only** by a deliberate human
  edit; never auto-derived.
- `mcctl agent --schema` emits a JSON document generated **from the dataclasses**
  (`Status`, config dataclasses, the param/result typeddicts of each method) plus
  the method table — single source, no hand-maintained second copy.
- **Golden-file test:** `tests/test_agent_schema.py` regenerates the schema and
  asserts it byte-equals `tests/golden/agent_schema_v1.json`. Change a field on
  `Status` and forget to think about the contract → CI fails → you either commit
  the new golden (conscious) or bump the version. The contract cannot drift by
  accident.
- Backward-compat rule documented in the schema header: within a major protocol
  version, only **additive** changes (new optional fields, new methods). Removals
  or semantic changes require a version bump, and `agent.hello` lets old clients
  detect it and degrade gracefully.

### 1.6 Concurrency & lifecycle

- **One reader, sequential dispatch** for request/response — mirrors the GUI's
  single-worker SSH serialization (`gui_app.py`) and the ControlMaster
  assumption. No surprise interleaving of remote commands.
- Events are delivered from a **background journal-watcher thread**; all writes
  to stdout (responses *and* notifications) go through one `threading.Lock` so
  lines never interleave on the wire.
- Clean shutdown on EOF/stdin close, SIGTERM, or `agent.shutdown`. `Ctx.close()`
  tears down the console/tunnel exactly as the CLI does.
- A long action (`start`, `backup.create`) blocks that connection's
  request queue by design; the client either waits (with a progress-friendly
  timeout) or opens a second cheap channel for `status` polling. We do **not**
  add intra-connection parallelism in 0.5.0 — it would fight the transport's
  serialization guarantees.

### 1.7 Errors

Standard JSON-RPC codes for protocol faults (`-32700` parse, `-32600` invalid
request, `-32601` method not found, `-32602` invalid params, `-32603` internal).
App-level failures reuse mcctl's existing exit-code vocabulary in
`error.data.exit_code`: `1` generic error, `3` server unreachable. Example:

```
← {"jsonrpc":"2.0","id":7,"error":{"code":-32000,"message":"server unreachable",
   "data":{"exit_code":3,"hint":"ssh BatchMode failed: …"}}}
```

---

## 2. Push bridge — ntfy / UnifiedPush

Today `util.notify(title, body, *, desktop, webhook_url, urgency)` does
`notify-send` + an optional Discord-compatible webhook (`util.py:204`). 0.5.0
adds a third sink that turns watchdog alerts into **phone push** with zero new
infra and no new dependency (stdlib `urllib`, like the existing webhook).

- **Config:** new keys under `[watchdog]`:
  ```toml
  ntfy_url   = "https://ntfy.sh"      # or a self-hosted server
  ntfy_topic = ""                      # empty disables; e.g. "carborioland-a8f3"
  ntfy_token = ""                      # optional bearer for protected topics
  ```
- **Mapping:** POST the body to `<ntfy_url>/<topic>` with headers
  `Title: <title>`, `Priority: <urgency→ntfy>` (`critical`→`urgent`/5,
  `normal`→`3`), `Tags: warning`/`rotating_light` by urgency. 5 s timeout,
  best-effort, failures logged not raised — identical discipline to the webhook
  path.
- **Why ntfy specifically:** ntfy *is* a UnifiedPush distributor. Supporting the
  ntfy publish protocol gives the Android app (Phase 2) push **for free** — the
  app subscribes to the topic through whatever UnifiedPush distributor the user
  runs, no Google FCM, no Anthropic/third-party relay, self-hostable. One config
  block covers both "ping my phone today" and the future app.
- **Doctor:** `mcctl doctor` validates `ntfy_topic` is set if `ntfy_url` is
  non-default, and warns that a topic is a public namespace unless `ntfy_token`
  or a self-hosted server is used (security note, not a failure).
- **Surface:** `mcctl notify-test` (or `watchdog test-alert`) fires a sample
  alert through every configured sink so users can confirm the pipe before
  trusting it at 3 a.m.

The webhook stays as-is; ntfy is purely additive. Both fire from the same
`_notify`/journal moment, so Discord and phone push are always consistent.

---

## 3. Prometheus textfile exporter

`metrics.jsonl` already records TPS/MSPT/heap/RAM/players/disk/load every sample.
0.5.0 makes that scrapeable by node_exporter's textfile collector — the standard
zero-port way to get a box into Grafana.

- **Command:** `mcctl metrics export [--out PATH]` writes Prometheus text format
  to `~/.local/state/mcctl/mcctl.prom` (configurable), pointed at by
  node_exporter's `--collector.textfile.directory`.
- **Pure renderer:** `render_prometheus(sample: dict, *, host: str, restarts: int) -> str`
  — pure, unit-tested, no I/O. Written atomically (tmp + `os.replace`, reusing
  the existing atomic-write idiom) so node_exporter never reads a half-written
  file.
- **Series** (`HELP`/`TYPE` annotated, `host` label):
  ```
  mcctl_up{host="…"}                     0|1
  mcctl_players{host="…"}                3
  mcctl_tps{host="…"}                    19.8
  mcctl_mspt_milliseconds{host="…"}      11.2
  mcctl_heap_used_bytes / _max_bytes
  mcctl_host_mem_used_bytes / _total_bytes
  mcctl_disk_free_bytes
  mcctl_load1
  mcctl_last_backup_age_seconds
  mcctl_watchdog_restarts_total          (counter, from state.restarts)
  mcctl_log_age_seconds
  mcctl_scrape_timestamp_seconds
  ```
- **Refresh:** new `mcctl-metrics.timer` (sibling to the backup/autosave timers
  already shipped in `src/mcctl/units/`) runs the export on an interval; or
  `mcctl watch` writes it inline when `--prom` is passed, reusing the sample it
  already records. Stale-data guard: emit `mcctl_up 0` and a recent
  `mcctl_scrape_timestamp_seconds` so Grafana can alert on a dead exporter
  distinctly from a dead server.

This is read-only, local-file, no-network on the export side — it cannot weaken
the security posture.

---

## 4. Scope, structure, and what stays out

### New / touched modules
| File | Change |
|---|---|
| `src/mcctl/agent.py` | **new** — NDJSON loop, method registry, schema gen, event-stream thread |
| `src/mcctl/events.py` | **new** — journal append/read/tail (shared by agent, CLI, GUI) |
| `src/mcctl/prometheus.py` | **new** — `render_prometheus` (pure) + atomic writer |
| `src/mcctl/watchdog.py` | `_emit_event()` alongside `_notify()` at each existing alert/heal point |
| `src/mcctl/util.py` | ntfy sink added to `notify()` |
| `src/mcctl/config.py` | `ntfy_*` keys; exporter path; template + validation |
| `src/mcctl/cli.py` | `agent`, `events`, `metrics export`, `notify-test` subcommands + `--schema` |
| `src/mcctl/units/` | `mcctl-metrics.timer` + `.service` |
| `tests/` | `test_agent_schema.py` (golden), `test_agent_methods.py` (FakeTransport), `test_prometheus.py`, `test_events.py`, ntfy in `test_notify.py` |
| `completions/`, `README.md`, `PKGBUILD`, `TODO.md` | track the new surface |

### Explicitly **out** of 0.5.0
- The Android app itself (that's Phase 1, the *next* cycle — 0.5.0 is the
  Phase 0 groundwork it renders over).
- `events.subscribe` server-push *over a second multiplexed channel* — one
  channel, long-poll, is enough for MVP.
- Any new authentication scheme — SSH key remains the only credential.

### Testing strategy
- All three pillars are unit-testable **in this repo** with the existing
  `FakeTransport` + `FakeClock` — no server needed (per AUDIT.md §1).
- The agent is driven by feeding NDJSON lines to its reader and asserting on
  emitted lines; the golden schema test locks the contract.
- One integration test extends the existing tmux-backed suite: launch
  `mcctl agent`, run `hello → status → cmd → stop` over a pipe.

### Security review (carry-forward of the AUDIT.md §5 posture)
- No new listening ports; agent reachable only via existing SSH auth, runs as the
  same unprivileged user.
- Agent results pass the same `sanitize_terminal` hygiene; AI/log payloads keep
  their redaction.
- ntfy publish is outbound-only to a user-configured URL; doctor flags the public
  topic namespace.
- Prometheus export is a local atomic file write — no network, no port.
- Destructive RPC methods are capability-gated + `confirm`-gated and still funnel
  through the same guard code that protects the CLI.

---

## 5. Acceptance — 0.5.0 is done when

1. `mcctl agent` answers `hello/status/start/stop/backup.create/cmd/logs.tail/
   watchdog.state` over NDJSON; `agent.schema` emits the versioned document and
   the golden test passes.
2. `events.subscribe` streams a watchdog restart and a TPS alert end-to-end in
   the integration test; `mcctl events --follow` shows the same journal.
3. A watchdog alert reaches a phone via ntfy; `mcctl notify-test` exercises every
   sink.
4. `mcctl metrics export` produces a valid `.prom` that node_exporter scrapes;
   `render_prometheus` is unit-tested; the timer ships in the package.
5. Full suite green (lint + unit + tmux integration); README/completions/PKGBUILD
   track the new surface; version bumped to `0.5.0`.
