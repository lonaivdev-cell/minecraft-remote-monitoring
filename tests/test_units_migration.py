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
    # Timers carry no ExecStart line at all; only check the ones that do.
    units = util.render_units(exe="/home/u/.local/bin/mcctl")
    for name, text in units.items():
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
