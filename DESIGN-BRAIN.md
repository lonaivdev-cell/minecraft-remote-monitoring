# Design — brain placement: the watchdog + state move to the box

**Decision (2026-06-11):** the brain — the watchdog daemon, the intent/state
file (`watchdog.json`), the event journal (`events.jsonl`), the metric history
(`metrics.jsonl`), and the automation timers — lives on the **OCI box**, as
systemd **user** units of the `ubuntu` user with `loginctl enable-linger`
set, with the box-side mcctl configured `transport = "local"`. Every face
(desktop CLI/GUI, the planned Android app, scripts) *renders* that one brain;
no face keeps its own copy of desired/armed/events, ever.

This is the prerequisite gate for Android Phase 1 ([TODO.md](TODO.md)): the
phone must render an always-on truth, and there must be exactly one.

---

## 1. The question, and why it must be answered now

Today the brain is wherever you happen to run mcctl — in practice the Arch
desktop: `mcctl-watchdog.service` runs there, and `state.py`, `events.py`,
`metrics.py` all resolve to the *local* `~/.local/state/mcctl/`
(`util.state_dir()`).

The 0.5.0 Android plan, however, already committed the other half of the brain
to the box without saying so out loud:

- The app's device key is "authorized on the OCI box like any other client"
  (TODO.md Phase 1) and the app runs `ssh carborio mcctl agent` — so the agent
  process runs **on the box**.
- The agent's `watchdog.state` / `watchdog.arm` / `watchdog.disarm` methods
  call `state.load()` / `state.set_armed()` (`agent.py`), and
  `events.subscribe` tails `events.jsonl` — all through `util.state_dir()`,
  which on the box is the **box's** `~/.local/state/mcctl/`.

Put a phone on that agent while the watchdog still runs on the desktop and the
split-brain is not a risk, it is the *construction*:

| The phone does | What actually happens |
|---|---|
| `watchdog.arm` | arms a `watchdog.json` on the box that no watchdog daemon reads |
| `events.subscribe` | tails an `events.jsonl` the desktop watchdog never writes |
| `stop` (Phase 2) | sets `desired=down` on the box; the desktop watchdog reads its own `desired=up` and resurrects the server you just stopped |

That last row is the migration foot-gun (`mcctl stop` must always stand the
watchdog down — `server.py` sets `desired=down` *before anything else*)
reappearing in distributed form. And it is the 2026-06-11 incident's lesson —
**exactly one restart authority, or healers fight** — promoted one level up:
exactly one *machine* may own desired/armed/events.

So, before any Kotlin is written: where does the brain live?

## 2. Options

| Option | Sketch | Verdict |
|---|---|---|
| A. Brain stays on the desktop | watchdog + state on the Arch laptop; the phone reaches the brain through the laptop, or talks to the box and accepts a second truth | ✗ a laptop is a part-time brain: asleep, roaming, NAT'd, behind residential internet — no 3 a.m. healing, no reachable agent for the phone; with the box agent it splits the brain by construction (§1) |
| **B. Brain on the OCI box, linger on** | mcctl installed on the box, `transport = "local"`; watchdog + timers as `ubuntu`'s systemd user units, `loginctl enable-linger ubuntu`; state/events/metrics in the box's `~/.local/state/mcctl/`; phone and desktop both render it over SSH | ✅ **chosen** — the only always-on machine; one state dir next to the server itself; the topology the integration suite already tests |
| C. Brain on both + state sync | run watchdogs on both ends, replicate `watchdog.json`/`events.jsonl` | ✗ two `decide()` loops racing, `OpsLock` is an flock and cannot span machines, last-writer-wins on `desired` — the 2026-06-11 incident as an architecture |

## 3. Why the box wins

1. **It closes the split the 0.5.0 plan already opened.** The agent runs on the
   box and reads the box's state dir (§1). Moving the watchdog and journal to
   the same dir makes `watchdog.*`, `events.subscribe`, `metrics.history`, and
   `status` all describe the same world — phone, desktop, and watchdog share
   one desired/events truth because there is physically only one file of each.
2. **The watchdog's whole value is unattended coverage.** Crash at 3 a.m.,
   freeze while traveling: a brain on the box is awake for all of it. A brain
   on the desktop heals only while the desktop is open, awake, and online —
   and every residential ISP blip is a spurious `ALERT_SSH` or a missed heal.
