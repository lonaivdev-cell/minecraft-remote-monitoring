# CLAUDE.md — working notes for this repo

`mcctl` is an Arch-Linux CLI/TUI + GTK desktop app + Android companion that fully
drive a remote modded Minecraft server over SSH (RCON through an SSH tunnel, with a
tmux + log-offset fallback). The guiding principle is **one brain, many faces**: the
tested Python core is the single source of truth, and every other surface (CLI, GTK
GUI, `mcctl agent` JSON-RPC, the phone) just *renders* it. When adding a feature,
put the logic in a core module with pure, tested functions and expose it — never
reimplement it per surface.

Full feature/architecture overview lives in **[README.md](README.md)**; the roadmap in
**[TODO.md](TODO.md)**; design records in `DESIGN-0.5.0.md` and `DESIGN-BRAIN.md`.

## Layout

```
src/mcctl/        the Python core + every surface
  cli.py          argparse tree (exit codes: 0 ok / 1 error / 2 usage / 3 unreachable)
  config.py       TOML config dataclasses, validation, template
  transport.py    SSH ControlMaster wrapper + LocalTransport (dev/tests)
  console.py      RCON-over-tunnel → tmux+log fallback
  server.py       status probe + start/stop/restart state machine
  agent.py        `mcctl agent` JSON-RPC 2.0 server over stdio (the phone's contract)
  crafting.py     recipe browser (jar+datapack scan, all EMI categories) + command-craft engine
  assets.py       EMI-style item index: lang display names + model→icon resolution + icon PNG fetch
  gui_app.py      GTK4 + libadwaita desktop app (single SSH worker thread)
  …               backup/watchdog/spark/metrics/props/players/logs/mods/modconfig/…
tests/            pytest; FakeTransport + FakeClock, golden schema, integration (real tmux)
android/          Kotlin companion: :core (pure JVM, no SDK) + :app (Compose)
.github/workflows ci.yml (python) · android.yml (APK CI) · release.yml (tagged release)
```

## Common commands

Python (run from the repo root):

```bash
make dev        # editable install + pytest + ruff
make test       # unit tests (seconds; FakeTransport, no server needed)
make test-all   # + integration suite (real tmux driving a fake "java")
make lint       # ruff
pytest tests/test_crafting.py -q     # one module
```

Android (run from `android/`):

```bash
./gradlew :core:test          # pure-Kotlin protocol layer — no Android SDK needed
./gradlew :app:assembleDebug  # the APK (needs the Android SDK: platform 35, build-tools 35)
```

## Conventions that bite if ignored

- **The agent contract is frozen by a golden file.** Adding/removing an agent method or
  changing a config dataclass changes `mcctl agent --schema`, so `tests/test_agent_schema.py`
  fails until you regenerate the golden:

  ```bash
  python -c "import json,sys; sys.path.insert(0,'src'); from mcctl import agent; \
    json.dump(agent.build_schema(), open('tests/golden/agent_schema_v1.json','w'), \
    indent=2, sort_keys=True); open('tests/golden/agent_schema_v1.json','a').write('\n')"
  ```

  Keep changes **additive** within a major version; only bump `agent.AGENT_PROTOCOL` for a
  genuinely breaking change (see the comment on it).
- **Surfaces render, they don't reimplement.** New behavior → a core module with pure
  functions + tests, then thin wrappers in `cli.py`, `agent.py` (a `@method`), and
  `gui_app.py` (a page). Example: `crafting.py` is rendered by all three.
- **Remote text is untrusted.** Sanitize console/log output (`util.strip_mc_codes` /
  `sanitize_terminal`); validate any value (player names, item ids) before it reaches a
  console command. This pack's crash logs are known to carry prompt-injection text.
