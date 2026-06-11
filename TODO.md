# TODO

## [P1] Android companion app — full development plan

**Goal:** manage CarborioLand from a phone with feature parity with `mcctl`:
status & dashboards, start/stop/restart, backups, spark TPS, alerts — without
weakening the security model (no exposed ports, no stored passwords).

### Architecture decision

| Option | Sketch | Verdict |
|---|---|---|
| A. Native SSH client app | Kotlin + sshj/Apache MINA reimplements all flows | ✗ duplicates every hard-won behavior; two codebases drift |
| **B. Thin client ↔ `mcctl agent` (JSON-RPC over SSH stdio)** | app opens one SSH channel, runs `mcctl agent --json-rpc`, speaks a small RPC contract; ALL logic stays in this repo | ✅ **chosen** — one brain, two faces |
| C. Termux + mcctl as-is | install Python mcctl inside Termux | stopgap only; useful for early dogfooding |

Option B keeps the phone as a *renderer*: the tested Python core (watchdog
semantics, backup safety, fish-proof remote scripting) remains the single
source of truth. The RPC surface is the existing `--json` payloads, formalized.

### Brain placement — decided 2026-06-11 (full record: [DESIGN-BRAIN.md](DESIGN-BRAIN.md))

"One brain" also needs an answer to *where the brain lives*. The phone runs
`mcctl agent` **on the box**, which reads/writes the box's
`~/.local/state/mcctl/` — while the watchdog and `mcctl start/stop` write the
desktop's. Two `watchdog.json`s and two `events.jsonl`s = split brain by
construction the moment the app connects.

| Option | Verdict |
|---|---|
| A. Brain stays on the desktop | ✗ part-time brain (sleep/roaming/NAT); phone gets a second truth on the box |
| **B. Brain on the OCI box, systemd user units + `loginctl enable-linger`** | ✅ **chosen** — the only always-on machine; one state dir next to the server; `transport = "local"` is already the integration-tested mode |
| C. Brain on both + state sync | ✗ two `decide()` loops, no cross-machine lock, last-writer-wins on `desired` — the 2026-06-11 incident as an architecture |

Consequences: the watchdog, `watchdog.json`, `events.jsonl`, `metrics.jsonl`,
and the timers move to the box; faces (desktop, phone) render the box's truth
and route intent ops through it; `ALERT_SSH` ("box down") must be detected from
*outside*; doctor enforces single-brain + linger (`ops: brain placement`,
`ops: brain linger`).

### Phase 0.5 — brain to the box (v0.6.0) → **gates Phase 1**
- [x] Decision record ([DESIGN-BRAIN.md](DESIGN-BRAIN.md)) + doctor enforcement:
      warn on two live watchdogs, warn on a lingerless brain host.
- [ ] Faces follow the brain: `[brain]` config (`location = "client" | "server"`);
      with `"server"`, desktop `start/stop/restart` intent, `watchdog
      arm|disarm|status`, `events`, `history`/`stats`, `postmortem` are served
      by the box over a thin Python agent client (same NDJSON contract as the
      phone), and local-state writes are refused.
- [ ] Box bootstrap per the DESIGN-BRAIN.md §6 runbook: pipx install,
      `transport = "local"` config, `mcctl watchdog install`, enable units,
      `sudo loginctl enable-linger ubuntu`.
- [ ] Cut over in stand-down order: desktop disarm + disable units FIRST, then
      arm the box brain; doctor green from both vantage points.
- [ ] Box-down dead-man ping (ntfy on silence) to replace the inverted
      `ALERT_SSH` — or explicitly accept "phone can't reach agent" as the signal.

### Phase 0 — API extraction (in this repo)  → **DONE in v0.5.0** (see [DESIGN-0.5.0.md](DESIGN-0.5.0.md))
- [x] `mcctl agent` subcommand: long-lived JSON-RPC 2.0 loop on stdin/stdout
      (status, start/stop/restart/kill, save, cmd, tps/health/profile/purge,
      players.*, backup.*, logs.tail, props.*, jvm.*, mods.list, inspect,
      watchdog.*, metrics.history, events.*).
- [x] Version-stamped schema (`mcctl agent --schema`) generated from the
      dataclasses; golden-file test (`tests/test_agent_schema.py`) so the
      contract can't drift silently without bumping `AGENT_PROTOCOL`.
- [x] `events.subscribe` stream + shared `events.jsonl` journal; also surfaced
      as `mcctl events [-f]`.
- [x] **ntfy / UnifiedPush push bridge** — `ntfy_*` sink in `util.notify()`;
      watchdog alerts reach a phone and the future app gets push for free.
- [x] **Prometheus textfile exporter** (`mcctl metrics export` + `mcctl-metrics.timer`).

### Phase 1 — Android MVP (read-mostly) — *requires Phase 0.5: the box is the brain the app renders*
- [ ] Kotlin + Jetpack Compose; sshj with **Ed25519 device key** generated in
      Android Keystore; key authorized on the OCI box like any other client.
- [ ] Screens: server card (state/players/TPS/heap), log tail, backup list.
- [ ] Home-screen widget: TPS + player count via WorkManager periodic refresh.
- [ ] Alerting v1: keep the existing webhook → Discord channel (zero new infra).

### Phase 2 — actions + alerts
- [ ] Start/stop/restart/backup/save with the same confirmation semantics
      (player-count warning before stop, typed confirm for restore).
- [ ] Foreground "session" service while a long action runs; resumable on
      network change (SSH channel re-establish + idempotent RPC ids).
- [ ] Push alerts: tiny UnifiedPush/ntfy bridge — watchdog already has the
      webhook hook; add `ntfy_url` config alongside `webhook_url`.
      *(server-side bridge pulled forward to v0.5.0; here = app subscription side)*

### Phase 3 — polish
- [ ] spark profiler launcher with result URL → in-app browser.
- [ ] TPS/heap history charts from `metrics.jsonl` (synced over the RPC).
- [ ] Wear OS tile (TPS at a glance); app shortcuts ("Backup now").

### Security model (non-negotiable)
- SSH only; no new open ports, RCON stays tunneled exactly as today.
- Per-device keypair, revocable server-side; biometric unlock for *actions*,
  none needed for read-only status.
- The agent runs as the same `ubuntu` user — no privilege expansion.

### Testing strategy
- Phase 0 tested in this repo (pytest, golden schemas, FakeTransport).
- Android: JVM unit tests against a recorded RPC fixture server; one
  instrumented happy-path (connect → status → stop confirmation) per release.

---

## Backlog (nice-to-have, unscheduled)
- [ ] `mcctl mods` — list server mods with versions; diff client vs server pack.
- [ ] The Hordes deployment helper (planned pack addition).
- [x] Prometheus textfile exporter from `metrics.jsonl` for Grafana. *(scheduled: v0.5.0)*
- [ ] `mcctl backup restore --to <dir>` for side-by-side world inspection.
- [ ] Off-site backup hook (rclone to OCI Object Storage) after local rotation.
