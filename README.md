# mcctl — Minecraft Remote Control & Monitoring

Arch Linux CLI/TUI that fully drives a remote modded Minecraft server over SSH.
Built for the **CarborioLand** stack — Medieval MC MMC5 (NeoForge 1.21.1) on an
ARM64 OCI box, launched via ServerPackCreator's `start.sh` inside tmux — but
everything is configurable.

```
mcctl init  →  mcctl doctor  →  mcctl start  →  mcctl dash
```

## Design at a glance

| Piece | Choice | Why |
|---|---|---|
| Transport | system OpenSSH + ControlMaster multiplexing | one handshake, every command ~10 ms; your `~/.ssh/config`, keys and agent just work |
| Remote shell | every payload piped to `bash -s` over stdin | the remote login shell (fish) never parses a byte — zero quoting hazards |
| Console channel | RCON through an SSH `-L` tunnel, tmux `send-keys` + log-offset fallback | reliable request/response; works even with RCON off |
| Secrets | none stored locally | RCON password is read from the remote `server.properties` on demand |
| Liveness | java process with `cwd == server_dir` **and** tmux session | survives the "wrong session name / wrong user" trap |
| Crash forensics | tmux `remain-on-exit` + evidence bundles | dead panes are captured before the watchdog reaps them |
| Server-side deps | bash + coreutils + tmux (+ zstd recommended) | nothing to install or maintain on the VM |

## Features

| Command | What it does |
|---|---|
| `mcctl status [--json] [--fast]` | process/tmux/port/players/TPS/heap/host RAM/disk/backup age, one screen |
| `mcctl start` / `stop` / `restart` | tmux + `start.sh` boot with readiness detection; graceful stop: player countdown → `save-all flush` → `stop` → SIGTERM → SIGKILL escalation |
| `mcctl dash` | live TUI: TPS sparkline, heap/RAM gauges, log tail; keys for save/backup/purge/start/stop |
| `mcctl gui` / `mcctl-gui` | native GTK4/libadwaita desktop app (sidebar, 17 pages): live status & actions, TPS/heap/players history charts, console, logs, players, backups, mods, OS/JVM inspector (learn mode), AI analysis, AI chat, doctor with safe fixes, validated server.properties editor, JVM settings, crash reports + evidence bundles, spark profiler, config sync, and a **Settings** editor for the whole config.toml (SSH key + flags, ollama model picker, every section) — no hand-editing required. Opening the app onto an already-running server auto-connects to its live tmux session instead of forcing a restart |
| `mcctl watch` | line-oriented live monitor: one compact status line per interval (state/players/TPS/MSPT/heap/RAM/load), scrollable and greppable; records metric history as it runs |
| `mcctl history [tps\|mspt\|heap\|players\|mem\|load\|all]` | terminal charts of recorded metric history with min/avg/max/last summaries |
| `mcctl trace [--learn]` | live JVM GC tracer (`jstat -gcutil`): young/full collections, pause times, eden/old/metaspace occupancy — watch how the JVM manages memory, with a learn-mode walkthrough |
| `mcctl backup [create\|list\|prune\|pull\|verify\|restore]` | consistent snapshots (`save-off` → flush → tar+zstd → verify → `save-on` guaranteed), GFS rotation, rsync pull, safe restore |
| `mcctl save` | `save-all flush` with confirmation; `--skip-if-down` for timers |
| `mcctl watchdog [run\|arm\|disarm\|status\|install]` | self-healing daemon: crash restart with backoff, freeze detection (stale log + dead console → thread dump → restart), crash-loop breaker, TPS/heap/disk/SSH alerts |
| `mcctl tps` / `health` / `profile` | spark TPS/MSPT/CPU, memory/disk health, async profiler → `spark.lucko.me` URL |
| `mcctl purge` | `jcmd GC.run` with before/after heap — honest *garbage vs real leak* verdict |
| `mcctl props [list\|get\|set]` | validated `server.properties` editor: typed/ranged keys, atomic writes, remote `.bak`, `--live` apply where supported |
| `mcctl jvm [show\|heap 12G\|java PATH]` | `variables.txt` editor — rewrites Xms/Xmx, preserves Aikar's flags |
| `mcctl player …` | list, whitelist add/remove/on/off, op/deop, kick/ban/pardon |
| `mcctl cmd <anything>` / `console` | arbitrary console commands; `console` attaches to the live tmux (detach: `Ctrl-b d`) |
| `mcctl logs [-f] [crash]` | tail/follow `latest.log` (timestamps auto-converted to your `[ui].timezone`, default São Paulo); list/fetch crash reports (escape-sequence-sanitized) |
| `mcctl inspect [SECTION] [--learn]` | deep OS/JVM introspection: process tree, /proc internals, every JVM thread, memory maps, fds, sockets, environment, jcmd flags/heap, host PSI — each section has a `--learn` walkthrough explaining what the kernel structures mean |
| `mcctl mods` | list every mod with id/version/size, metadata read from inside each jar (NeoForge/Forge/Fabric descriptors) |
| `mcctl ai [logs\|crash\|mods\|inspect\|ask\|chat]` | AI analysis & multi-turn chat, powered by **Claude or a local LLM via ollama** (`[llm].provider`): review logs, root-cause crash reports, explain what the mods do, teacher-mode walkthroughs, free-form questions, and an interactive `chat` session — all with live server context attached |
| `mcctl stats` | local JSONL metrics history (TPS, MSPT, heap, RAM, players) |
| `mcctl sync --pull/--push` | rsync the `config/` dir — the Better Compatibility Checker mismatch fix |
| `mcctl agent [--schema]` | **JSON-RPC 2.0 server over SSH stdio** — the programmable contract every client (the planned phone app, scripts, dashboards) renders over; `--schema` prints the versioned, golden-tested contract |
| `mcctl events [-f] [--since N]` | the watchdog's audit log: every heal/restart/alert, tail or follow (also streamed live over the agent's `events.subscribe`) |
| `mcctl metrics export [--cat]` | Prometheus textfile exporter from `metrics.jsonl` for node_exporter → Grafana (atomic write; ships with `mcctl-metrics.timer`) |
| `mcctl notify-test` | fire a test alert through every configured sink (desktop, Discord webhook, ntfy push) |
| `mcctl doctor [--fix]` | end-to-end preflight; encodes the hard-won knowledge (below) |