3. **It is the configuration the tests already exercise.** `transport =
   "local"` is a first-class transport (`make_transport`, `LocalTransport`),
   and the tmux integration suite drives start → console → backup → watchdog
   corpse-detection through it end to end. Brain-on-box is not a new mode; it
   is the tested mode, deployed.
4. **Observation gets better, not worse.** The watchdog's probe (pgrep,
   `/proc/<pid>/cwd`, tmux, log mtime, `jcmd`) becomes local — faster, no SSH
   round-trips, no ControlMaster to keep warm, immune to client-side network
   weather. Freeze/crash detection latency stops depending on the desktop's
   Wi-Fi.
5. **The side pieces all point the same way.** ntfy push fires from box egress
   (a machine that is up to send "I healed the server"); the Prometheus
   textfile lands on the box where node_exporter actually runs (today the
   `.prom` describing the box is written on the desktop); backups already
   execute box-side (`tar | zstd` over the transport) — the timer might as
   well fire next to them.

## 4. What it costs (owned, not hidden)

- **"Nothing to install on the VM" ends.** The README's design-table virtue
  ("server-side deps: bash + coreutils + tmux") narrows to *nothing to
  maintain on the VM except mcctl itself*. This was already conceded the
  moment `ssh carborio mcctl agent` became the app contract — the brain
  decision just stops pretending otherwise. Install is pipx, upgrade is one
  command, and `mcctl doctor` runs on the box too.
- **`ALERT_SSH` inverts.** A box-resident watchdog cannot observe the box's
  own unreachability (`LocalTransport.run` essentially never fails). "Box
  down" detection must come from outside: the phone failing to reach the
  agent, and/or a dead-man ping (ntfy topic that alerts on *silence*) —
  tracked in TODO.md, Phase 0.5.
- **The faces must follow the brain.** Until the v0.6.0 client work lands, a
  desktop `mcctl stop` writes the *desktop's* `desired=down` while an armed
  box watchdog reads the *box's* `desired=up` — resurrection war (§1). The
  migration is therefore **gated on the faces-follow-the-brain work** (§6
  step 2), and `mcctl doctor` now warns whenever watchdog daemons are alive on
  both ends (§7). Do not arm a box watchdog while the desktop one exists.

## 5. Target topology

```
            OCI box (the brain — always on, linger)                 faces (render only)
  ┌────────────────────────────────────────────────────┐
  │ tmux: java (the server)                            │   ┌───────────────┐
  │                                                    │◄──┤ Android app   │ ssh → mcctl agent
  │ systemd --user (ubuntu, linger):                   │   ├───────────────┤
  │   mcctl-watchdog.service   (transport = "local")   │◄──┤ desktop CLI/  │ ssh → mcctl agent
  │   mcctl-backup.timer  mcctl-autosave.timer         │   │ GUI (v0.6.0)  │ (intent ops + state reads)
  │   mcctl-metrics.timer → mcctl.prom → node_exporter │   ├───────────────┤
  │                                                    │◄──┤ scripts       │ ssh carborio mcctl …
  │ ~/.local/state/mcctl/                              │   └───────────────┘
  │   watchdog.json  events.jsonl  metrics.jsonl       │        ▲ ntfy push (alerts, heals)
  │   crashes/  (evidence, thread dumps)               │────────┘
  └────────────────────────────────────────────────────┘
```

Placement, explicitly:

| Piece | Lives on | Notes |
|---|---|---|
| watchdog daemon | box | user unit, `transport = "local"`, probe is local |
| `watchdog.json` (armed/desired/halted/restarts) | box | the *only* copy with authority |
| `events.jsonl` / `metrics.jsonl` / `crashes/` | box | journal written where decisions are made |
| backup/autosave/metrics timers | box | they already act box-side |
| `mcctl agent` | box | unchanged — this was always box-side |
| ntfy / webhook alerting | box | fires from the machine that is up |
| desktop CLI/GUI | desktop | renders + drives the brain (v0.6.0); outside-in checks stay here |
| `mcctl backup pull` mirror, `mcctl ai` (Claude key) | desktop | pull and analysis are face-side by nature |
| RCON exposure probe (`doctor`) | desktop | must probe from *outside* the box |

Box config sketch (`~/.config/mcctl/config.toml` on the box):

```toml
[server]
transport = "local"          # the server is this machine
server_dir = "/opt/minecraft"
# host/user/ssh_* are inert under local transport

[watchdog]
notify_desktop = false       # headless box: no notify-send
ntfy_topic = "…"             # push moves here, fires from box egress
webhook_url = "…"
```

