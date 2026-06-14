# mcctl for Android

A native Android companion for the `mcctl` Minecraft server toolkit — manage your
CarborioLand box from a phone with a lush, Minecraft-themed UI.

It is a **thin client over `mcctl agent`** (the JSON-RPC 2.0 server the desktop's
`DESIGN-0.5.0.md` introduced): the app opens one SSH channel, runs `mcctl agent`, and
renders the contract. The tested Python core on the box stays the single source of truth —
*one brain, two faces*. No new ports, no stored passwords; auth is a per-device SSH key.

## What it does

Feature parity with the desktop GUI, adapted to a phone, every screen backed by a real
agent method:

| Screen | Agent methods |
|---|---|
| **Overview** | `status`, `start`/`stop`/`restart`/`kill`, `save`, `backup.create`, `purge`, `watchdog.arm`/`.disarm` |
| **History** | `metrics.history` → six pixel charts (TPS, MSPT, heap %, players, RAM %, load) |
| **Console** | `cmd` |
| **Logs** | `logs.tail` |
| **Events** | `events.list` + live `events.subscribe` stream (watchdog heals/alerts) |
| **Players** | `players.list`, `players.whitelist`/`.op`/`.kick`/`.ban` |
| **Backups** | `backup.list`/`.create`/`.prune`/`.verify`/`.restore` (typed confirm) |
| **Mods** | `mods.list` |
| **Mod Configs** | `config.tree`/`.get`/`.set` (browse by mod, edit in place, save = live-reload + restart) |
| **Properties** | `props.list`/`.set` (validated) |
| **JVM** | `jvm.show`/`.heap` |
| **Crashes** | `logs.crashes`, `postmortem` |
| **Inspect** | `inspect` (16 OS/JVM sections) |
| **Profiler** | `profile` → spark.lucko.me URL |
| **AI** | placeholder (see below) |

The **AI** screen is an intentional stub: running an LLM (cloud key on a phone, or an
on-device model) is its own project and out of scope for this first cut. The agent already
carries everything the analysis would need, so it's wiring left for a later cycle.

## Architecture

Two Gradle modules:

- **`:core`** — pure Kotlin/JVM, Maven-Central-only. The JSON-RPC client (`AgentClient`),
  `@Serializable` models mirroring the golden schema, the sshj transport, and the Ed25519
  device key + OpenSSH encoding. **Builds and unit-tests anywhere — no Android SDK needed.**
- **`:app`** — Jetpack Compose UI (Material 3), ViewModels, encrypted key storage and the
  biometric gate. Needs the Android SDK + Google's Maven.

```
phone ──ssh──> mcctl agent (on the box) ──> ServerControl / backup / console / …
   │  :app (Compose)         │ :core (AgentClient over SshAgentTransport)
   └── renders ──────────────┘
```

### Security model (unchanged from `TODO.md`)

- **SSH only**, no new open ports; RCON stays tunneled exactly as the CLI does it.
- **Per-device Ed25519 key**, generated on the phone and held in `EncryptedSharedPreferences`
  (Android Keystore). Only the *public* key leaves the device — you authorize it on the box.
  Revoke it server-side like any other key; rotate it in-app under **Connection → New key**.
- **Host key TOFU**: the server's fingerprint is shown on first connect and pinned; a changed
  key warns loudly.
- **Biometric gate** for state-changing actions (off for read-only status). Destructive
  methods (`kill`, `restore`, `props.set`, `jvm.heap`, `ban`) additionally need the
  `destructive` capability and an explicit typed/confirm step — the agent enforces this too.

## Building

The dev sandbox where this was written can't reach Google's Maven, so the APK is built by
CI (`.github/workflows/android.yml`) and uploaded as the **`mcctl-debug-apk`** artifact on
every push. To build locally you need the Android SDK (platform 35, build-tools 35):

```bash
cd android
./gradlew :app:assembleDebug          # -> app/build/outputs/apk/debug/app-debug.apk
```

The pure-Kotlin core builds and tests without the SDK (and is what the sandbox verifies):

```bash
cd android
./gradlew :core:test
```

`settings.gradle.kts` includes `:app` only when an Android SDK is configured (via
`ANDROID_HOME` or `local.properties`), so `:core` builds cleanly on a box without one.

## First run

1. Open the app → **Connection**. It shows this device's public key.
2. On the box, append that line to the server user's `~/.ssh/authorized_keys`
   (the same user `mcctl agent` runs as).
3. Enter host / user / port, pick capabilities, and tap **Connect**. Verify the host-key
   fingerprint on first connect.

## Fonts & licensing

Three SIL OFL pixel fonts give the retro look — **Press Start 2P**, **VT323**, **Silkscreen**
— with their license texts bundled in `app/src/main/assets/licenses/`. No Minecraft assets
are used; the palette and grass-block icon are originals in that spirit.
