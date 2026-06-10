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

### Phase 0 — API extraction (in this repo)
- [ ] `mcctl agent` subcommand: long-lived JSON-RPC 2.0 loop on stdin/stdout
      (methods: `status`, `start`, `stop`, `backup.create`, `backup.list`,
      `tps`, `health`, `players`, `cmd`, `logs.tail`, `watchdog.state`).
- [ ] Version-stamped schema (`mcctl agent --schema`) generated from the
      dataclasses; golden-file tests so the contract can't drift silently.
- [ ] Long-poll `events` method: watchdog actions/alerts streamed as they happen.

### Phase 1 — Android MVP (read-mostly)
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
- [ ] Prometheus textfile exporter from `metrics.jsonl` for Grafana.
- [ ] `mcctl backup restore --to <dir>` for side-by-side world inspection.
- [ ] Off-site backup hook (rclone to OCI Object Storage) after local rotation.
