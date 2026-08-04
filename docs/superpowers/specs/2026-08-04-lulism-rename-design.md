# mcctl → LULiSM rename (2.0.0)

**Status:** approved design, ready for implementation planning
**Date:** 2026-08-04
**Scope:** Phase 1 of the LULiSM generalisation only — a pure refactor with zero
behaviour change.

## 1. Context

`DESIGN 0.6.0` proposes generalising the mcctl core from "one Minecraft server
over SSH" to "N services on N hosts over SSH" across nine phases. That document
was written against a 0.6.0-era tree; the repository is at **v1.1.2**
(`867adb4`). Several of its instructions are stale as a result, and are
superseded here:

| `DESIGN 0.6.0` says | Superseded by |
|---|---|
| ship at 0.6.0 | ship at **2.0.0** (§3) |
| delete the `mcctl` shim at 0.8.0 | remove at **3.0.0** (§7) — 0.8.0 shipped 2026-06-19 |
| migrate a **v0.5** config | migrate the **v1.1.2** config shape (§6) |
| rewrite `[server]` into `[services.minecraft]` | **deferred to Phase 2** (§2) |
| units live in a root `systemd/` dir | they live in `src/mcctl/units/` (§6.2) |
| (silent on Android) | `android/` is **out of scope** (§4) |

This spec covers **only** the rename. Phases 2–9 (the `Service` protocol,
`PalworldService`, `HostService`, the multi-service watchdog, `AGENT_PROTOCOL=2`,
the eww widget, the loserver migration, Android) each get their own spec.

### Baseline

`main` at `867adb4`, v1.1.2, `399 passed, 1 skipped` in 46.6s, ruff clean.
Every phase gate in this document means that suite, green.

## 2. The load-bearing constraint: zero behaviour change

`DESIGN 0.6.0` §1 contains a contradiction. It requires both:

> rewrite the `[server]` table into the new `[services.minecraft]` shape

and

> **Do not touch behaviour in this phase.** The test suite should pass with only
> import-path and string edits.

These are incompatible. Reshaping `[server]` → `[services.minecraft]` requires a
new dataclass tree, a new loader, new validation, and edits to every `cfg.server`
consumer. Critically, `agent.build_schema()` derives the wire contract *from the
config dataclasses* (`config.server` → `_dataclass_types(ServerCfg)`), so
reshaping config changes the phone's frozen contract mid-rename.

**Resolution:** Phase 1 relocates config; it does not reshape it. All seven
sections (`server`, `backup`, `watchdog`, `metrics`, `llm`, `ui`, `crafting`)
stay byte-identical. The reshape belongs to Phase 2, where the `Service`
abstraction actually needs it.

### The proof obligation

`tests/golden/agent_schema_v1.json` contains **zero** occurrences of the string
`mcctl`. The contract is built from method names, dataclass field names and type
names — none carry the product name. Therefore:

- `AGENT_PROTOCOL` stays `1`.
- The golden file must come out **byte-identical**.
- `tests/test_agent_schema.py` is the headline regression test: if it fails, the
  rename leaked into the wire protocol.

Do **not** regenerate the golden during Phase 1. A diff there is a bug report.

## 3. Version

Ships as **2.0.0**. Config, state, cache and systemd unit paths all move, and the
operator must act on the server (disable old units, reinstall). That is a
breaking operational change even though the CLI shim preserves command
compatibility.

Per `CLAUDE.md`, `src/lulism/__init__.py` `__version__` is the single source of
truth, and merging to `main` auto-cuts a release. Consequences:

- `versionCode` goes `10102` → `20000` (`major*10000 + minor*100 + patch`). Legal
  increase, so Obtainium upgrades normally.
- The 2.0.0 merge publishes `mcctl-android-v2.0.0.apk` with **no Android code
  change**. Expected, not a mistake.
- The `v2.0.0` tag is created automatically and is the one-way door (§9).

## 4. Scope

`APP = "mcctl"` at `util.py:17` is a single chokepoint feeding `config_dir()`,
`state_dir()`, `cache_dir()`, `runtime_dir()`, the root logger name,
`notify-send -a`, and the outbound HTTP `User-Agent`. One constant relocates four
XDG directories.

**In scope:** `src/`, `tests/`, `completions/`, `data/` (desktop entry + icon),
`PKGBUILD`, `pyproject.toml`, `Makefile`, `update.sh`, the version path in
`release.yml`, and the docs (`README.md`, `CLAUDE.md`, `TODO.md`).

**Out of scope:** everything under `android/` (537 of the ~1,300 `mcctl`
occurrences).