## Install

**Arch (recommended):**

```fish
git clone https://github.com/lonaivdev-cell/minecraft-remote-monitoring
cd minecraft-remote-monitoring
makepkg -si
```

Installs the CLI, the desktop app entry, systemd user units, and fish completions.
Dependencies: `python` `python-rich` `openssh` `rsync` (optional: `libnotify`, `zstd`).

For the GUI (optional — shows up in your app launcher as **mcctl**):

```fish
sudo pacman -S --needed gtk4 libadwaita python-gobject
mcctl-gui   # or `mcctl gui`, or launch it from the app grid
```

For AI analysis & chat (optional — powers `mcctl ai`, the GUI's AI and Chat
pages). Pick **one** backend under `[llm]` in the config:

**Claude (cloud)** — `provider = "anthropic"` (default):

```fish
sudo pacman -S python-anthropic       # or: pipx inject mcctl anthropic
set -Ux ANTHROPIC_API_KEY sk-ant-…    # mcctl never stores the key itself
mcctl ai logs                         # sanity check
```

**Local LLM (ollama)** — `provider = "ollama"`, nothing leaves the box and no
API key is involved (mcctl talks ollama's HTTP API directly — no extra package):

```fish
ollama serve &                        # the local model server
ollama pull llama3.1                  # set [llm].ollama_model to match
# in ~/.config/mcctl/config.toml: [llm] provider = "ollama"
mcctl ai logs                         # sanity check
mcctl ai chat                         # interactive conversation
```

In the GUI you don't have to edit the file: open **Settings → AI / LLM**, flip the
provider, and pick the model straight from a list of everything `ollama` has pulled
(it queries ollama's `/api/tags`, the `ollama list` set).

Whichever backend you pick, everything sent to it is secret-redacted
(rcon.password, token-looking env values) and wrapped as untrusted data — the
system prompt explicitly refuses instructions embedded in logs, because this
modpack's crash logs are known to carry prompt-injection text.

**Anywhere else:** `pipx install .` then `mcctl watchdog install` for the user units.

## Quickstart

```fish
mcctl init                  # writes ~/.config/mcctl/config.toml (CarborioLand defaults)
mcctl doctor --fix          # verifies SSH→layout→JVM→props; applies safe fixes
mcctl start                 # boots in tmux, waits for "Done (…)!"
mcctl dash                  # watch it live
```

`doctor --fix` will: set `SKIP_JAVA_CHECK=true`, `WAIT_FOR_USER_INPUT=false`,
`SERVERSTARTERJAR_FORCE_FETCH=false` in `variables.txt`, create the backup dir,
and enable RCON with a generated password (active after next restart).

## Automation (systemd user units)

```fish
systemctl --user daemon-reload
systemctl --user enable --now mcctl-watchdog.service   # self-healing
systemctl --user enable --now mcctl-backup.timer       # daily 04:30 backup + rotation
systemctl --user enable --now mcctl-autosave.timer     # save-all every 20 min
systemctl --user enable --now mcctl-metrics.timer      # refresh the Prometheus textfile every minute
mcctl watchdog arm                                     # actually allow healing
loginctl enable-linger $USER                           # keep units running after logout
```

### Self-healing semantics (read once)

| State | Meaning |
|---|---|
| `armed` | master switch — **off by default**; disarm during migrations so a stale server can't be relaunched |
| `desired` | user intent, set by `mcctl start`/`stop` — the watchdog never resurrects a server you stopped on purpose |
| `halted` | crash-loop breaker tripped (default: 3 restarts/hour) — alerts loudly, stays down until `mcctl start` or re-arm |

Freeze = log silent beyond `freeze_log_age` **and** console unresponsive → thread
dump saved locally → forced restart. Evidence bundles (pane capture, log tail,
crash report) land in `~/.local/state/mcctl/crashes/` before every heal.

## Backups

- **Consistent while live:** `save-off` → `save-all flush` → wait "Saved the game" → `tar | zstd` → integrity test → `save-on` (re-enabled on *every* code path).
- **Rotation (GFS):** newest 8 + 1/day for 7 days + 1/ISO-week for 4 weeks; `--full` instance archives are never auto-deleted.
- **Disk guard:** refuses below `min_free_gb`; never overwrites — restore moves the current world to `world.pre-restore-<ts>`.
- `mcctl backup pull` mirrors archives to this machine over rsync.

## Security notes

- RCON is reached **only** through the SSH tunnel; keep 25575 closed in the OCI
  security list — `mcctl doctor` actively probes from outside and fails if it's reachable.
- All remote output (logs, crash reports, console replies) is stripped of ANSI/OSC
  escape sequences before printing — remote text can't drive your terminal.
- SSH runs with `BatchMode=yes` (keys/agent only) and `accept-new` host keys.
  Your `~/.ssh/config`, agent and default keys are used as-is; to pin a specific
  key set `[server].ssh_key` (or edit it in the GUI's Settings tab) — mcctl then
  passes `ssh -i <key> -o IdentitiesOnly=yes`.
- Heads-up: crash logs from this modpack are known to contain embedded
  prompt-injection text. It's inert noise — read the stack trace, ignore the prose.

## Hard-won knowledge, encoded

| Lesson | Where it lives |
|---|---|
| Launch = `start.sh` + `variables.txt` (ServerStarterJar), not `run.sh` | `server.py` start flow, config default |
| GraalVM vs SPC java check → `SKIP_JAVA_CHECK=true` | `doctor --fix` |
| `WAIT_FOR_USER_INPUT=false` or tmux hangs on Enter | `doctor --fix` |
| IPv4/IPv6: `server-ip=0.0.0.0`, `use-native-transport=false`, `-Djava.net.preferIPv4Stack=true` | `doctor` checks, props specs |
| `-XX:+ExplicitGCInvokesConcurrent` so `jcmd GC.run` works | `mcctl purge` |
| Verify by **process + session**, not session name | `find_pid` (pgrep + `/proc/<pid>/cwd`) |
| Watchdog must stand down during migrations | disarmed by default, `desired` intent tracking |
| `config/` drift → BCC version mismatch | `mcctl sync` |

## Architecture

```
src/mcctl/
├── cli.py        argparse tree, exit codes (0 ok / 1 error / 2 usage / 3 unreachable)
├── config.py     TOML config, validation, template
├── transport.py  SSH ControlMaster wrapper + LocalTransport (dev/tests)
├── rcon.py       Source-RCON client (fragmentation-aware)
├── console.py    channel facade: RCON-over-tunnel → tmux+log fallback
├── server.py     status probe (1 round-trip), start/stop/restart state machine
├── backup.py     snapshots, GFS rotation (pure+tested), pull, verify, restore
├── watchdog.py   observe → decide (pure) → act; crash-loop breaker
├── spark.py      tps/health parsers, async profiler
├── metrics.py    jcmd heap, purge verdict, JSONL history
├── props.py      server.properties + variables.txt editors
├── players.py    whitelist/op/kick/ban
├── logs.py       tail/follow/crash reports, evidence bundles, sanitization
├── doctor.py     preflight checks + safe fixes
├── inspector.py  deep OS/JVM introspection (/proc, threads, maps, fds, jcmd) + learn-mode texts
├── mods.py       mod inventory — descriptors read from inside the jars, one round-trip
├── llm.py        AI analysis & chat: Anthropic + ollama backends, redaction, data envelopes, streaming
├── tracer.py     JVM GC tracer — jstat -gcutil parsing (pure) + one streaming round-trip
├── charts.py     terminal charting primitives (sparklines, block charts) — pure
├── watch.py      `mcctl watch` line-oriented live monitor + metric recorder
├── agent.py      `mcctl agent` JSON-RPC 2.0 server over stdio — method registry reusing the core, generated+golden-tested schema, event stream
├── events.py     append-only event journal (watchdog ⇄ agent ⇄ `mcctl events`)
├── prometheus.py textfile exporter — pure render + atomic write from metrics.jsonl
├── dash.py       rich Live dashboard
├── gui.py        GUI launcher: dependency check, friendly pacman hint
├── gui_app.py    GTK4 + libadwaita desktop app (single worker thread for SSH)
└── state.py      armed/desired/halted/restart-history persistence
```

## Programmable: the agent

`mcctl agent` is a long-lived **JSON-RPC 2.0** server speaking newline-delimited
JSON over its stdin/stdout — meant to be run at the end of a single SSH channel.
Every method reuses the same tested core the CLI calls; nothing is reimplemented.
This is the contract the planned Android app (and any script or dashboard)
renders over — *one brain, two faces*.

```fish
# locally, eyeball it (each line is one request/response):
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"agent.hello","params":{"capabilities":["actions"]}}' \
              '{"jsonrpc":"2.0","id":2,"method":"status","params":{"fast":true}}' | mcctl agent

# over SSH, exactly how a client drives it:
ssh carborio mcctl agent      # then write JSON-RPC lines, read responses + `event` notifications

mcctl agent --schema          # the versioned, machine-readable contract
```

- **No new surface:** no listening port, no stored credential — auth is the SSH
  key the client already holds, the agent runs as the same unprivileged user.
- **Versioned + drift-proof:** the schema is generated from the dataclasses and
  frozen by a golden-file test; the contract can't change without bumping
  `AGENT_PROTOCOL` on purpose.
- **Destructive methods** (`kill`, `backup.restore`, `props.set`, …) require both
  a capability granted in `agent.hello` and an explicit `"confirm": true`.
- **Events:** `events.subscribe` streams watchdog heals/alerts as JSON-RPC
  notifications, backed by the same `events.jsonl` journal `mcctl events` tails.

## Off-box: push & metrics

- **Phone push (ntfy / UnifiedPush):** set `[watchdog].ntfy_topic` (server
  defaults to `https://ntfy.sh`) and watchdog alerts reach your phone. ntfy is a
  UnifiedPush distributor, so the future app gets push for free — no FCM, no
  relay. `mcctl notify-test` exercises every sink.
- **Prometheus / Grafana:** `mcctl metrics export` writes a node_exporter
  textfile from the recorded history; enable `mcctl-metrics.timer` to refresh it
  every minute and point `--collector.textfile.directory` at it.

## Development & testing

```fish
make dev        # editable install + pytest + ruff
make test       # unit tests, seconds — FakeTransport + FakeClock, no server needed
make test-all   # + integration: real tmux session driving a fake "java" server
make lint
```

The integration suite boots an actual tmux session running a bash renamed to
`java` (so pgrep/cwd detection is genuinely exercised), then drives start →
console → backup → verify → stop → restore → crash-corpse detection end to end.
CI runs lint + both suites on every push.

---

## #TODO — HIGH PRIORITY: Android companion app

> **Next development cycle: build the Android app with feature parity (status,
> start/stop, dashboards, backups, alerts) for managing CarborioLand from a
> phone.** The full development plan — architecture decision, phased roadmap,
> security model, testing strategy — lives in **[TODO.md](TODO.md)**.
> **Phase 0 (the server-side API) shipped in 0.5.0:** `mcctl agent` is the
> JSON-RPC contract the app renders over, the `events.jsonl` journal +
> `events.subscribe` give it a push-style stream, and the ntfy bridge already
> delivers watchdog alerts to a phone. The app is now a thin client over a
> tested brain — see [DESIGN-0.5.0.md](DESIGN-0.5.0.md).
