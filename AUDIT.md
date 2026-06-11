# Repository audit — 2026-06-11 (v0.2.0)

Full review of mcctl: functionality, incompatibilities/inconsistencies, GUI
coverage, and security posture. Items marked **[fixed]** were corrected in the
same change-set that introduced this report; items marked **[open]** are
follow-ups.

## 1. Method

- Read every module in `src/mcctl/` and every test.
- Ran the full suite (148 passed, 1 skipped before changes; 171+ after) and
  `ruff` (clean before and after).
- Smoke-tested the CLI against a real Linux kernel via `transport = "local"`
  (status probes, inspector sections, mod scanning, AI error paths).
- Cross-checked CLI ⇄ README ⇄ fish completions ⇄ systemd units ⇄ PKGBUILD.

## 2. Functionality audit (by module)

| Module | Verdict | Notes |
|---|---|---|
| `transport.py` | ✅ solid | ControlMaster reuse, `bash -s` over stdin kills quoting risk; rc=255 vs payload-rc separation correct; atomic `write_text` (tmp+mv) with timestamped .bak |
| `console.py` | ✅ solid | RCON-over-tunnel with clean teardown + tmux/log-offset fallback; `send_and_wait` captures the offset *before* sending — async replies (spark, save confirmation) can't be missed |
| `rcon.py` | ✅ solid | fragmentation-aware read loop, auth-failure (-1) handled, length sanity check; short-fragment heuristic is reasonable |
| `server.py` | ✅ solid | liveness = pgrep + `/proc/<pid>/cwd` (not session name); stop path sets `desired=down` *first*, then countdown → flush → stop → TERM → KILL with verification at each step |
| `backup.py` | ✅ solid | `save-on` guaranteed in `finally`; tar→verify→rename(tmp) ordering means a half-written archive can never be mistaken for a backup; GFS rotation pure + tested; restore refuses running server, verifies integrity, never deletes the old world |
| `watchdog.py` | ✅ solid | `decide()` pure & unit-tested; armed/desired/halted semantics prevent both the resurrection foot-gun and crash loops; evidence collected before reaping |
| `props.py` | ✅ solid | comment/order-preserving editor, typed validation, masked password in diffs |
| `doctor.py` | ✅ solid | encodes the GraalVM/tmux/IPv6/RCON lessons; actively probes RCON exposure from outside |
| `spark.py`, `metrics.py`, `logs.py`, `players.py`, `state.py`, `util.py` | ✅ | tolerant parsers, JSONL rotation, escape-sequence hygiene on all remote text |
| `dash.py`, `gui_app.py` | ✅ | single-worker serialization in the GUI mirrors CLI semantics; destructive actions confirm |

## 3. Incompatibilities & inconsistencies found

1. **[fixed — real bug]** `mcctl --config X <cmd>` silently ignored `--config`
   (and `-v`) when placed *before* the subcommand: every subparser re-applied
   its `default=None` over the value the main parser had already stored. The
   code comment explicitly promised both positions work. Fixed with
   `argparse.SUPPRESS` defaults + backfill in `main()`.
2. **[fixed]** `watchdog._thread_dump()` invoked `$JAVA_HOME/bin/jcmd`
   unconditionally, while `metrics._jcmd()` falls back to `jcmd` from PATH when
   the pinned one isn't executable. A moved GraalVM would have silently lost
   freeze thread-dumps. Now uses the same fallback.
3. **[fixed]** `llm`-style raw tracebacks: new in this change-set originally,
   `import anthropic` ran before the friendly dependency check; now the
   actionable hint always wins.
4. **[fixed]** README claimed “141 unit tests” (actual: 148 then, 171+ now) —
   replaced the hardcoded count.
5. **[fixed]** fish completions drift: `init --tmux-session` was missing; new
   commands added.
6. **[fixed in 0.3.0]** systemd unit drift: the units now live *inside* the
   package (`src/mcctl/units/`) as the single source — the PKGBUILD installs
   them verbatim and `mcctl watchdog install` reads the same files, rewriting
   `ExecStart` for pipx installs and honoring `$XDG_CONFIG_HOME`.
7. **[fixed in 0.3.0]** `backup.create(full=True)` self-exclude: now computed
   relative to `server_dir` and only added when the backup dir is actually
   nested (`backup.full_backup_excludes`, unit-tested incl. the
   `/opt/minecraft` vs `/opt/minecraft-backups` prefix-collision case).
