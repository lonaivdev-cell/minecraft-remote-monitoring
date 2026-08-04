"""update.sh: the parts of the 2.0.0 upgrade path that only the shell script does.

`update.sh` is the actual upgrade mechanism on the box, and two of its jobs are
load-bearing for this release:

* it must migrate the systemd units. Nothing else on the upgrade path does, and
  skipping it leaves the pre-2.0.0 mcctl-* units enabled beside the new lulism-*
  ones — two restart authorities, the 2026-06-11 outage.
* it must survive pulling the commit that renames the package directory out from
  under itself.

Neither can be exercised end-to-end here (that needs pipx, a user systemd bus
and a real 1.1.2 checkout), so the shell functions that carry the logic are
extracted by name and run for real, and the structure around them is asserted.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lulism import util

UPDATE_SH = Path(__file__).resolve().parent.parent / "update.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _extract(*names: str) -> str:
    """Pull whole `name() { ... }` definitions out of update.sh by name.

    Depends on those functions keeping their closing brace in column 0, which is
    why they are written that way (there is a comment in update.sh saying so).
    """
    text = UPDATE_SH.read_text(encoding="utf-8")
    out = []
    for name in names:
        m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.S | re.M)
        assert m, f"{name}() not found in update.sh (or its closing brace moved)"
        out.append(m.group(0))
    return "\n".join(out)


def _bash(script: str, cwd: Path | None = None) -> str:
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                       cwd=str(cwd) if cwd else None, timeout=30)
    assert r.returncode == 0, f"bash failed ({r.returncode}): {r.stderr}"
    return r.stdout


def test_update_sh_is_valid_bash():
    subprocess.run([BASH, "-n", str(UPDATE_SH)], check=True, timeout=30)


def test_help_block_range_still_covers_the_whole_usage_comment():
    """`--help` prints a fixed line range of its own source; adding a usage line
    without moving the range silently truncates the help."""
    out = _bash(f"{BASH} {UPDATE_SH} --help")
    assert "./update.sh -h | --help" in out, out  # the last usage line survived
    assert "--no-restart" in out and "--no-status" in out


# ---------------------------------------------------------------- src_version

def _src_version_harness(repo: Path) -> str:
    return (f'set -euo pipefail\n{_extract("src_version")}\n'
            f'cd {repo}\nprintf "[%s]" "$(src_version)"\n')


@pytest.mark.parametrize(("layout", "expected"), [
    ({"src/mcctl/__init__.py": "1.1.2"}, "[1.1.2]"),                        # pre-pull, 1.1.2 box
    ({"src/lulism/__init__.py": "2.0.0"}, "[2.0.0]"),                       # post-pull, 2.0.0
    ({"src/lulism/__init__.py": "2.0.0",
      "src/mcctl/__init__.py": "1.1.2"}, "[2.0.0]"),                        # both: new wins
    ({}, "[]"),                                                             # neither: no crash
])
def test_src_version_reads_either_package_layout(tmp_path, layout, expected):
    """The regression this exists for: update.sh pulls the commit that deletes
    src/mcctl/, and the *running* copy then has to read a version out of the
    tree. Reading only one hardcoded path makes `sed` fail on a missing file,
    which under `set -euo pipefail` aborts the run — after `pipx install --force`
    has already swapped the package, so the box is left mid-upgrade with both
    pipx packages installed and no health check run.
    """
    for rel, ver in layout.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'__version__ = "{ver}"\n', encoding="utf-8")
    assert _bash(_src_version_harness(tmp_path)) == expected


def test_preflight_accepts_the_pre_2_0_0_checkout():
    """The 1.1.2 box runs the preflight *before* the pull that creates
    src/lulism/, so demanding only the new path refuses to start the upgrade."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    m = re.search(r"^\[\[ -f src/lulism/__init__\.py.*?\]\]", text, re.M | re.S)
    assert m, "the preflight package check moved"
    assert "src/mcctl/__init__.py" in m.group(0), m.group(0)


# ---------------------------------------------------------------- unit names

def test_legacy_unit_list_matches_the_python_one():
    """A name update.sh does not know about is a unit it never disables and
    never re-enables under the new name — a silent second restart authority."""
    out = _bash(f'{_extract("legacy_units")}\nlegacy_units\n')
    assert tuple(out.split()) == util.legacy_unit_names()


def test_every_legacy_unit_maps_onto_a_shipped_lulism_unit():
    out = _bash(f'{_extract("legacy_units", "new_unit_for")}\n'
                'legacy_units | while read -r u; do new_unit_for "$u"; done\n')
    assert set(out.split()) == set(util.render_units())


# ---------------------------------------------------------------- leftovers

