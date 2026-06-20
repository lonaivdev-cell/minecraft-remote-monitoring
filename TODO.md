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

### Phase 1 — Android MVP — **SHIPPED in [android/](android/)**
- [x] Kotlin + Jetpack Compose; sshj with an **Ed25519 device key**, held in
      `EncryptedSharedPreferences` (Android Keystore master key) — only the public
      key leaves the device; authorize it on the box like any other client. Host-key
      TOFU with fingerprint display; rotate the key in-app.
- [x] Screens — far past the MVP card: live Overview (status + actions + watchdog
      arm), TPS/MSPT/heap/RAM/players/load **history charts**, console, log tail,
      players, backups, mods, properties, JVM, crashes+postmortem, inspect, profiler,
      events. Lush Minecraft theme (grass/dirt/stone/redstone palette, pixel fonts).
- [ ] Home-screen widget: TPS + player count via WorkManager periodic refresh.
- [ ] Alerting v1: keep the existing webhook → Discord channel (zero new infra).

### Phase 2 — actions + alerts
- [x] Start/stop/restart/backup/save with the same confirmation semantics
      (player-count warning before stop, typed confirm for restore) — plus a
      biometric gate for every state change and capability/confirm gating that
      matches the agent's.
- [ ] Foreground "session" service while a long action runs; resumable on
      network change (SSH channel re-establish + idempotent RPC ids).
- [ ] Push alerts: app subscribes to the ntfy topic (server-side bridge already
      shipped in v0.5.0). Live `events.subscribe` streaming is done; background
      push is the remaining piece.
- [ ] AI screen: wire `mcctl ai`-style analysis (currently a deliberate placeholder).

### Phase 2.5 — recipe browser + command-craft  → **brain shipped**
The "pick a recipe on my phone and have it crafted" ask. mcctl can't reach the
client's crafting GUI (that's a client mod), so the *outcome* is reproduced over the
console — browse recipes from the jars+datapacks, then consume inputs (`/clear`) and
grant output (`/give`), loose-inventory-only so it stays survival-honest.
- [x] `crafting.py`: jar+datapack recipe scan (pure parsers, tested), live-inventory
      plan, and a survival-safe craft engine (anti-dupe: never grants more than it
      removed). `[crafting]` config — player/source_player/receiver, one-stack cap.
- [x] CLI: `mcctl recipes search|show`, `mcctl craft <id> [--count|--max] [--preview]`.
- [x] Agent contract: `recipes.search`, `recipes.get`, `craft.preview`, `craft.do`
      (actions + confirm gated), golden-schema regenerated.
- [x] **Android screen (the renderer):** `CraftingScreen` — recipe picker (search →
      grid + ingredients), a live `craft.preview` plan, and a **press-and-hold craft
      button** → `craft.do {count:null}` (hold-to-max) vs tap → `count:1`, with the
      biometric gate like other actions. `[crafting].hold_ms` is honored — surfaced in
      the `craft.preview` plan and rendered as the hold threshold.
- [x] Tag display: `recipes.tag` resolves a `#tag` predicate to its concrete items
      (jar+datapack scan, pure-tested merge/recursion); the phone expands a tag
      ingredient on demand, and `mcctl recipes tag <id>` renders the same on the CLI.

### Phase 2.6 — EMI parity: icons, full recipe compat, interactive browser
Make the phone's recipe browser feel like [EMI](https://emi.dev): real item
**icons**, an item index searchable by name, **every** vanilla recipe category, and
click-through (item → recipes that make it / uses). EMI is a *client* mod with the
resource packs on hand; mcctl is server-side, so the brain reads the same files EMI
reads (mod jars + `resourcepacks/`) and ships the item index + PNGs down the SSH
channel for the app to cache and render offline. Decided 2026-06-20:
**bundle all item PNGs to the phone**, cover **all EMI categories**, backend first.

- [x] **Backend (the brain), tested in this repo — PR #1:**
  - `crafting.py` now parses every vanilla data-driven category EMI shows —
    crafting (shaped/shapeless), the cook family (smelting/blasting/smoking/
    campfire, with cook time + xp), stonecutting, and smithing-transform. The
    plan/craft engine reproduces each outcome with the same survival-honest
    `/clear`+`/give`. `search_recipes` gained `offset` so a client can page the
    whole pack into a cache.
  - `assets.py` (new): one server-side pass reads `assets/<ns>/lang/en_us.json`
    and the item/block **models** from the mod jars + `resourcepacks/`; pure,
    tested resolvers turn that into a manifest (`{id, name, icon}`) — model
    `parent`-chain walk → representative texture, lang → display name. A second
    pass returns the icon **PNG bytes** (base64) for offline caching. Resource
    packs override mods override vanilla (load order), like the recipe/tag scans.
  - Agent contract (additive, no protocol bump): `items.manifest` (paged item
    index), `icons.fetch` (PNGs by texture id), `recipes.search` gained `offset`.
    Golden schema regenerated.
  - CLI: `mcctl items list|search|icon`.
- [x] **Android `:core` bindings — PR #3:** typed `AgentClient` methods for the new
      contract — `itemsManifest` (paged), `iconsFetch` (base64→`ByteArray` for
      `BitmapFactory`), `assetsSync`, and `recipesSearch(offset=…)` for paging — plus
      models (`ItemEntry`/`ItemManifest`/`IconBatch`/`VanillaSync`) and the `Recipe`
      fields for the new categories (`category`/`cookingTime`/`experience`). Pure JVM,
      tested by `:core:test` (no SDK).
- [x] **Android UI — PR #4 (the renderer):** an `ItemsScreen` — EMI-style icon grid,
      search-by-name, tap an item → the recipes that make it → the existing craft view.
      `IconCache` (app-scoped) batch-fetches `icons.fetch` PNGs and decodes them to
      `ImageBitmap`, drawn crisp with `FilterQuality.None` (no new dep); a "Get vanilla
      icons" button runs `assets.sync`. Registered in the nav drawer (Manage group).
- [ ] **EMI polish — PR #5:** icons *inside* the recipe panel (result + ingredient
      pictures, furnace-arrow for the cook family), full uses/making click-through (tap an
      ingredient → what it makes / what uses it, over the cached recipe set), a persistent
      on-disk icon cache for true offline, and a craftable-only filter.
- [x] **Vanilla icons — PR #2:** a server has no client `assets/` (mods carry their
      own), so `assets.py` now fetches the **matching Mojang client jar** and caches it
      where the scans look first (lowest priority — mods/resourcepacks still override).
      Version is auto-detected (logs/libraries probe) or set via `[server].mc_version`;
      the manifest→client-jar selection is pure + tested, the sha1-verified download runs
      server-side ("brain on the box"). Surfaces: `assets.sync` agent method (actions-gated)
      + `mcctl assets status|sync`. Verified end-to-end (probe + resourcepack-over-vanilla
      override) through `LocalTransport`.
- [ ] **Stretch:** favorites, craftable-only filter, recipe-tree cost breakdown
      (EMI's killer feature — total base materials + leftovers for a deep craft).

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
