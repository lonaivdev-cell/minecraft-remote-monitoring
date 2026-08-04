# mcctl → LULiSM Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the `mcctl` Python package, CLI, config/state/cache directories and systemd units to `lulism`, shipping as 2.0.0, with zero observable behaviour change.

**Architecture:** `src/mcctl/` uses 117 relative imports and **zero** absolute `mcctl` imports, so the package move is almost entirely `git mv` plus two constants in `util.py`. Absolute imports exist only in `tests/` (30 files). A `mcctl` console script remains as an `execvp` shim so the phones in the field — which have the literal string `mcctl agent` persisted in their profile DataStore — keep working. Config, state and cache directories are copied (never moved) on first run; systemd units are migrated stop → disable → remove → install, because a half-migrated box means two watchdogs.

**Tech Stack:** Python 3.11+, setuptools (src layout, dynamic version), pytest, ruff, systemd user units, fish completions, Arch PKGBUILD, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-04-lulism-rename-design.md`

## Global Constraints

- Ships as **2.0.0**. `src/lulism/__init__.py` `__version__` is the single source of truth; `pyproject.toml` reads it dynamically.
- **`AGENT_PROTOCOL` stays `1`.** `tests/golden/agent_schema_v1.json` must remain **byte-identical**. Never regenerate it during this plan — a diff there is a bug report, not a golden to refresh.
- **The 13 `mcctl_*` Prometheus metric names must not change.** They are a public interface queried by Grafana panels and alert rules.
- **`android/` must not be modified.** Not one file.
- Preserve-list — these `mcctl` occurrences must survive verbatim: everything under `android/`; `provides`/`replaces`/`conflicts=('mcctl')` in PKGBUILD; the `mcctl` console-script name; the `~/.config/mcctl`, `~/.local/state/mcctl`, `~/.cache/mcctl` literals in the migrator; `util.LEGACY_APP` and the `mcctl-*.service`/`.timer` literals in `util.legacy_unit_names()` and `doctor`; the `(mcctl|lulism)` alternation in `doctor`'s watchdog pgrep patterns; **the `"mcctl_version"` response key in `agent.py`'s `agent.hello`**; `mcctl-android-v*.apk` in `release.yml:164`; `mcctl-debug-apk` in `android.yml:60`; the 13 `mcctl_*` metric names; historical incident references (`2026-06-11`, "mcctl postmortem").
- **`"mcctl_version"` is a live wire-contract key, not prose.** `android/core/…/Models.kt:225` deserializes it as `mcctlVersion`, and `SettingsScreen.kt:102` / `ConnectScreen.kt:77` display it. Since `android/` is frozen, renaming the key would make every installed phone show a blank version. It is not covered by the golden schema (which contains zero `mcctl` strings), so Task 7 adds an explicit freeze test.
- The shim's deprecation notice goes to **stderr only**. `mcctl agent` is NDJSON on stdout.
- Migration **copies, never moves**, so the originals remain a rollback path.
- Baseline is `399 passed, 1 skipped` in ~47s, ruff clean. Every task ends with that suite green.
- Run everything through the project venv: `.venv/bin/python -m pytest`, `.venv/bin/ruff`.

**Deviation from the spec's §5:** the spec lists seven commits. This plan has eight — it adds **Task 1**, a guard-test commit that runs *before* any renaming, so the invariants above are enforced by tests rather than by care. Tasks 2–8 map 1:1 onto the spec's seven.

---

## File Structure

**Created:**
- `src/lulism/shim.py` — the deprecated `mcctl` entry point; `execvp`s into `lulism`. Sole responsibility: argv passthrough with a stderr notice.
- `tests/test_public_interfaces.py` — freezes the Prometheus metric names and the config section names. Guards against a blanket substitution renaming a public interface.
- `tests/test_shim.py` — shim behaviour, including the stdout-purity regression test.
- `tests/test_migrate.py` — legacy XDG directory migration.
- `tests/test_units_migration.py` — unit migration ordering and the dual-authority doctor check.

**Renamed (via `git mv`, preserving history):**
- `src/mcctl/` → `src/lulism/` (32 modules + `units/`)
- `src/lulism/units/mcctl-*.{service,timer}` → `lulism-*.{service,timer}` (7 files)
- `completions/mcctl.fish` → `completions/lulism.fish`
- `data/io.github.lonaivdev_cell.mcctl.desktop` → `…lulism.desktop`
- `data/icons/io.github.lonaivdev_cell.mcctl.svg` → `…lulism.svg`

**Modified:**
- `src/lulism/util.py:17,257,262` — `APP`, `render_units` default, `resources.files()`
- `src/lulism/cli.py:639,647,1380` — unit-render predicate, enable hint, `prog=`
- `src/lulism/doctor.py:323,353,363` — unit names, new legacy-unit and stale-prom checks
- `src/lulism/prometheus.py:69` — output filename
- `src/lulism/gui.py`, `gui_app.py:142,243,2918` — window titles, stderr hint
- `src/lulism/llm.py:31,163,265` — pipx hint, User-Agent
- `src/lulism/config.py:93,397,399` — path comments
- `tests/*.py` — 30 files, absolute imports
- `pyproject.toml`, `PKGBUILD`, `Makefile`, `update.sh`, `.github/workflows/release.yml`
- `README.md`, `CLAUDE.md`, `TODO.md`

**Untouched:** `android/`, `.github/workflows/android.yml`, `.github/workflows/ci.yml`, `tests/golden/agent_schema_v1.json`.

---

### Task 1: Freeze the public interfaces

Guard tests, written and committed **before** any renaming, so later tasks cannot silently rename a public interface. This task is pure addition — no production code changes.

**Files:**
- Create: `tests/test_public_interfaces.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `EXPECTED_METRICS: set[str]` and `EXPECTED_CONFIG_SECTIONS: tuple[str, ...]` — later tasks must keep these tests green without editing the constants.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_public_interfaces.py`:

```python
"""Public interfaces that must survive the LULiSM rename unchanged.

Prometheus metric names are queried by Grafana panels and alert rules; config
section names are part of the agent schema. Renaming either is a behaviour
change, which the 2.0.0 rename explicitly forbids. If a test here fails, do not
update the constant — revert whatever renamed the interface.
"""

from __future__ import annotations

from mcctl import prometheus
from mcctl.config import Config

EXPECTED_METRICS = {
    "mcctl_up",
    "mcctl_players",
    "mcctl_tps",
    "mcctl_mspt_milliseconds",
    "mcctl_heap_used_bytes",
    "mcctl_heap_max_bytes",
    "mcctl_host_mem_used_bytes",
    "mcctl_host_mem_total_bytes",
    "mcctl_disk_free_bytes",
    "mcctl_load1",
    "mcctl_log_age_seconds",
    "mcctl_watchdog_restarts_total",
    "mcctl_scrape_timestamp_seconds",
}

EXPECTED_CONFIG_SECTIONS = ("server", "backup", "watchdog", "metrics", "llm", "ui", "crafting")


def test_prometheus_metric_names_are_frozen():
    # render() emits "# TYPE <name> <type>" for every series, even when the
    # sample lacks that key, so an empty sample still lists all of them.
    text = prometheus.render({}, host="box", restarts=0, now=1700.0)
    names = {ln.split()[2] for ln in text.splitlines() if ln.startswith("# TYPE ")}
    assert names == EXPECTED_METRICS


def test_config_section_names_are_frozen():
    assert tuple(Config().to_dict()) == EXPECTED_CONFIG_SECTIONS
```

- [ ] **Step 2: Run the tests to see them pass against the current tree**

Run: `.venv/bin/python -m pytest tests/test_public_interfaces.py -v`
Expected: **2 passed**. These are characterisation tests — they must pass *now*, on unrenamed code. If `test_config_section_names_are_frozen` fails, inspect `Config.to_dict()` (`src/mcctl/config.py:239`) and correct the assertion to match the real key order; do not change `EXPECTED_CONFIG_SECTIONS` membership.

- [ ] **Step 3: Prove the metric guard actually bites**

Temporarily edit the `_SERIES` table in `src/mcctl/prometheus.py` and rename `mcctl_tps` to `lulism_tps`. Run:

`.venv/bin/python -m pytest tests/test_public_interfaces.py -v`
Expected: FAIL on `test_prometheus_metric_names_are_frozen`. **Revert the edit** with `git checkout src/mcctl/prometheus.py` and re-run to confirm 2 passed. A guard test that cannot fail is worthless; this step verifies it.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: `401 passed, 1 skipped`.

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check src tests
git add tests/test_public_interfaces.py
git commit -m "test: freeze Prometheus metric names and config sections before the rename"
```

---

### Task 2: Move the package

`git mv` plus two constants. Because `src/mcctl/` has zero absolute self-imports, the 117 relative imports survive the move untouched.

**Files:**
- Rename: `src/mcctl/` → `src/lulism/`
- Modify: `src/lulism/util.py:17,257,262`
- Modify: `tests/*.py` (30 files) and `tests/conftest.py` — absolute imports
- Modify: `pyproject.toml` — `version` attr path only (full packaging comes in Task 6)

**Interfaces:**
- Consumes: Task 1's guard tests (their imports change from `mcctl` to `lulism` here).
- Produces: the `lulism` package. `util.APP == "lulism"`; `util.config_dir()`, `state_dir()`, `cache_dir()` now resolve under `…/lulism`.

- [ ] **Step 1: Move the package directory**

```bash
git mv src/mcctl src/lulism
```

- [ ] **Step 2: Update the three structural references in `util.py`**

`src/lulism/util.py:17`:

```python
APP = "lulism"
```

`src/lulism/util.py:262` — change **only** the `resources.files()` argument, which is the one that raises at runtime if missed:

```python
    for entry in (resources.files("lulism") / "units").iterdir():
```

**Leave `exe: str = "mcctl"` and the `"ExecStart=/usr/bin/mcctl "` match string alone.** Those describe the *unit files*, which still say `ExecStart=/usr/bin/mcctl` until Task 5 renames them. Changing the code's match string here while the data still says `mcctl` would make the substitution a guaranteed no-op, which silently hollows out two existing tests in `tests/test_util.py`:

- `test_render_units_rewrites_execstart_for_pipx` would assert a rewrite that no longer happens
- `test_render_units_keeps_usrbin_for_system_install` would pass by accident, matching nothing

Task 5 changes these two strings and the unit files together, atomically, so code and data never disagree. Leaving them here means all three `render_units` tests pass **unmodified** in this task — if you find yourself editing them, stop: that is the signal you changed a string you shouldn't have.

- [ ] **Step 3: Point `pyproject.toml` at the moved version attribute**

`pyproject.toml` — change only this line for now:

```toml
version = { attr = "lulism.__version__" }
```

- [ ] **Step 4: Rewrite absolute imports in the test suite**

```bash
grep -rl '\bmcctl\b' tests/*.py | xargs sed -i 's/\bfrom mcctl\b/from lulism/g; s/\bimport mcctl\b/import lulism/g; s/\bmcctl\./lulism./g'
```

Then verify nothing was missed, and that the substitution did not touch metric names (`mcctl_tps` has an underscore, so `\bmcctl\b` does not match it — this check confirms that):

```bash
grep -rn '\bmcctl\b' tests/ | grep -v 'mcctl_' | grep -v 'mcctl-' || echo "clean"
```

Expected: `clean`, or only occurrences inside string literals that are genuinely about the old name.

- [ ] **Step 5: Reinstall so the console script and package metadata follow the move**

```bash
.venv/bin/python -m pip install -q -e ".[dev]"
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: `401 passed, 1 skipped`.

The golden schema test passing here is the headline result — it proves the move is invisible to the phone.

- [ ] **Step 7: Verify the XDG paths actually moved**

```bash
.venv/bin/python -c "from lulism import util; print(util.config_dir(), util.state_dir(), util.cache_dir())"
```

Expected: three paths ending in `/lulism`.

- [ ] **Step 8: Lint and commit**

```bash
.venv/bin/ruff check src tests
git add -A
git commit -m "refactor: move src/mcctl to src/lulism (no behaviour change)"
```

---

### Task 3: Entry points and the `mcctl` shim

**Files:**
- Create: `src/lulism/shim.py`
- Create: `tests/test_shim.py`
- Modify: `pyproject.toml` — `[project.scripts]`, `[project.gui-scripts]`
- Modify: `src/lulism/cli.py:1380` — `prog=`

**Interfaces:**
- Consumes: the `lulism` package from Task 2.
- Produces: `lulism.shim.main(argv: list[str] | None = None) -> int`, wired to the `mcctl` console script. Console scripts after this task: `lulism`, `mcctl` (deprecated), `lulism-gui`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shim.py`:

```python
"""The deprecated `mcctl` entry point.

The stdout-purity test is the load-bearing one: `mcctl agent` is a JSON-RPC 2.0
NDJSON stream on stdout, and every phone in the field still invokes the agent by
that name (its profile DataStore has the literal string "mcctl agent" saved). A
deprecation line on stdout corrupts the first frame and breaks every install.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lulism import shim

SCRIPTS = Path(sys.executable).parent


def test_execs_lulism_with_same_argv(monkeypatch, capsys):
    seen = {}

    def fake_execvp(file, args):
        seen["file"] = file
        seen["args"] = list(args)
        raise SystemExit(0)  # stand in for "never returns"

    monkeypatch.setattr(os, "execvp", fake_execvp)
    with pytest.raises(SystemExit):
        shim.main(["status", "--json"])

    assert seen["file"] == "lulism"
    assert seen["args"] == ["lulism", "status", "--json"]


def test_notice_goes_to_stderr_not_stdout(monkeypatch, capsys):
    monkeypatch.setattr(os, "execvp", lambda f, a: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        shim.main(["status"])
    out = capsys.readouterr()
    assert out.out == ""
    assert "deprecated" in out.err


@pytest.mark.integration
def test_mcctl_agent_schema_stdout_is_pure_json():
    """The regression test for 'we broke every phone in the field'."""
    env = {**os.environ, "PATH": f"{SCRIPTS}{os.pathsep}{os.environ.get('PATH', '')}"}
    r = subprocess.run([str(SCRIPTS / "mcctl"), "agent", "--schema"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    schema = json.loads(r.stdout)          # must not raise
    assert schema["protocol"] == 1
    assert "deprecated" in r.stderr


@pytest.mark.integration
@pytest.mark.parametrize("args,code", [(["--version"], 0), (["definitely-not-a-command"], 2)])
def test_exit_codes_pass_through(args, code):
    env = {**os.environ, "PATH": f"{SCRIPTS}{os.pathsep}{os.environ.get('PATH', '')}"}
    r = subprocess.run([str(SCRIPTS / "mcctl"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == code


@pytest.mark.integration
def test_works_when_scripts_dir_is_not_on_path():
    """The field case: systemd user units and non-interactive SSH both run with
    a minimal PATH that excludes ~/.local/bin, where pipx puts both scripts."""
    r = subprocess.run([str(SCRIPTS / "mcctl"), "agent", "--schema"],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["protocol"] == 1


def test_reports_exec_failure_on_stderr(monkeypatch, capsys):
    def boom(file, args):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(os, "execvp", boom)
    monkeypatch.setattr(shim, "_target", lambda: "lulism")
    assert shim.main(["status"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "cannot exec lulism" in out.err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_shim.py -v`
Expected: FAIL — `ImportError: cannot import name 'shim' from 'lulism'`.

- [ ] **Step 3: Write the shim**

Create `src/lulism/shim.py`:

```python
"""Deprecated `mcctl` entry point — execs into `lulism` with the same argv.

Removed at 3.0.0. Until then this is a compatibility bridge, not a courtesy:
every Android client in the field has the literal string "mcctl agent" persisted
in its profile DataStore (ConnectionProfile.agentCommand), so shipping a new APK
default cannot fix them. This shim is what keeps them connecting.

The notice is written to **stderr**. `mcctl agent` is a JSON-RPC 2.0 NDJSON
stream on stdout; a line there corrupts the first frame.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

NOTICE = (
    "mcctl: deprecated, and removed in 3.0.0 — use `lulism` instead.\n"
    "mcctl: on the phone, set Settings → agent command to `lulism agent`.\n"
)


def _target() -> str:
    """Resolve `lulism` next to this script before falling back to PATH.

    A bare-name execvp searches PATH, and the contexts this shim exists to
    serve are exactly the ones with a minimal PATH: systemd user units (whose
    default PATH excludes ~/.local/bin) and non-interactive SSH sessions (how
    the phone invokes `mcctl agent`). pipx installs both scripts into the same
    directory, so the sibling lookup succeeds precisely where PATH does not.
    """
    try:
        sibling = Path(sys.argv[0]).resolve().with_name("lulism")
    except (OSError, ValueError):
        return "lulism"
    return str(sibling) if sibling.exists() else "lulism"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    sys.stderr.write(NOTICE)
    sys.stderr.flush()
    # execvp replaces this process: exit codes, signals and stdio wiring all
    # pass through untouched, which a subprocess wrapper would not guarantee.
    # argv[0] stays "lulism" so the child's own prog name is right.
    try:
        os.execvp(_target(), ["lulism", *args])
    except OSError as e:
        sys.stderr.write(f"mcctl: cannot exec lulism: {e}\n")
        return 1
    return 0  # unreachable — execvp does not return on success


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Register the console scripts**

`pyproject.toml`:

```toml
[project.scripts]
lulism = "lulism.cli:main"
mcctl = "lulism.shim:main"          # deprecated bridge, removed at 3.0.0

[project.gui-scripts]
lulism-gui = "lulism.gui:main"
```

- [ ] **Step 5: Fix the argparse program name**

`src/lulism/cli.py:1380`:

```python
        prog="lulism",
```

- [ ] **Step 6: Reinstall and run the tests**

```bash
.venv/bin/python -m pip install -q -e ".[dev]"
.venv/bin/python -m pytest tests/test_shim.py -v
```

Expected: PASS (4 tests, including the two integration-marked ones).

- [ ] **Step 7: Run the full suite and commit**

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
git add -A
git commit -m "feat: lulism entry points + deprecated mcctl shim (stderr notice, execvp passthrough)"
```

Expected: `405 passed, 1 skipped`.

---

### Task 4: Legacy XDG directory migration

**Files:**
- Modify: `src/lulism/util.py` — add `migrate_legacy_dirs()`
- Modify: `src/lulism/cli.py` — call it in `main()` after `setup_logging`
- Modify: `src/lulism/gui.py` — call it in `main()`
- Create: `tests/test_migrate.py`

**Interfaces:**
- Consumes: `util.config_dir()`, `util.state_dir()`, `util.cache_dir()` from Task 2.
- Produces: `util.migrate_legacy_dirs() -> list[tuple[Path, Path]]` — returns the (source, destination) pairs it copied, empty when there was nothing to do.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate.py`:

```python
"""Migration of ~/.config/mcctl, ~/.local/state/mcctl and ~/.cache/mcctl.

Copies, never moves: the originals stay put as the 2.0.0 rollback path.
"""

from __future__ import annotations

from pathlib import Path

from lulism import util
from lulism.config import Config

V112_CONFIG = """\
[server]
host = "carborio"
user = "ubuntu"
transport = "ssh"

[backup]
remote_dir = "/opt/backups"

[watchdog]
poll_s = 30

[metrics]
prom_path = ""

[llm]
provider = "ollama"

[ui]
tz = "local"

[crafting]
enabled = true
"""


def _legacy(root: Path, kind: str) -> Path:
    d = {"cfg": root / "cfg" / "mcctl", "state": root / "state" / "mcctl",
         "cache": root / "cache" / "mcctl"}[kind]
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_migrates_config_state_and_cache(isolated_xdg):
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    (_legacy(isolated_xdg, "state") / "metrics.jsonl").write_text('{"tps":19.9}\n', encoding="utf-8")
    (_legacy(isolated_xdg, "cache") / "icon.png").write_bytes(b"\x89PNG")

    moved = util.migrate_legacy_dirs()

    assert len(moved) == 3
    assert (util.config_dir() / "config.toml").read_text(encoding="utf-8") == V112_CONFIG
    assert (util.state_dir() / "metrics.jsonl").exists()
    assert (util.cache_dir() / "icon.png").read_bytes() == b"\x89PNG"


def test_migrated_v112_config_loads_clean(isolated_xdg):
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    util.migrate_legacy_dirs()

    cfg = Config.load()

    assert cfg.server.host == "carborio"
    assert cfg.server.user == "ubuntu"
    assert cfg.llm.provider == "ollama"


def test_originals_survive(isolated_xdg):
    legacy = _legacy(isolated_xdg, "cfg")
    (legacy / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    util.migrate_legacy_dirs()
    assert (legacy / "config.toml").exists(), "migration must copy, not move"


def test_is_idempotent_and_never_clobbers(isolated_xdg):
    (_legacy(isolated_xdg, "cfg") / "config.toml").write_text(V112_CONFIG, encoding="utf-8")
    util.migrate_legacy_dirs()

    util.config_dir().mkdir(parents=True, exist_ok=True)
    (util.config_dir() / "config.toml").write_text("# hand-edited\n", encoding="utf-8")

    assert util.migrate_legacy_dirs() == []
    assert (util.config_dir() / "config.toml").read_text(encoding="utf-8") == "# hand-edited\n"


def test_no_legacy_dir_is_a_noop(isolated_xdg):
    assert util.migrate_legacy_dirs() == []
    assert not util.config_dir().exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -v`
Expected: FAIL — `AttributeError: module 'lulism.util' has no attribute 'migrate_legacy_dirs'`.

- [ ] **Step 3: Implement the migration**

Add to `src/lulism/util.py`, immediately after `crashes_dir()`:

```python
LEGACY_APP = "mcctl"


def migrate_legacy_dirs() -> list[tuple[Path, Path]]:
    """Copy pre-2.0.0 mcctl XDG dirs to their lulism equivalents, once.

    Copies rather than moves so the mcctl trees remain a rollback path. Skips
    any destination that already exists, which makes this idempotent and means
    a hand-edited lulism config is never clobbered.
    """
    pairs = (
        (_xdg("XDG_CONFIG_HOME", ".config") / LEGACY_APP, config_dir()),
        (_xdg("XDG_STATE_HOME", ".local/state") / LEGACY_APP, state_dir()),
        (_xdg("XDG_CACHE_HOME", ".cache") / LEGACY_APP, cache_dir()),
    )
    done: list[tuple[Path, Path]] = []
    for src, dst in pairs:
        if dst.exists() or not src.is_dir():
            continue
        shutil.copytree(src, dst)
        log.info("migrated %s -> %s (the original is kept as a rollback path)", src, dst)
        done.append((src, dst))
    return done
```

`shutil` is already imported at `util.py:11`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Call it from both entry points**

`src/lulism/cli.py`, in `main()` immediately after `util.setup_logging(args.verbose)` — placed there so the migration's log line honours `-v`:

```python
    util.setup_logging(args.verbose)
    util.migrate_legacy_dirs()
```

`src/lulism/gui.py`, in `main()` as the first statement inside the function body:

```python
def main(argv: list[str] | None = None) -> int:
    from . import util
    util.migrate_legacy_dirs()
    try:
        import gi
```

- [ ] **Step 6: Run the full suite and commit**

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
git add -A
git commit -m "feat: migrate pre-2.0.0 mcctl config/state/cache dirs on first run"
```

Expected: `413 passed, 1 skipped`.

---

### Task 5: systemd units — rename, migrate, and detect a half-migrated box

The riskiest task. `doctor.py:217` records a real 2026-06-11 outage caused by two restart authorities; renaming units without disabling the old ones re-creates it exactly.

**Files:**
- Rename: `src/lulism/units/mcctl-*.{service,timer}` → `lulism-*` (7 files)
- Modify: all 7 unit bodies — `ExecStart`, `Description`, `Unit=` cross-references
- Modify: `src/lulism/cli.py:639,647`
- Modify: `src/lulism/doctor.py` — legacy `mcctl-*` unit check; fix `:363`
- Modify: `src/lulism/prometheus.py:69`
- Create: `tests/test_units_migration.py`

**Interfaces:**
- Consumes: `util.render_units(exe=...)` from Task 2.
- Produces: `util.legacy_unit_names() -> tuple[str, ...]` (the 7 old names) and `util.migrate_units(run) -> list[str]`, where `run` is a callable taking a `list[str]` command and returning its exit code. Injecting `run` keeps the ordering testable without systemd.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_units_migration.py`:

```python
"""Unit migration ordering, and detection of a half-migrated box.

The 2026-06-11 outage was two restart authorities fighting. Renaming the units
without disabling the old ones reproduces it, so the ordering below is the
safety property: every old unit is stopped and disabled before any new one is
installed.
"""

from __future__ import annotations

from lulism import util


class FakeSystemctl:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(list(cmd))
        return 0

    def verbs(self) -> list[tuple[str, str]]:
        out = []
        for c in self.calls:
            verb = next((v for v in ("stop", "disable", "daemon-reload") if v in c), None)
            unit = next((a for a in c if a.endswith((".service", ".timer"))), "")
            if verb:
                out.append((verb, unit))
        return out


def test_legacy_unit_names_covers_all_seven():
    names = util.legacy_unit_names()
    assert set(names) == {
        "mcctl-watchdog.service",
        "mcctl-autosave.service", "mcctl-autosave.timer",
        "mcctl-backup.service", "mcctl-backup.timer",
        "mcctl-metrics.service", "mcctl-metrics.timer",
    }


def test_every_old_unit_is_stopped_then_disabled():
    fake = FakeSystemctl()
    util.migrate_units(fake)
    verbs = fake.verbs()

    for unit in util.legacy_unit_names():
        stops = [i for i, (v, u) in enumerate(verbs) if v == "stop" and u == unit]
        disables = [i for i, (v, u) in enumerate(verbs) if v == "disable" and u == unit]
        assert stops and disables, f"{unit} was not both stopped and disabled"
        assert stops[0] < disables[0], f"{unit} disabled before it was stopped"


def test_daemon_reload_happens_after_all_disables():
    fake = FakeSystemctl()
    util.migrate_units(fake)
    verbs = fake.verbs()
    reload_at = next(i for i, (v, _) in enumerate(verbs) if v == "daemon-reload")
    last_disable = max(i for i, (v, _) in enumerate(verbs) if v == "disable")
    assert reload_at > last_disable


def test_migrate_units_reports_what_it_touched():
    touched = util.migrate_units(FakeSystemctl())
    assert set(touched) == set(util.legacy_unit_names())


def test_rendered_units_never_point_at_the_shim():
    # Even when the CLI was reached through the deprecated `mcctl` script.
    units = util.render_units(exe="/home/u/.local/bin/mcctl")
    for name, text in units.items():
        assert "ExecStart" in text, name
        for line in text.splitlines():
            if line.startswith("ExecStart="):
                assert not line.rstrip().split()[0].endswith("mcctl"), f"{name}: {line}"


def test_shipped_units_are_all_lulism_named():
    names = set(util.render_units().keys())
    assert names == {
        "lulism-watchdog.service",
        "lulism-autosave.service", "lulism-autosave.timer",
        "lulism-backup.service", "lulism-backup.timer",
        "lulism-metrics.service", "lulism-metrics.timer",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_units_migration.py -v`
Expected: FAIL — `AttributeError: module 'lulism.util' has no attribute 'legacy_unit_names'`.

- [ ] **Step 3: Rename the unit files**

```bash
cd src/lulism/units
for f in mcctl-*; do git mv "$f" "lulism-${f#mcctl-}"; done
cd -
```

- [ ] **Step 4: Rewrite the unit bodies**

```bash
sed -i 's|ExecStart=/usr/bin/mcctl |ExecStart=/usr/bin/lulism |g; s/\bmcctl-/lulism-/g; s/^Description=mcctl /Description=lulism /' src/lulism/units/*
```

Then read all seven and confirm by eye that `Description=`, `ExecStart=`, and any `Unit=`/`Requires=`/`After=` cross-references between a `.timer` and its `.service` now say `lulism-`:

```bash
grep -n 'Description=\|ExecStart=\|Unit=\|Requires=\|After=' src/lulism/units/*
```

- [ ] **Step 4a: Switch `render_units` to the lulism ExecStart, atomically with the file rename**

Task 2 deliberately left these two strings alone so the code never disagreed with the unit files. Now that Step 4 has rewritten the units to `ExecStart=/usr/bin/lulism`, change both in `src/lulism/util.py:257,266-267`:

```python
def render_units(*, exe: str = "lulism") -> dict[str, str]:
    """The unit files shipped in lulism/units/ (the PKGBUILD installs the same
    files verbatim), with ExecStart rewritten for non-/usr/bin installs (pipx)."""
    from importlib import resources
    units: dict[str, str] = {}
    for entry in (resources.files("lulism") / "units").iterdir():
        if not entry.name.endswith((".service", ".timer")):
            continue
        text = entry.read_text(encoding="utf-8")
        if exe != "/usr/bin/lulism":
            text = text.replace("ExecStart=/usr/bin/lulism ", f"ExecStart={exe} ")
        units[entry.name] = text
    return units
```

- [ ] **Step 4b: Update the three existing `render_units` tests to the new unit names**

`tests/test_util.py` has three tests that name the old units and paths. They must keep asserting real behaviour — the pipx rewrite must still be proven to *happen*:

```python
def test_render_units_ships_all_units():
    from lulism import util
    units = util.render_units()
    assert set(units) == {"lulism-watchdog.service", "lulism-autosave.service",
                          "lulism-autosave.timer", "lulism-backup.service",
                          "lulism-backup.timer", "lulism-metrics.service",
                          "lulism-metrics.timer"}


def test_render_units_rewrites_execstart_for_pipx():
    from lulism import util
    units = util.render_units(exe="/home/u/.local/bin/lulism")
    assert "ExecStart=/home/u/.local/bin/lulism watchdog run" in units["lulism-watchdog.service"]
    assert "/usr/bin/lulism" not in units["lulism-watchdog.service"]
    # timers carry no ExecStart and must come through untouched
    assert "OnCalendar=*-*-* 04:30:00" in units["lulism-backup.timer"]


def test_render_units_keeps_usrbin_for_system_install():
    from lulism import util
    units = util.render_units(exe="/usr/bin/lulism")
    assert "ExecStart=/usr/bin/lulism save --skip-if-down" in units["lulism-autosave.service"]
```

- [ ] **Step 5: Add the migration helpers**

Add to `src/lulism/util.py`, after `render_units()`:

```python
def legacy_unit_names() -> tuple[str, ...]:
    """The pre-2.0.0 unit names. All seven, not just the three the CLI hints at
    enabling — an operator may have enabled lulism-metrics.timer's predecessor
    by hand, and that is the one most often forgotten."""
    return (
        "mcctl-watchdog.service",
        "mcctl-autosave.service", "mcctl-autosave.timer",
        "mcctl-backup.service", "mcctl-backup.timer",
        "mcctl-metrics.service", "mcctl-metrics.timer",
    )


def migrate_units(run=None) -> list[str]:
    """Stop, disable and remove the pre-2.0.0 units, then daemon-reload.

    Ordering is the safety property: a box with both mcctl-watchdog.service and
    lulism-watchdog.service enabled has two restart authorities, which is the
    2026-06-11 outage. Every old unit is stopped and disabled before any new one
    is installed. `run` is injected so the ordering is testable without systemd.
    """
    if run is None:
        def run(cmd: list[str]) -> int:
            return subprocess.run(cmd, capture_output=True).returncode

    names = legacy_unit_names()
    for unit in names:
        run(["systemctl", "--user", "stop", unit])
    for unit in names:
        run(["systemctl", "--user", "disable", unit])
    unit_dir = user_unit_dir()
    for unit in names:
        (unit_dir / unit).unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    log.info("migrated %d pre-2.0.0 systemd units", len(names))
    return list(names)
```

`subprocess` is already imported at `util.py:13`.

- [ ] **Step 6: Fix the shim-dependency bug and the enable hint**

`src/lulism/cli.py:639` — the predicate must key on `lulism`, or units installed via the deprecated `mcctl` script would hardcode a permanent dependency on the shim:

```python
    units = util.render_units(exe=sys.argv[0] if sys.argv[0].endswith("lulism") else "lulism")
```

`src/lulism/cli.py:647`:

```python
    rc.print("  systemctl --user enable --now lulism-watchdog.service lulism-backup.timer lulism-autosave.timer")
```

And in `_install_units()`, migrate before writing the new units — insert immediately before `unit_dir = util.user_unit_dir()`:

```python
    for unit in util.migrate_units():
        rc.print(f"[yellow]removed legacy unit[/yellow] {unit}")
```

- [ ] **Step 7: Update `doctor` and the Prometheus filename**

`src/lulism/doctor.py:323,353,363` — replace `mcctl-watchdog.service` with `lulism-watchdog.service`. Line 363 is a live `subprocess` invocation, so this one is functional, not cosmetic.

Add a legacy-unit check inside `_ops_checks()`, right after the existing "legacy watchdog on server" block:

```python
    # a pre-2.0.0 unit still enabled alongside its lulism replacement is the
    # 2026-06-11 incident in miniature: two restart authorities.
    stale = [u for u in util.legacy_unit_names()
             if subprocess.run(["systemctl", "--user", "is-enabled", u],
                               capture_output=True, text=True).stdout.strip() == "enabled"]
    if stale:
        out.append(_warn("ops: pre-2.0.0 units still enabled",
                         ", ".join(stale),
                         "two restart authorities — run `lulism watchdog install` to "
                         "migrate, or: systemctl --user disable --now " + " ".join(stale)))
    else:
        out.append(_ok("ops: no pre-2.0.0 units enabled"))
```

`src/lulism/prometheus.py:69` — the file name is not an interface (node_exporter globs `*.prom` in a directory), unlike the metric names:

```python
    return util.state_dir() / "lulism.prom"
```

`src/lulism/util.py:98` — same category, the rotating log file inside the now-`lulism` state dir:

```python
        state_dir() / "lulism.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
```

Neither filename is migrated: `mcctl.log` and `mcctl.prom` are transient outputs that regenerate on the next run, and the Task 4 migration copies the whole state directory anyway, so the old files come across harmlessly and are simply superseded.

Add a stale-scrape check to `_ops_checks()`, since `state_dir()` moving silently breaks a collector configured for the old path:

```python
    legacy_prom = util._xdg("XDG_STATE_HOME", ".local/state") / "mcctl" / "mcctl.prom"
    if legacy_prom.exists():
        out.append(_warn("ops: stale Prometheus textfile",
                         f"{legacy_prom} still present",
                         "node_exporter's --collector.textfile.directory must now point at "
                         f"{util.state_dir()} — metrics go stale with no error otherwise"))
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_units_migration.py -v`
Expected: PASS (6 tests).

- [ ] **Step 9: Run the full suite and commit**

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
git add -A
git commit -m "feat: rename systemd units to lulism-* with a stop/disable/remove migration"
```

Expected: `419 passed, 1 skipped`. If `tests/test_cli.py` asserts on the old enable-hint string or unit names, update those assertions — they are characterising the strings this task deliberately changes.

---

### Task 6: Packaging, CI and the server updater

**Files:**
- Modify: `pyproject.toml` — `name`, description, keywords
- Modify: `PKGBUILD`
- Rename: `completions/mcctl.fish` → `completions/lulism.fish`; `data/io.github.lonaivdev_cell.mcctl.desktop` → `…lulism.desktop`; `data/icons/io.github.lonaivdev_cell.mcctl.svg` → `…lulism.svg`
- Modify: `Makefile` — `install-units` target
- Modify: `update.sh`
- Modify: `.github/workflows/release.yml:37-38`

**Interfaces:**
- Consumes: the `lulism` console scripts from Task 3 and unit names from Task 5.
- Produces: an installable `lulism` package; `update.sh` performs the pipx package swap.

- [ ] **Step 1: Rename the package in `pyproject.toml`**

```toml
name = "lulism"
description = "Legit Ultimate Linux Server Monitor — remote control & monitoring for game servers and hosts over SSH"
keywords = ["minecraft", "ssh", "server", "monitoring", "neoforge", "spark"]
```

- [ ] **Step 2: Rename the packaged data files**

```bash
git mv completions/mcctl.fish completions/lulism.fish
git mv data/io.github.lonaivdev_cell.mcctl.desktop data/io.github.lonaivdev_cell.lulism.desktop
git mv data/icons/io.github.lonaivdev_cell.mcctl.svg data/icons/io.github.lonaivdev_cell.lulism.svg
```

- [ ] **Step 3: Rewrite the fish completions**

These are hand-written (97 lines), not generated — the design note's "regenerated" is wrong. Every line carries `complete -c mcctl`:

```bash
sed -i 's/complete -c mcctl/complete -c lulism/g; s|~/.config/mcctl/config.toml|~/.config/lulism/config.toml|g; s/^# fish completions for mcctl/# fish completions for lulism/' completions/lulism.fish
grep -c 'complete -c lulism' completions/lulism.fish
```

Expected: a non-zero count, and `grep -n 'mcctl' completions/lulism.fish` returns nothing.

- [ ] **Step 4: Update the desktop entry**

Edit `data/io.github.lonaivdev_cell.lulism.desktop`: set `Exec=lulism-gui`, `Icon=io.github.lonaivdev_cell.lulism`, and `Name=LULiSM`. Note in the release notes that the reverse-DNS app ID changes, so pinned launchers and dock entries reset — the desktop environment treats it as a new application.

- [ ] **Step 5: Rewrite the PKGBUILD**

`pkgver` is currently `0.5.0`, already stale against `__version__ = 1.1.2`; Task 8 sets the real number. The `provides`/`replaces`/`conflicts` triple is what makes pacman remove the old package cleanly:

```bash
pkgname=lulism
pkgver=1.1.2
pkgrel=1
pkgdesc="Legit Ultimate Linux Server Monitor — remote control & monitoring for game servers and hosts over SSH"
provides=('mcctl')
replaces=('mcctl')
conflicts=('mcctl')
```

Replace the seven unit install lines, the completion line and the desktop/icon lines:

```bash
    install -Dm644 src/lulism/units/lulism-watchdog.service \
        "$pkgdir/usr/lib/systemd/user/lulism-watchdog.service"
    install -Dm644 src/lulism/units/lulism-autosave.service \
        "$pkgdir/usr/lib/systemd/user/lulism-autosave.service"
    install -Dm644 src/lulism/units/lulism-autosave.timer \
        "$pkgdir/usr/lib/systemd/user/lulism-autosave.timer"
    install -Dm644 src/lulism/units/lulism-backup.service \
        "$pkgdir/usr/lib/systemd/user/lulism-backup.service"
    install -Dm644 src/lulism/units/lulism-backup.timer \
        "$pkgdir/usr/lib/systemd/user/lulism-backup.timer"
    install -Dm644 src/lulism/units/lulism-metrics.service \
        "$pkgdir/usr/lib/systemd/user/lulism-metrics.service"
    install -Dm644 src/lulism/units/lulism-metrics.timer \
        "$pkgdir/usr/lib/systemd/user/lulism-metrics.timer"
    install -Dm644 completions/lulism.fish \
        "$pkgdir/usr/share/fish/vendor_completions.d/lulism.fish"
    install -Dm644 data/io.github.lonaivdev_cell.lulism.desktop \
        "$pkgdir/usr/share/applications/io.github.lonaivdev_cell.lulism.desktop"
    install -Dm644 data/icons/io.github.lonaivdev_cell.lulism.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.lonaivdev_cell.lulism.svg"
```

Also update the `optdepends` strings that say `mcctl-gui` and `mcctl ai` to `lulism-gui` and `lulism ai`.

- [ ] **Step 5a: Rename the makepkg artifact pattern in `.gitignore`**

`.gitignore:17` ignores `src/mcctl-*/`, a makepkg build artifact path. `makepkg` will now produce `src/lulism-*/`, so without this the build tree gets committed:

```bash
sed -i 's|^src/mcctl-\*/$|src/lulism-*/|' .gitignore
grep -n 'lulism' .gitignore
```

Expected: the `src/lulism-*/` line, and no remaining `src/mcctl-*/`.

- [ ] **Step 6: Verify every PKGBUILD source path exists**

```bash
grep -oP 'install -Dm644 \K[^ ]+' PKGBUILD | while read -r f; do
  [ -e "$f" ] || echo "MISSING: $f"