### 4.1 Why Android is excluded

`ConnectionProfile.kt:15` and `SshAgentTransport.kt:23` declare
`val agentCommand: String = "mcctl agent"`, and `ProfileStore` persists it to a
DataStore. **Every phone in the field has the literal string `mcctl agent` saved
in its profile.** Shipping a new APK with a new default does not fix them — the
stored value wins. The `mcctl` shim, not an APK change, is what keeps those
phones working.

`android/` is also dense with persisted identity strings, each its own data-loss
event if renamed:

| String | Location | Cost of renaming |
|---|---|---|
| `mcctl_secure` | `SecureStore.kt:24` | **stored SSH private key lost** |
| `mcctl_profile` | `ProfileStore.kt:13` | host/user/agent-command reset |
| `mcctl_push` | `PushStore.kt:12` | push config reset |
| `mcctl_alerts`, `mcctl_session` | `McctlNotifications.kt:19-20` | notification prefs reset |
| `mcctl_ntfy_poll` | `PushScheduler.kt:15` | duplicate periodic work |
| `applicationId` | `build.gradle.kts:27` | **no upgrade path at all** |

`CLAUDE.md` also records that the dev sandbox cannot reach Google's Maven, so the
APK builds only in CI — an Android rename could not be verified locally. It
becomes its own phase.

### 4.2 Preserve-list

These occurrences of `mcctl` **must survive verbatim**. A blind find-and-replace
breaks all of them.

| Must stay `mcctl` | Why |
|---|---|
| everything under `android/` | §4.1 |
| `provides`/`replaces`/`conflicts=('mcctl')` in `PKGBUILD` | how pacman finds the old package to replace |
| the `mcctl` console-script name | it *is* the shim |
| `~/.config/mcctl`, `~/.local/state/mcctl`, `~/.cache/mcctl` literals in the migrator | the migrator must read the old paths |
| `mcctl-*.service` / `mcctl-*.timer` literals in `doctor` | must recognise old units to warn about them |
| `mcctl-android-v*.apk` in `release.yml:164` | Obtainium's release asset name |
| `mcctl-debug-apk` in `android.yml:60` | CI artifact for the untouched Android build |
| the 13 `mcctl_*` Prometheus metric names | §6.4 — a public interface; Grafana and alert rules query them by name |
| historical incident references (`2026-06-11`, "mcctl postmortem") | they name what actually happened |

## 5. Commit sequence

Seven commits. Each leaves `make test-all` green, so a misbehaving 2.0.0 upgrade
on `loserver` bisects to one labelled commit rather than an ~800-file diff.

| # | Commit | Gate |
|---|---|---|
| 1 | `git mv src/mcctl src/lulism`, rewrite imports, `APP = "lulism"` | `make test-all` |
| 2 | entry points (`lulism`, `lulism-gui`) + the `mcctl` shim | shim tests (§8) |
| 3 | config/state/cache migration + tests | v1.1.2-config-loads-clean |
| 4 | unit rename + migration + `doctor` legacy check | interlock tests (§8) |
| 5 | packaging: `PKGBUILD`, `release.yml`, completions, desktop/icon | every `install -Dm644` source path exists |
| 6 | docs sweep (`README.md`, `CLAUDE.md`, `TODO.md`) | — |
| 7 | `__version__ = "2.0.0"` | full suite |

Commit 1 uses `git mv` so rename detection survives in history.

## 6. Migration mechanics

### 6.1 XDG directories

A single `util.migrate_legacy_dirs()`, called once from `cli.main()` and
`gui.main()` — not from `config_dir()`, which is called constantly.

Behaviour: for each of config, state and cache, if the `lulism` directory is
absent and the `mcctl` one exists, **copy** (never move) and log what happened.

- **Copy, not move**, so the originals remain as the rollback path (§9).
- **Idempotent.** Running twice is a no-op, and it never clobbers an existing
  `lulism` directory.
- `cache_dir()` is included: it holds the EMI icon set, and orphaning it forces a
  full re-download of the modpack's icons.
- `runtime_dir()` is **not** migrated — it holds ephemeral SSH ControlMaster
  sockets. Note that `lulism` is one byte longer than `mcctl` against the ~104-byte
  `sun_path` limit; the existing short-path logic already guards this, but the
  margin shrinks by one.

Existing tests are unaffected: `conftest.py:16`'s autouse `isolated_xdg` fixture
points all four XDG vars at `tmp_path`, so no legacy directory exists and the
migration is a no-op in all 399 of them.

