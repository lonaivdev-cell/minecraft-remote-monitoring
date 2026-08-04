"""Unit migration ordering, and detection of a half-migrated box.

The 2026-06-11 outage was two restart authorities fighting. Renaming the units
without disabling the old ones reproduces it, so the ordering below is the
safety property: every old unit is stopped and disabled before any new one is
installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_migrate_units_reports_only_the_files_it_actually_removed(isolated_xdg):
    """On a fresh install there is nothing to remove, and saying otherwise makes
    the CLI print "removed legacy unit …" seven times for units that never
    existed. Stop/disable still run for all seven regardless -- that is what
    clears a dangling enable symlink left by pacman's replaces=('mcctl')."""
    fake = FakeSystemctl()
    assert util.migrate_units(fake) == []
    assert len({u for _v, u in fake.verbs() if u}) == 7, "still touched all seven"

    unit_dir = util.user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in ("mcctl-watchdog.service", "mcctl-backup.timer"):
        (unit_dir / name).write_text("[Unit]\n", encoding="utf-8")

    removed = util.migrate_units(FakeSystemctl())
    assert set(removed) == {"mcctl-watchdog.service", "mcctl-backup.timer"}
    assert not any((unit_dir / n).exists() for n in util.legacy_unit_names())


def test_migrate_units_can_defer_the_daemon_reload(isolated_xdg):
    """An install has to reload *after* the new unit files land, or systemd does
    not know about them until somebody reloads a second time."""
    fake = FakeSystemctl()
    util.migrate_units(fake, daemon_reload=False)
    assert not any("daemon-reload" in c for c in fake.calls)


# ---------------------------------------------------------------- ExecStart

def _exec_start_targets(units: dict[str, str]) -> list[tuple[str, str]]:
    """(unit name, the binary an ExecStart= line runs). Timers carry none."""
    return [(name, line.removeprefix("ExecStart=").split()[0])
            for name, text in units.items()
            for line in text.splitlines() if line.startswith("ExecStart=")]


@pytest.mark.parametrize("exe", [
    None,                              # the default
    "lulism",                          # a bare name (argv[0] as typed)
    "/home/u/.local/bin/lulism",       # the pipx install
    "/home/u/.local/bin/mcctl",        # reached through the deprecated shim
    "mcctl",                           # the shim, bare
    "/usr/bin/python3",                # something that is not lulism at all
    "/opt/foomcctl/bin/foomcctl",      # a fork whose name merely ends in those letters
])
def test_every_rendered_exec_start_is_an_absolute_non_shim_path(exe):
    """systemd resolves a bare ExecStart filename against its own fixed search
    path, which does NOT include ~/.local/bin -- so a relative target fails with
    203/EXEC on exactly the pipx deployment this project targets. The old guard
    only checked that the target did not *end with* "mcctl", which the broken
    bare-word `ExecStart=lulism watchdog run` passed happily."""
    units = util.render_units() if exe is None else util.render_units(exe=exe)
    targets = _exec_start_targets(units)
    assert targets, "no ExecStart lines rendered at all"
    for name, target in targets:
        assert Path(target).is_absolute(), f"{name}: relative ExecStart {target!r}"
        assert Path(target).name != "mcctl", f"{name}: points at the shim ({target})"


def test_the_shims_lulism_sibling_is_preferred_over_a_hardcoded_default():
    """pipx installs both entry points into the same bin dir, so the shim's
    sibling is the right substitute -- falling back to /usr/bin/lulism for a
    pipx box would name a binary that is not there."""
    units = util.render_units(exe="/home/u/.local/bin/mcctl")
    assert all(t == "/home/u/.local/bin/lulism" for _n, t in _exec_start_targets(units))


def test_a_fork_whose_basename_merely_ends_in_mcctl_keeps_its_own_exe():
    units = util.render_units(exe="/opt/foomcctl/bin/foomcctl")
    # not the fork's binary (wrong basename) -- but still absolute and not the shim
    assert all(t == "/usr/bin/lulism" for _n, t in _exec_start_targets(units))


def test_shipped_units_are_all_lulism_named():
    names = set(util.render_units().keys())
    assert names == {
        "lulism-watchdog.service",
        "lulism-autosave.service", "lulism-autosave.timer",
        "lulism-backup.service", "lulism-backup.timer",
        "lulism-metrics.service", "lulism-metrics.timer",
    }