## 6. Migration plan (v0.6.0 — gates Android Phase 1)

1. **Decision + enforcement (this change).** This document; doctor grows
   `ops: brain placement` + `ops: brain linger` (§7) so the dangerous interim
   states are loud.
2. **Faces follow the brain (in-repo work).** New `[brain]` config
   (`location = "client" | "server"`, default `"client"` for compatibility).
   With `location = "server"`, every state-touching desktop operation —
   `start`/`stop`/`restart` intent, `watchdog arm|disarm|status`, `events`,
   `history`/`stats`, `postmortem` — is served by the box (a thin Python
   agent client over the existing SSH transport; the same NDJSON contract the
   phone speaks), and local-state writes are refused. One lock domain, one
   truth, zero replication code.
3. **Bootstrap the box brain** (runbook):
   ```fish
   ssh carborio
     pipx install git+https://github.com/lonaivdev-cell/minecraft-remote-monitoring
     mcctl init && $EDITOR ~/.config/mcctl/config.toml   # transport = "local", §5
     mcctl doctor                                        # box-side preflight
     mcctl watchdog install
     systemctl --user daemon-reload
     systemctl --user enable --now mcctl-watchdog.service mcctl-backup.timer \
                                   mcctl-autosave.timer mcctl-metrics.timer
   sudo loginctl enable-linger ubuntu                    # brain survives logout/reboot
   ```
4. **Cut over — watchdog stand-down rule, applied to its own move.** Order
   matters: on the **desktop** first `mcctl watchdog disarm` and
   `systemctl --user disable --now mcctl-watchdog.service` (and the timers);
   only then `mcctl watchdog arm` **on the box**. Flip the desktop config to
   `[brain] location = "server"`. `mcctl doctor` from the desktop must show
   `ops: brain placement → watchdog runs on the box` and `ops: brain linger →
   ok`; doctor on the box covers layout/JVM/ops from inside.
5. **Decommission.** Desktop `~/.local/state/mcctl/` becomes cache only
   (pulled backups, GUI history cache); the authoritative journal is the
   box's. Add the box-down dead-man ping (or accept "phone can't reach agent"
   as the signal) — TODO.md tracks it.

## 7. Enforcement (shipped with this decision)

`mcctl doctor` now encodes the invariant next to its sibling, the
single-restart-authority check:

- **`ops: brain placement`** — finds `mcctl watchdog run` daemons on both ends
  of the transport. Exactly one (either end) → ok, with the topology named;
  **both → warn** (two brains fight over desired/armed — disable one now);
  none → warn (self-healing is off everywhere).
- **`ops: brain linger`** — when the watchdog-hosting machine is known, checks
  `loginctl show-user <user> --property=Linger` there: without linger, user
  units die at logout and never start at boot — the brain must outlive SSH
  sessions. Warn with the exact `sudo loginctl enable-linger` command.

## 8. Security review

- **No new surface.** Nothing new listens; the agent stays stdio-over-SSH; the
  watchdog talks to localhost. Auth remains SSH keys already authorized for
  `ubuntu@box`; the brain runs as the same unprivileged user that owns the
  server process today. RCON stays tunneled; port 25575 stays closed.
- **Linger is not an exposure.** It only lets `ubuntu`'s user manager run
  without a login session — no port, no privilege, no credential.
- **Secrets posture unchanged or better.** The box stores no new secrets
  (`rcon.password` was always read on demand from `server.properties`, which
  lives there). `ntfy_token`/`webhook_url` move into the box config — same
  sensitivity class as before, one fewer copy on a roaming laptop. The
  Anthropic API key stays desktop-side; `mcctl ai` remains a face feature.
- **Outside-in checks stay outside.** The doctor RCON-exposure probe keeps
  running from the desktop — the box cannot probe its own firewall from
  within.

## 9. The decision is done when

1. This document is merged and TODO.md/README.md point at it. **(this change)**
2. `mcctl doctor` warns on two live watchdogs and on a lingerless brain host,
   with tests. **(this change)**
3. v0.6.0 ships faces-follow-the-brain (§6 step 2) — tracked in TODO.md
   Phase 0.5.
4. The cut-over runbook (§6 steps 3–4) has been executed on CarborioLand and
   doctor is green from both vantage points.
5. Android Phase 1 starts against the box brain — and never learns there was
   ever another place state could live.