done; echo "checked"
```

Expected: no `MISSING:` lines.

- [ ] **Step 7: Update `update.sh` — including the pipx package swap**

This is the subtle one. `pipx install --force .` installs the **`lulism`** package into a new pipx venv, and leaves the old **`mcctl`** pipx package installed. Both then try to own a `mcctl` binary, so the shim may fail to install. The old package must be removed first.

In `update.sh`, change `src_version()` and `inst_version()` (lines 54-55):

```bash
src_version() { sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' src/lulism/__init__.py; }
inst_version(){ have lulism && lulism --version 2>/dev/null | awk '{print $NF}' || echo "(none)"; }
```

Add before the `pipx install` at line 129:

```bash
# 2.0.0: the pipx package itself was renamed. Removing the old one first stops
# the two packages fighting over the `mcctl` binary the shim needs to install.
# Capture the match separately: `pipx list` exits 1 when ANY pipx venv has a
# problem — unrelated to mcctl — and under `set -o pipefail` that non-zero
# status propagates through the pipe even when grep matches, silently skipping
# the uninstall on exactly the long-lived servers this script targets.
mcctl_pipx=$(pipx list --short 2>/dev/null | grep '^mcctl ' || true)
if [[ -n "$mcctl_pipx" ]]; then
  step "Removing the pre-2.0.0 pipx package"
  pipx uninstall mcctl && ok "old mcctl pipx package removed"
fi
```

Do **not** write this as `if pipx list --short | grep -q '^mcctl '; then` — that is the form with the `pipefail` bug described in the comment. It was verified failing against a live pipx install.

Update line 103's preflight to `src/lulism/__init__.py`, line 128's step text, lines 70-77's `server_reach()` to call `lulism status`, and `health_panel()` (lines 80-91) to check `lulism-watchdog.service`, `lulism-autosave.timer`, `lulism-backup.timer`, `lulism-metrics.timer`. Change the restart block (lines 144-159) to `lulism-watchdog.service`, the doctor/status calls (lines 161-176) to `lulism`, and the banner text.

- [ ] **Step 8: Update the Makefile and the release workflow**

`Makefile` — the `install-units` target:

```makefile
install-units:  ## user units without pacman (pipx installs)
	lulism watchdog install
```

`.github/workflows/release.yml:37-38` — the `|| exit 1` means a miss fails the job loudly rather than shipping a mis-versioned APK:

```yaml
          VERSION=$(grep -oP '__version__\s*=\s*"\K[^"]+' src/lulism/__init__.py)
          [ -n "$VERSION" ] || { echo "could not read __version__ from src/lulism/__init__.py"; exit 1; }
```

Leave `release.yml:164`'s `mcctl-android-v*.apk` asset name and `android.yml:60`'s `mcctl-debug-apk` **unchanged** — Obtainium matches the former, and `android/` is out of scope.

- [ ] **Step 9: Verify the release version extraction works**

```bash
grep -oP '__version__\s*=\s*"\K[^"]+' src/lulism/__init__.py
```

Expected: `1.1.2` (Task 8 changes it).

- [ ] **Step 10: Reinstall, run the full suite, commit**

```bash
.venv/bin/python -m pip install -q -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
git add -A
git commit -m "build: rename the package, units, completions and desktop entry to lulism"
```

Expected: `419 passed, 1 skipped`.

---

### Task 7: Source-string and documentation sweep

**Files:**
- Modify: `src/lulism/*.py` — every module EXCEPT the five allowlisted ones (see Step 0)
- Modify: `README.md` (99 hits), `CLAUDE.md` (18), `TODO.md` (21)
- Modify: `tests/test_public_interfaces.py` — add the `mcctl_version` freeze test

**Interfaces:**
- Consumes: the final command names, unit names and paths from Tasks 2–6.
- Produces: no code interface.

- [ ] **Step 0: Sweep the source prose first — Task 8's leak guard depends on it**

At the end of Task 5 there were **120 `\bmcctl\b` occurrences across 26 modules** in `src/lulism/`. These are not merely cosmetic: many are user-facing output telling people to run the *deprecated* command, e.g. `cli.py`'s `rc.print("next: [bold]mcctl doctor[/bold] …")` and `Table(title="mcctl doctor")`. Task 8's leak-guard test fails until they are swept.

Five modules are the allowlist and must **not** be swept — they hold preserve-list identifiers by design: `shim.py`, `util.py`, `doctor.py`, `prometheus.py`, `config.py`.

```bash
# Protect the wire-contract key and the thread/script names that are not prose.
# \bmcctl\b already leaves mcctl_version alone ("_" is a word char), but be explicit.
for f in src/lulism/*.py; do
  case "$(basename "$f")" in shim.py|util.py|doctor.py|prometheus.py|config.py) continue;; esac
  sed -i 's/\bmcctl-gui\b/lulism-gui/g; s/\bmcctl\b/lulism/g' "$f"
done
```

Then read the whole diff before committing. `mcctl-gui` becomes `lulism-gui` (Task 6 renamed that script). Thread names like `mcctl-stream-cancel` and `mcctl-log-follow` become `lulism-*` — cosmetic but consistent. Confirm no `mcctl_version` was touched:

```bash
grep -rn 'mcctl_version\|lulism_version' src/lulism/agent.py
```

Expected: `agent.py` still returns `"mcctl_version"`, unchanged.

- [ ] **Step 0a: Freeze `mcctl_version` so it can never be swept later**

Append to `tests/test_public_interfaces.py`:

```python
def test_agent_hello_version_key_is_frozen():
    """`mcctl_version` is a wire-contract key, not prose.

    android/core/.../Models.kt:225 deserializes it as `mcctlVersion` and two
    screens display it. android/ is frozen for this release, so renaming the key
    would blank the version on every installed phone. The golden schema does not
    cover it — agent.hello's payload is a response, not part of build_schema().
    """
    from lulism import agent

    srv = agent.AgentServer.__new__(agent.AgentServer)
    srv.caps = set()
    hello = agent.METHODS["agent.hello"]["fn"](srv, {"capabilities": []})
    assert "mcctl_version" in hello
    assert "lulism_version" not in hello
```

If `METHODS`'s entry shape differs from `{"fn": ...}`, inspect `agent.py`'s `@method` decorator and adapt the call — the assertion is what matters, not the plumbing. If constructing an `AgentServer` proves awkward, asserting on the source text of `agent.py` is an acceptable fallback, but prefer exercising the real function.

- [ ] **Step 1: Sweep the three documents**

**`\bmcctl\b` matches more than it looks like it does.** Verified behaviour:

| Input | `\bmcctl\b` matches? | |
|---|---|---|
| `mcctl_load1` | **no** | `_` is a word char, so there is no boundary — metric names are safe |
| `mcctl-watchdog.service` | **yes** | `-` is not a word char, so there *is* a boundary — and this rename is wanted |
| `mcctl-android-v2.0.0.apk` | **yes** | same boundary — and this rename is **forbidden** (preserve-list) |

So the Obtainium asset name must be protected across the sweep with a sentinel:

```bash
# 1. Protect the preserve-list strings that \bmcctl\b would otherwise eat.
sed -i 's/mcctl-android/@@KEEP_ANDROID@@/g; s/mcctl-debug-apk/@@KEEP_DEBUG@@/g' README.md CLAUDE.md TODO.md

# 2. Sweep.
sed -i 's/\bmcctl-gui\b/lulism-gui/g; s/\bmcctl\b/lulism/g; s|src/mcctl/|src/lulism/|g; s|~/.config/mcctl|~/.config/lulism|g; s|~/.local/state/mcctl|~/.local/state/lulism|g' README.md CLAUDE.md TODO.md

# 3. Restore.
sed -i 's/@@KEEP_ANDROID@@/mcctl-android/g; s/@@KEEP_DEBUG@@/mcctl-debug-apk/g' README.md CLAUDE.md TODO.md

# 4. No sentinel may survive.
grep -n '@@KEEP' README.md CLAUDE.md TODO.md && echo "SENTINEL LEAKED — fix before committing" || echo "clean"
```

- [ ] **Step 2: Restore the preserve-list occurrences**

Review every remaining and every changed `mcctl`/`lulism` mention and restore these to `mcctl` where the sweep over-reached:

```bash
grep -n 'mcctl\|lulism' README.md CLAUDE.md TODO.md | grep -iE 'apk|obtainium|applicationId|android|prometheus|metric|2026-06-11|postmortem|pipx'
```

The following must read `mcctl` in the docs: the Android/Obtainium sections (app identity is unchanged), `mcctl-android-v<version>.apk`, the 13 `mcctl_*` metric names, and the 2026-06-11 incident references.

- [ ] **Step 3: Add the 2.0.0 upgrade section to README.md**

Document, under a "Upgrading to 2.0.0" heading: the config/state/cache auto-migration and that originals are kept; the systemd unit migration via `lulism watchdog install`; that `pipx uninstall mcctl` happens automatically in `update.sh`; that the `mcctl` command still works but is removed at 3.0.0; that phones keep working unchanged but their agent command should be updated to `lulism agent` in Settings; that node_exporter's `--collector.textfile.directory` must be repointed at `~/.local/state/lulism`; and that the desktop entry's app ID changed, so pinned launchers reset.

- [ ] **Step 4: Update CLAUDE.md's layout and commands sections**

The `src/mcctl/` tree diagram, the `make` commands, the golden-schema regeneration snippet (its `from mcctl import agent` becomes `from lulism import agent`), and the "Updating mcctl on the server" section all reference the old name.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest
git add -A
git commit -m "docs: rename to LULiSM and document the 2.0.0 upgrade path"
```

---

### Task 8: Version bump to 2.0.0

**Files:**
- Modify: `src/lulism/__init__.py:3`
- Modify: `PKGBUILD` — `pkgver`

**Interfaces:**
- Consumes: everything above.
- Produces: `lulism.__version__ == "2.0.0"`; merging to `main` auto-cuts the release.

- [ ] **Step 1: Bump the gospel version**

`src/lulism/__init__.py:3`:

```python
__version__ = "2.0.0"
```

`PKGBUILD`:

```bash
pkgver=2.0.0
```

- [ ] **Step 2: Verify the version is consistent everywhere**

```bash
.venv/bin/python -m pip install -q -e ".[dev]"
.venv/bin/lulism --version
grep -oP '__version__\s*=\s*"\K[^"]+' src/lulism/__init__.py
grep -oP '^pkgver=\K.*' PKGBUILD
```

Expected: all three report `2.0.0`. `versionCode` will be `2*10000 + 0*100 + 0 = 20000`, up from `10102` — a legal increase, so Obtainium upgrades normally.

- [ ] **Step 3: Add the leak-guard test**

Append to `tests/test_public_interfaces.py`:

```python
import re
from pathlib import Path

import lulism

# Modules where every `mcctl` mention is a preserve-list identifier by design.
LEAK_ALLOWLIST_MODULES = {
    "shim.py",        # the deprecated entry point — it *is* the mcctl command
    "util.py",        # LEGACY_APP + legacy_unit_names(): the migrator must know the old names
    "doctor.py",      # legacy unit detection, the (mcctl|lulism) pgrep, 2026-06-11 notes
    "prometheus.py",  # the 13 frozen mcctl_* metric names
    "config.py",      # prom_path comment referencing the legacy default
}

# Individual lines elsewhere that must keep the old name. Allowlisting the exact
# text rather than the whole module keeps the guard live for everything else in
# these files.
LEAK_ALLOWLIST_LINES = {
    ".mcctl/vanilla",  # assets.py: a cache path on the REMOTE server. migrate_legacy_dirs()
                       # only moves local XDG dirs, so renaming this orphans every server's
                       # cached vanilla jar with no migration path.
    "an older mcctl",  # server.py: a historical reference; "an older lulism" would be false.
}


def test_no_stray_mcctl_identifiers_survive():
    pkg = Path(lulism.__file__).parent
    # \bmcctl\b does not match mcctl_tps or mcctl_version ("_" is a word char, so
    # no boundary), which is why the frozen metric names and the agent.hello wire
    # key do not trip this. It DOES match mcctl-watchdog.service — those live in
    # allowlisted modules by design.
    pattern = re.compile(r"\bmcctl\b")
    leaks = {}
    for py in sorted(pkg.rglob("*.py")):
        if py.name in LEAK_ALLOWLIST_MODULES:
            continue
        hits = [ln.strip() for ln in py.read_text(encoding="utf-8").splitlines()
                if pattern.search(ln)
                and not any(ok in ln for ok in LEAK_ALLOWLIST_LINES)]
        if hits:
            leaks[py.name] = hits
    assert leaks == {}, f"stray mcctl identifiers: {leaks}"


def test_units_shipped_in_the_package_are_lulism_named():
    pkg = Path(lulism.__file__).parent
    shipped = {p.name for p in (pkg / "units").iterdir() if p.suffix in {".service", ".timer"}}
    assert shipped and not any(n.startswith("mcctl-") for n in shipped)
```

- [ ] **Step 3a: Run it**

Run: `.venv/bin/python -m pytest tests/test_public_interfaces.py -v`
Expected: 4 passed. If `test_no_stray_mcctl_identifiers_survive` fails, fix the module it names — do **not** widen `LEAK_ALLOWLIST` without confirming the occurrence is on the spec's preserve-list.

- [ ] **Step 4: Full suite, lint, and the contract check**

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
git diff --stat HEAD~7 -- tests/golden/agent_schema_v1.json
git status --short android/
```

Expected: `425 passed, 1 skipped`; ruff clean; **the golden schema diff is empty**; **`android/` is empty**. Those last two are the proof obligations from the spec — if either shows output, stop and investigate before committing.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "release: 2.0.0 — mcctl is now LULiSM

BREAKING: config, state, cache and systemd unit paths move to lulism.
Config/state/cache migrate automatically on first run (originals kept as a
rollback path). Old units are stopped, disabled and removed by
\`lulism watchdog install\`. The \`mcctl\` command still works via a shim and is
removed at 3.0.0. Android is unchanged: phones keep working through the shim.
Prometheus metric names are unchanged, but node_exporter's textfile directory
must be repointed at ~/.local/state/lulism."
```

---

## Verification Checklist

Run before opening the PR:

- [ ] `.venv/bin/python -m pytest` → `425 passed, 1 skipped`
- [ ] `.venv/bin/ruff check src tests` → clean
- [ ] `git diff --stat main -- tests/golden/agent_schema_v1.json` → empty
- [ ] `git diff --stat main -- android/` → empty
- [ ] `.venv/bin/lulism --version` → `2.0.0`
- [ ] `.venv/bin/mcctl agent --schema | .venv/bin/python -m json.tool > /dev/null` → exit 0
- [ ] `.venv/bin/python -m pytest tests/test_public_interfaces.py` → 5 passed, constants and allowlists unedited