8. **[fixed in 0.3.0]** `mcctl gui` now forwards `-v` to the GUI logging setup.
9. **[noted]** `mcctl logs crash` (no flags) prints the *newest report*
   rather than listing — intentional per the help text, but easy to trip on;
   `--list` is the explicit form.

No version incompatibilities found: `requires-python >= 3.11` matches the
`tomllib`/`slots=True` usage; CI (3.12), PKGBUILD and Makefile agree; GTK < 4.12
is handled via the `load_from_data` fallback.

## 4. GUI coverage matrix

CLI surface ⇄ GUI exposure after this change-set:

| Capability | CLI | GUI |
|---|---|---|
| status / start / stop / restart / save / purge | ✅ | ✅ Overview (confirmations on destructive) |
| console commands | ✅ | ✅ Console page |
| log tail | ✅ `logs` | ✅ Logs page (auto-refresh while visible) |
| players / whitelist / op / kick | ✅ | ✅ Players page |
| backups create/list/verify | ✅ | ✅ Backups page (restore deliberately CLI-only — typed confirm) |
| watchdog arm/disarm + state | ✅ | ✅ Overview switch |
| **mod inventory** | ✅ `mods` *(new)* | ✅ Mods page *(new)* |
| **OS/JVM introspection** | ✅ `inspect` *(new)* | ✅ Inspect page with Learn mode *(new)* |
| **AI analysis** | ✅ `ai` *(new)* | ✅ AI page, streaming *(new)* |
| doctor (+ safe fixes) | ✅ | ✅ Doctor page *(0.3.0)* |
| server.properties editing | ✅ `props` | ✅ Properties page — validated widgets, diff-reviewed save, live apply *(0.3.0)* |
| JVM heap / JAVA path | ✅ `jvm` | ✅ JVM page *(0.3.0)* |
| crash reports + evidence bundles | ✅ `logs crash` | ✅ Crashes page with AI analyze *(0.3.0)* |
| spark profiler | ✅ `profile` | ✅ Profiler page *(0.3.0)* |
| config sync | ✅ `sync` | ✅ Sync page (push confirms) *(0.3.0)* |
| watchdog restart history | ✅ `watchdog status` | ✅ Overview “Self-heals” row *(0.3.0)* |
| stats history charts | ✅ `stats` | ❌ planned for 0.4.0 (Charts page) |
| backup restore | ✅ | ❌ deliberately CLI-only (typed confirm) |

Look & feel: pure libadwaita widgets (ViewSwitcher, PreferencesGroup,
SwitchRow, Banner, ToastOverlay), level-bars reusing Adwaita's semantic
offset colors, no custom CSS beyond the status pill — consistent and native.

## 5. Security review

- RCON only over the SSH tunnel; doctor actively fails if 25575 answers from
  the internet. Unchanged.
- All remote text passes `sanitize_terminal` (ANSI/OSC/C0 stripped) before any
  terminal/GUI render. New inspector/mods output goes through the same path.
- New AI path: payloads are **secret-redacted** (`rcon.password`, `*KEY*`,
  `*TOKEN*`, `*SECRET*`, `*PASS*` assignments) and wrapped in `<data>`
  envelopes; the system prompt instructs the model to treat envelope content
  strictly as data — this modpack's crash logs are known to embed
  prompt-injection text. The API key is read from the environment only.
- `inspect env` masks secret-looking environment values *locally* before
  display.
- No new listening ports, no new stored credentials.

## 6. What this change-set adds

- `mcctl inspect` + GUI Inspect page: process ancestry, /proc status/limits/io,
  per-thread CPU with role annotations, memory-map categories + smaps_rollup,
  fd classification, live sockets, frozen environment, jcmd JVM internals,
  host PSI/meminfo — each with a `--learn` plain-language walkthrough.
- `mcctl mods` + GUI Mods page: one-round-trip jar metadata extraction
  (NeoForge/Forge/Fabric descriptors).
- `mcctl ai {logs,crash,mods,inspect,ask}` + GUI AI page: streaming Claude
  analysis (optional `anthropic` dependency, `[llm]` config section).
- Fixes 1–5 above; version bump to 0.2.0.