_LEFTOVER_HARNESS = """
set -euo pipefail
{fns}
unit_enabled() {{ [[ " $ENABLED " == *" $1 "* ]]; }}
unit_active()  {{ [[ " $ACTIVE "  == *" $1 "* ]]; }}
legacy_leftovers | tr '\\n' ' '
"""


@pytest.mark.parametrize(("enabled", "active", "expected"), [
    ("", "", []),
    ("mcctl-watchdog.service", "", ["mcctl-watchdog.service"]),
    # the pacman path: `replaces=('mcctl')` deleted the unit files, so is-enabled
    # answers "not-found" while the old watchdog is very much still running.
    ("", "mcctl-watchdog.service", ["mcctl-watchdog.service"]),
    ("mcctl-watchdog.service", "mcctl-watchdog.service", ["mcctl-watchdog.service"]),
    ("mcctl-backup.timer mcctl-metrics.timer", "mcctl-watchdog.service",
     ["mcctl-watchdog.service", "mcctl-backup.timer", "mcctl-metrics.timer"]),
])
def test_legacy_leftovers_reports_enabled_and_active(enabled, active, expected):
    script = _LEFTOVER_HARNESS.format(fns=_extract("legacy_units", "legacy_leftovers"))
    out = _bash(f'ENABLED="{enabled}" ACTIVE="{active}"\n{script}')
    assert out.split() == expected


@pytest.mark.parametrize(("stub", "expected"), [
    ('pgrep() { printf "0\\n"; return 1; }', "0"),   # pgrep -c prints 0 and exits 1
    ('pgrep() { printf "1\\n"; }', "1"),
    ('pgrep() { printf "2\\n"; }', "2"),
    ('pgrep() { return 127; }', "0"),                # no pgrep installed at all
])
def test_watchdog_procs_counts_and_never_aborts_the_run(stub, expected):
    """Counted, not tested for truthiness: one watchdog is the target state and
    two is the outage. It also runs under `set -euo pipefail`, so a non-zero
    pgrep (the normal "no matches" answer) must not abort the upgrade."""
    out = _bash(f'set -euo pipefail\n{stub}\n{_extract("watchdog_procs")}\nwatchdog_procs\n')
    assert out.strip() == expected


# ---------------------------------------------------------------- wiring

def test_update_sh_runs_the_unit_migration_and_re_enables_the_replacements():
    """Spec §9.4: update.sh performs the §6.2 unit migration. Before this, the
    pipx swap happened and update.sh then tested `is-active
    lulism-watchdog.service` — a unit that did not exist yet — fell through to
    "watchdog not running — nothing to restart", and printed success while the
    old mcctl-watchdog.service stayed enabled and active."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "lulism watchdog install" in text
    assert re.search(r"systemctl --user enable --now \"\$\{targets\[@\]\}\"", text), \
        "the captured legacy units are never re-enabled under their new names"
    # the capture must happen in the Before panel, ahead of the pipx swap and of
    # `lulism watchdog install` -- afterwards the information no longer exists.
    capture = text.index("LEGACY_SEEN=()")
    swap = re.search(r"^pipx install --force \.", text, re.M)
    install = re.search(r"^\s*if lulism watchdog install;", text, re.M)
    assert swap and install
    assert capture < swap.start(), "the legacy units are captured after the pipx swap"
    assert capture < install.start(), "the legacy units are captured after they are removed"


def test_surviving_legacy_units_are_a_failure_not_a_footnote():
    """`doctor` warns about them, but cli.py maps Level.WARN -> exit 0, so a
    doctor warning alone still ends with "lulism is now 2.0.0. done!"."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    m = re.search(r"leftover=\(\).*?\bfi\b", text, re.S)
    assert m, "the post-migration leftover check is gone"
    block = m.group(0)
    assert "FAILED=1" in block, block
    assert "systemctl --user disable --now" in block, "no next step for the operator"


def test_a_second_watchdog_daemon_is_also_a_failure():
    """A watchdog started by hand belongs to no unit, so the unit checks above
    can come back clean with two brains alive."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    m = re.search(r"wd_procs=\$\(watchdog_procs\).*?\bfi\b", text, re.S)
    assert m, "the duplicate-daemon check is gone"
    assert "wd_procs > 1" in m.group(0)
    assert "FAILED=1" in m.group(0)


def test_the_script_body_is_one_brace_group():
    """update.sh git-pulls itself. bash reads a script incrementally, so a file
    that changes size mid-run makes the remaining commands be read from the new
    file at the old offset. A single compound command is parsed in full before
    any of it runs."""
    lines = UPDATE_SH.read_text(encoding="utf-8").splitlines()
    opens = [i for i, ln in enumerate(lines) if ln.strip() == "{"]
    assert opens, "the script body is no longer wrapped in a brace group"
    assert lines[-1].startswith("}"), lines[-1]