### 6.2 systemd units — the outage risk

`src/mcctl/units/` ships seven files: `mcctl-watchdog.service`, plus
`autosave`/`backup`/`metrics` as `.service` + `.timer` pairs. `cli.py:647` tells
the operator to enable three of them (`mcctl-watchdog.service`,
`mcctl-backup.timer`, `mcctl-autosave.timer`).

`doctor.py:217` is headed:

```python
# ---------------- one restart authority + boot resilience (2026-06-11 incident)
```

There was a real outage on 2026-06-11 caused by multiple restart authorities
fighting, and `doctor` already carries a legacy-watchdog detection check because
of it. **Renaming the units re-creates that exact condition**: the old units stay
enabled and running after the upgrade, the new ones get installed alongside, and
two watchdogs both believe they own restart authority. Nothing in a file rename
prevents this, and the §6.1 config migration does not touch systemd.

Required order — **stop → disable → remove old, then install and enable new**:

1. `systemctl --user stop` the old units. Operate on **all seven** shipped names,
   not just the three in the enable hint — an operator may have enabled
   `mcctl-metrics.timer` by hand, and it is the one most likely to be forgotten.
2. `systemctl --user disable` them.
3. Remove the old unit files from `$XDG_CONFIG_HOME/systemd/user`.
4. `systemctl --user daemon-reload`.
5. Write and enable the `lulism-*` units.

`doctor` learns the `mcctl-*` unit names as legacy, so a half-migrated box is
**reported as dual-armed** rather than silently running two healers.

### 6.3 A latent bug the rename surfaces

`cli.py:639` renders units with:

```python
units = util.render_units(exe=sys.argv[0] if sys.argv[0].endswith("mcctl") else "mcctl")
```

After the rename, invoking `mcctl watchdog install` *through the shim* leaves
`argv[0]` ending in `mcctl`, so freshly-installed units would hardcode
`ExecStart=…/mcctl …` — a permanent dependency on the deprecated shim. The
predicate must key on `lulism`, and units must never render an `ExecStart`
pointing at the shim regardless of how the CLI was invoked.

Shipped units currently use the absolute `ExecStart=/usr/bin/mcctl watchdog run`;
the renamed units use `/usr/bin/lulism`.

### 6.4 Prometheus: metric names stay, the scrape path moves

`prometheus.py` exports thirteen metrics, all prefixed `mcctl_`:

```
mcctl_up  mcctl_tps  mcctl_players  mcctl_load1  mcctl_mspt_milliseconds
mcctl_heap_used_bytes  mcctl_heap_max_bytes  mcctl_disk_free_bytes
mcctl_log_age_seconds  mcctl_scrape_timestamp_seconds
mcctl_host_mem_used_bytes  mcctl_host_mem_total_bytes
mcctl_watchdog_restarts_total
```

**The metric names do not change in 2.0.0.** They are a public interface: Grafana
panels and Prometheus alerting rules query them by name, and renaming produces a
permanent seam in TSDB history where the old series stop and new ones begin.
Renaming metrics is a behaviour change, which this phase forbids. Phases 2 and 5
make metrics per-service and will revisit the naming once, deliberately, with a
documented bridge — renaming here would mean renaming twice.

The output **file** is not an interface: node_exporter's textfile collector globs
`*.prom` in a directory. `prometheus.py:69`'s `state_dir() / "mcctl.prom"`
therefore becomes `state_dir() / "lulism.prom"`.

**The scrape path break is unavoidable and needs a release note.** `state_dir()`
moves from `~/.local/state/mcctl/` to `~/.local/state/lulism/`, so a node_exporter
configured with `--collector.textfile.directory=…/.local/state/mcctl` silently
stops finding the file. Metrics go stale with no error surfaced anywhere. `doctor`
gains a check for this: if a legacy `~/.local/state/mcctl/*.prom` exists and is
newer than the `lulism` one, or the configured collector directory still points at
the `mcctl` path, report it.

`config.py:93`'s `prom_path` defaults to `""` (computed from `state_dir()`), so
default installs follow the move automatically. An operator who set `prom_path` to
an explicit absolute path keeps writing to that path — correct, and left alone.

## 7. The shim

`mcctl` remains a real console script (`mcctl = "lulism.shim:main"`). It writes
one deprecation line **to stderr**, then `os.execvp("lulism", ["lulism", *argv[1:]])`.

**Stderr is non-negotiable.** `mcctl agent` is a JSON-RPC 2.0 NDJSON stream on
stdout, and every phone in the field still invokes it by that name (§4.1). A line
on stdout corrupts the first frame and breaks every installed phone on contact.