- **mcctl is server-side.** It drives the server over RCON/console; it cannot reach a
  player's client GUI. Features like command-craft reproduce the *outcome* via
  `/clear`+`/give` (loose inventory only → can't dupe), not by touching the game UI.
- **The version is gospel — bump it with the change.** `src/mcctl/__init__.py` `__version__`
  is the **single source of truth** (`pyproject.toml` reads it dynamically via
  `[tool.setuptools.dynamic]`; the Android APK derives its `versionName`/`versionCode` from it
  in `release.yml`). Any PR with a user-facing change (CLI/agent/GUI/phone) **bumps
  `__version__`** (semver) in the same PR, so `mcctl --version` is an honest freshness signal
  and merging to `main` auto-cuts a release. No bump → no release. The number must only ever go
  **up** (`versionCode = major*10000 + minor*100 + patch` must increase or Obtainium won't
  upgrade).

## CI/CD pipeline

Three GitHub Actions workflows:

| Workflow | Trigger | What it does |
|---|---|---|
| **`ci.yml`** | every push / PR | Python: `ruff` + unit + integration (`real tmux`) tests |
| **`android.yml`** | push/PR touching `android/**` | `:core` tests → builds the debug APK → uploads the **`mcctl-debug-apk`** artifact (CI check, not a release) |
| **`release.yml`** | push to `main` (auto-cuts when `__version__` changed), or manual **Run workflow** | reads the gospel version; if `v<version>` isn't released yet, builds the APK and publishes a **GitHub Release** tagged `v<version>` with it attached — the artifact Obtainium installs |

### Cutting an Android release (→ Obtainium)

**You don't tag by hand — the version does it.** Bump `__version__` in
`src/mcctl/__init__.py` as part of your PR (see the "version is gospel" convention above).
When that PR merges to `main`, `release.yml`:
1. reads `__version__` and computes `tag = v<version>`,
   `versionCode = major*10000 + minor*100 + patch` (e.g. `1.1.2` → `versionCode 10102`),
2. **no-ops if a `v<version>` tag already exists** (a merge that didn't touch the version
   ships nothing) — otherwise runs the `:core` gate,
3. builds a **signed release** APK if the keystore secrets are set, else the
   **debug-signed** APK as a side-loadable fallback,
4. names it `mcctl-android-v<version>.apk`, **creates the `v<version>` tag** and publishes a
   GitHub Release with the APK attached.

Need to re-cut a version (same number)? **Actions → release → Run workflow** and tick
**force**. (The manual run still reads `__version__`; it never asks you to type a version.)

**Install on the phone:** [Obtainium](https://github.com/ImranR98/Obtainium) → Add App →
paste `https://github.com/lonaivdev-cell/minecraft-remote-monitoring`. Obtainium watches the
Releases page and offers every new tag as an update.

**Two rules or upgrades silently fail:**
- `versionCode` **must increase** each release (the formula above guarantees it as long as
  the semver tag goes up).
- **Stay on one track.** The debug fallback uses `applicationId …mcctl.debug`; Android won't
  upgrade across a different `applicationId`. Set the signing secrets once and stay signed:
  `KEYSTORE_BASE64`, `KEYSTORE_STORE_PASSWORD`, `KEYSTORE_KEY_ALIAS`, `KEYSTORE_KEY_PASSWORD`
  (setup steps are commented in `release.yml`).

### Updating `mcctl` on the server

The phone auto-updates via Obtainium, but the **server's `mcctl` is updated by hand** — and a
skew (newer phone, older agent) is what makes the agent answer `unknown method: …`. The quickest
path is the bundled **`./update.sh`** (or `make update`): it pulls, reinstalls, restarts the
watchdog onto the new code, and runs `mcctl doctor`/`mcctl status` with a before/after health
panel — and it *verifies the install took* (the dropped-`.` trap below). By hand it's:

```bash
cd /path/to/minecraft-remote-monitoring
git pull
pipx install --force .     # NOT `pipx upgrade`: it compares versions and no-ops; --force reinstalls
mcctl --version           # confirm it now reports the version you pulled
```

`pipx upgrade` is a trap with a local checkout: it copies the code into pipx's own venv and
only reinstalls when the **version number** rises, so a same-version pull is a silent no-op.
`pipx install --force .` reinstalls unconditionally. (An editable `make dev` install tracks the
working tree live and needs no reinstall.) Because `__version__` is now bumped per change,
`mcctl --version` is the freshness check — match it against the latest release tag.

> The dev sandbox can't reach Google's Maven, so the **APK only builds in CI** — verify
> Android changes by pushing and watching `android.yml`, not locally.