`execvp` (not a subprocess) preserves exit codes, signal behaviour and stdio
wiring transparently. mcctl's exit vocabulary — `0` ok / `1` error / `2` usage /
`3` unreachable — passes through unchanged.

Removal horizon: **3.0.0**.

## 8. Testing

### 8.1 Contract

`tests/test_agent_schema.py` must pass **without regenerating the golden**. A
byte-identical `agent_schema_v1.json` is the proof that Phase 1 changed nothing
the phone can observe.

### 8.2 New coverage

All new tests ride the existing `isolated_xdg` fixture.

**`tests/test_migrate.py`**
- A real v1.1.2 `config.toml` fixture planted in `~/.config/mcctl/` loads clean
  under `lulism`, with all seven sections intact.
- Idempotency: running the migration twice is a no-op and never clobbers an
  existing `lulism` config.
- Originals survive (rollback path).
- Cache is migrated.
- No legacy directory → no-op.

**Shim tests**
- **The load-bearing one:** run `mcctl agent` and assert stdout's first frame
  parses as JSON. This is the regression test for "we broke every phone."
- The deprecation notice appears on stderr, never stdout.
- argv passthrough.
- Exit-code passthrough across `0/1/2/3`.

**Unit migration**
- Table-driven over (old unit enabled?, new unit enabled?) → expected `doctor`
  verdict, so a half-migrated box reports dual restart authority.
- `render_units` never emits an `ExecStart` pointing at the shim, even when
  `argv[0]` ends in `mcctl` (§6.3).

**Prometheus**
- The thirteen exported metric names are asserted **verbatim** against a frozen
  list, so a later blanket substitution cannot quietly rename a public interface
  (§6.4). This is the metrics counterpart to the golden schema test.

**Leak guard**
- No `mcctl` identifier survives in `src/lulism/` outside the §4.2 preserve-list.
  This is what catches a partial rename several commits later.

## 9. Packaging, CI and rollback

### 9.1 Python packaging

```toml
name = "lulism"
version = { attr = "lulism.__version__" }

[project.scripts]
lulism = "lulism.cli:main"
mcctl  = "lulism.shim:main"      # deprecated, removed at 3.0.0

[project.gui-scripts]
lulism-gui = "lulism.gui:main"
```

### 9.2 PKGBUILD

`pkgname=lulism` with `provides=('mcctl')`, `replaces=('mcctl')`,
`conflicts=('mcctl')` — that triple is what makes pacman remove the old package
cleanly. Seven renamed unit install lines, `completions/lulism.fish`, and the
desktop/icon pair.

The desktop entry is a reverse-DNS application ID
(`io.github.lonaivdev_cell.mcctl`). Renaming it makes the desktop environment
treat it as a **new** application: pinned launchers and dock entries reset.
Unavoidable, and it belongs in the release notes.

### 9.3 CI

- `release.yml:37-38` reads `__version__` from a hardcoded
  `src/mcctl/__init__.py` and has an explicit `|| exit 1`. It moves to
  `src/lulism/__init__.py`; a miss fails the release job loudly rather than
  shipping a mis-versioned APK.
- `release.yml:164`'s `mcctl-android-v*.apk` asset name **stays** (Obtainium).
- `android.yml` is untouched.

### 9.4 update.sh

`update.sh` is the actual upgrade path on the box: it pulls, reinstalls with
`pipx install --force .`, restarts the watchdog, and runs a before/after health
check. It must perform the §6.2 unit migration in the correct order — it is
precisely the component that would otherwise re-create the 2026-06-11
dual-authority outage. Its `mcctl --version` freshness check becomes
`lulism --version`.

### 9.5 Rollback

Because §6.1 copies rather than moves, the `mcctl` config, state and cache trees
survive intact. Reverting is: reinstall 1.1.2, re-enable the `mcctl-*` units. The
one-way door is the `v2.0.0` git tag, which `release.yml` creates automatically
on merge to `main`.

## 10. Out of scope

Deferred to their own specs: the `Service` protocol and registry (Phase 2),
`PalworldService` (Phase 3), `HostService` (Phase 4), the multi-service watchdog
and exclusivity interlock (Phase 5), `AGENT_PROTOCOL=2` namespacing (Phase 6),
the eww widget (Phase 7), the Minecraft migration to loserver (Phase 8), and the
Android client (Phase 9).

Also explicitly deferred: any `android/` change, and the `[server]` →
`[services.minecraft]` config reshape.
